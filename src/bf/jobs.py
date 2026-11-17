"""Orchestration of `jobs`, `.bf/<unit>.json`, `structlog` `logs/<unit>.log`, and channels."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from bf import config, engine, markdown, models, records
from bf.config import ChannelSpec, EventMatch, JobSpec, ServiceSpec
from bf.models import AUTHORED, MAX_NOTE, Error, InputError, clean, decode, encode, now_utc
from bf.storage import Store, private_dir, relative, writer

_ENV_1 = "PATH HOME USER LOGNAME TMPDIR SHELL TERM TZ SSH_AUTH_SOCK DISPLAY WAYLAND_DISPLAY XDG_CONFIG_HOME"
_ENV_2 = "XDG_CACHE_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS GH_TOKEN GITHUB_TOKEN"
_ENV_3 = "GOOGLE_APPLICATION_CREDENTIALS CLOUDSDK_CONFIG SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE"
_ENV_4 = "HTTP_PROXY HTTPS_PROXY NO_PROXY ALL_PROXY http_proxy https_proxy no_proxy all_proxy"
_ENV_ALLOWLIST: tuple[str, ...] = tuple(f"{_ENV_1} {_ENV_2} {_ENV_3} {_ENV_4}".split())
_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")
_GIT_HOOK_ENV = (*_GIT_ENV, "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_QUARANTINE_PATH")


def load_unit_state(store: Store, name: str, kind: str = "job") -> dict[str, Any]:
    """Load `.bf/<name>.json`, returning an initialized `idle` state when absent."""
    state = private_dir(store)
    path = f"{name}.json"
    with suppress(FileNotFoundError):
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


def _ev_meta(ev_d: Mapping[str, Any], *keys: str) -> str:
    for k in keys:
        if isinstance(v := ev_d.get(k), str) or type(v) is int:
            with suppress(Error):
                return clean(str(v).strip(), k)
    return ""


def _match_event(event: dict[str, Any], rule: EventMatch) -> bool:
    if (rule.kind and event.get("kind") != rule.kind) or (rule.source and event.get("source") != rule.source):
        return False
    if not rule.filter:
        return True
    if (jq := shutil.which("jq")) is None:
        raise Error("jq is required for event filters; install it with mise install")
    proc = subprocess.run(  # noqa: S603
        [jq, "-e", "--", rule.filter], input=encode(event), capture_output=True, timeout=5, check=False
    )
    return proc.returncode == 0


def read_events_doc(store: Store) -> dict[str, Any] | None:
    """Read `.bf/events.json` if present and a JSON object."""
    with suppress(FileNotFoundError):
        loaded = decode(private_dir(store).read("events.json", 16 << 20), ".bf/events.json")
        if isinstance(loaded, dict):
            return dict(loaded)
    return None


def pending_events(
    store: Store, last_seq: int, rules: tuple[EventMatch, ...], *, events_doc: Mapping[str, Any] | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Return `(matching_events, latest_seq)` since `last_seq` from `.bf/events.json`."""
    if not rules:
        return [], 0
    if events_doc is None and (events_doc := read_events_doc(store)) is None:
        return [], last_seq
    if not isinstance(raw_evs := events_doc.get("events"), list):
        return [], last_seq
    latest = raw_seq if type(raw_seq := events_doc.get("seq")) is int else last_seq
    matched = [
        dict(ev)
        for ev in raw_evs
        if isinstance(ev, dict)
        and type(ev_seq := ev.get("seq")) is int
        and ev_seq > last_seq
        and any(_match_event(ev, rule) for rule in rules)
    ]
    return matched, max(latest, last_seq)


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
    store: Store,
    name: str,
    base_argv: Sequence[str],
    run_id: str,
    start_iso: str,
    *,
    end_iso: str = "",
    trigger: str = "manual",
    input_rel: str = "",
    events: Sequence[dict[str, Any]] = (),
    stdin_text: str = "",
    unit_env: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Expand `{{...}}` placeholders, write `.bf/<name>.context.json`, and build clean `env`."""
    effective_end = end_iso or start_iso
    bf_dir = private_dir(store)
    input_abs = ""
    if input_rel:
        relative(input_rel)
        if not store.exists(input_rel):
            raise Error(f"{name}: input file {input_rel} does not exist")
        input_abs = str(store.root / input_rel)
    ctx_path = f"{name}.context.json"
    ctx = {"run": run_id, "kind": trigger, "job": name, "events": list(events), "stdin": stdin_text}
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
    for ek, ev in (unit_env or {}).items():
        env[ek] = re.sub(r"\{\{[a-z]+\}\}", lambda m: env_map.get(m[0], m[0]), ev)
    env |= config.load_dotenv(store)
    bf_env = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "BF_BRAIN": str(store.root), "BF_TRIGGER": trigger}
    bf_env |= {"BF_STATE_DIR": str(bf_dir.root / name), "BF_START": start_iso, "BF_END": effective_end}
    return argv, {**env, **bf_env}


def stop_proc(proc: subprocess.Popen[bytes]) -> None:
    """Terminate a subprocess process group (`SIGTERM` then `SIGKILL` after 2s)."""
    with suppress(OSError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(timeout=2)


def exec_argv(
    argv: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: int, stdin_bytes: bytes = b""
) -> tuple[int, bytes, str]:
    """Run literal `argv` in a dedicated process group with bounded timeout and clean teardown."""
    if not argv:
        raise Error("empty command argv")
    pipe = subprocess.PIPE
    try:
        with subprocess.Popen(  # noqa: S603
            list(argv), cwd=cwd, env=env, stdin=pipe, stdout=pipe, stderr=pipe, start_new_session=True
        ) as proc:
            try:
                out, err = proc.communicate(input=stdin_bytes, timeout=timeout)
                return proc.returncode, out, err[-65536:].decode("utf-8", "replace").strip()
            except BaseException as exc:
                stop_proc(proc)
                if isinstance(exc, subprocess.TimeoutExpired):
                    return 124, b"", f"timed out after {timeout}s"
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


def record_run(
    store: Store,
    kind: str,
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
    emit_job_event: bool = False,
    trigger: str = "",
) -> None:
    """Update `.bf/<name>.json`, append a `structlog` JSON line to `logs/<name>.log`, and emit `job.*`."""
    cfg_name = config.load(store).name
    ended_iso, ok = now_utc(ended), code == 0
    status = "ok" if ok else "failed"
    with writer(store):
        latest = load_unit_state(store, name, kind=kind)
        last_ok = ended_iso if ok else str(latest.get("last_success") or "")
        last_sched = ended_iso if ok and kind == "job" and trigger not in {"event", "hook"} else _sched_anchor(latest)
        consec = 0 if ok else int(latest.get("consecutive_failures") or 0) + 1
        runs, fails = int(latest.get("runs") or 0) + 1, int(latest.get("failures") or 0) + int(not ok)
        seq = cursor_seq if ok and cursor_seq is not None else int(latest.get("event_seq") or 0)
        d1 = {"kind": kind, "name": name, "status": status, "updated": ended_iso}
        d2 = {"last_start": started_iso, "last_end": ended_iso, "last_success": last_ok, "last_scheduled": last_sched}
        d3 = {"event_seq": seq, "runs": runs, "failures": fails, "consecutive_failures": consec, "duration_ms": dur_ms}
        err_clean = models.redact_secrets(err_msg)[:4096]
        stderr_clean = models.redact_secrets(stderr)[:8192]
        new_doc: dict[str, Any] = {**d1, **d2, **d3, "last_run": dict(summary or {}), "error": err_clean}
        save_unit_state(store, new_doc, locked=True)
        ev_k = ("brain", "unit", "kind", "run", "trigger", "started", "ended", "duration_ms", "exit_code")
        ev_v = (cfg_name, name, kind, run_id, trigger or kind, started_iso, ended_iso, dur_ms, code)
        ev_fields = dict(zip(ev_k, ev_v, strict=True))
        extras = {k: v for k, v in (("summary", summary), ("error", err_clean), ("stderr", stderr_clean)) if v}
        msg, sev = f"{kind}.{'succeeded' if ok else 'failed'}", "INFO" if ok else "ERROR"
        store.append_log(name, models.log_line(msg, severity=sev, time_iso=ended_iso, **ev_fields, **extras))
        if emit_job_event:
            ev_data = {"run": run_id, "exit_code": code, "duration_ms": dur_ms}
            records.append_event(store, "job.succeeded" if ok else "job.failed", name, ev_data, locked=True, now=ended)


def execute_one_job(
    store: Store,
    job: JobSpec,
    *,
    trigger: str,
    events: Sequence[dict[str, Any]] = (),
    cursor_target_seq: int | None = None,
    extra_args: Sequence[str] = (),
    stdin_bytes: bytes = b"",
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute one `JobSpec`, optionally ingesting sensor stdout and recording `.bf/` + `logs/`."""
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    started_iso = now_utc(instant)
    run_id = f"{job.name}-{int(instant.timestamp())}-{os.urandom(3).hex()}"
    state_doc = load_unit_state(store, job.name, kind="job")
    last_ok = _sched_anchor(state_doc) if trigger == "schedule" else str(state_doc.get("last_success") or "")
    base_dt = _parse_dt(last_ok) or instant
    win_start = base_dt - timedelta(seconds=job.overlap if last_ok else job.lookback)
    if missing := check_requires(job.requires, f"jobs.{job.name}"):
        raise Error("; ".join(missing))
    if dry_run:
        return {"job": job.name, "run": run_id, "status": "dry_run", "trigger": trigger}

    t0 = time.monotonic()
    summary: dict[str, Any] = {}
    err_msg = err_txt = ""
    in_txt = stdin_bytes.decode("utf-8", "replace")
    prep = {"end_iso": started_iso, "input_rel": job.input, "trigger": trigger, "events": events, "stdin_text": in_txt}
    cmd = [*job.run, *extra_args]
    argv, env = prepare_unit(store, job.name, cmd, run_id, now_utc(win_start), **prep, unit_env=job.env)
    proc_stdin = stdin_bytes or (store.read(job.input, MAX_NOTE) if job.input else b"")
    save_unit_state(store, {**state_doc, "status": "running", "updated": started_iso, "last_start": started_iso})
    code, out_bytes, err_txt = exec_argv(argv, cwd=store.root, env=env, timeout=job.timeout, stdin_bytes=proc_stdin)
    if code != 0:
        err_msg = models.redact_secrets(err_txt or f"exit code {code}")[-1024:]
    else:
        try:
            if job.ingest:
                ing_kw: dict[str, Any] = {"mode": job.mode, "allow_removal": job.allow_removal, "now": now}
                summary = records.ingest(store, job.ingest, out_bytes, **ing_kw)
            elif job.action_keep and out_bytes.strip():
                act_time = datetime.now(UTC) if now is None else instant
                summary = {"action": _save_report_action(store, job.name, out_bytes, job.action_keep, act_time)}
            txt = out_bytes.decode("utf-8", "replace").strip()
            if not job.ingest and job.channel and txt and txt != "NO_REPLY":
                src_ev = (ev for ev in events if ev.get("source") == job.channel)
                ch_evs = [ev.get("data") for ev in src_ev if ev.get("kind") == "channel.message"]
                ev_d = ch_evs[0] if len(ch_evs) == 1 and isinstance(ch_evs[0], dict) else {}
                th, rp = _ev_meta(ev_d, "conversation", "thread"), _ev_meta(ev_d, "reply_to", "id")
                send_channel_message(store, txt, to=job.channel, thread=th, reply_to=rp, run_id=run_id, now=now)
            elif not summary and txt.startswith("{") and len(out_bytes) <= 65536:
                with suppress(Error):
                    if isinstance(dec := decode(out_bytes, f"jobs.{job.name}"), dict):
                        summary = dict(dec)
        except Error as error:
            code, err_msg = 1, models.redact_secrets(str(error))[-1024:]

    ended = datetime.now(UTC) if now is None else instant
    dur_ms = max(0, int((time.monotonic() - t0) * 1000))
    kw: dict[str, Any] = {"ended": ended, "dur_ms": dur_ms, "code": code, "err_msg": err_msg, "stderr": err_txt}
    rec_kw: dict[str, Any] = {"summary": summary, "cursor_seq": cursor_target_seq, "emit_job_event": True}
    record_run(store, "job", job.name, run_id, started_iso, **kw, **rec_kw, trigger=trigger)
    status = "ok" if code == 0 else "failed"
    out_res = {"job": job.name, "run": run_id, "status": status, "exit_code": code, "duration_ms": dur_ms}
    return {**out_res, **({"error": err_msg} if err_msg else {}), **({"summary": summary} if summary else {})}


def run_jobs(
    store: Store,
    names: Sequence[str] = (),
    *,
    due_only: bool = False,
    hook: str = "",
    extra_args: Sequence[str] = (),
    stdin_bytes: bytes = b"",
    dry_run: bool = False,
    rebuild: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run selected, due, or Git-hook jobs in declaration order."""
    cfg = config.load(store)
    sched_now = datetime.now(UTC) if now is None else now.astimezone(UTC)
    if bool(names) + bool(due_only) + bool(hook) != 1:
        raise InputError("specify job names, --due, or --hook (mutually exclusive)")
    if hook and hook not in {"pre-commit", "pre-push"}:
        raise InputError("hook must be pre-commit or pre-push")
    for wanted in names:
        if wanted not in cfg.jobs:
            raise InputError(f"unknown job {wanted!r} in bf.yaml")
    if extra_args and (due_only or len(names) > 1):
        raise InputError("passthrough -- arguments require a single job or --hook")

    built = engine.build(store, now=now) if due_only and rebuild and not dry_run else None
    cached_events = read_events_doc(store) if due_only else None

    selected: list[tuple[JobSpec, str, list[dict[str, Any]], int | None]] = []
    advance_cursors: list[tuple[str, int]] = []
    for name, job in cfg.jobs.items():
        if (names and name not in names) or (not names and not job.enabled):
            continue
        if hook:
            if hook in job.hooks:
                selected.append((job, "hook", [], None))
            continue
        if due_only:
            if job.hooks and not (job.schedule or job.events):
                continue
            state_doc = load_unit_state(store, name, kind="job")
            if _in_backoff(state_doc, job.every or 3600, sched_now):
                continue
            cur_seq = int(state_doc.get("event_seq") or 0)
            matched_evs, latest_seq = pending_events(store, cur_seq, job.events, events_doc=cached_events)
            target_seq = latest_seq if job.events and latest_seq else None
            if matched_evs:
                selected.append((job, "event", matched_evs, target_seq))
            elif is_schedule_due(job, state_doc, sched_now, tz_name=cfg.timezone):
                selected.append((job, "schedule", [], target_seq))
            elif target_seq is not None and target_seq != cur_seq:
                advance_cursors.append((name, target_seq))
        else:
            selected.append((job, "manual", [], None))

    if advance_cursors and not dry_run:
        with writer(store):
            for cursor_name, cursor_seq in advance_cursors:
                latest = load_unit_state(store, cursor_name, kind="job")
                seq = max(int(latest.get("event_seq") or 0), cursor_seq)
                save_unit_state(store, {**latest, "event_seq": seq}, locked=True)

    ex_kw: dict[str, Any] = {"extra_args": extra_args, "stdin_bytes": stdin_bytes, "dry_run": dry_run, "now": now}
    results = [execute_one_job(store, j, trigger=t, events=e, cursor_target_seq=s, **ex_kw) for j, t, e, s in selected]
    ok_all = (built is None or bool(built["ok"])) and all(r["status"] in {"ok", "dry_run"} for r in results)
    out_d = {"brain": cfg.name, "ok": ok_all, "ran": len(results), "results": results}
    return {**out_d, "problems": list(built["problems"])} if built and built["problems"] else out_d


def send_channel_message(
    store: Store,
    text: str,
    *,
    to: str,
    thread: str = "",
    reply_to: str = "",
    run_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Send an outbound message through a `channels.<name>.send` command."""
    cfg = config.load(store)
    if not (cleaned_text := text.strip()):
        raise InputError("message text must be nonempty")
    ch = cfg.channels.get(to)
    if ch is None or not ch.send:
        raise Error(f"channel {to!r} has no send command configured in bf.yaml")
    if missing := check_requires(ch.requires, f"channels.{to}"):
        raise Error("; ".join(missing))

    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    msg_id = f"post-{int(instant.timestamp())}-{os.urandom(3).hex()}"
    extra_meta = (("conversation", thread, "thread"), ("reply_to", reply_to, "reply_to"), ("run", run_id, "run"))
    payload: dict[str, Any] = {"id": msg_id, "text": cleaned_text, **{k: clean(v, f) for k, v, f in extra_meta if v}}
    argv, env = prepare_unit(store, to, ch.send, run_id or msg_id, now_utc(instant), trigger="post", unit_env=ch.env)
    code, out_bytes, err_txt = exec_argv(argv, cwd=store.root, env=env, timeout=ch.timeout, stdin_bytes=encode(payload))
    if code != 0:
        raise Error(f"channels.{to} send failed: {models.redact_secrets(err_txt or f'exit code {code}')[-1024:]}")
    dec = decode(out_bytes.strip(), f"channels.{to} send output") if out_bytes.strip() else {}
    reply_obj = dict(dec) if isinstance(dec, dict) else {}
    r_id, r_conv = reply_obj.get("id"), reply_obj.get("conversation")
    ev_id = clean(r_id, "reply id") if isinstance(r_id, str) and r_id.strip() else msg_id
    ev_conv = clean(r_conv, "reply conversation") if isinstance(r_conv, str) and r_conv.strip() else thread
    records.append_event(store, "channel.sent", to, {"id": ev_id, "conversation": ev_conv}, now=instant)
    return {"brain": cfg.name, "channel": to, "status": "ok", "reply": reply_obj or {"id": msg_id}}


def _collect_unit_status(
    store: Store,
    units: Mapping[str, JobSpec | ChannelSpec | ServiceSpec],
    kind: str,
    *,
    check: bool,
    problems: list[str],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, unit in units.items():
        doc = load_unit_state(store, name, kind=kind)
        if check and unit.enabled:
            problems.extend(check_requires(unit.requires, f"{kind}s.{name}"))
        if doc.get("status") == "failed" and unit.enabled:
            problems.append(f"{kind}s.{name}: last run failed")
        st_s = doc.get("status", "idle")
        info: dict[str, Any] = {"enabled": unit.enabled, "status": st_s, "last_success": doc.get("last_success", "")}
        info["consecutive_failures"] = doc.get("consecutive_failures", 0)
        if isinstance(unit, JobSpec) and unit.ingest:
            info["mode"] = unit.mode
        if isinstance(lr := doc.get("last_run"), dict) and lr:
            info["last_run"] = lr
        if doc.get("error"):
            info["error"] = doc["error"]
        out[name] = info
    return out


def status_summary(store: Store, *, check: bool = False) -> dict[str, Any]:
    """Return folder counts, failed-unit attention, `jobs`/`channels`/`services` states, and `events_seq`."""
    cfg = config.load(store)
    problems: list[str] = []
    groups: Any = (("job", cfg.jobs), ("channel", cfg.channels), ("service", cfg.services))
    units = {f"{k}s": _collect_unit_status(store, g, k, check=check, problems=problems) for k, g in groups}
    ev_doc = read_events_doc(store)
    events_seq = raw_seq if ev_doc and type(raw_seq := ev_doc.get("seq")) is int else 0
    folders = (*AUTHORED, "memories", "schemas", "evals")
    counts = {f: len([p for p in store.files(f) if not p.endswith("/.forgotten")]) for f in folders}
    att = ((k[:-1], n) for k, g in units.items() for n, i in g.items() if i["enabled"] and i["status"] == "failed")
    attention = [{"kind": k, "name": n, "failed": True} for k, n in att]
    inside = False
    with suppress(Error):
        inside = config.inside_brain() is not None
    res = {"brain": cfg.name, "ok": not problems, "inside_brain": inside, "events_seq": events_seq}
    return {**res, "counts": counts, "attention": attention, **units, "problems": problems}
