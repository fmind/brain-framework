"""Orchestration of unified `loops`, `.bf/<loop>.json`, and JSON `logs/<loop>.log`."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bf import config, engine, models, proc, query, records
from bf.config import Config, EventMatch, LoopSpec
from bf.models import AUTHORED, MAX_BATCH, MAX_NOTE, Error, InputError, decode, encode, now_utc
from bf.proc import exec_argv as exec_argv
from bf.storage import Store, private_dir, relative, writer

_ENV_SYS = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "TERM", "TZ", "SSH_AUTH_SOCK")
_ENV_GUI = ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
_ENV_XDG = tuple(f"XDG_{k}_HOME" for k in ("CONFIG", "CACHE", "DATA", "STATE"))
_ENV_AUTH = ("GH_TOKEN", "GITHUB_TOKEN", "CLOUDSDK_CONFIG", "GOOGLE_APPLICATION_CREDENTIALS")
_ENV_TLS = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_ENV_PROXY = tuple(p for k in ("HTTP", "HTTPS", "NO", "ALL") for p in (f"{k}_PROXY", f"{k.lower()}_proxy"))
_ENV_ALLOWLIST = (*_ENV_SYS, *_ENV_GUI, *_ENV_XDG, *_ENV_AUTH, *_ENV_TLS, *_ENV_PROXY)
_GIT_KEYS = ("DIR", "WORK_TREE", "INDEX_FILE", "OBJECT_DIRECTORY", "ALTERNATE_OBJECT_DIRECTORIES", "QUARANTINE_PATH")
_GIT_HOOK_ENV = tuple(f"GIT_{k}" for k in _GIT_KEYS)

type _Events = tuple[list[dict[str, Any]], int]
type _DueItem = tuple[LoopSpec, str, list[dict[str, Any]], int | None]


def load_unit_state(store: Store, name: str) -> dict[str, Any]:
    """Load `.bf/<name>.json`, returning an initialized `idle` state when absent."""
    with suppress(FileNotFoundError):
        state = private_dir(store, create=False)
        if isinstance(parsed := decode(state.read(f"{name}.json", 1 << 20), f".bf/{name}.json"), dict):
            return dict(parsed)
    zeros = dict.fromkeys(("event_seq", "runs", "failures", "consecutive_failures", "duration_ms"), 0)
    empty = dict.fromkeys(("last_start", "last_end", "last_success", "last_scheduled", "error"), "")
    return {"kind": "loop", "name": name, "status": "idle", "updated": now_utc(), **empty, **zeros, "last_run": {}}


def save_unit_state(store: Store, doc: dict[str, Any], *, locked: bool = False) -> None:
    """Write `.bf/<name>.json` atomically with `0600` permissions."""
    state, path = private_dir(store), f"{doc['name']}.json"
    if locked:
        state.write(path, encode(doc), mode=0o600)
    else:
        with writer(store):
            state.write(path, encode(doc), mode=0o600)


def mark_unit_state(store: Store, name: str, **fields: Any) -> None:
    """Atomically read-modify-write fields on `.bf/<name>.json` under `writer(store)`."""
    with writer(store):
        cur = load_unit_state(store, name)
        save_unit_state(store, {**cur, "kind": "loop", "name": name, **fields}, locked=True)


def _rule_head_matches(ev: Mapping[str, Any], r: EventMatch) -> bool:
    return (not r.kind or ev.get("kind") == r.kind) and (not r.source or ev.get("source") == r.source)


def pending_events(
    store: Store, last_seq: int, rules: tuple[EventMatch, ...], *, events_data: _Events | None = None
) -> _Events:
    """Return `(matching_events, latest_seq)` since `last_seq` from `.bf/events.jsonl`."""
    if not rules:
        return [], 0
    if events_data is not None:
        all_evs, latest = events_data
        floor = 0 if (latest and last_seq > latest) else last_seq
        cands = [ev for ev in all_evs if int(ev["seq"]) > floor]
    else:
        cands, latest = records.read_events(store, after_seq=last_seq)
    if not cands:
        return [], latest
    plain_rules = [r for r in rules if not r.filter]
    matched_idx = {i for i, ev in enumerate(cands) if any(_rule_head_matches(ev, r) for r in plain_rules)}
    groups: dict[str, list[EventMatch]] = {}
    for rule in rules:
        if rule.filter:
            groups.setdefault(rule.filter, []).append(rule)
    jq_bin: str | None = None
    for flt, flt_rules in groups.items():
        unmatched = (i for i in range(len(cands)) if i not in matched_idx)
        sub_idx = [i for i in unmatched if any(_rule_head_matches(cands[i], r) for r in flt_rules)]
        if not sub_idx:
            continue
        jq_bin = jq_bin or proc.resolve_tool("jq")
        wrapped = (
            f"{query.JQ_PRELUDE}\ntry ([\n{flt}\n] | "
            "if length == 0 then false else (.[-1] // false | . != false) end) "
            'catch (if . == "disabled" then error else false end)'
        )
        jq_kw: dict[str, Any] = {"capture_output": True, "cwd": "/", "env": proc.safe_env(jq_bin), "timeout": 5}
        try:
            cmd = [jq_bin, "-L", "/dev/null", "-c", "--", wrapped]
            cp = subprocess.run(cmd, input=b"".join(encode(cands[i]) for i in sub_idx), **jq_kw, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as error:
            raise Error("event filter timed out after 5s") from error
        if cp.returncode != 0:
            err = cp.stderr.decode("utf-8", "replace").strip()
            raise Error(f"event filter failed: {err.splitlines()[-1] if err else 'jq exited nonzero'}")
        outs = cp.stdout.splitlines()[: len(sub_idx)]
        matched_idx.update(sub_idx[pos] for pos, ln in enumerate(outs) if ln == b"true")
    return [ev for i, ev in enumerate(cands) if i in matched_idx], latest


def _parse_dt(raw: str) -> datetime | None:
    with suppress(ValueError):
        return dt if raw and (dt := datetime.fromisoformat(raw)).tzinfo is not None else None
    return None


def _backoff_cap(loop: LoopSpec) -> int:
    return min((s.every for s in loop.schedules if s.every is not None), default=3600)


def _in_backoff(doc: Mapping[str, Any], cap: int, now: datetime) -> bool:
    fails = int(doc.get("consecutive_failures") or 0)
    last_dt = _parse_dt(str(doc.get("last_end") or "")) if fails > 0 else None
    return last_dt is not None and (now - last_dt).total_seconds() < min(60 * (1 << min(fails - 1, 10)), cap)


def _sched_anchor(doc: Mapping[str, Any]) -> str:
    return str(doc["last_scheduled"] if "last_scheduled" in doc else doc.get("last_success") or "")


def is_schedule_due(loop: LoopSpec, state_doc: Mapping[str, Any], now: datetime, *, tz_name: str = "UTC") -> bool:
    """Whether any duration or cron entry in `loop.schedules` is due at `now`, respecting failure backoff."""
    if not loop.schedules or _in_backoff(state_doc, _backoff_cap(loop), now):
        return False
    last_dt, tz, local_now = _parse_dt(_sched_anchor(state_doc)), None, None
    for sc in loop.schedules:
        if sc.every is not None:
            if last_dt is None or (now - last_dt).total_seconds() >= sc.every:
                return True
            continue
        if tz is None or local_now is None:
            tz = ZoneInfo(tz_name)
            local_now = (now.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=1)).astimezone(tz)
        with suppress(ValueError, KeyError, TypeError):
            if last_dt is None or last_dt.astimezone(tz) < config.prev_cron(sc.cron, local_now):
                return True
    return False


def check_requires(bins: tuple[str, ...], label: str) -> list[str]:
    return [f"{label}: missing required command {b!r} on PATH" for b in bins if shutil.which(b) is None]


def prepare_unit(
    store: Store,
    name: str,
    base_argv: Sequence[str],
    run_id: str,
    start_iso: str,
    *,
    trigger: str = "manual",
    input_rel: str = "",
    end_iso: str = "",
    events: Sequence[dict[str, Any]] = (),
    stdin_text: str = "",
    base_env: Mapping[str, str] | None = None,
    unit_env: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Expand `{{...}}` placeholders, write `.bf/<name>.context.json`, and build layered `env`."""
    effective_end, bf_dir, input_abs = end_iso or start_iso, private_dir(store), ""
    if input_rel:
        relative(input_rel)
        if not store.exists(input_rel):
            raise Error(f"{name}: input file {input_rel} does not exist")
        input_abs = str(store.root / input_rel)
    ctx_path = f"{name}.context.json"
    ctx_a = {"run": run_id, "kind": trigger, "loop": name, "events": list(events), "stdin": stdin_text}
    bf_dir.write(ctx_path, encode({**ctx_a, "start": start_iso, "end": effective_end, "input": input_abs}), mode=0o600)
    home_str = os.environ.get("HOME", "")
    with suppress(RuntimeError):
        home_str = str(Path.home())
    env_map = {"{{brain}}": str(store.root), "{{home}}": home_str, "{{state}}": str(bf_dir.root)}
    toks = {**env_map, "{{run}}": run_id, "{{start}}": start_iso, "{{end}}": effective_end}
    toks |= {"{{input}}": input_abs, "{{context}}": str(bf_dir.root / ctx_path)}

    def _sub(text: str, table: Mapping[str, str]) -> str:
        return re.sub(r"\{\{[a-z]+\}\}", lambda m: table.get(m[0], m[0]), text)

    argv = [_sub(token, toks) for token in base_argv]
    if argv and argv[0] == "bf":
        argv = [sys.executable, "-m", "bf", *argv[1:]]
    elif argv and "/" in argv[0] and not Path(argv[0]).is_absolute():
        parts = relative(argv[0])
        if not store.exists(argv[0]):
            raise Error(f"{name}: command {argv[0]} does not exist inside brain")
        argv[0] = str(store.root.joinpath(*parts))

    # Precedence: host allowlist < bf.yaml env: < .env < loops.<name>.env: < protected BF_*.
    def _expand(mapping: Mapping[str, str] | None) -> dict[str, str]:
        return {k: _sub(v, env_map) for k, v in (mapping or {}).items()}

    keys = (*_ENV_ALLOWLIST, *(_GIT_HOOK_ENV if trigger == "hook" else ()))
    env: dict[str, str] = {key: val for key in keys if (val := os.environ.get(key))}
    env |= _expand(base_env) | config.load_dotenv(store) | _expand(unit_env)
    bf_env = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "BF_BRAIN": str(store.root), "BF_TRIGGER": trigger}
    bf_env |= {"BF_STATE_DIR": str(bf_dir.root / name), "BF_START": start_iso, "BF_END": effective_end}
    return argv, {**env, **bf_env}


def _save_report_action(store: Store, name: str, out_bytes: bytes, keep: int, instant: datetime) -> str:
    from bf import markdown

    cfg = config.load(store)
    day = instant.astimezone(ZoneInfo(cfg.timezone)).date().isoformat()
    rel = f"actions/{day}_{name}-{os.urandom(4).hex()}/ACTION.md"
    compiled = markdown.compile_note(rel, out_bytes, brain_name=cfg.name)
    pat = re.compile(rf"^actions/\d{{4}}-\d{{2}}-\d{{2}}_{re.escape(name)}-[0-9a-f]{{8}}/ACTION\.md$")
    with writer(store):
        store.write(rel, out_bytes)
        store.write(markdown.compiled_note_path(rel), encode(compiled))
        by_t = lambda p: (p == rel, p[:18], store.stat(p).st_mtime_ns, p)  # noqa: E731
        old_list = sorted((p for p in store.files("actions") if pat.fullmatch(p)), key=by_t)
        for old in old_list[:-keep] if keep > 0 else ():
            store.delete(old)
            with suppress(FileNotFoundError):
                store.delete(markdown.compiled_note_path(old))
            with suppress(OSError), store.parent(old.rsplit("/", 1)[0]) as (pfd, leaf):
                os.rmdir(leaf, dir_fd=pfd)
    return rel


def parse_emitted_event(raw_line: bytes, default_kind: str, label: str) -> tuple[str, dict[str, Any]]:
    """Parse one emitted JSONL line into `(event_kind, payload_dict)` for both daemon and cycle loops."""
    parsed = decode(raw_line, label)
    if not isinstance(parsed, dict):
        raise Error(f"{label}: emitted event must be a JSON object")
    raw_k = parsed.get("kind")
    if raw_k is not None and not isinstance(raw_k, str):
        raise Error(f"{label}: event kind must be a string")
    ev_kind = models.clean(raw_k or default_kind, f"{label} kind")
    if not models.EVENT_KIND_RE.fullmatch(ev_kind):
        raise Error(f"{label}: invalid event kind")
    return ev_kind, dict(parsed)


def emit_output_events(
    store: Store, name: str, default_kind: str, out_bytes: bytes, *, now: datetime | None = None
) -> int:
    """Validate all JSONL stdout events first, then atomically append deduplicated events."""
    if len(out_bytes) > MAX_BATCH:
        raise Error(f"loops.{name}: emitted output exceeds {MAX_BATCH} bytes")
    parsed_evs: list[tuple[str, dict[str, Any]]] = []
    label = f"loops.{name}"
    for idx, raw_ln in enumerate(out_bytes.splitlines(), 1):
        if stripped := raw_ln.strip():
            parsed_evs.append(parse_emitted_event(stripped, default_kind, f"{label} line {idx}"))
    if parsed_evs:
        with writer(store):
            for ev_k, payload in parsed_evs:
                records.append_event(store, ev_k, name, payload, dedupe=True, locked=True, now=now)
    return len(parsed_evs)


def _merge_cursor(stored: int, target: int, disk: int) -> int:
    return stored if target > disk else (target if disk < stored else max(stored, target))


def record_run(
    store: Store,
    name: str,
    run_id: str,
    started_iso: str,
    *,
    ended: datetime,
    dur_ms: int,
    code: int,
    err_msg: str = "",
    stderr: str = "",
    summary: dict[str, Any] | None = None,
    cursor_seq: int | None = None,
    emit_loop_event: bool = False,
    trigger: str = "",
) -> None:
    """Update `.bf/<name>.json`, append a JSON line to `logs/<name>.log`, and optionally emit `loop.*`."""
    cfg_name, ended_iso, ok = config.load(store).name, now_utc(ended), code == 0
    with writer(store):
        latest = load_unit_state(store, name)
        last_ok = ended_iso if ok else str(latest.get("last_success") or "")
        last_sched = ended_iso if ok and trigger not in {"event", "hook", "daemon"} else _sched_anchor(latest)
        consec = 0 if ok else int(latest.get("consecutive_failures") or 0) + 1
        runs, fails = int(latest.get("runs") or 0) + 1, int(latest.get("failures") or 0) + int(not ok)
        stored_seq = int(latest.get("event_seq") or 0)
        disk_seq = records.latest_event_seq(store) if ok and cursor_seq is not None else 0
        seq = _merge_cursor(stored_seq, cursor_seq, disk_seq) if ok and cursor_seq is not None else stored_seq
        raw_err = err_msg if ok else (err_msg.strip() or stderr.strip() or f"exit code {code}")
        err_clean = models.redact_secrets(raw_err)[:4096]
        err_clean = err_clean if (ok or err_clean.strip()) else f"exit code {code}"
        stderr_clean = models.redact_secrets(stderr)[:8192]
        safe_sum = models.redact_value(dict(summary)) if isinstance(summary, dict) and summary else {}
        d1 = {"kind": "loop", "name": name, "status": "ok" if ok else "failed", "updated": ended_iso}
        d2 = {"last_start": started_iso, "last_end": ended_iso, "last_success": last_ok, "last_scheduled": last_sched}
        d3 = {"event_seq": seq, "runs": runs, "failures": fails, "consecutive_failures": consec, "duration_ms": dur_ms}
        save_unit_state(store, {**d1, **d2, **d3, "last_run": safe_sum, "error": err_clean}, locked=True)
        extras = {k: v for k, v in (("summary", safe_sum), ("error", err_clean), ("stderr", stderr_clean)) if v}
        ev_a = {"brain": cfg_name, "unit": name, "kind": "loop", "run": run_id, "trigger": trigger or "loop"}
        ev_b = {"started": started_iso, "ended": ended_iso, "duration_ms": dur_ms, "exit_code": code}
        msg, sev = f"loop.{'succeeded' if ok else 'failed'}", "INFO" if ok else "ERROR"
        store.append_log(name, models.log_line(msg, severity=sev, time_iso=ended_iso, **ev_a, **ev_b, **extras))
        if emit_loop_event:
            ev_d = {"run": run_id, "exit_code": code, "duration_ms": dur_ms}
            records.append_event(store, f"loop.{'succeeded' if ok else 'failed'}", name, ev_d, locked=True, now=ended)


def _bf_error_summary(run_argv: Sequence[str], out_bytes: bytes) -> str:
    if len(run_argv) < 2 or run_argv[0] != "bf" or not out_bytes.strip():
        return ""
    with suppress(Error):
        if not isinstance(res := decode(out_bytes, "bf output", check_duplicates=False), dict):
            return ""
        cmd = run_argv[1]
        subs = [res.get(k) or {} for k in ("build", "validate", "eval")] if cmd == "check" else [res]
        probs = next((p for s in subs if isinstance(s, dict) and not s.get("ok") and (p := s.get("problems"))), None)
        if isinstance(probs, list) and probs:
            return "; ".join(str(p) for p in probs[:3])
        ev_failed = res.get("failed", 0) if cmd == "eval" else (res.get("eval") or {}).get("failed", 0)
        if ev_failed:
            return f"{ev_failed} eval case(s) failed"
    return ""


def execute_one_loop(
    store: Store,
    loop: LoopSpec,
    *,
    trigger: str,
    events: Sequence[dict[str, Any]] = (),
    cursor_target_seq: int | None = None,
    extra_args: Sequence[str] = (),
    stdin_bytes: bytes = b"",
    dry_run: bool = False,
    now: datetime | None = None,
    stop_event: threading.Event | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Execute one triggered `LoopSpec`, handling `ingest`/`action`/`emit` and recording `.bf/` + `logs/`."""
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    started_iso, run_id = now_utc(instant), f"{loop.name}-{int(instant.timestamp())}-{os.urandom(3).hex()}"
    state_doc = load_unit_state(store, loop.name)
    last_ok = _sched_anchor(state_doc) if trigger == "schedule" else str(state_doc.get("last_success") or "")
    last_dt = _parse_dt(last_ok)
    win_start = (last_dt or instant) - timedelta(seconds=loop.overlap if last_dt else loop.lookback)
    if missing := check_requires(loop.requires, f"loops.{loop.name}"):
        raise Error("; ".join(missing))
    if dry_run:
        return {"loop": loop.name, "run": run_id, "status": "dry_run", "trigger": trigger}

    t0, summary, err_msg, err_txt = time.monotonic(), {}, "", ""
    p_a: dict[str, Any] = {"end_iso": started_iso, "input_rel": loop.input, "trigger": trigger, "events": events}
    b_env = config.load(store).env if base_env is None else base_env
    p_b: dict[str, Any] = {
        "stdin_text": stdin_bytes.decode("utf-8", "replace"),
        "base_env": b_env,
        "unit_env": loop.env,
    }
    argv, env = prepare_unit(store, loop.name, loop.run, run_id, now_utc(win_start), **p_a, **p_b)
    argv = [*argv, *extra_args]
    proc_stdin, recorded = stdin_bytes or (store.read(loop.input, MAX_NOTE) if loop.input else b""), False
    mark_unit_state(store, loop.name, status="running", updated=started_iso, last_start=started_iso)
    try:
        code, out_bytes, err_txt = exec_argv(
            argv, cwd=store.root, env=env, timeout=loop.timeout, stdin_bytes=proc_stdin, stop_event=stop_event
        )
        if stop_event is not None and stop_event.is_set() and code == 130:
            dur_ms = max(0, int((time.monotonic() - t0) * 1000))
            return {"loop": loop.name, "run": run_id, "status": "stopped", "exit_code": 130, "duration_ms": dur_ms}
        if code != 0:
            raw_fail = err_txt or _bf_error_summary(loop.run, out_bytes) or f"exit code {code}"
            err_msg = (models.redact_secrets(raw_fail).strip() or f"exit code {code}")[-1024:]
        else:
            try:
                if loop.ingest:
                    summary = records.ingest(
                        store, loop.ingest, out_bytes, mode=loop.mode, allow_removal=loop.allow_removal, now=now
                    )
                elif loop.action_keep and out_bytes.strip():
                    act_t = datetime.now(UTC) if now is None else instant
                    summary = {"action": _save_report_action(store, loop.name, out_bytes, loop.action_keep, act_t)}
                elif loop.emit and out_bytes.strip():
                    summary = {"emitted": emit_output_events(store, loop.name, loop.emit, out_bytes, now=now)}
                elif len(out_bytes) <= 65536 and out_bytes.decode("utf-8", "replace").strip().startswith("{"):
                    with suppress(Error):
                        if isinstance(dec := decode(out_bytes, f"loops.{loop.name}"), dict):
                            summary = rv if isinstance(rv := models.redact_value(dict(dec)), dict) else {}
            except models.FAILURES as error:
                code, err_msg = 1, (models.redact_secrets(models.failure(error) or "failed")[-1024:] or "failed")

        ended, dur_ms = (datetime.now(UTC) if now is None else instant), max(0, int((time.monotonic() - t0) * 1000))
        r_a: dict[str, Any] = {"ended": ended, "dur_ms": dur_ms, "code": code, "err_msg": err_msg, "stderr": err_txt}
        r_b: dict[str, Any] = {"summary": summary, "cursor_seq": cursor_target_seq, "emit_loop_event": True}
        record_run(store, loop.name, run_id, started_iso, **r_a, **r_b, trigger=trigger)
        recorded = True
    finally:
        if not recorded:
            with suppress(Error, OSError):
                t_end = now_utc(None if now is None else instant)
                mark_unit_state(store, loop.name, status="stopped", updated=t_end, last_end=t_end)
    res = {"loop": loop.name, "run": run_id, "status": "ok" if code == 0 else "failed", "exit_code": code}
    extra = {**({"error": err_msg} if err_msg else {}), **({"summary": summary} if summary else {})}
    return {**res, "duration_ms": dur_ms, **extra}


def record_failure(
    store: Store, name: str, trigger: str, err: BaseException, *, now: datetime | None = None
) -> dict[str, Any]:
    """Record a pre-flight or event-filter failure for loop `name` and return its result row."""
    msg = (models.redact_secrets(models.failure(err)) or "failed")[-1024:]
    end_dt = datetime.now(UTC) if now is None else now.astimezone(UTC)
    pre_id = f"{name}-{int(end_dt.timestamp())}-{os.urandom(3).hex()}"
    with suppress(Error, OSError):
        r_kw: dict[str, Any] = {"ended": end_dt, "dur_ms": 0, "code": 1, "err_msg": msg, "emit_loop_event": True}
        record_run(store, name, pre_id, now_utc(end_dt), **r_kw, trigger=trigger)
    return {"loop": name, "run": pre_id, "status": "failed", "exit_code": 1, "duration_ms": 0, "error": msg}


def select_due_loops(
    store: Store,
    cfg: Config,
    *,
    sched_now: datetime,
    exclude: frozenset[str] = frozenset(),
    dry_run: bool = False,
    now: datetime | None = None,
) -> tuple[list[_DueItem], list[dict[str, Any]]]:
    """Select enabled due/event cycle loops not in `exclude`, advancing idle event cursors atomically."""
    latest_disk_seq: int | None = None
    cached_events: _Events | None = None
    selected, advance_cursors, failures = [], [], []

    for name, loop in cfg.loops.items():
        if not loop.enabled or loop.daemon or name in exclude or not (loop.schedules or loop.events):
            continue
        state_doc = load_unit_state(store, name)
        if _in_backoff(state_doc, _backoff_cap(loop), sched_now):
            continue
        cur_seq, matched_evs, target_seq = int(state_doc.get("event_seq") or 0), [], None
        if loop.events:
            latest_disk_seq = records.latest_event_seq(store) if latest_disk_seq is None else latest_disk_seq
            target_seq = latest_disk_seq
            if cur_seq != latest_disk_seq and latest_disk_seq > 0:
                cached_events = records.read_events(store) if cached_events is None else cached_events
                try:
                    matched_evs, target_seq = pending_events(store, cur_seq, loop.events, events_data=cached_events)
                except models.FAILURES as err:
                    if dry_run:
                        raise
                    failures.append(record_failure(store, name, "event", err, now=now))
                    continue
        if matched_evs:
            selected.append((loop, "event", matched_evs, target_seq))
        elif is_schedule_due(loop, state_doc, sched_now, tz_name=cfg.timezone):
            selected.append((loop, "schedule", [], target_seq))
        elif target_seq is not None and target_seq != cur_seq:
            advance_cursors.append((name, target_seq))

    if advance_cursors and not dry_run:
        with writer(store):
            disk_seq = records.latest_event_seq(store)
            for cursor_name, cursor_seq in advance_cursors:
                latest = load_unit_state(store, cursor_name)
                stored = int(latest.get("event_seq") or 0)
                if (nxt := _merge_cursor(stored, cursor_seq, disk_seq)) != stored:
                    save_unit_state(store, {**latest, "event_seq": nxt}, locked=True)
    return selected, failures


def run_loops(
    store: Store,
    names: Sequence[str] = (),
    *,
    due_only: bool = False,
    hook: str = "",
    rebuild: bool = True,
    extra_args: Sequence[str] = (),
    stdin_bytes: bytes = b"",
    dry_run: bool = False,
    now: datetime | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run selected, due (concurrently up to `watch.concurrency`), or Git-hook loops."""
    cfg, sched_now = config.load(store), datetime.now(UTC) if now is None else now.astimezone(UTC)
    if bool(names) + bool(due_only) + bool(hook) != 1:
        raise InputError("specify loop names, --due, or --hook (mutually exclusive)")
    if hook and hook not in {"pre-commit", "pre-push"}:
        raise InputError("hook must be pre-commit or pre-push")
    if bad := next((w for w in names if w not in cfg.loops), ""):
        raise InputError(f"unknown loop {bad!r} in bf.yaml")
    if dm := next((w for w in names if cfg.loops[w].daemon), ""):
        raise InputError(f"loop {dm!r} is a daemon; run it with bf watch")
    if extra_args and (due_only or len(names) > 1):
        raise InputError("passthrough -- arguments require a single loop or --hook")

    built = engine.build(store, now=now, incremental=True) if due_only and rebuild and not dry_run else None
    results: list[dict[str, Any]] = []
    if due_only:
        selected, pre_fails = select_due_loops(store, cfg, sched_now=sched_now, dry_run=dry_run, now=now)
        results.extend(pre_fails)
    elif hook:
        selected = [
            (lp, "hook", [], None) for lp in cfg.loops.values() if lp.enabled and not lp.daemon and hook in lp.hooks
        ]
    else:
        selected = [(cfg.loops[n], "manual", [], None) for n in dict.fromkeys(names)]

    def run_item(item: _DueItem, *, stop: threading.Event | None = stop_event) -> dict[str, Any]:
        loop, trig, evs, seq_target = item
        if stop is not None and stop.is_set():
            return {"loop": loop.name, "status": "stopped", "exit_code": 130, "duration_ms": 0}
        try:
            e_a: dict[str, Any] = {"trigger": trig, "events": evs, "cursor_target_seq": seq_target}
            e_b: dict[str, Any] = {"extra_args": extra_args, "stdin_bytes": stdin_bytes, "dry_run": dry_run}
            return execute_one_loop(store, loop, **e_a, **e_b, now=now, stop_event=stop, base_env=cfg.env)
        except models.FAILURES as err:
            if not due_only or dry_run:
                raise
            return record_failure(store, loop.name, trig, err, now=now)

    if due_only and not dry_run and len(selected) > 1 and cfg.watch.concurrency > 1:
        local_stop = stop_event or threading.Event()
        pool = ThreadPoolExecutor(max_workers=min(cfg.watch.concurrency, len(selected)))
        try:
            futs = [pool.submit(run_item, it, stop=local_stop) for it in selected]
            results.extend(f.result() for f in futs)
        except BaseException:
            local_stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
    else:
        for item in selected:
            if stop_event is not None and stop_event.is_set():
                break
            results.append(run_item(item))

    ok_all = (built is None or bool(built["ok"])) and all(r["status"] in {"ok", "dry_run", "stopped"} for r in results)
    out_d = {"brain": cfg.name, "ok": ok_all, "ran": len(results), "results": results}
    return {**out_d, "problems": list(built["problems"])} if built and built["problems"] else out_d


def _log_error_fallback(store: Store, name: str) -> str:
    with suppress(Error, OSError, FileNotFoundError):
        tail, _ = private_dir(store, "logs", create=False).read_tail(f"{name}.log", 65536)
        for raw_ln in reversed(tail.splitlines()):
            if stripped := raw_ln.strip():
                with suppress(Error):
                    if isinstance(entry := decode(stripped, f"logs/{name}.log", check_duplicates=False), dict):
                        if raw := str(entry.get("error") or "").strip() or str(entry.get("stderr") or "").strip():
                            return models.redact_secrets(raw)[:1024]
                        if type(ec := entry.get("exit_code")) is int and ec != 0:
                            return f"exit code {ec}"
                break
    return "last run failed"


def status_summary(
    store: Store, *, check: bool = False, all_units: bool = False, fields: Sequence[str] | str | None = None
) -> dict[str, Any]:
    """Return compact counts, failing attention loops, orphaned `.bf/` states, and optional `loops`."""
    wanted, cfg, problems, attention, loops_out = engine.parse_fields(fields), config.load(store), [], [], {}
    for name, loop in cfg.loops.items():
        doc = load_unit_state(store, name)
        st, consec = str(doc.get("status") or "idle"), int(doc.get("consecutive_failures") or 0)
        err = str(doc.get("error") or "").strip() or (_log_error_fallback(store, name) if st == "failed" else "")
        mode = "daemon" if loop.daemon else "cycle"
        if loop.enabled:
            if check:
                problems.extend(check_requires(loop.requires, f"loops.{name}"))
            if st == "failed" or consec > 0:
                reason = "last run failed" if st == "failed" else f"{consec} consecutive failure(s)"
                problems.append(f"loops.{name}: {reason}")
                att = {"kind": "loop", "mode": mode, "name": name, "status": st, "failed": st == "failed"}
                attention.append({**att, "consecutive_failures": consec, **({"error": err} if err else {})})
        lr = doc.get("last_run") if isinstance(doc.get("last_run"), dict) and doc.get("last_run") else None
        extra_i = {
            k: v for k, v in (("ingest_mode", loop.mode if loop.ingest else ""), ("last_run", lr), ("error", err)) if v
        }
        base_i = {"enabled": loop.enabled, "mode": mode, "status": st, "last_success": doc.get("last_success", "")}
        loops_out[name] = {**base_i, "consecutive_failures": consec, **extra_i}

    events_seq, known_units, orphaned_states = records.latest_event_seq(store), set(cfg.loops), []
    with suppress(FileNotFoundError):
        private_dir(store, create=False)
        for p, _ in store.walk(".bf", ordered=True):
            is_st = p.count("/") == 1 and p.endswith(".json") and not p.endswith(".context.json")
            if is_st and (s := p[4:-5]) not in known_units:
                orphaned_states.append(s)

    from bf import markdown

    notes_total = sum(1 for f in AUTHORED for p, _ in store.walk(f, ordered=False) if markdown.okf_role(p) is not None)
    rec_total, rec_sources = 0, set()
    for p, _ in store.walk("memories", ordered=False):
        if (
            len(parts := p.split("/")) == 3
            and p.endswith(".json")
            and models.NAME_RE.fullmatch(src := parts[1])
            and src not in models.RESERVED_SOURCES
        ):
            rec_total += 1
            rec_sources.add(src)
    counts = {"notes": notes_total, "records": rec_total, "sources": len(rec_sources), "loops": len(cfg.loops)}
    inside = False
    with suppress(Error):
        inside = config.inside_brain() is not None
    out_a = {"brain": cfg.name, "ok": not problems, "inside_brain": inside, "events_seq": events_seq, "counts": counts}
    out_b = {"attention": attention, "orphaned_states": orphaned_states, "problems": problems}
    out: dict[str, Any] = {**out_a, **out_b, **({"loops": loops_out} if all_units else {})}
    return {k: out[k] for k in wanted if k in out} if wanted is not None else out
