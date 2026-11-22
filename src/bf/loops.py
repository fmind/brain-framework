"""Orchestration of unified `loops:` (`sensors/`, `routines/`, `agents/`), `.bf/<loop>.json`, and `logs/<loop>.jsonl`."""

from __future__ import annotations

import fnmatch
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from bf import config, engine, models, proc, records
from bf.config import Config, EventMatch, LoopSpec, ScheduleSpec, due_schedule
from bf.models import Error, InputError, decode, encode, now_utc
from bf.proc import prepare_unit
from bf.records import emit_output_events, find_orphans, load_unit_state, mark_unit_state, save_unit_state
from bf.storage import Store, private_dir, writer

_GLOB_CHARS = frozenset("*?[")
_LOOP_DIRS = ("sensors", "routines", "agents")
_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "fish", "python", "python3", "uv"})
_AGENT_DEFAULT_TIMEOUT = 900

type _Events = tuple[list[dict[str, Any]], int]
type _DueItem = tuple[LoopSpec, str, list[dict[str, Any]], int | None, ScheduleSpec | None]


def _script_folder(argv: Sequence[str]) -> str:
    """Detect convention folder (`sensors`, `routines`, `agents`) from direct or interpreter-prefixed argv."""
    if not argv:
        return ""
    first = argv[0].removeprefix("./")
    if hit := next((f for f in _LOOP_DIRS if first.startswith(f"{f}/")), ""):
        return hit
    exe = Path(first).name
    if exe in _INTERPRETERS or exe.startswith("python3."):
        for tok in argv[1:]:
            if tok.startswith("-") or (exe == "uv" and tok == "run"):
                continue
            cleaned = tok.removeprefix("{{brain}}/").removeprefix("./")
            return next((f for f in _LOOP_DIRS if cleaned.startswith(f"{f}/")), "")
    return ""


def resolve_loop(
    store: Store, loop: LoopSpec, reserved: frozenset[str] = models.RESERVED_SOURCES
) -> tuple[tuple[str, ...], str, records.Target, Literal["upsert", "snapshot"], int]:
    """Resolve `(argv, ingest_source, ingest_target, ingest_mode, timeout)` via explicit `run:` or folder conventions."""
    if loop.run:
        argv, folder = loop.run, _script_folder(loop.run)
    else:
        found: list[tuple[str, str, os.stat_result]] = []
        for fld in _LOOP_DIRS:
            for ext in ("", ".sh", ".py"):
                cand = f"{fld}/{loop.name}{ext}"
                try:
                    info = store.stat(cand)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise Error(f"loops.{loop.name}: {cand}: expected a regular file")
                found.append((fld, cand, info))
        if not found:
            raise Error(f"loops.{loop.name}: no run command and no script found in sensors/, routines/, or agents/")
        if len(found) > 1:
            names = ", ".join(c for _, c, _ in found)
            raise Error(f"loops.{loop.name}: ambiguous convention scripts ({names}); keep one or set run:")
        folder, cand, info = found[0]
        if info.st_mode & stat.S_IXUSR:
            argv = (cand,)
        elif cand.endswith(".sh"):
            argv = ("bash", cand)
        elif cand.endswith(".py"):
            argv = (sys.executable, cand)
        else:
            raise Error(f"loops.{loop.name}: {cand} is not executable; run chmod +x {cand}")

    timeout = (
        max(_AGENT_DEFAULT_TIMEOUT, loop.timeout)
        if (folder == "agents" and not loop.explicit_timeout)
        else loop.timeout
    )
    if loop.explicit_ingest:
        return argv, loop.ingest, loop.target, loop.mode, timeout
    if folder == "sensors" and not loop.daemon and not loop.emit:
        if loop.name in reserved:
            raise Error(f"loops.{loop.name}: sensor name {loop.name!r} is reserved")
        return argv, loop.name, "memories", "upsert", timeout
    return argv, "", "memories", "upsert", timeout


def effective_ingest(
    store: Store, loop: LoopSpec, reserved: frozenset[str] = models.RESERVED_SOURCES
) -> tuple[str, records.Target, str]:
    """Return `(ingest_source, ingest_target, ingest_mode)` for status display without raising on missing scripts."""
    if loop.explicit_ingest:
        return loop.ingest, loop.target, loop.mode
    if not loop.daemon and not loop.emit:
        with suppress(Error):
            _, src, tgt, mode, _ = resolve_loop(store, loop, reserved)
            return src, tgt, mode
    return "", "memories", "upsert"


def _rule_head_matches(ev: Mapping[str, Any], rule: EventMatch, *, self_name: str = "") -> bool:
    if (
        self_name
        and not rule.source
        and ev.get("source") == self_name
        and ev.get("kind") in {"loop.succeeded", "loop.failed"}
    ):
        return False
    return (not rule.kind or ev.get("kind") == rule.kind) and (not rule.source or ev.get("source") == rule.source)


def pending_events(
    store: Store,
    last_seq: int,
    rules: tuple[EventMatch, ...],
    *,
    self_name: str = "",
    events_data: _Events | None = None,
) -> _Events:
    """Return `(matching_events, latest_seq)` since `last_seq` from `logs/events.jsonl`."""
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
    matched_idx = {
        i for i, ev in enumerate(cands) if any(_rule_head_matches(ev, r, self_name=self_name) for r in plain_rules)
    }
    groups: dict[str, list[EventMatch]] = {}
    for rule in rules:
        if rule.filter:
            groups.setdefault(rule.filter, []).append(rule)
    jq_bin: str | None = None
    for flt, flt_rules in groups.items():
        unmatched = (i for i in range(len(cands)) if i not in matched_idx)
        if not (
            sub_idx := [
                i for i in unmatched if any(_rule_head_matches(cands[i], r, self_name=self_name) for r in flt_rules)
            ]
        ):
            continue
        jq_bin = jq_bin or proc.resolve_tool("jq")
        wrapped = (
            f"{proc.JQ_PRELUDE}\ntry ([\n{flt}\n] | "
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
    schedule_key: str = "",
) -> None:
    """Update `.bf/<name>.json`, append a summary line to `logs/<name>.jsonl`, and optionally emit `loop.*`."""
    cfg_name, ended_iso, ok = config.load(store).name, now_utc(ended), code == 0
    with writer(store):
        latest: dict[str, Any]
        try:
            latest = load_unit_state(store, name)
        except Error:
            latest = {"kind": "loop", "name": name, "schedule_last": {}, "last_run": {}}
        last_ok = started_iso if ok else str(latest.get("last_success") or "")
        last_sched = started_iso if ok and trigger == "schedule" else str(latest.get("last_scheduled") or "")
        sched_last = dict(latest.get("schedule_last") or {}) if isinstance(latest.get("schedule_last"), dict) else {}
        if ok and schedule_key:
            sched_last[schedule_key] = started_iso
        consec = 0 if ok else int(latest.get("consecutive_failures") or 0) + 1
        runs, fails = int(latest.get("runs") or 0) + 1, int(latest.get("failures") or 0) + int(not ok)
        stored_seq = int(latest.get("event_seq") or 0)
        disk_seq = records.latest_event_seq(store) if ok and cursor_seq is not None else 0
        seq = _merge_cursor(stored_seq, cursor_seq, disk_seq) if ok and cursor_seq is not None else stored_seq
        raw_err = err_msg if ok else (err_msg.strip() or stderr.strip() or f"exit code {code}")
        err_clean = models.redact_secrets(raw_err)[-4096:]
        err_clean = err_clean if (ok or err_clean.strip()) else f"exit code {code}"
        stderr_clean = models.redact_secrets(stderr)[-8192:]
        safe_sum = models.redact_value(dict(summary)) if isinstance(summary, dict) and summary else {}
        doc = {
            "kind": "loop",
            "name": name,
            "status": "ok" if ok else "failed",
            "updated": ended_iso,
            "last_start": started_iso,
            "last_end": ended_iso,
            "last_success": last_ok,
            "last_scheduled": last_sched,
            "schedule_last": sched_last,
            "event_seq": seq,
            "runs": runs,
            "failures": fails,
            "consecutive_failures": consec,
            "duration_ms": dur_ms,
            "last_run": safe_sum,
            "error": err_clean,
        }
        save_unit_state(store, doc, locked=True)
        extras = {k: v for k, v in (("summary", safe_sum), ("error", err_clean), ("stderr", stderr_clean)) if v}
        msg, sev = f"loop.{'succeeded' if ok else 'failed'}", "INFO" if ok else "ERROR"
        log_meta = {
            "brain": cfg_name,
            "loop": name,
            "run": run_id,
            "trigger": trigger or "loop",
            "started": started_iso,
            "ended": ended_iso,
            "duration_ms": dur_ms,
            "exit_code": code,
            **extras,
        }
        store.append_log(name, models.log_line(msg, severity=sev, time_iso=ended_iso, **log_meta))
        if emit_loop_event:
            ev_data = {"run": run_id, "exit_code": code, "duration_ms": dur_ms}
            records.append_event(store, msg, name, ev_data, locked=True, now=ended)


def _bf_error_summary(run_argv: Sequence[str], out_bytes: bytes) -> str:
    if len(run_argv) < 2 or run_argv[0] != "bf" or not out_bytes.strip():
        return ""
    with suppress(Error):
        if (
            isinstance(res := decode(out_bytes, "bf output", check_duplicates=False), dict)
            and not res.get("ok")
            and isinstance(probs := res.get("problems"), list)
            and probs
        ):
            errs = [str(p) for p in probs if ": warning: " not in str(p)] or [str(p) for p in probs]
            return "; ".join(errs[:3])
    return ""


def _last_error_line(err_txt: str) -> str:
    for line in reversed(err_txt.splitlines()):
        if not (stripped := line.strip()):
            continue
        if stripped.startswith("{"):
            with suppress(Error):
                parsed = decode(stripped, "stderr", check_duplicates=False)
                if isinstance(parsed, dict) and (
                    msg := parsed.get("message") or parsed.get("event") or parsed.get("error")
                ):
                    return str(msg)
        return stripped
    return ""


def execute_one_loop(
    store: Store,
    loop: LoopSpec,
    *,
    trigger: str,
    events: Sequence[dict[str, Any]] = (),
    cursor_target_seq: int | None = None,
    schedule: ScheduleSpec | None = None,
    extra_args: Sequence[str] = (),
    dry_run: bool = False,
    now: datetime | None = None,
    stop_event: threading.Event | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Execute one triggered `LoopSpec` with `BF_*` env + stdin JSON, structured stderr logs, and `ingest`/`emit`."""
    cfg = config.load(store)
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    started_iso, run_id = now_utc(instant), f"{loop.name}-{int(instant.timestamp())}-{os.urandom(3).hex()}"
    try:
        state_doc = load_unit_state(store, loop.name)
    except Error:
        state_doc = {"schedule_last": {}, "last_run": {}}
    last_ok = (
        config.sched_anchor(state_doc, schedule) if trigger == "schedule" else str(state_doc.get("last_success") or "")
    )
    last_dt = config.parse_dt(last_ok)
    backfill = schedule.backfill if (schedule and schedule.backfill is not None) else loop.backfill
    overlap = schedule.overlap if (schedule and schedule.overlap is not None) else loop.overlap
    win_start = min(
        (last_dt or instant) - timedelta(seconds=overlap if last_dt else backfill), instant - timedelta(seconds=1)
    )
    resolved_run, ingest_src, ingest_target, ingest_mode, timeout = resolve_loop(store, loop, cfg.reserved_sources)
    if ingest_src:
        records.require_ingest_validators(store, ingest_src, ingest_target)

    b_env = cfg.env if base_env is None else base_env
    argv, env, ctx_bytes = prepare_unit(
        store,
        loop.name,
        resolved_run,
        run_id,
        now_utc(win_start),
        end_iso=started_iso,
        trigger=trigger,
        events=events,
        base_env=b_env,
        unit_env=loop.env,
    )
    if dry_run:
        ing = {"ingest": {"source": ingest_src, "target": ingest_target, "mode": ingest_mode}} if ingest_src else {}
        return {
            "loop": loop.name,
            "run": run_id,
            "status": "dry_run",
            "trigger": trigger,
            "argv": [*argv, *extra_args],
            **ing,
        }

    t0, summary, err_msg, err_txt = time.monotonic(), {}, "", ""
    argv, recorded = [*argv, *extra_args], False
    mark_unit_state(store, loop.name, status="running", updated=started_iso, last_start=started_iso)
    try:
        code, out_bytes, err_txt = proc.exec_argv(
            argv,
            cwd=store.root,
            env=env,
            timeout=timeout,
            stdin_data=ctx_bytes,
            stop_event=stop_event,
            on_stderr_line=records.make_stderr_logger(store, loop.name, run_id),
        )
        if stop_event is not None and stop_event.is_set() and code == 130:
            dur_ms = max(0, int((time.monotonic() - t0) * 1000))
            return {"loop": loop.name, "run": run_id, "status": "stopped", "exit_code": 130, "duration_ms": dur_ms}
        if code != 0:
            safe_err = models.redact_secrets(err_txt)
            raw_fail = _last_error_line(safe_err) or _bf_error_summary(resolved_run, out_bytes) or f"exit code {code}"
            err_msg = (models.redact_secrets(raw_fail).strip() or f"exit code {code}")[-1024:]
        else:
            try:
                if ingest_src:
                    summary = records.ingest(
                        store, ingest_src, out_bytes, target=ingest_target, mode=ingest_mode, now=now
                    )
                elif loop.emit and out_bytes.strip():
                    summary = {"emitted": emit_output_events(store, loop.name, loop.emit, out_bytes, now=now)}
                elif len(out_bytes) <= 65536 and out_bytes.decode("utf-8", "replace").strip().startswith("{"):
                    with suppress(Error):
                        if isinstance(dec := decode(out_bytes, f"loops.{loop.name}"), dict):
                            summary = rv if isinstance(rv := models.redact_value(dict(dec)), dict) else {}
            except models.FAILURES as error:
                code, err_msg = 1, (models.redact_secrets(models.failure(error) or "failed")[-1024:] or "failed")

        ended, dur_ms = (datetime.now(UTC) if now is None else instant), max(0, int((time.monotonic() - t0) * 1000))
        record_run(
            store,
            loop.name,
            run_id,
            started_iso,
            ended=ended,
            dur_ms=dur_ms,
            code=code,
            err_msg=err_msg,
            stderr=err_txt,
            summary=summary,
            cursor_seq=cursor_target_seq,
            emit_loop_event=True,
            trigger=trigger,
            schedule_key=schedule.raw if schedule else "",
        )
        recorded = True
    finally:
        if not recorded:
            with suppress(Error, OSError):
                t_end = now_utc(None if now is None else instant)
                mark_unit_state(store, loop.name, status="stopped", updated=t_end, last_end=t_end)
    res = {
        "loop": loop.name,
        "run": run_id,
        "status": "ok" if code == 0 else "failed",
        "exit_code": code,
        "duration_ms": dur_ms,
    }
    return {**res, **({"error": err_msg} if err_msg else {}), **({"summary": summary} if summary else {})}


def record_failure(
    store: Store, name: str, trigger: str, err: BaseException, *, now: datetime | None = None
) -> dict[str, Any]:
    """Record a pre-flight or event-filter failure for loop `name` and return its result row."""
    msg = (models.redact_secrets(models.failure(err)) or "failed")[-1024:]
    end_dt = datetime.now(UTC) if now is None else now.astimezone(UTC)
    pre_id = f"{name}-{int(end_dt.timestamp())}-{os.urandom(3).hex()}"
    with suppress(Error, OSError):
        record_run(
            store,
            name,
            pre_id,
            now_utc(end_dt),
            ended=end_dt,
            dur_ms=0,
            code=1,
            err_msg=msg,
            emit_loop_event=True,
            trigger=trigger,
        )
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
    selected: list[_DueItem] = []
    advance_cursors: list[tuple[str, int]] = []
    failures: list[dict[str, Any]] = []

    for name, loop in cfg.loops.items():
        if not loop.enabled or loop.daemon or name in exclude or not (loop.schedules or loop.events):
            continue
        try:
            state_doc = load_unit_state(store, name)
        except Error as err:
            if dry_run:
                raise
            failures.append(record_failure(store, name, "state", err, now=now))
            continue
        if config.backoff_remaining(state_doc, config.backoff_cap(loop), sched_now) > 0:
            continue
        cur_seq, matched_evs, target_seq = int(state_doc.get("event_seq") or 0), [], None
        if loop.events:
            latest_disk_seq = records.latest_event_seq(store) if latest_disk_seq is None else latest_disk_seq
            target_seq = latest_disk_seq
            if cur_seq != latest_disk_seq and latest_disk_seq > 0:
                cached_events = records.read_events(store) if cached_events is None else cached_events
                try:
                    matched_evs, target_seq = pending_events(
                        store, cur_seq, loop.events, self_name=name, events_data=cached_events
                    )
                except models.FAILURES as err:
                    if dry_run:
                        raise
                    failures.append(record_failure(store, name, "event", err, now=now))
                    continue
        if matched_evs:
            selected.append((loop, "event", matched_evs, target_seq, None))
        elif (matched_sched := due_schedule(loop, state_doc, sched_now, tz_name=cfg.timezone)) is not None:
            selected.append((loop, "schedule", [], target_seq, matched_sched))
        elif target_seq is not None and target_seq != cur_seq:
            advance_cursors.append((name, target_seq))

    if advance_cursors and not dry_run:
        with writer(store):
            disk_seq = records.latest_event_seq(store)
            for cursor_name, cursor_seq in advance_cursors:
                with suppress(Error):
                    latest = load_unit_state(store, cursor_name)
                    stored = int(latest.get("event_seq") or 0)
                    if (nxt := _merge_cursor(stored, cursor_seq, disk_seq)) != stored:
                        save_unit_state(store, {**latest, "event_seq": nxt}, locked=True)
    return selected, failures


def _expand_loop_selectors(cfg: Config, selectors: Sequence[str]) -> list[str]:
    resolved: list[str] = []
    for token in selectors:
        if any(ch in _GLOB_CHARS for ch in token):
            matches = [
                n for n, lp in cfg.loops.items() if lp.enabled and not lp.daemon and fnmatch.fnmatchcase(n, token)
            ]
            if not matches:
                raise InputError(f"pattern {token!r} matched no enabled cycle loops in bf.yaml")
            resolved.extend(matches)
        else:
            if token not in cfg.loops:
                raise InputError(f"unknown loop {token!r} in bf.yaml")
            if cfg.loops[token].daemon:
                raise InputError(f"loop {token!r} is a daemon; run it with bf watch")
            resolved.append(token)
    return list(dict.fromkeys(resolved))


def run_loops(
    store: Store,
    names: Sequence[str] = (),
    *,
    due_only: bool = False,
    all_loops: bool = False,
    extra_args: Sequence[str] = (),
    dry_run: bool = False,
    now: datetime | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run selected, glob-matched, `--all`, or `--due` loops (concurrently up to `cfg.concurrency` for `--due`)."""
    cfg, sched_now = config.load(store), datetime.now(UTC) if now is None else now.astimezone(UTC)
    if bool(names) + bool(due_only) + bool(all_loops) != 1:
        raise InputError("specify loop names/patterns, --due, or --all (mutually exclusive)")
    if extra_args and (due_only or all_loops):
        raise InputError("passthrough -- arguments require a single loop")

    built = engine.build(store, now=now) if due_only and not dry_run else None
    results: list[dict[str, Any]] = []
    if due_only:
        selected, pre_fails = select_due_loops(store, cfg, sched_now=sched_now, dry_run=dry_run, now=now)
        results.extend(pre_fails)
    elif all_loops:
        selected = [(lp, "manual", [], None, None) for lp in cfg.loops.values() if lp.enabled and not lp.daemon]
    else:
        matched_names = _expand_loop_selectors(cfg, names)
        if extra_args and len(matched_names) > 1:
            raise InputError("passthrough -- arguments require a single loop")
        selected = [(cfg.loops[n], "manual", [], None, None) for n in matched_names]

    def run_item(item: _DueItem, *, stop: threading.Event | None = stop_event) -> dict[str, Any]:
        loop, trig, evs, seq_target, sched = item
        if stop is not None and stop.is_set():
            return {"loop": loop.name, "status": "stopped", "exit_code": 130, "duration_ms": 0}
        try:
            return execute_one_loop(
                store,
                loop,
                trigger=trig,
                events=evs,
                cursor_target_seq=seq_target,
                schedule=sched,
                extra_args=extra_args,
                dry_run=dry_run,
                now=now,
                stop_event=stop,
                base_env=cfg.env,
            )
        except models.FAILURES as err:
            if not due_only or dry_run:
                raise
            return record_failure(store, loop.name, trig, err, now=now)

    if due_only and not dry_run and len(selected) > 1 and cfg.concurrency > 1:
        local_stop = stop_event or threading.Event()
        pool = ThreadPoolExecutor(max_workers=min(cfg.concurrency, len(selected)))
        try:
            results.extend(f.result() for f in [pool.submit(run_item, it, stop=local_stop) for it in selected])
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
    out: dict[str, Any] = {"brain": cfg.name, "ok": ok_all, "ran": len(results), "results": results}
    return {**out, "problems": list(built["problems"])} if built and built["problems"] else out


def _delete_states(store: Store, names: Sequence[str], *, dry_run: bool) -> list[str]:
    unique = list(dict.fromkeys(names))
    if dry_run or not unique:
        with suppress(FileNotFoundError):
            bf_dir = private_dir(store, create=False)
            return [name for name in unique if bf_dir.exists(f"{name}.json")]
        return []
    removed: list[str] = []
    with writer(store), suppress(FileNotFoundError):
        bf_dir = private_dir(store, create=False)
        for name in unique:
            if bf_dir.exists(f"{name}.json"):
                bf_dir.delete(f"{name}.json", durable=False)
                removed.append(name)
        if removed:
            store.sync_dirs([".bf"])
    return removed


def reset_loops(store: Store, names: Sequence[str], *, dry_run: bool = False) -> dict[str, Any]:
    """Reset `.bf/<loop>.json` state for named loops back to idle."""
    cfg = config.load(store)
    if not names:
        raise InputError("--reset requires one or more loop names")
    if bad := next((w for w in names if w not in cfg.loops), ""):
        raise InputError(f"unknown loop {bad!r} in bf.yaml")
    return {"ok": True, "brain": cfg.name, "reset": _delete_states(store, names, dry_run=dry_run), "dry_run": dry_run}


def prune_state(store: Store, *, dry_run: bool = False) -> dict[str, Any]:
    """Delete orphaned `.bf/<loop>.json` state files."""
    cfg = config.load(store)
    orphans = find_orphans(store, set(cfg.loops))
    pruned = orphans if (dry_run or not orphans) else sorted(_delete_states(store, orphans, dry_run=False))
    return {"ok": True, "brain": cfg.name, "pruned": pruned, "dry_run": dry_run}
