"""Orchestration of `jobs`, `logs/<name>.json`, `logs/events.json`, Git hooks, and outbound `bf post`."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bf import config, engine, markdown, query, records
from bf.config import ChannelSpec, EventMatch, JobSpec, ServerSpec
from bf.models import MAX_NOTE, Error, InputError, clean, decode, encode, now_utc
from bf.schemas import LOGS_SCHEMA, load_validator, validate_instance
from bf.storage import Store, private_dir, relative, writer

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_ENV_BASE = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "TERM", "TZ", "SSH_AUTH_SOCK")
_ENV_XDG = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")
_ENV_XDG2 = ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
_ENV_AUTH = ("GH_TOKEN", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG")
_ENV_TLS = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_ENV_ALLOWLIST = (*_ENV_BASE, *_ENV_XDG, *_ENV_XDG2, *_ENV_AUTH, *_ENV_TLS)
_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")
_GIT_HOOK_ENV = (*_GIT_ENV, "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_QUARANTINE_PATH")


def load_run_log(store: Store, name: str, kind: str = "job") -> dict[str, Any]:
    """Load `logs/<name>.json`, returning an initialized `idle` document when absent."""
    path = f"logs/{name}.json"
    with suppress(FileNotFoundError):
        if isinstance(parsed := decode(store.read(path, 4 << 20), path), dict):
            return dict(parsed)
    doc = {"kind": kind, "name": name, "status": "idle", "updated": now_utc()}
    return {**doc, "runs": 0, "failures": 0, "consecutive_failures": 0, "history": []}


def save_run_log(store: Store, doc: dict[str, Any], *, keep: int = 50, locked: bool = False) -> None:
    """Validate and write `logs/<name>.json` atomically."""
    path = f"logs/{doc['name']}.json"
    hist = doc.get("history")
    trimmed = {**doc, "history": list(hist[-keep:]) if isinstance(hist, list) else []}

    def write_doc() -> None:
        val = load_validator(store, "schemas/logs.json", LOGS_SCHEMA)
        if val is not None and (errs := validate_instance(val, trimmed, path)):
            raise Error("; ".join(errs))
        store.write(path, encode(trimmed))

    if locked:
        write_doc()
    else:
        with writer(store):
            write_doc()


def _cursor_seq(state_store: Store, name: str) -> int:
    path = f"{name}.cursor.json"
    with suppress(FileNotFoundError):
        parsed = decode(state_store.read(path, 65536), path)
        if isinstance(parsed, dict) and type(parsed.get("seq")) is int:
            return int(parsed["seq"])
    return 0


def save_cursor_seq(state_store: Store, name: str, seq: int) -> None:
    state_store.write(f"{name}.cursor.json", encode({"seq": seq}))


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


def pending_events(store: Store, name: str, rules: tuple[EventMatch, ...]) -> tuple[list[dict[str, Any]], int]:
    """Return `(matching_events, latest_seq)` since `name`'s saved cursor."""
    if not rules:
        return [], 0
    last_seq = _cursor_seq(private_dir(store, ".bf", "state"), name)
    try:
        raw = decode(store.read("logs/events.json", 16 << 20), "logs/events.json")
    except FileNotFoundError:
        return [], last_seq
    if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
        return [], last_seq
    latest = int(raw.get("seq", last_seq))
    matched = [
        dict(ev)
        for ev in raw["events"]
        if isinstance(ev, dict) and int(ev.get("seq", 0)) > last_seq and any(_match_event(ev, rule) for rule in rules)
    ]
    return matched, max(latest, last_seq)


def _in_backoff(log_doc: Mapping[str, Any], cap: int, now: datetime) -> bool:
    fails = int(log_doc.get("consecutive_failures") or 0)
    if fails > 0 and (raw_end := str(log_doc.get("last_end") or "")):
        backoff = min(60 * (1 << min(fails - 1, 10)), cap)
        return (now - datetime.fromisoformat(raw_end)).total_seconds() < backoff
    return False


def is_schedule_due(job: JobSpec, log_doc: dict[str, Any], now: datetime) -> bool:
    """Whether `job`'s `every` or `at` schedule is due at `now`, respecting failure backoff."""
    if _in_backoff(log_doc, job.every or 3600, now):
        return False
    raw_last = str(log_doc.get("last_success") or "")
    last_dt = datetime.fromisoformat(raw_last) if raw_last else None
    if job.every is not None and (last_dt is None or (now - last_dt).total_seconds() >= job.every):
        return True
    if job.at:
        tz = ZoneInfo(job.timezone)
        local_now = now.astimezone(tz)
        if not job.days or _WEEKDAYS[local_now.weekday()] in job.days:
            hh, mm = (int(part) for part in job.at.split(":"))
            slot = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if local_now >= slot and (last_dt is None or last_dt.astimezone(tz) < slot):
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
    end_iso: str = "",
    trigger: str = "manual",
    input_rel: str = "",
    events: Sequence[dict[str, Any]] = (),
    stdin_text: str = "",
    cursor: str = "",
    unit_env: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Expand `{{...}}` placeholders, write `.bf/state/<name>.context.json`, and build clean `env`."""
    effective_end = end_iso or start_iso
    state_root, unit_state = private_dir(store, ".bf", "state"), private_dir(store, ".bf", "state", name)
    input_abs = ""
    if input_rel:
        relative(input_rel)
        if not store.exists(input_rel):
            raise Error(f"{name}: input file {input_rel} does not exist")
        input_abs = str(store.root / input_rel)
    ctx_path = f"{name}.context.json"
    ctx_doc = {
        "run": run_id,
        "kind": trigger,
        "job": name,
        "events": list(events),
        "stdin": stdin_text,
        "start": start_iso,
        "end": effective_end,
        "input": input_abs,
    }
    state_root.write(ctx_path, encode(ctx_doc), mode=0o600)
    home_str = os.environ.get("HOME", "")
    with suppress(RuntimeError):
        home_str = str(Path.home())
    mapping = {
        "{{brain}}": str(store.root),
        "{{home}}": home_str,
        "{{state}}": str(state_root.root),
        "{{run}}": run_id,
        "{{start}}": start_iso,
        "{{end}}": effective_end,
        "{{cursor}}": cursor,
        "{{input}}": input_abs,
        "{{context}}": str(state_root.root / ctx_path),
    }
    argv = [re.sub(r"\{\{[a-z]+\}\}", lambda m: mapping.get(m[0], m[0]), token) for token in base_argv]
    if argv and "/" in argv[0] and not Path(argv[0]).is_absolute() and store.exists(argv[0]):
        argv[0] = str(store.root.joinpath(*relative(argv[0])))
    env_map = {"{{brain}}": str(store.root), "{{home}}": home_str, "{{state}}": str(state_root.root)}
    keys = (*_ENV_ALLOWLIST, *(_GIT_HOOK_ENV if trigger == "hook" else ()))
    env = {key: val for key in keys if (val := os.environ.get(key))}
    for ek, ev in (unit_env or {}).items():
        env[ek] = re.sub(r"\{\{[a-z]+\}\}", lambda m: env_map.get(m[0], m[0]), ev)
    env.update(config.load_dotenv(store))
    env.update(
        {
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            "BF_BRAIN": str(store.root),
            "BF_STATE_DIR": str(unit_state.root),
            "BF_START": start_iso,
            "BF_END": effective_end,
            "BF_TRIGGER": trigger,
        }
    )
    return argv, env


def exec_argv(
    argv: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: int, stdin_bytes: bytes = b""
) -> tuple[int, bytes, str]:
    """Run literal `argv` in a dedicated process group with bounded timeout and clean teardown."""
    if not argv:
        raise Error("empty command argv")
    pipe = subprocess.PIPE
    with subprocess.Popen(  # noqa: S603
        list(argv), cwd=cwd, env=env, stdin=pipe, stdout=pipe, stderr=pipe, start_new_session=True
    ) as proc:
        try:
            out, err = proc.communicate(input=stdin_bytes, timeout=timeout)
            return proc.returncode, out, err.decode("utf-8", "replace").strip()
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=2)
            except OSError, subprocess.TimeoutExpired:
                with suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)
            return 124, b"", f"timed out after {timeout}s"


def _save_report_action(store: Store, name: str, out_bytes: bytes, keep: int, instant: datetime) -> str:
    day = instant.astimezone().date().isoformat()
    rel = f"actions/{day}_{name}-{os.urandom(4).hex()}/ACTION.md"
    store.write(rel, out_bytes)
    with suppress(Error):
        compiled = markdown.compile_note(rel, out_bytes, brain_name=config.load(store).name)
        store.write(markdown.compiled_note_path(rel), encode(compiled))
    pat = re.compile(rf"^actions/\d{{4}}-\d{{2}}-\d{{2}}_{re.escape(name)}-[0-9a-f]{{8}}/ACTION\.md$")
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


def _run_builtin(store: Store, builtin: str, now: datetime) -> tuple[int, dict[str, Any], str]:
    if builtin in {"build", "review"}:
        res = engine.build(store, now=now) if builtin == "build" else engine.review(store, now=now)
        ok = not res["problems"]
    else:
        res = engine.validate(store) if builtin == "validate" else query.evaluate(store)
        ok = bool(res["valid" if builtin == "validate" else "ok"])
    return (0 if ok else 1), res, "; ".join(res["problems"][:3])


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
    keep: int = 50,
    cursor_seq: int | None = None,
    emit_job_event: bool = False,
) -> None:
    """Persist run outcome in `logs/<name>.json`, advance cursor on success, and optionally emit `job.*`."""
    ended_iso, ok = now_utc(ended), code == 0
    status = "ok" if ok else "failed"
    with writer(store):
        latest = load_run_log(store, name, kind=kind)
        hist: dict[str, Any] = {
            "run": run_id,
            "status": status,
            "started": started_iso,
            "ended": ended_iso,
            "duration_ms": dur_ms,
            "exit_code": code,
        }
        if err_msg:
            hist["error"] = err_msg[:4096]
        if stderr:
            hist["stderr"] = stderr[:8192]
        if summary:
            hist["summary"] = summary
        last_ok = ended_iso if ok else str(latest.get("last_success") or "")
        consec = 0 if ok else int(latest.get("consecutive_failures", 0)) + 1
        runs, fails = int(latest.get("runs", 0)) + 1, int(latest.get("failures", 0)) + int(not ok)
        new_doc: dict[str, Any] = {
            "kind": kind,
            "name": name,
            "status": status,
            "updated": ended_iso,
            "last_start": started_iso,
            "last_end": ended_iso,
            "last_success": last_ok,
            "runs": runs,
            "failures": fails,
            "consecutive_failures": consec,
            "history": [*list(latest.get("history") or []), hist],
        }
        if latest.get("cursor"):
            new_doc["cursor"] = str(latest["cursor"])
        save_run_log(store, new_doc, keep=keep, locked=True)
        if ok and cursor_seq is not None:
            save_cursor_seq(private_dir(store, ".bf", "state"), name, cursor_seq)
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
    """Execute one `JobSpec`, optionally ingesting sensor stdout and recording `logs/<name>.json`."""
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    started_iso = now_utc(instant)
    run_id = f"{job.name}-{int(instant.timestamp())}-{os.urandom(3).hex()}"
    log_doc = load_run_log(store, job.name, kind="job")
    last_ok = str(log_doc.get("last_success") or "")
    base_dt = datetime.fromisoformat(last_ok) if last_ok else instant
    win_start = base_dt - timedelta(seconds=job.overlap if last_ok else job.lookback)
    if missing := check_requires(job.requires, f"jobs.{job.name}"):
        raise Error("; ".join(missing))
    if dry_run:
        return {"job": job.name, "run": run_id, "status": "dry_run", "trigger": trigger}

    t0 = time.monotonic()
    summary: dict[str, Any] = {}
    err_msg = err_txt = ""
    if job.builtin:
        code, summary, err_msg = _run_builtin(store, job.builtin, instant)
    else:
        cmd = [*list(job.run), *list(extra_args)]
        argv, env = prepare_unit(
            store,
            job.name,
            cmd,
            run_id,
            now_utc(win_start),
            end_iso=started_iso,
            input_rel=job.input,
            trigger=trigger,
            unit_env=job.env,
            cursor=str(log_doc.get("cursor") or ""),
            events=events,
            stdin_text=stdin_bytes.decode("utf-8", "replace"),
        )
        proc_stdin = stdin_bytes or (store.read(job.input, MAX_NOTE) if job.input else b"")
        code, out_bytes, err_txt = exec_argv(argv, cwd=store.root, env=env, timeout=job.timeout, stdin_bytes=proc_stdin)
        if code != 0:
            err_msg = (err_txt or out_bytes.decode("utf-8", "replace").strip() or f"exit code {code}")[-1024:]
        else:
            try:
                if job.ingest:
                    summary = records.ingest(
                        store, job.ingest, out_bytes, mode=job.mode, allow_removal=job.allow_removal, now=now
                    )
                elif job.emit == "action" and out_bytes.strip():
                    act_time = datetime.now(UTC) if now is None else instant
                    summary = {"action": _save_report_action(store, job.name, out_bytes, job.keep, act_time)}
                elif job.emit == "events":
                    ev_count = 0
                    for raw_ln in out_bytes.splitlines():
                        if stripped := raw_ln.strip():
                            if not isinstance(parsed := decode(stripped, f"jobs.{job.name}"), dict):
                                raise Error(f"jobs.{job.name}: emitted event must be a JSON object")
                            ev_kind = str(parsed.get("kind") or "job.event")
                            records.append_event(store, ev_kind, job.name, dict(parsed), dedupe=True, now=now)
                            ev_count += 1
                    summary = {"events": ev_count}
                txt = out_bytes.decode("utf-8", "replace").strip()
                if not job.ingest and job.emit != "events" and job.channel and txt and txt != "NO_REPLY":
                    post_message(store, txt, to=job.channel, run_id=run_id, now=now)
            except Error as error:
                code, err_msg = 1, str(error)

    ended = datetime.now(UTC) if now is None else instant
    dur_ms = max(0, int((time.monotonic() - t0) * 1000))
    record_run(
        store,
        "job",
        job.name,
        run_id,
        started_iso,
        ended=ended,
        dur_ms=dur_ms,
        code=code,
        err_msg=err_msg,
        stderr=err_txt,
        summary=summary,
        keep=50 if job.emit == "action" else job.keep,
        cursor_seq=cursor_target_seq,
        emit_job_event=True,
    )
    out_res: dict[str, Any] = {
        "job": job.name,
        "run": run_id,
        "status": "ok" if code == 0 else "failed",
        "exit_code": code,
        "duration_ms": dur_ms,
    }
    if err_msg:
        out_res["error"] = err_msg
    if summary:
        out_res["summary"] = summary
    return out_res


def run_jobs(
    store: Store,
    names: Sequence[str] = (),
    *,
    due_only: bool = False,
    hook: str = "",
    extra_args: Sequence[str] = (),
    stdin_bytes: bytes = b"",
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run selected, due, or Git-hook jobs in declaration order."""
    cfg = config.load(store)
    sched_now = datetime.now(UTC) if now is None else now.astimezone(UTC)
    if hook and hook not in {"pre-commit", "pre-push"}:
        raise InputError("hook must be pre-commit or pre-push")
    for wanted in names:
        if wanted not in cfg.jobs:
            raise InputError(f"unknown job {wanted!r} in bf.yaml")

    selected: list[tuple[JobSpec, str, list[dict[str, Any]], int | None]] = []
    advance_cursors: list[tuple[str, int]] = []
    for name, job in cfg.jobs.items():
        if (names and name not in names) or (not names and not job.enabled):
            continue
        if hook:
            if hook in job.hooks:
                selected.append((job, "hook", [], None))
            continue
        if not names and job.hooks and not (job.every or job.at or job.events):
            continue
        if due_only:
            log_doc = load_run_log(store, name, kind="job")
            if _in_backoff(log_doc, job.every or 3600, sched_now):
                continue
            matched_evs, latest_seq = pending_events(store, name, job.events)
            target_seq = latest_seq if job.events and latest_seq else None
            if matched_evs:
                selected.append((job, "event", matched_evs, target_seq))
            elif is_schedule_due(job, log_doc, sched_now):
                selected.append((job, "schedule", [], target_seq))
            elif target_seq is not None:
                advance_cursors.append((name, target_seq))
        elif not names and job.events and not (job.every or job.at):
            continue
        else:
            _, latest_seq = pending_events(store, name, job.events)
            selected.append((job, "manual", [], latest_seq if job.events else None))

    if advance_cursors and not dry_run:
        with writer(store):
            state_root = private_dir(store, ".bf", "state")
            for cursor_name, cursor_seq in advance_cursors:
                save_cursor_seq(state_root, cursor_name, cursor_seq)

    results = [
        execute_one_job(
            store,
            j,
            trigger=t,
            events=e,
            cursor_target_seq=s,
            extra_args=extra_args,
            stdin_bytes=stdin_bytes,
            dry_run=dry_run,
            now=now,
        )
        for j, t, e, s in selected
    ]
    ok = all(r["status"] in {"ok", "dry_run"} for r in results)
    return {"brain": cfg.name, "ok": ok, "ran": len(results), "results": results}


def post_message(
    store: Store,
    text: str,
    *,
    to: str = "",
    thread: str = "",
    reply_to: str = "",
    run_id: str = "",
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Send an outbound message through a `channels.<name>.send` command."""
    cfg = config.load(store)
    if not (cleaned_text := text.strip()):
        raise InputError("message text must be nonempty")
    sendable = [n for n, c in cfg.channels.items() if c.enabled and c.send]
    if not (target_name := (to or (sendable[0] if len(sendable) == 1 else ""))):
        raise InputError("specify --to CHANNEL (required unless exactly one enabled channel defines send)")
    ch = cfg.channels.get(target_name)
    if ch is None or not ch.send:
        raise Error(f"channel {target_name!r} has no send command configured in bf.yaml")
    if missing := check_requires(ch.requires, f"channels.{target_name}"):
        raise Error("; ".join(missing))

    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    msg_id = f"post-{int(instant.timestamp())}-{os.urandom(3).hex()}"
    extra_meta = (("conversation", thread, "thread"), ("reply_to", reply_to, "reply_to"), ("run", run_id, "run"))
    payload: dict[str, Any] = {"id": msg_id, "text": cleaned_text, **{k: clean(v, f) for k, v, f in extra_meta if v}}
    if dry_run:
        return {"brain": cfg.name, "channel": target_name, "status": "dry_run", "message": payload}

    argv, env = prepare_unit(
        store, target_name, ch.send, run_id or msg_id, now_utc(instant), trigger="post", unit_env=ch.env
    )
    code, out_bytes, err_txt = exec_argv(argv, cwd=store.root, env=env, timeout=ch.timeout, stdin_bytes=encode(payload))
    if code != 0:
        raise Error(f"channels.{target_name} send failed: {err_txt or f'exit code {code}'}")
    reply_obj: dict[str, Any] = {}
    if out_bytes.strip() and isinstance(dec := decode(out_bytes.strip(), f"channels.{target_name} send output"), dict):
        reply_obj = dict(dec)
    ev_data = {"id": str(reply_obj.get("id") or msg_id), "conversation": str(reply_obj.get("conversation") or thread)}
    records.append_event(store, "channel.sent", target_name, ev_data, now=instant)
    return {"brain": cfg.name, "channel": target_name, "status": "ok", "reply": reply_obj or {"id": msg_id}}


def _collect_unit_status(
    store: Store,
    units: Mapping[str, JobSpec | ChannelSpec | ServerSpec],
    kind: str,
    *,
    check: bool,
    problems: list[str],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, unit in units.items():
        doc = load_run_log(store, name, kind=kind)
        if check and unit.enabled:
            problems.extend(check_requires(unit.requires, f"{kind}s.{name}"))
        if doc.get("status") == "failed" and unit.enabled:
            problems.append(f"{kind}s.{name}: last run failed")
        info: dict[str, Any] = {"enabled": unit.enabled, "status": doc.get("status", "idle")}
        info.update(
            {"last_success": doc.get("last_success", ""), "consecutive_failures": doc.get("consecutive_failures", 0)}
        )
        if isinstance(unit, JobSpec) and unit.ingest:
            info["mode"] = unit.mode
        hist = doc.get("history")
        if isinstance(hist, list) and hist and isinstance(last := hist[-1], dict):
            if isinstance(last.get("summary"), dict):
                info["last_run"] = last["summary"]
            if last.get("error"):
                info["error"] = last["error"]
        out[name] = info
    return out


def status_summary(store: Store, *, check: bool = False) -> dict[str, Any]:
    """Return current status of configured `jobs`, `channels`, `servers`, and `logs/events.json`."""
    cfg = config.load(store)
    problems: list[str] = []
    jobs_out = _collect_unit_status(store, cfg.jobs, "job", check=check, problems=problems)
    channels_out = _collect_unit_status(store, cfg.channels, "channel", check=check, problems=problems)
    servers_out = _collect_unit_status(store, cfg.servers, "server", check=check, problems=problems)
    events_seq = 0
    with suppress(FileNotFoundError):
        if isinstance(ev_doc := decode(store.read("logs/events.json", 16 << 20), "logs/events.json"), dict):
            events_seq = int(ev_doc.get("seq", 0))
    res = {"brain": cfg.name, "ok": not problems, "inside_brain": config.inside_brain() is not None}
    return {
        **res,
        "events_seq": events_seq,
        "jobs": jobs_out,
        "channels": channels_out,
        "servers": servers_out,
        "problems": problems,
    }
