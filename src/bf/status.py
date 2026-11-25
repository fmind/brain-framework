"""Strictly read-only brain status summary with O(sources) indexed counts and failed/degraded/overdue/crashed attention."""

from __future__ import annotations

import sqlite3
from contextlib import closing, suppress
from datetime import UTC, datetime
from typing import Any

from bf import config, cron, engine, index, markdown, models, records, state
from bf.config import LoopSpec
from bf.models import Error, decode
from bf.storage import Store, is_loop_locked, private_dir


def _log_error_fallback(store: Store, name: str) -> str:
    with suppress(Error, OSError, FileNotFoundError):
        tail, _ = private_dir(store, "logs", create=False).read_tail(f"{name}.jsonl", 65536)
        for raw_ln in reversed(tail.splitlines()):
            if stripped := raw_ln.strip():
                with suppress(Error):
                    if isinstance(entry := decode(stripped, f"logs/{name}.jsonl", check_duplicates=False), dict):
                        if raw := str(entry.get("error") or "").strip() or str(entry.get("stderr") or "").strip():
                            return models.redact_secrets(raw)[-1024:]
                        if type(ec := entry.get("exit_code")) is int and ec != 0:
                            return f"exit code {ec}"
                break
    return "last run failed"


def _loop_trigger_label(loop: LoopSpec) -> str:
    if loop.daemon:
        return f"daemon ({loop.emit})" if loop.emit else "daemon"
    parts: list[str] = []
    if loop.schedules:
        sc0 = loop.schedules[0].raw.replace("every:", "every ").replace("cron:", "cron ")
        parts.append(sc0 if len(loop.schedules) == 1 else f"{len(loop.schedules)} schedules")
    if loop.events:
        ev0 = loop.events[0].kind or loop.events[0].source or "event"
        parts.append(f"on {ev0}" if len(loop.events) == 1 else f"on {len(loop.events)} events")
    return " · ".join(parts) or "manual"


def _is_crashed_running(store: Store, name: str) -> bool:
    """Return True if `.bf/locks/loop-<name>.lock` exists (a process created it) yet is no longer locked."""
    try:
        locks_dir = private_dir(store, ".bf", "locks", create=False)
        if not locks_dir.exists(f"loop-{name}.lock"):
            return False
    except (FileNotFoundError, Error, OSError):
        return False
    return not is_loop_locked(store, name)


def corpus_counts(
    store: Store,
    cfg: config.Config,
    *,
    scale_warnings: list[str] | None = None,
    problems: list[str] | None = None,
) -> tuple[dict[str, int], list[str], bool]:
    """Compute corpus counts, out-of-sync note folders, and index staleness without mutating disk."""
    authored_list = markdown.authored_notes(store, cfg.notes, skipped=problems)
    mem_sources = records.valid_memory_sources(store, reserved=cfg.reserved_sources, skipped=problems)
    try:
        stale_idx = index.is_stale(store, mem_sources)
    except (Error, OSError) as err:
        if problems is None:
            raise
        msg = str(err) if isinstance(err, Error) else f"{models.NOTES_FILE}: unreadable file"
        if msg not in problems:
            problems.append(msg)
        stale_idx = True
    mem_total = 0
    active_sources = 0

    if not stale_idx:
        con = index.open_readonly(store)
        if con is not None:
            try:
                _, mem_total, active_sources = index.indexed_counts(con)
                if scale_warnings is not None:
                    for src in mem_sources:
                        rec_path = records.memory_file(src)
                        with suppress(Error, OSError):
                            size_b = store.stat(rec_path).st_size
                            row = con.execute("SELECT record_count FROM sources WHERE source = ?", (src,)).fetchone()
                            cnt = int(row[0]) if row else 0
                            if cnt > models.SCALE_WARN_RECORDS or size_b > models.SCALE_WARN_BYTES:
                                mb = size_b / (1 << 20)
                                scale_warnings.append(
                                    f"{rec_path} ({cnt:,} records, {mb:.1f} MiB; consider partitioning)"
                                )
            except sqlite3.Error:
                stale_idx = True
            finally:
                con.close()
        else:
            stale_idx = bool(mem_sources or store.exists(models.NOTES_FILE))

    if stale_idx:
        mem_total = 0
        active_sources = 0
        for src in mem_sources:
            rec_path = records.memory_file(src)
            try:
                with closing(store.iter_lines(rec_path, models.MAX_FILE)) as lines:
                    cnt = sum(1 for ln in lines if ln.strip())
                size_b = store.stat(rec_path).st_size if (cnt and scale_warnings is not None) else 0
            except (Error, OSError) as err:
                if problems is None:
                    raise
                problems.append(str(err) if isinstance(err, Error) else f"{rec_path}: unreadable file")
                continue
            if cnt:
                mem_total += cnt
                if mem_total > models.MAX_TOTAL_FILES:
                    raise Error(f"memories exceeds the {models.MAX_TOTAL_FILES:,}-record total scan limit")
                active_sources += 1
                if scale_warnings is not None and (cnt > models.SCALE_WARN_RECORDS or size_b > models.SCALE_WARN_BYTES):
                    mb = size_b / (1 << 20)
                    scale_warnings.append(f"{rec_path} ({cnt:,} records, {mb:.1f} MiB; consider partitioning)")

    counts = {
        "notes": len(authored_list),
        "memories": mem_total,
        "sources": active_sources,
        "loops": len(cfg.loops),
    }
    return counts, engine.unbuilt_note_dirs(store, cfg.notes, authored_list, fast=True), stale_idx


def _is_inside(store: Store, check_inside: bool) -> bool:
    if not check_inside:
        return False
    with suppress(Error, OSError):
        return (enclosing := config.inside_brain()) is not None and enclosing.root == store.root
    return False


def status_summary(
    store: Store,
    *,
    include_loops: bool = False,
    cfg: config.Config | None = None,
    check_inside: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Strictly read-only brain counts, failing/degraded/overdue/crashed attention loops, orphans, and optional `loops`."""
    try:
        active_cfg = config.load(store) if cfg is None else cfg
    except Error as err:
        fallback_probs = [str(err)]
        try:
            ev_seq = state.latest_event_seq(store)
        except Error as seq_err:
            ev_seq = 0
            fallback_probs.append(str(seq_err))
        return {
            "brain": store.root.name,
            "ok": False,
            "inside_brain": _is_inside(store, check_inside),
            "events_seq": ev_seq,
            "counts": {"notes": 0, "memories": 0, "sources": 0, "loops": 0},
            "attention": [],
            "orphans": {"states": [], "dirs": [], "logs": []},
            "unbuilt_notes": [],
            "problems": fallback_probs,
        }

    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    first_seq, latest_seq = state.event_bounds(store)
    pending_timers = len(state.read_timers(store))
    problems: list[str] = []
    attention: list[dict[str, Any]] = []
    paused_names: list[str] = []
    loops_out: dict[str, Any] = {}

    for name, loop in active_cfg.loops.items():
        has_state = state.has_unit_state(store, name)
        try:
            doc = state.load_unit_state(store, name, default_event_seq=0)
        except Error as err:
            doc = {"status": "failed", "consecutive_failures": 1, "error": str(err)}
        st = str(doc.get("status") or "idle")
        paused_now = state.is_paused(doc, instant)
        paused_until = str(doc.get("paused_until") or "") if paused_now else ""
        crashed = not paused_now and st == "running" and _is_crashed_running(store, name)
        if crashed:
            try:
                re_doc = state.load_unit_state(store, name, default_event_seq=0)
            except Error as err:
                doc = {"status": "failed", "consecutive_failures": 1, "error": str(err)}
                st, crashed = "failed", False
            else:
                re_st = str(re_doc.get("status") or "idle")
                if re_st != "running" or re_doc.get("last_start") != doc.get("last_start"):
                    doc, st = re_doc, re_st
                    paused_now = state.is_paused(doc, instant)
                    paused_until = str(doc.get("paused_until") or "") if paused_now else ""
                    crashed = False
        consec = state.int_field(doc, "consecutive_failures")
        ev_seq = state.int_field(doc, "event_seq")
        dl_cnt = state.int_field(doc, "dead_lettered")
        pending_missed = state.int_field(doc, "pending_events_missed")
        live_missed = (
            (first_seq - 1) - ev_seq if (loop.events and has_state and first_seq > 1 and ev_seq < first_seq - 1) else 0
        )
        ev_missed = pending_missed + live_missed
        err_s = str(doc.get("error") or "").strip() or (_log_error_fallback(store, name) if st == "failed" else "")
        err_preview = " ".join(err_s.split())[:256] if err_s else ""
        mode = "daemon" if loop.daemon else "cycle"
        lr = doc.get("last_run") if isinstance(doc.get("last_run"), dict) and doc.get("last_run") else None
        degraded = bool(isinstance(lr, dict) and lr.get("incomplete") is True)
        overdue = not paused_now and cron.is_schedule_overdue(loop, doc, instant, tz_name=active_cfg.timezone)

        if loop.enabled:
            if paused_now:
                paused_names.append(name)
            elif st == "failed" or consec > 0:
                why = "last run failed" if st == "failed" else f"{consec} consecutive failure(s)"
                problems.append(f"loops.{name}: {why}")
                if ev_missed > 0:
                    problems.append(f"loops.{name}: missed {ev_missed} compacted event(s)")
                att: dict[str, Any] = {
                    "name": name,
                    "mode": mode,
                    "reason": "failed",
                    "status": st,
                    "consecutive_failures": consec,
                }
                if ev_missed > 0:
                    att["events_missed"] = ev_missed
                if err_preview:
                    att["error"] = err_preview
                attention.append(att)
            elif crashed:
                problems.append(f"loops.{name}: process exited unexpectedly (lock not held)")
                if ev_missed > 0:
                    problems.append(f"loops.{name}: missed {ev_missed} compacted event(s)")
                attention.append(
                    {
                        "name": name,
                        "mode": mode,
                        "reason": "crashed",
                        "status": "stopped",
                        "error": "process exited unexpectedly (lock not held)",
                        **({"events_missed": ev_missed} if ev_missed > 0 else {}),
                    }
                )
            elif ev_missed > 0:
                problems.append(f"loops.{name}: missed {ev_missed} compacted event(s)")
                attention.append(
                    {
                        "name": name,
                        "mode": mode,
                        "reason": "events_missed",
                        "status": st,
                        "events_missed": ev_missed,
                    }
                )
            elif degraded:
                problems.append(f"loops.{name}: degraded (incomplete collection)")
                attention.append({"name": name, "mode": mode, "reason": "degraded", "status": st})
            elif overdue:
                problems.append(f"loops.{name}: overdue schedule")
                attention.append({"name": name, "mode": mode, "reason": "overdue", "status": st})

        if include_loops:
            optional: dict[str, Any] = {
                "description": loop.description,
                "trigger": _loop_trigger_label(loop),
                "pool": loop.pool if (not loop.daemon and loop.pool != "default") else None,
                "paused": paused_now or None,
                "paused_until": paused_until,
                "last_start": doc.get("last_start", ""),
                "last_end": doc.get("last_end", ""),
                "duration_ms": state.int_field(doc, "duration_ms") or None,
                "last_run": lr,
                "error": err_s,
                "dead_lettered": dl_cnt or None,
                "events_missed": ev_missed or None,
                "crashed": crashed or None,
                "degraded": degraded or None,
                "overdue": overdue or None,
            }
            if loop.ingest:
                optional["ingest_mode"] = loop.mode
            eff_st = "paused" if paused_now else ("stopped" if crashed else st)
            loops_out[name] = {
                "enabled": loop.enabled,
                "mode": mode,
                "status": eff_st,
                "last_success": doc.get("last_success", ""),
                "event_seq": ev_seq,
                "runs": state.int_field(doc, "runs"),
                "failures": state.int_field(doc, "failures"),
                "consecutive_failures": consec,
                **{key: val for key, val in optional.items() if val},
            }

    scale_warnings: list[str] = []
    counts, unbuilt_notes, index_stale = corpus_counts(
        store, active_cfg, scale_warnings=scale_warnings, problems=problems
    )

    inside = _is_inside(store, check_inside)

    raw_orphans = state.find_orphans(store, set(active_cfg.loops))
    orphans_out = {k: raw_orphans[k] for k in ("states", "dirs", "logs")}

    out: dict[str, Any] = {
        "brain": active_cfg.name,
        **({"description": active_cfg.description} if active_cfg.description else {}),
        "ok": not problems,
        "inside_brain": inside,
        "events_seq": latest_seq,
        **({"pending_timers": pending_timers} if pending_timers else {}),
        **({"paused": paused_names} if paused_names else {}),
        "counts": counts,
        "attention": attention,
        "orphans": orphans_out,
        "unbuilt_notes": unbuilt_notes,
        **({"index_stale": True} if index_stale else {}),
        "problems": problems,
    }
    if scale_warnings:
        out["scale_warnings"] = scale_warnings
    if include_loops:
        out["loops"] = loops_out
    return out
