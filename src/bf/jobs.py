"""Orchestration of `jobs`, `.bf/<unit>.json`, and `structlog` `logs/<unit>.log`."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Required, TypedDict, Unpack
from zoneinfo import ZoneInfo

from croniter import croniter

from bf import config, engine, markdown, models, query, records
from bf.config import EventMatch, JobSpec, ServiceSpec
from bf.models import AUTHORED, MAX_NOTE, Error, InputError, decode, encode, now_utc
from bf.storage import Store, private_dir, relative, writer

_ENV_SYS = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "TERM", "TZ", "SSH_AUTH_SOCK", "DISPLAY")
_ENV_XDG = ("WAYLAND_DISPLAY", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")
_ENV_BUS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "GH_TOKEN", "GITHUB_TOKEN", "CLOUDSDK_CONFIG")
_ENV_TLS = ("GOOGLE_APPLICATION_CREDENTIALS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_ENV_PRX = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy")
_ENV_ALLOWLIST = (*_ENV_SYS, *_ENV_XDG, *_ENV_BUS, *_ENV_TLS, *_ENV_PRX)
_GIT_A = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")
_GIT_HOOK_ENV = (*_GIT_A, "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_QUARANTINE_PATH")


class _PrepOpts(TypedDict, total=False):
    trigger: str
    input_rel: str
    end_iso: str
    events: Sequence[dict[str, Any]]
    stdin_text: str
    unit_env: Mapping[str, str]


class _RecordOpts(TypedDict, total=False):
    ended: Required[datetime]
    dur_ms: Required[int]
    code: Required[int]
    err_msg: str
    stderr: str
    summary: Mapping[str, Any] | None
    cursor_seq: int | None
    emit_job_event: bool
    trigger: str


class _ExecOpts(TypedDict, total=False):
    stdin_bytes: bytes
    stop_event: threading.Event | None


class RunOpts(_ExecOpts, total=False):
    extra_args: Sequence[str]
    dry_run: bool
    now: datetime | None


class _JobOpts(RunOpts, total=False):
    events: Sequence[dict[str, Any]]
    cursor_target_seq: int | None


type _Events = tuple[list[dict[str, Any]], int]


def load_unit_state(store: Store, name: str, kind: str = "job") -> dict[str, Any]:
    """Load `.bf/<name>.json`, returning an initialized `idle` state when absent."""
    path = f"{name}.json"
    with suppress(FileNotFoundError):
        state = private_dir(store, create=False)
        if isinstance(parsed := decode(state.read(path, 1 << 20), f".bf/{path}"), dict):
            return dict(parsed)
    zeros = dict.fromkeys(("event_seq", "runs", "failures", "consecutive_failures", "duration_ms"), 0)
    empty = dict.fromkeys(("last_start", "last_end", "last_success", "last_scheduled", "error"), "")
    return {"kind": kind, "name": name, "status": "idle", "updated": now_utc(), **empty, **zeros, "last_run": {}}


def save_unit_state(store: Store, doc: dict[str, Any], *, locked: bool = False) -> None:
    """Write `.bf/<name>.json` atomically with `0600` permissions."""
    state = private_dir(store)
    path = f"{doc['name']}.json"
    if locked:
        state.write(path, encode(doc), mode=0o600)
    else:
        with writer(store):
            state.write(path, encode(doc), mode=0o600)


def _rule_head_matches(event: Mapping[str, Any], rule: EventMatch) -> bool:
    return (not rule.kind or event.get("kind") == rule.kind) and (not rule.source or event.get("source") == rule.source)


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
        jq_bin = jq_bin or query.resolve_tool("jq")
        wrapped = (
            f"{query.JQ_PRELUDE}\ntry ([\n{flt}\n] | "
            'if length == 0 then false else (.[-1] // false | . != false) end) catch (if . == "disabled" then error else false end)'
        )
        jq_kw: dict[str, Any] = {"capture_output": True, "cwd": "/", "env": query.safe_env(jq_bin), "timeout": 5}
        try:
            cmd = [jq_bin, "-L", "/dev/null", "-c", "--", wrapped]
            proc = subprocess.run(cmd, input=b"".join(encode(cands[i]) for i in sub_idx), **jq_kw, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as error:
            raise Error("event filter timed out after 5s") from error
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise Error(f"event filter failed: {err.splitlines()[-1] if err else 'jq exited nonzero'}")
        outs = proc.stdout.splitlines()[: len(sub_idx)]
        matched_idx.update(sub_idx[pos] for pos, ln in enumerate(outs) if ln == b"true")
    return [ev for i, ev in enumerate(cands) if i in matched_idx], latest


def _parse_dt(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _in_backoff(state_doc: Mapping[str, Any], cap: int, now: datetime) -> bool:
    fails = int(state_doc.get("consecutive_failures") or 0)
    if fails > 0 and (last_dt := _parse_dt(str(state_doc.get("last_end") or ""))) is not None:
        backoff = min(60 * (1 << min(fails - 1, 10)), cap)
        return (now - last_dt).total_seconds() < backoff
    return False


def _sched_anchor(doc: Mapping[str, Any]) -> str:
    return str(doc["last_scheduled"] if "last_scheduled" in doc else doc.get("last_success") or "")


def is_schedule_due(job: JobSpec, state_doc: Mapping[str, Any], now: datetime, *, tz_name: str = "UTC") -> bool:
    """Whether `job`'s duration or cron `schedule` is due at `now`, respecting failure backoff."""
    if not job.schedule or _in_backoff(state_doc, job.every or 3600, now):
        return False
    last_dt = _parse_dt(_sched_anchor(state_doc))
    if job.every is not None:
        return last_dt is None or (now - last_dt).total_seconds() >= job.every
    tz = ZoneInfo(tz_name)
    local_now = (now.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=1)).astimezone(tz)
    try:
        slot = croniter(job.cron, local_now).get_prev(datetime)
    except ValueError, KeyError, TypeError:
        return False
    return last_dt is None or (last_dt.astimezone(tz) < slot)


def check_requires(bins: tuple[str, ...], label: str) -> list[str]:
    return [f"{label}: missing required command {b!r} on PATH" for b in bins if shutil.which(b) is None]


def prepare_unit(
    store: Store, name: str, base_argv: Sequence[str], run_id: str, start_iso: str, **opts: Unpack[_PrepOpts]
) -> tuple[list[str], dict[str, str]]:
    """Expand `{{...}}` placeholders, write `.bf/<name>.context.json`, and build clean `env`."""
    trigger, input_rel = opts.get("trigger", "manual"), opts.get("input_rel", "")
    effective_end = opts.get("end_iso") or start_iso
    events, stdin_text = list(opts.get("events", ())), opts.get("stdin_text", "")
    unit_env = opts.get("unit_env") or {}
    bf_dir = private_dir(store)
    input_abs = ""
    if input_rel:
        relative(input_rel)
        if not store.exists(input_rel):
            raise Error(f"{name}: input file {input_rel} does not exist")
        input_abs = str(store.root / input_rel)
    ctx_path = f"{name}.context.json"
    ctx = {"run": run_id, "kind": trigger, "job": name, "events": events, "stdin": stdin_text}
    bf_dir.write(ctx_path, encode({**ctx, "start": start_iso, "end": effective_end, "input": input_abs}), mode=0o600)
    home_str = os.environ.get("HOME", "")
    with suppress(RuntimeError):
        home_str = str(Path.home())
    env_map = {"{{brain}}": str(store.root), "{{home}}": home_str, "{{state}}": str(bf_dir.root)}
    toks = {**env_map, "{{run}}": run_id, "{{start}}": start_iso, "{{end}}": effective_end}
    toks |= {"{{input}}": input_abs, "{{context}}": str(bf_dir.root / ctx_path)}
    argv = [re.sub(r"\{\{[a-z]+\}\}", lambda m: toks.get(m[0], m[0]), token) for token in base_argv]
    if argv and argv[0] == "bf":
        argv = [sys.executable, "-m", "bf", *argv[1:]]
    elif argv and "/" in argv[0] and not Path(argv[0]).is_absolute() and store.exists(argv[0]):
        argv[0] = str(store.root.joinpath(*relative(argv[0])))
    keys = (*_ENV_ALLOWLIST, *(_GIT_HOOK_ENV if trigger == "hook" else ()))
    env = {key: val for key in keys if (val := os.environ.get(key))}
    for ek, ev in unit_env.items():
        env[ek] = re.sub(r"\{\{[a-z]+\}\}", lambda m: env_map.get(m[0], m[0]), ev)
    env |= config.load_dotenv(store)
    bf_env = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "BF_BRAIN": str(store.root), "BF_TRIGGER": trigger}
    bf_env |= {"BF_STATE_DIR": str(bf_dir.root / name), "BF_START": start_iso, "BF_END": effective_end}
    return argv, {**env, **bf_env}


stop_proc = query.stop_proc


def exec_argv(
    argv: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: int, **opts: Unpack[_ExecOpts]
) -> tuple[int, bytes, str]:
    """Run literal `argv` in a dedicated process group with bounded timeout and clean teardown."""
    stdin_bytes, stop_event = opts.get("stdin_bytes", b""), opts.get("stop_event")
    if not argv:
        raise Error("empty command argv")
    if stop_event is not None and stop_event.is_set():
        return 130, b"", "stopped"
    pipe, deadline = subprocess.PIPE, time.monotonic() + timeout
    try:
        with subprocess.Popen(  # noqa: S603
            list(argv), cwd=cwd, env=env, stdin=pipe, stdout=pipe, stderr=pipe, start_new_session=True
        ) as proc:
            try:
                inp: bytes | None = stdin_bytes
                while True:
                    if stop_event is not None and stop_event.is_set():
                        stop_proc(proc)
                        return 130, b"", "stopped"
                    if (rem := deadline - time.monotonic()) <= 0:
                        stop_proc(proc)
                        return 124, b"", f"timed out after {timeout}s"
                    try:
                        step = rem if stop_event is None else min(0.2, rem)
                        out, err = proc.communicate(input=inp, timeout=step)
                        return proc.returncode, out, err[-65536:].decode("utf-8", "replace").strip()
                    except subprocess.TimeoutExpired:
                        inp = None
            except BaseException:
                stop_proc(proc)
                raise
    except (OSError, ValueError) as error:
        return 127, b"", models.failure(error)


def _save_report_action(store: Store, name: str, out_bytes: bytes, keep: int, instant: datetime) -> str:
    cfg = config.load(store)
    day = instant.astimezone(ZoneInfo(cfg.timezone)).date().isoformat()
    rel = f"actions/{day}_{name}-{os.urandom(4).hex()}/ACTION.md"
    compiled = markdown.compile_note(rel, out_bytes, brain_name=cfg.name)
    pat = re.compile(rf"^actions/\d{{4}}-\d{{2}}-\d{{2}}_{re.escape(name)}-[0-9a-f]{{8}}/ACTION\.md$")
    with writer(store):
        store.write(rel, out_bytes)
        store.write(markdown.compiled_note_path(rel), encode(compiled))
        existing = sorted(
            (p for p in store.files("actions") if pat.fullmatch(p)),
            key=lambda p: (p == rel, p[:18], (store.root / p).lstat().st_mtime_ns, p),
        )
        for old in existing[:-keep] if keep > 0 else ():
            store.delete(old)
            with suppress(FileNotFoundError):
                store.delete(markdown.compiled_note_path(old))
            with suppress(OSError), store.parent(old.rsplit("/", 1)[0]) as (pfd, leaf):
                os.rmdir(leaf, dir_fd=pfd)
    return rel


def _merge_cursor(stored: int, target: int, disk: int) -> int:
    return stored if target > disk else (target if disk < stored else max(stored, target))


def record_run(store: Store, kind: str, name: str, run_id: str, started_iso: str, **opts: Unpack[_RecordOpts]) -> None:
    """Update `.bf/<name>.json`, append a `structlog` JSON line to `logs/<name>.log`, and emit `job.*`."""
    ended, dur_ms, code = opts["ended"], opts["dur_ms"], opts["code"]
    err_msg, stderr, summary = opts.get("err_msg", ""), opts.get("stderr", ""), opts.get("summary")
    cursor_seq, emit_ev, trigger = opts.get("cursor_seq"), opts.get("emit_job_event", False), opts.get("trigger", "")
    cfg_name, ended_iso, ok = config.load(store).name, now_utc(ended), code == 0
    with writer(store):
        latest = load_unit_state(store, name, kind=kind)
        last_ok = ended_iso if ok else str(latest.get("last_success") or "")
        last_sched = ended_iso if ok and kind == "job" and trigger not in {"event", "hook"} else _sched_anchor(latest)
        consec = 0 if ok else int(latest.get("consecutive_failures") or 0) + 1
        runs, fails = int(latest.get("runs") or 0) + 1, int(latest.get("failures") or 0) + int(not ok)
        stored_seq = int(latest.get("event_seq") or 0)
        disk_seq = records.latest_event_seq(store) if ok and cursor_seq is not None else 0
        seq = _merge_cursor(stored_seq, cursor_seq, disk_seq) if ok and cursor_seq is not None else stored_seq
        err_clean, stderr_clean = models.redact_secrets(err_msg)[:4096], models.redact_secrets(stderr)[:8192]
        d1 = {"kind": kind, "name": name, "status": "ok" if ok else "failed", "updated": ended_iso}
        d2 = {"last_start": started_iso, "last_end": ended_iso, "last_success": last_ok, "last_scheduled": last_sched}
        d3 = {"event_seq": seq, "runs": runs, "failures": fails, "consecutive_failures": consec, "duration_ms": dur_ms}
        save_unit_state(store, {**d1, **d2, **d3, "last_run": dict(summary or {}), "error": err_clean}, locked=True)
        ev_a = {"brain": cfg_name, "unit": name, "kind": kind, "run": run_id, "trigger": trigger or kind}
        ev_b = {"started": started_iso, "ended": ended_iso, "duration_ms": dur_ms, "exit_code": code}
        extras = {k: v for k, v in (("summary", summary), ("error", err_clean), ("stderr", stderr_clean)) if v}
        msg, sev = f"{kind}.{'succeeded' if ok else 'failed'}", "INFO" if ok else "ERROR"
        store.append_log(name, models.log_line(msg, severity=sev, time_iso=ended_iso, **ev_a, **ev_b, **extras))
        if emit_ev:
            ev_data = {"run": run_id, "exit_code": code, "duration_ms": dur_ms}
            records.append_event(store, "job.succeeded" if ok else "job.failed", name, ev_data, locked=True, now=ended)


def execute_one_job(store: Store, job: JobSpec, *, trigger: str, **opts: Unpack[_JobOpts]) -> dict[str, Any]:
    """Execute one `JobSpec`, optionally ingesting sensor stdout and recording `.bf/` + `logs/`."""
    events, cursor_target_seq = opts.get("events", ()), opts.get("cursor_target_seq")
    extra_args, stdin_bytes = opts.get("extra_args", ()), opts.get("stdin_bytes", b"")
    dry_run, now, stop_event = opts.get("dry_run", False), opts.get("now"), opts.get("stop_event")
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    started_iso, run_id = now_utc(instant), f"{job.name}-{int(instant.timestamp())}-{os.urandom(3).hex()}"
    state_doc = load_unit_state(store, job.name, kind="job")
    last_ok = _sched_anchor(state_doc) if trigger == "schedule" else str(state_doc.get("last_success") or "")
    win_start = (_parse_dt(last_ok) or instant) - timedelta(seconds=job.overlap if last_ok else job.lookback)
    if missing := check_requires(job.requires, f"jobs.{job.name}"):
        raise Error("; ".join(missing))
    if dry_run:
        return {"job": job.name, "run": run_id, "status": "dry_run", "trigger": trigger}

    t0, summary, err_msg, err_txt = time.monotonic(), {}, "", ""
    prep: _PrepOpts = {"end_iso": started_iso, "input_rel": job.input, "trigger": trigger, "events": events}
    prep |= {"stdin_text": stdin_bytes.decode("utf-8", "replace"), "unit_env": job.env}
    argv, env = prepare_unit(store, job.name, [*job.run, *extra_args], run_id, now_utc(win_start), **prep)
    proc_stdin, recorded = stdin_bytes or (store.read(job.input, MAX_NOTE) if job.input else b""), False
    save_unit_state(store, {**state_doc, "status": "running", "updated": started_iso, "last_start": started_iso})
    try:
        code, out_bytes, err_txt = exec_argv(
            argv, cwd=store.root, env=env, timeout=job.timeout, stdin_bytes=proc_stdin, stop_event=stop_event
        )
        if stop_event is not None and stop_event.is_set() and code == 130:
            dur_ms = max(0, int((time.monotonic() - t0) * 1000))
            return {"job": job.name, "run": run_id, "status": "stopped", "exit_code": 130, "duration_ms": dur_ms}
        if code != 0:
            err_msg = models.redact_secrets(err_txt or f"exit code {code}")[-1024:]
        else:
            try:
                if job.ingest:
                    summary = records.ingest(
                        store, job.ingest, out_bytes, mode=job.mode, allow_removal=job.allow_removal, now=now
                    )
                elif job.action_keep and out_bytes.strip():
                    act_time = datetime.now(UTC) if now is None else instant
                    summary = {"action": _save_report_action(store, job.name, out_bytes, job.action_keep, act_time)}
                elif len(out_bytes) <= 65536 and out_bytes.decode("utf-8", "replace").strip().startswith("{"):
                    with suppress(Error):
                        if isinstance(dec := decode(out_bytes, f"jobs.{job.name}"), dict):
                            summary = dict(dec)
            except models.FAILURES as error:
                code, err_msg = 1, models.redact_secrets(models.failure(error))[-1024:]

        ended, dur_ms = (datetime.now(UTC) if now is None else instant), max(0, int((time.monotonic() - t0) * 1000))
        rec_o: _RecordOpts = {"ended": ended, "dur_ms": dur_ms, "code": code, "err_msg": err_msg, "stderr": err_txt}
        rec_o |= {"summary": summary, "cursor_seq": cursor_target_seq, "emit_job_event": True, "trigger": trigger}
        record_run(store, "job", job.name, run_id, started_iso, **rec_o)
        recorded = True
    finally:
        if not recorded:
            with suppress(Error, OSError):
                t_end = now_utc(None if now is None else instant)
                save_unit_state(store, {**state_doc, "status": "stopped", "updated": t_end, "last_end": t_end})
    st = "ok" if code == 0 else "failed"
    out_res = {"job": job.name, "run": run_id, "status": st, "exit_code": code, "duration_ms": dur_ms}
    return {**out_res, **({"error": err_msg} if err_msg else {}), **({"summary": summary} if summary else {})}


def run_jobs(
    store: Store,
    names: Sequence[str] = (),
    *,
    due_only: bool = False,
    hook: str = "",
    rebuild: bool = True,
    **opts: Unpack[RunOpts],
) -> dict[str, Any]:
    """Run selected, due, or Git-hook jobs in declaration order."""
    extra_args, dry_run = opts.get("extra_args", ()), opts.get("dry_run", False)
    now, stop_event = opts.get("now"), opts.get("stop_event")
    cfg = config.load(store)
    sched_now = datetime.now(UTC) if now is None else now.astimezone(UTC)
    if bool(names) + bool(due_only) + bool(hook) != 1:
        raise InputError("specify job names, --due, or --hook (mutually exclusive)")
    if hook and hook not in {"pre-commit", "pre-push"}:
        raise InputError("hook must be pre-commit or pre-push")
    if bad := next((w for w in names if w not in cfg.jobs), ""):
        raise InputError(f"unknown job {bad!r} in bf.yaml")
    if extra_args and (due_only or len(names) > 1):
        raise InputError("passthrough -- arguments require a single job or --hook")

    built = engine.build(store, now=now, incremental=True) if due_only and rebuild and not dry_run else None
    latest_disk_seq: int | None = None
    cached_events: tuple[list[dict[str, Any]], int] | None = None
    selected: list[tuple[JobSpec, str, list[dict[str, Any]], int | None]] = []
    advance_cursors: list[tuple[str, int]] = []
    results: list[dict[str, Any]] = []

    def fail_due(job: JobSpec, trig: str, err: BaseException) -> None:
        msg = models.redact_secrets(models.failure(err))[-1024:]
        end_dt = datetime.now(UTC) if now is None else sched_now
        pre_id = f"{job.name}-{int(end_dt.timestamp())}-{os.urandom(3).hex()}"
        pos = (store, "job", job.name, pre_id, now_utc(end_dt))
        with suppress(Error, OSError):
            record_run(*pos, ended=end_dt, dur_ms=0, code=1, err_msg=msg, emit_job_event=True, trigger=trig)
        row = {"job": job.name, "run": pre_id, "status": "failed", "exit_code": 1, "duration_ms": 0, "error": msg}
        results.append(row)

    for name, job in cfg.jobs.items():
        if (names and name not in names) or (not names and not job.enabled):
            continue
        if hook:
            if hook in job.hooks:
                selected.append((job, "hook", [], None))
            continue
        if not due_only:
            selected.append((job, "manual", [], None))
            continue
        if job.hooks and not (job.schedule or job.events):
            continue
        state_doc = load_unit_state(store, name, kind="job")
        if _in_backoff(state_doc, job.every or 3600, sched_now):
            continue
        cur_seq, matched_evs, target_seq = int(state_doc.get("event_seq") or 0), [], None
        if job.events:
            latest_disk_seq = records.latest_event_seq(store) if latest_disk_seq is None else latest_disk_seq
            target_seq = latest_disk_seq
            if cur_seq != latest_disk_seq and latest_disk_seq > 0:
                cached_events = records.read_events(store) if cached_events is None else cached_events
                try:
                    matched_evs, target_seq = pending_events(store, cur_seq, job.events, events_data=cached_events)
                except models.FAILURES as err:
                    if dry_run:
                        raise
                    fail_due(job, "event", err)
                    continue
        if matched_evs:
            selected.append((job, "event", matched_evs, target_seq))
        elif is_schedule_due(job, state_doc, sched_now, tz_name=cfg.timezone):
            selected.append((job, "schedule", [], target_seq))
        elif target_seq is not None and target_seq != cur_seq:
            advance_cursors.append((name, target_seq))

    if advance_cursors and not dry_run:
        with writer(store):
            disk_seq = records.latest_event_seq(store)
            for cursor_name, cursor_seq in advance_cursors:
                latest = load_unit_state(store, cursor_name, kind="job")
                stored = int(latest.get("event_seq") or 0)
                if (nxt := _merge_cursor(stored, cursor_seq, disk_seq)) != stored:
                    save_unit_state(store, {**latest, "event_seq": nxt}, locked=True)

    for job, trig, evs, seq_target in selected:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            results.append(execute_one_job(store, job, trigger=trig, events=evs, cursor_target_seq=seq_target, **opts))
        except models.FAILURES as err:
            if not due_only or dry_run:
                raise
            fail_due(job, trig, err)
    ok_all = (built is None or bool(built["ok"])) and all(r["status"] in {"ok", "dry_run", "stopped"} for r in results)
    out_d = {"brain": cfg.name, "ok": ok_all, "ran": len(results), "results": results}
    return {**out_d, "problems": list(built["problems"])} if built and built["problems"] else out_d


def _collect_unit_status(
    store: Store, units: Mapping[str, JobSpec | ServiceSpec], kind: str, problems: list[str], *, check: bool
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, unit in units.items():
        doc = load_unit_state(store, name, kind=kind)
        if check and unit.enabled:
            problems.extend(check_requires(unit.requires, f"{kind}s.{name}"))
        if doc.get("status") == "failed" and unit.enabled:
            problems.append(f"{kind}s.{name}: last run failed")
        info: dict[str, Any] = {"enabled": unit.enabled, "status": doc.get("status", "idle")}
        info.update(last_success=doc.get("last_success", ""), consecutive_failures=doc.get("consecutive_failures", 0))
        if isinstance(unit, JobSpec) and unit.ingest:
            info["mode"] = unit.mode
        if isinstance(lr := doc.get("last_run"), dict) and lr:
            info["last_run"] = lr
        if doc.get("error"):
            info["error"] = doc["error"]
        out[name] = info
    return out


def status_summary(store: Store, *, check: bool = False) -> dict[str, Any]:
    """Return folder counts, failed-unit attention, `jobs`/`services` states, and `events_seq`."""
    cfg = config.load(store)
    problems: list[str] = []
    groups: Any = (("job", cfg.jobs), ("service", cfg.services))
    units = {f"{k}s": _collect_unit_status(store, g, k, problems, check=check) for k, g in groups}
    events_seq = records.latest_event_seq(store)
    folders = (*AUTHORED, "memories", "schemas", "evals")
    counts = {f: store.count_files(f, exclude_suffix="/.forgotten") for f in folders}
    att = ((k[:-1], n) for k, g in units.items() for n, i in g.items() if i["enabled"] and i["status"] == "failed")
    attention = [{"kind": k, "name": n, "failed": True} for k, n in att]
    inside = False
    with suppress(Error):
        inside = config.inside_brain() is not None
    res = {"brain": cfg.name, "ok": not problems, "inside_brain": inside, "events_seq": events_seq}
    return {**res, "counts": counts, "attention": attention, **units, "problems": problems}
