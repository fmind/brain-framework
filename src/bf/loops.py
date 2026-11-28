"""Orchestration of unified `loops:` with pure-Python event matching, per-loop locks, dead-letter caps, and state maintenance."""

from __future__ import annotations

import fnmatch
import os
import threading
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from bf import config, cron, models, proc, records, state
from bf.config import Config, EventMatch, LoopSpec, resolve_loop
from bf.cron import ScheduleSpec, due_schedules
from bf.models import BusyError, Error, InputError, decode, now_utc
from bf.proc import StopFlag, prepare_unit
from bf.state import (
    emit_output_events,
    find_orphans,
    latest_event_seq,
    load_unit_state,
    mark_unit_state,
    save_unit_state,
)
from bf.storage import Store, is_loop_locked, loop_lock, private_dir, try_unlink_loop_lock, writer

_GLOB_CHARS = frozenset("*?[")
MAX_EVENT_RETRIES = 3

type _Events = tuple[list[dict[str, Any]], int, int]
type _DueItem = tuple[LoopSpec, str, list[dict[str, Any]], int | None, tuple[ScheduleSpec, ...], int]


@dataclass(frozen=True)
class DueSnapshot:
    """Reusable state/event snapshot loaded during `select_due_with_snapshot` to avoid duplicate `bf watch` disk scans."""

    states: dict[str, dict[str, Any]]
    latest_seq: int | None
    events: _Events | None
    cached_floor: int = 0


def _resolve_dot_path(obj: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    cur: Any = obj
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _scalar_eq(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def match_event_where(ev: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
    """Evaluate declarative dot-path predicates (`exact`, `{in}`, `{exists}`, `{prefix}`) against one event."""
    for path_key, pred in where.items():
        found, val = _resolve_dot_path(ev, path_key)
        if isinstance(pred, Mapping):
            if "exists" in pred:
                if bool(pred["exists"]) != found:
                    return False
            elif "in" in pred:
                if not found or not any(_scalar_eq(val, cand) for cand in pred["in"]):
                    return False
            elif "prefix" in pred and (
                not found or not isinstance(val, str) or not val.startswith(str(pred["prefix"]))
            ):
                return False
        else:
            if not found or not _scalar_eq(val, pred):
                return False
    return True


def _rule_matches(ev: Mapping[str, Any], rule: EventMatch, *, self_name: str = "") -> bool:
    if (
        self_name
        and not rule.source
        and ev.get("source") == self_name
        and ev.get("kind") in {"loop.succeeded", "loop.failed"}
    ):
        return False
    if rule.kind and ev.get("kind") != rule.kind:
        return False
    if rule.source and ev.get("source") != rule.source:
        return False
    return not rule.where or match_event_where(ev, rule.where)


def event_debounce_remaining(
    matched_evs: Sequence[Mapping[str, Any]],
    rules: Sequence[EventMatch],
    now: datetime,
    *,
    self_name: str = "",
) -> float:
    """Return earliest remaining trailing-edge debounce seconds across per-debounce groups (`0.0` when any group is ready)."""
    if not matched_evs or not rules:
        return 0.0
    by_debounce: dict[int, list[datetime]] = {}
    for ev in matched_evs:
        matching_rules = [r for r in rules if _rule_matches(ev, r, self_name=self_name)]
        if not matching_rules:
            continue
        deb = min(r.debounce for r in matching_rules)
        if deb <= 0:
            return 0.0
        if (dt := cron.parse_dt(str(ev.get("time") or ""))) is not None:
            by_debounce.setdefault(deb, []).append(dt.astimezone(UTC))
    if not by_debounce:
        return 0.0
    utc_now = now.astimezone(UTC)
    waits: list[float] = []
    for deb, times in by_debounce.items():
        first_dt, last_dt = min(times), max(times)
        trailing = last_dt + timedelta(seconds=deb)
        max_wait = first_dt + timedelta(seconds=min(deb * 4, 3600))
        ready_at = min(trailing, max_wait)
        waits.append(min(float(deb), max(0.0, (ready_at - utc_now).total_seconds())))
    return min(waits)


def pending_events(
    store: Store,
    last_seq: int,
    rules: Sequence[EventMatch],
    *,
    self_name: str = "",
    events_data: _Events | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return `(matching_events, latest_seq)` since `last_seq` from `.bf/state/events.jsonl` in pure Python."""
    if not rules:
        return [], 0
    if events_data is not None:
        all_evs, latest, _ = events_data
        floor = 0 if (latest and last_seq > latest) else last_seq
        cands = [ev for ev in all_evs if int(ev["seq"]) > floor]
    else:
        cands, latest = state.read_events(store, after_seq=last_seq)
    if not cands:
        return [], latest
    matched = [ev for ev in cands if any(_rule_matches(ev, rule, self_name=self_name) for rule in rules)]
    return matched, latest


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
    summary: Mapping[str, Any] | None = None,
    cursor_seq: int | None = None,
    events_missed: int = 0,
    emit_loop_event: bool = False,
    trigger: str = "",
    schedule_keys: Sequence[str] = (),
    secrets: Sequence[str] = (),
) -> None:
    """Update `.bf/state/<name>.json`, hold watermarks on `incomplete`, append `logs/<name>.jsonl`, and emit `loop.*`."""
    cfg_name, ended_iso, ok = config.load(store).name, now_utc(ended), code == 0
    is_incomplete = isinstance(summary, Mapping) and summary.get("incomplete") is True
    advance_watermark = ok and not is_incomplete
    dead_letter = False
    with writer(store):
        latest: dict[str, Any]
        try:
            latest = load_unit_state(store, name)
        except Error:
            latest = {"name": name, "schedule_last": {}, "last_run": {}, "event_seq": latest_event_seq(store)}
        last_ok = started_iso if advance_watermark else str(latest.get("last_success") or "")
        last_sched = (
            started_iso if advance_watermark and trigger == "schedule" else str(latest.get("last_scheduled") or "")
        )
        sched_last = dict(latest.get("schedule_last") or {}) if isinstance(latest.get("schedule_last"), dict) else {}
        if advance_watermark and schedule_keys:
            for sk in schedule_keys:
                sched_last[sk] = started_iso
        consec = 0 if ok else state.int_field(latest, "consecutive_failures") + 1
        prev_sf = (
            state.int_field(latest, "schedule_failures")
            if type(latest.get("schedule_failures")) is int
            else state.int_field(latest, "consecutive_failures")
        )
        prev_ef = state.int_field(latest, "event_failures")
        prev_incomp = state.int_field(latest, "incomplete_runs")
        if ok:
            sched_fails = prev_sf if trigger == "event" else 0
            ev_fails = prev_ef if trigger == "schedule" else 0
            incomp_runs = prev_incomp if trigger == "event" else ((prev_incomp + 1) if is_incomplete else 0)
        else:
            sched_fails = prev_sf if trigger == "event" else prev_sf + 1
            ev_fails = prev_ef + 1 if trigger == "event" else prev_ef
            incomp_runs = prev_incomp if trigger == "event" else 0
        runs = state.int_field(latest, "runs") + 1
        fails = state.int_field(latest, "failures") + int(not ok)
        stored_seq = state.int_field(latest, "event_seq")
        dead_lettered = state.int_field(latest, "dead_lettered")
        last_dl_seq = state.int_field(latest, "last_dead_letter_seq")
        advance_cursor = ok and cursor_seq is not None
        if not ok and trigger == "event" and cursor_seq is not None and ev_fails >= MAX_EVENT_RETRIES:
            advance_cursor = True
            dead_letter = True
            ev_fails = 0
            dead_lettered += 1
            last_dl_seq = cursor_seq
        prev_sched_end = str(latest.get("last_schedule_end") or "")
        prev_ev_end = str(latest.get("last_event_end") or "")
        if trigger == "event":
            last_sched_end = prev_sched_end
            last_ev_end = "" if (ok or dead_letter) else ended_iso
        elif trigger == "schedule":
            last_sched_end = "" if (ok and not is_incomplete) else ended_iso
            last_ev_end = prev_ev_end
        else:
            last_sched_end = "" if (ok and not is_incomplete) else ended_iso
            last_ev_end = "" if ok else prev_ev_end
        disk_seq = latest_event_seq(store) if advance_cursor else 0
        seq = (
            _merge_cursor(stored_seq, cursor_seq, disk_seq) if advance_cursor and cursor_seq is not None else stored_seq
        )
        pending_missed = (
            0 if (advance_cursor or (ok and events_missed > 0)) else state.int_field(latest, "pending_events_missed")
        )
        paused_active = state.is_paused(latest, ended)
        paused_until = str(latest.get("paused_until") or "") if paused_active else ""
        raw_err = err_msg if ok else (err_msg.strip() or stderr.strip() or f"exit code {code}")
        err_clean = models.redact_secrets(raw_err, secrets=secrets)[-4096:]
        err_clean = err_clean if (ok or err_clean.strip()) else f"exit code {code}"
        stderr_clean = models.redact_secrets(stderr, secrets=secrets)[-8192:]
        safe_sum = (
            models.redact_value(dict(summary), secrets=secrets) if isinstance(summary, Mapping) and summary else {}
        )
        doc = {
            "name": name,
            "status": "ok" if ok else "failed",
            "updated": ended_iso,
            "last_start": started_iso,
            "last_end": ended_iso,
            "last_schedule_end": last_sched_end,
            "last_event_end": last_ev_end,
            "last_success": last_ok,
            "last_scheduled": last_sched,
            "schedule_last": sched_last,
            "event_seq": seq,
            "runs": runs,
            "failures": fails,
            "consecutive_failures": consec,
            "schedule_failures": sched_fails,
            "incomplete_runs": incomp_runs,
            "event_failures": ev_fails,
            "dead_lettered": dead_lettered,
            "last_dead_letter_seq": last_dl_seq,
            "pending_events_missed": pending_missed,
            "paused": paused_active,
            "paused_until": paused_until,
            "duration_ms": dur_ms,
            "last_run": safe_sum,
            "error": err_clean,
        }
        save_unit_state(store, doc, locked=True)
    extras = {k: v for k, v in (("summary", safe_sum), ("error", err_clean), ("stderr", stderr_clean)) if v}
    if dead_letter:
        extras["dead_letter_seq"] = cursor_seq
    if events_missed > 0:
        extras["events_missed"] = events_missed
    msg, sev = f"loop.{'succeeded' if ok else 'failed'}", "INFO" if ok else "ERROR"
    if emit_loop_event:
        ev_data = {"run": run_id, "exit_code": code, "duration_ms": dur_ms}
        with suppress(Error, OSError):
            state.append_event(store, msg, name, ev_data, now=ended)
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
    with suppress(Error, OSError):
        store.append_log(name, models.log_line(msg, severity=sev, time_iso=ended_iso, **log_meta))


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
    events_missed: int = 0,
    cursor_target_seq: int | None = None,
    schedules: Sequence[ScheduleSpec] = (),
    extra_args: Sequence[str] = (),
    dry_run: bool = False,
    now: datetime | None = None,
    stop_event: StopFlag | None = None,
    term_event: StopFlag | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Execute one `LoopSpec` under `.bf/locks/loop-<name>.lock`."""
    cfg = config.load(store)
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    started_iso, run_id = now_utc(instant), f"{loop.name}-{int(instant.timestamp())}-{os.urandom(3).hex()}"
    try:
        state_doc = load_unit_state(store, loop.name)
    except Error:
        state_doc = {"schedule_last": {}, "last_run": {}}
    win_start = cron.schedule_window_start(loop, state_doc, instant, trigger=trigger, schedules=schedules)
    eff_missed = events_missed or (state.int_field(state_doc, "pending_events_missed") if trigger == "manual" else 0)
    resolved_run, ingest_src, ingest_mode, ingest_schema, timeout = resolve_loop(store, loop, cfg.reserved_sources)

    b_env = cfg.env if base_env is None else base_env
    argv, env, stdin_bytes, secret_vals = prepare_unit(
        store,
        loop.name,
        resolved_run,
        run_id,
        now_utc(win_start),
        end_iso=started_iso,
        trigger=trigger,
        events=events,
        events_missed=eff_missed,
        base_env=b_env,
        unit_env=loop.env,
        unit_env_from=loop.env_from,
        extra_args=extra_args,
        create_state=not dry_run,
    )
    if dry_run:
        ing = {"ingest": {"source": ingest_src, "mode": ingest_mode, "schema": ingest_schema}} if ingest_src else {}
        return {
            "loop": loop.name,
            "run": run_id,
            "status": "dry_run",
            "trigger": trigger,
            "argv": argv,
            **ing,
        }

    with loop_lock(store, loop.name, wait=0.05):
        t0, summary, err_msg, err_txt = time.monotonic(), {}, "", ""
        recorded = False
        mark_unit_state(store, loop.name, status="running", updated=started_iso, last_start=started_iso)
        try:
            err_cb = state.make_stderr_logger(store, loop.name, run_id, secrets=secret_vals)
            code, out_bytes, err_txt = proc.exec_argv(
                argv,
                cwd=store.root,
                env=env,
                timeout=timeout,
                stdin_data=stdin_bytes,
                stop_event=stop_event,
                on_stderr_line=err_cb,
                secrets=secret_vals,
            )
            if stop_event is not None and stop_event.is_set() and code == 130:
                stop_code = 143 if (term_event is not None and term_event.is_set()) else 130
                dur_ms = max(0, int((time.monotonic() - t0) * 1000))
                return {
                    "loop": loop.name,
                    "run": run_id,
                    "status": "stopped",
                    "exit_code": stop_code,
                    "duration_ms": dur_ms,
                }
            if code != 0:
                safe_err = models.redact_secrets(err_txt, secrets=secret_vals)
                raw_fail = (
                    _last_error_line(safe_err) or _bf_error_summary(resolved_run, out_bytes) or f"exit code {code}"
                )
                err_msg = (models.redact_secrets(raw_fail, secrets=secret_vals).strip() or f"exit code {code}")[-1024:]
            else:
                try:
                    if ingest_src:
                        summary = records.ingest(
                            store,
                            ingest_src,
                            out_bytes,
                            mode=ingest_mode,
                            schema_name=loop.schema_name,
                            now=now,
                        )
                    elif loop.emit and out_bytes.strip():
                        summary = {
                            "emitted": emit_output_events(
                                store, loop.name, loop.emit, out_bytes, now=now, secrets=secret_vals
                            )
                        }
                    elif len(out_bytes) <= 65536 and out_bytes.decode("utf-8", "replace").strip().startswith("{"):
                        with suppress(Error):
                            if isinstance(dec := decode(out_bytes, f"loops.{loop.name}"), dict):
                                summary = (
                                    rv
                                    if isinstance(rv := models.redact_value(dict(dec), secrets=secret_vals), dict)
                                    else {}
                                )
                except models.FAILURES as error:
                    code = 1
                    err_msg = (
                        models.redact_secrets(models.failure(error) or "failed", secrets=secret_vals)[-1024:]
                        or "failed"
                    )

            ended = datetime.now(UTC) if now is None else instant
            dur_ms = max(0, int((time.monotonic() - t0) * 1000))
            s_keys = tuple(s.raw for s in schedules)
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
                events_missed=eff_missed,
                emit_loop_event=True,
                trigger=trigger,
                schedule_keys=s_keys,
                secrets=secret_vals,
            )
            recorded = True
        finally:
            if not recorded:
                with suppress(Error, OSError):
                    t_end = now_utc(None if now is None else instant)
                    mark_unit_state(store, loop.name, status="stopped", updated=t_end, last_end=t_end)
    res: dict[str, Any] = {
        "loop": loop.name,
        "run": run_id,
        "status": "ok" if code == 0 else "failed",
        "exit_code": code,
        "duration_ms": dur_ms,
    }
    if eff_missed > 0:
        res["events_missed"] = eff_missed
    return {**res, **({"error": err_msg} if err_msg else {}), **({"summary": summary} if summary else {})}


def record_failure(
    store: Store, name: str, trigger: str, err: BaseException, *, now: datetime | None = None
) -> dict[str, Any]:
    """Record a pre-flight failure for loop `name` and return its result row."""
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


def select_due_with_snapshot(
    store: Store,
    cfg: Config,
    *,
    sched_now: datetime,
    exclude: frozenset[str] = frozenset(),
    dry_run: bool = False,
    now: datetime | None = None,
) -> tuple[list[_DueItem], list[dict[str, Any]], DueSnapshot]:
    """Promote due timers, select enabled due/event cycle loops, and return the loaded state/event snapshot."""
    if not dry_run:
        with suppress(Error, OSError):
            state.promote_due_timers(store, now=sched_now)
    latest_disk_seq: int | None = None
    first_disk_seq: int = 0
    cached_events: _Events | None = None
    selected_events: list[_DueItem] = []
    selected_scheds: list[_DueItem] = []
    advance_cursors: list[tuple[str, int, int]] = []
    init_cursors: list[tuple[str, int]] = []
    failures: list[dict[str, Any]] = []
    loaded_states: dict[str, dict[str, Any]] = {}

    loaded_loops: list[tuple[str, LoopSpec, bool, dict[str, Any]]] = []
    for name, loop in cfg.loops.items():
        if not loop.enabled or name in exclude:
            continue
        if loop.events and latest_disk_seq is None:
            first_disk_seq, latest_disk_seq = state.event_bounds(store)
        has_state = state.has_unit_state(store, name)
        try:
            state_doc = load_unit_state(store, name, default_event_seq=latest_disk_seq if loop.events else 0)
        except Error as err:
            if loop.daemon or not (loop.schedules or loop.events):
                continue
            if dry_run:
                raise
            failures.append(record_failure(store, name, "state", err, now=now))
            continue
        loaded_states[name] = state_doc
        if loop.daemon or not (loop.schedules or loop.events) or state.is_paused(state_doc, sched_now):
            continue
        loaded_loops.append((name, loop, has_state, state_doc))

    cached_floor = 0
    if latest_disk_seq is not None:
        max_cur = max((state.int_field(sd, "event_seq") for _, lp, _, sd in loaded_loops if lp.events), default=0)
        if max_cur > latest_disk_seq:
            fresh_first, fresh_seq = state.event_bounds(store)
            if fresh_seq > latest_disk_seq:
                first_disk_seq, latest_disk_seq = fresh_first, fresh_seq
        if latest_disk_seq > 0:
            lag_floors = [
                0 if (cur := state.int_field(sd, "event_seq")) > latest_disk_seq else cur
                for _, lp, _, sd in loaded_loops
                if lp.events and state.int_field(sd, "event_seq") != latest_disk_seq
            ]
            if lag_floors:
                cached_floor = min(lag_floors)
                cached_events = state.read_events_meta(store, after_seq=cached_floor)

    for name, loop, has_state, state_doc in loaded_loops:
        cur_seq = state.int_field(state_doc, "event_seq")
        pending_missed = state.int_field(state_doc, "pending_events_missed")
        matched_evs, target_seq, debounced, ev_blocked, live_missed = [], None, False, False, 0
        if loop.events:
            assert latest_disk_seq is not None  # noqa: S101
            target_seq = latest_disk_seq
            if cur_seq != latest_disk_seq and latest_disk_seq > 0:
                need_floor = 0 if cur_seq > latest_disk_seq else cur_seq
                if cached_events is None or need_floor < cached_floor:
                    cached_floor = need_floor
                    cached_events = state.read_events_meta(store, after_seq=cached_floor)
                matched_evs, target_seq = pending_events(
                    store, cur_seq, loop.events, self_name=name, events_data=cached_events
                )
            eff_first = cached_events[2] if cached_events is not None else first_disk_seq
            if has_state and eff_first > 1 and cur_seq < eff_first - 1:
                live_missed = (eff_first - 1) - cur_seq
            if matched_evs:
                if cron.event_backoff_remaining(state_doc, sched_now) > 0:
                    ev_blocked = True
                elif event_debounce_remaining(matched_evs, loop.events, sched_now, self_name=name) > 0:
                    debounced = True
        total_missed = pending_missed + live_missed
        if matched_evs and not ev_blocked and not debounced:
            selected_events.append((loop, "event", matched_evs, target_seq, (), total_missed))
        elif matched_scheds := due_schedules(loop, state_doc, sched_now, tz_name=cfg.timezone):
            sched_cursor = None if (debounced or ev_blocked) else target_seq
            sched_missed = total_missed if sched_cursor is not None else 0
            selected_scheds.append((loop, "schedule", [], sched_cursor, matched_scheds, sched_missed))
        elif loop.events and not has_state and target_seq is not None:
            init_cursors.append((name, target_seq))
        elif target_seq is not None and target_seq != cur_seq and not debounced and not ev_blocked:
            advance_cursors.append((name, target_seq, live_missed))

    if (advance_cursors or init_cursors) and not dry_run:
        missed_logs: list[tuple[str, int]] = []
        with writer(store):
            disk_seq = latest_event_seq(store)
            wrote_any = False
            for init_name, init_seq in init_cursors:
                if not state.has_unit_state(store, init_name):
                    anchor = min(init_seq, disk_seq)
                    new_doc = state.default_unit_state(init_name, event_seq=anchor)
                    save_unit_state(store, new_doc, locked=True, durable=False)
                    loaded_states[init_name] = new_doc
                    wrote_any = True
            for cursor_name, cursor_seq, gap_missed in advance_cursors:
                with suppress(Error):
                    latest = load_unit_state(store, cursor_name)
                    stored = state.int_field(latest, "event_seq")
                    if (nxt := _merge_cursor(stored, cursor_seq, disk_seq)) != stored:
                        next_pending = state.int_field(latest, "pending_events_missed") + gap_missed
                        upd_doc = {**latest, "event_seq": nxt, "pending_events_missed": next_pending}
                        save_unit_state(store, upd_doc, locked=True, durable=False)
                        loaded_states[cursor_name] = upd_doc
                        wrote_any = True
                        if gap_missed > 0:
                            missed_logs.append((cursor_name, gap_missed))
            if wrote_any:
                store.sync_dirs([".bf/state"])
        for log_name, gap_n in missed_logs:
            with suppress(Error, OSError):
                store.append_log(
                    log_name,
                    models.log_line(
                        "loop.events_missed",
                        severity="WARN",
                        loop=log_name,
                        events_missed=gap_n,
                    ),
                )
    return (
        [*selected_events, *selected_scheds],
        failures,
        DueSnapshot(loaded_states, latest_disk_seq, cached_events, cached_floor),
    )


def select_due_loops(
    store: Store,
    cfg: Config,
    *,
    sched_now: datetime,
    exclude: frozenset[str] = frozenset(),
    dry_run: bool = False,
    now: datetime | None = None,
) -> tuple[list[_DueItem], list[dict[str, Any]]]:
    """Promote due timers and select enabled unpaused due/event cycle loops, prioritizing events ahead of schedules."""
    selected, failures, _ = select_due_with_snapshot(
        store, cfg, sched_now=sched_now, exclude=exclude, dry_run=dry_run, now=now
    )
    return selected, failures


def _expand_loop_selectors(
    cfg: Config, selectors: Sequence[str], *, allow_daemons: bool = False, allow_disabled: bool = False
) -> list[str]:
    resolved: list[str] = []
    for token in selectors:
        if any(ch in _GLOB_CHARS for ch in token):
            matches = [
                n
                for n, lp in cfg.loops.items()
                if (allow_disabled or lp.enabled) and (allow_daemons or not lp.daemon) and fnmatch.fnmatchcase(n, token)
            ]
            if not matches:
                raise InputError(f"pattern {token!r} matched no matching loops in bf.yaml")
            resolved.extend(matches)
        else:
            if token not in cfg.loops:
                raise InputError(f"unknown loop {token!r} in bf.yaml")
            if not allow_daemons and cfg.loops[token].daemon:
                raise InputError(f"loop {token!r} is a daemon; run it with bf watch")
            if not allow_disabled and not cfg.loops[token].enabled:
                raise InputError(f"loop {token!r} is disabled in bf.yaml")
            resolved.append(token)
    return list(dict.fromkeys(resolved))


def run_loops(
    store: Store,
    names: Sequence[str] = (),
    *,
    due_only: bool = False,
    all_loops: bool = False,
    extra_args: Sequence[str] = (),
    has_passthrough: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
    stop_event: StopFlag | None = None,
    term_event: StopFlag | None = None,
) -> dict[str, Any]:
    """Run selected, glob-matched, `--all`, or `--due` cycle loops (never mutates notes implicitly)."""
    if bool(names) + bool(due_only) + bool(all_loops) != 1:
        raise InputError("specify loop names/patterns, --due, or --all (mutually exclusive)")
    if (extra_args or has_passthrough) and (due_only or all_loops):
        raise InputError("passthrough -- arguments require a single loop")
    cfg, sched_now = config.load(store), datetime.now(UTC) if now is None else now.astimezone(UTC)

    results: list[dict[str, Any]] = []
    if due_only:
        selected, pre_fails = select_due_loops(store, cfg, sched_now=sched_now, dry_run=dry_run, now=now)
        results.extend(pre_fails)
    elif all_loops:
        selected = [(lp, "manual", [], None, (), 0) for lp in cfg.loops.values() if lp.enabled and not lp.daemon]
    else:
        matched_names = _expand_loop_selectors(cfg, names)
        if (extra_args or has_passthrough) and len(matched_names) > 1:
            raise InputError("passthrough -- arguments require a single loop")
        selected = [(cfg.loops[n], "manual", [], None, (), 0) for n in matched_names]

    multi_batch = due_only or all_loops or len(selected) > 1

    def run_item(item: _DueItem, *, stop: StopFlag | None = stop_event) -> dict[str, Any]:
        loop, trig, evs, seq_target, scheds, missed = item
        if stop is not None and stop.is_set():
            stop_code = 143 if (term_event is not None and term_event.is_set()) else 130
            return {"loop": loop.name, "status": "stopped", "exit_code": stop_code, "duration_ms": 0}
        try:
            return execute_one_loop(
                store,
                loop,
                trigger=trig,
                events=evs,
                events_missed=missed,
                cursor_target_seq=seq_target,
                schedules=scheds,
                extra_args=extra_args,
                dry_run=dry_run,
                now=now,
                stop_event=stop,
                term_event=term_event,
                base_env=cfg.env,
            )
        except BusyError as err:
            if due_only:
                return {"loop": loop.name, "status": "busy", "exit_code": 0, "duration_ms": 0, "error": str(err)}
            if multi_batch and not dry_run:
                return {"loop": loop.name, "status": "failed", "exit_code": 1, "duration_ms": 0, "error": str(err)}
            raise
        except models.FAILURES as err:
            if not multi_batch or dry_run:
                raise
            return record_failure(store, loop.name, trig, err, now=now)

    pools_map = cfg.effective_pools()
    total_cap = cfg.total_concurrency()
    if due_only and not dry_run and len(selected) > 1 and total_cap > 1:
        local_stop: StopFlag = stop_event or threading.Event()
        pool = ThreadPoolExecutor(max_workers=min(total_cap, len(selected)))
        pending_items: list[tuple[int, _DueItem]] = list(enumerate(selected))
        active_futs: dict[Future[dict[str, Any]], tuple[int, str]] = {}
        pool_counts: Counter[str] = Counter()
        ordered_out: dict[int, dict[str, Any]] = {}
        try:
            while pending_items or active_futs:
                next_pending: list[tuple[int, _DueItem]] = []
                for idx_i, item in pending_items:
                    if local_stop.is_set():
                        ordered_out[idx_i] = run_item(item, stop=local_stop)
                        continue
                    p_name = item[0].pool
                    p_cap = pools_map.get(p_name, cfg.concurrency)
                    if pool_counts[p_name] < p_cap and len(active_futs) < total_cap:
                        pool_counts[p_name] += 1
                        active_futs[pool.submit(run_item, item, stop=local_stop)] = (idx_i, p_name)
                    else:
                        next_pending.append((idx_i, item))
                pending_items = next_pending
                if not active_futs:
                    break
                done, _ = wait_futures(active_futs, return_when=FIRST_COMPLETED)
                for fut in done:
                    idx_i, p_name = active_futs.pop(fut)
                    pool_counts[p_name] -= 1
                    ordered_out[idx_i] = fut.result()
            results.extend(ordered_out[i] for i in sorted(ordered_out))
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

    interrupted = bool(stop_event is not None and stop_event.is_set())
    ok_all = (not interrupted) and all(r["status"] in {"ok", "dry_run", "busy"} for r in results)
    return {
        "brain": cfg.name,
        "ok": ok_all,
        "dry_run": dry_run,
        "ran": len(results),
        "results": results,
    }


def _select_state_targets(store: Store, selectors: Sequence[str], *, all_loops: bool) -> tuple[Config, list[str]]:
    if bool(selectors) == bool(all_loops):
        raise InputError("specify loop names/patterns or --all (mutually exclusive)")
    cfg = config.load(store)
    targets = (
        sorted(cfg.loops)
        if all_loops
        else _expand_loop_selectors(cfg, selectors, allow_daemons=True, allow_disabled=True)
    )
    return cfg, targets


def _update_loop_states(store: Store, targets: Sequence[str], updates: Mapping[str, Any]) -> None:
    with writer(store):
        for name in targets:
            try:
                cur = load_unit_state(store, name)
            except Error:
                cur = state.default_unit_state(name, event_seq=latest_event_seq(store))
            save_unit_state(store, {**cur, "name": name, **updates}, locked=True)


def pause_loops(
    store: Store,
    selectors: Sequence[str],
    *,
    all_loops: bool = False,
    duration_s: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pause named/glob loops or `--all` indefinitely (or for `duration_s` seconds) without editing `bf.yaml`."""
    cfg, targets = _select_state_targets(store, selectors, all_loops=all_loops)
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    now_iso = now_utc(instant)
    until_iso = now_utc(instant + timedelta(seconds=duration_s)) if duration_s is not None else ""
    if not dry_run:
        _update_loop_states(store, targets, {"paused": True, "paused_until": until_iso, "updated": now_iso})
    return {
        "ok": True,
        "brain": cfg.name,
        "paused": targets,
        **({"paused_until": until_iso} if until_iso else {}),
        "dry_run": dry_run,
    }


def resume_loops(
    store: Store,
    selectors: Sequence[str],
    *,
    all_loops: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resume paused named/glob loops or `--all`."""
    cfg, targets = _select_state_targets(store, selectors, all_loops=all_loops)
    if not dry_run:
        _update_loop_states(store, targets, {"paused": False, "paused_until": "", "updated": now_utc(now)})
    return {"ok": True, "brain": cfg.name, "resumed": targets, "dry_run": dry_run}


def reset_loops(
    store: Store, selectors: Sequence[str], *, all_loops: bool = False, dry_run: bool = False
) -> dict[str, Any]:
    """Reset `.bf/state/<loop>.json` state to idle anchored at `latest_event_seq()` (preventing historical event replay)."""
    cfg, targets = _select_state_targets(store, selectors, all_loops=all_loops)
    if dry_run:
        return {"ok": True, "brain": cfg.name, "reset": targets, "event_seq": latest_event_seq(store), "dry_run": True}
    with writer(store):
        cur_seq = latest_event_seq(store)
        for name in targets:
            save_unit_state(store, state.default_unit_state(name, event_seq=cur_seq), locked=True)
    return {"ok": True, "brain": cfg.name, "reset": targets, "event_seq": cur_seq, "dry_run": False}


def _filter_unlocked_orphans(store: Store, orphans: dict[str, list[str]]) -> dict[str, list[str]]:
    """Exclude orphaned loop artifacts belonging to any still-running loop holding `.bf/locks/loop-<name>.lock`."""
    all_names = {
        *orphans["states"],
        *orphans["dirs"],
        *orphans["logs"],
        *(lk.removeprefix("loop-").removesuffix(".lock") for lk in orphans["locks"]),
    }
    active = {n for n in all_names if is_loop_locked(store, n)}
    if not active:
        return orphans
    return {
        "states": [n for n in orphans["states"] if n not in active],
        "dirs": [n for n in orphans["dirs"] if n not in active],
        "locks": [lk for lk in orphans["locks"] if lk.removeprefix("loop-").removesuffix(".lock") not in active],
        "logs": [n for n in orphans["logs"] if n not in active],
    }


def prune_state(store: Store, *, dry_run: bool = False) -> dict[str, Any]:
    """Remove orphaned `.bf/state/<loop>.json`, `.bf/state/<loop>/`, unlocked `.bf/locks/loop-*.lock`, and `logs/<loop>.jsonl`."""
    cfg = config.load(store)
    if dry_run:
        pruned_preview = _filter_unlocked_orphans(store, find_orphans(store, set(cfg.loops)))
        return {"ok": True, "brain": cfg.name, "pruned": pruned_preview, "dry_run": True}
    with writer(store):
        cfg = config.load(store)
        orphans = find_orphans(store, set(cfg.loops))
        if not any(orphans.values()):
            return {"ok": True, "brain": cfg.name, "pruned": orphans, "dry_run": False}
        active_locked: set[str] = set()
        pruned_locks: list[str] = []
        for lock_name in orphans["locks"]:
            lp_name = lock_name.removeprefix("loop-").removesuffix(".lock")
            if try_unlink_loop_lock(store, lock_name):
                pruned_locks.append(lock_name)
            elif is_loop_locked(store, lp_name):
                active_locked.add(lp_name)
        if pruned_locks:
            with suppress(Error, OSError):
                store.sync_dirs([".bf/locks"])
        for cand in (*orphans["states"], *orphans["dirs"], *orphans["logs"]):
            if cand not in active_locked and is_loop_locked(store, cand):
                active_locked.add(cand)
        pruned_states = [n for n in orphans["states"] if n not in active_locked]
        pruned_dirs = [d for d in orphans["dirs"] if d not in active_locked]
        pruned_logs = [lg for lg in orphans["logs"] if lg not in active_locked]
        with suppress(FileNotFoundError):
            st_dir = private_dir(store, ".bf", "state", create=False)
            for name in pruned_states:
                with suppress(FileNotFoundError):
                    st_dir.delete(f"{name}.json", durable=False)
            for sub in pruned_dirs:
                with suppress(FileNotFoundError):
                    st_dir.delete_tree(sub, durable=False)
            if pruned_states or pruned_dirs:
                store.sync_dirs([".bf/state"])
        if pruned_logs:
            with writer(store, name="logs.lock"), suppress(FileNotFoundError):
                logs_dir = private_dir(store, "logs", create=False)
                for log_name in pruned_logs:
                    with suppress(FileNotFoundError):
                        logs_dir.delete(f"{log_name}.jsonl", durable=False)
                store.sync_dirs(["logs"])
    pruned = {"states": pruned_states, "dirs": pruned_dirs, "locks": pruned_locks, "logs": pruned_logs}
    return {"ok": True, "brain": cfg.name, "pruned": pruned, "dry_run": False}
