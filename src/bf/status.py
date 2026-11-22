"""Strictly read-only brain status summary and bird's-eye text table formatter (zero external UI deps)."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from bf import config, loops, markdown, models, records
from bf.config import LoopSpec
from bf.models import Error, decode, printable
from bf.storage import Store, private_dir


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
    if len(loop.schedules) == 1:
        sc = loop.schedules[0]
        parts.append(sc.raw.replace("every:", "every ").replace("cron:", "cron "))
    elif loop.schedules:
        parts.append(f"{len(loop.schedules)} schedules")
    if loop.events:
        ev0 = loop.events[0].kind or loop.events[0].source or "event"
        parts.append(f"on {ev0}" if len(loop.events) == 1 else f"on {len(loop.events)} events")
    return " · ".join(parts) or "manual"


def unbuilt_note_dirs(store: Store, folders: Iterable[str], authored: Iterable[str] | None = None) -> list[str]:
    """Return sorted note folders whose `memories/<dir>.jsonl` is missing or out of sync with Markdown files."""
    dirs = tuple(dict.fromkeys(folders))
    if not dirs:
        return []
    by_dir: dict[str, list[str]] = {d: [] for d in dirs}
    if authored is not None:
        for path in authored:
            if (d := path.split("/", 1)[0]) in by_dir:
                by_dir[d].append(path)
    out: list[str] = []
    for folder in dirs:
        if authored is None:
            try:
                notes = markdown.authored_notes(store, (folder,), skipped=[])
            except Error:
                out.append(folder)
                continue
        else:
            notes = by_dir[folder]
        mem_file = records.memory_file(folder)
        has_mem = store.exists(mem_file)
        if not notes:
            if has_mem:
                out.append(folder)
            continue
        if not has_mem:
            out.append(folder)
            continue
        try:
            built: dict[str, str] = {}
            bad = False
            for _, obj in records.iter_jsonl(store, mem_file):
                if not isinstance(ref := obj.get("ref"), str) or ref in built:
                    bad = True
                    break
                built[ref] = str(obj.get("sha256") or "")
            if (
                bad
                or set(built) != set(notes)
                or any(models.digest(store.read(p, models.MAX_NOTE)) != built[p] for p in notes)
            ):
                out.append(folder)
        except Error, OSError:
            out.append(folder)
    return sorted(out)


def status_summary(store: Store, *, all_units: bool = False) -> dict[str, Any]:
    """Strictly read-only brain counts, failing attention loops, orphaned `.bf/` states, and optional `loops`."""
    cfg, problems, attention, loops_out = config.load(store), [], [], {}
    for name, loop in cfg.loops.items():
        try:
            doc = loops.load_unit_state(store, name)
        except Error as err:
            doc = {"status": "failed", "consecutive_failures": 1, "error": str(err)}
        st, consec = str(doc.get("status") or "idle"), int(doc.get("consecutive_failures") or 0)
        err_s = str(doc.get("error") or "").strip() or (_log_error_fallback(store, name) if st == "failed" else "")
        mode = "daemon" if loop.daemon else "cycle"
        if loop.enabled and (st == "failed" or consec > 0):
            reason = "last run failed" if st == "failed" else f"{consec} consecutive failure(s)"
            problems.append(f"loops.{name}: {reason}")
            att = {"kind": "loop", "mode": mode, "name": name, "status": st, "failed": st == "failed"}
            attention.append({**att, "consecutive_failures": consec, **({"error": err_s} if err_s else {})})
        lr = doc.get("last_run") if isinstance(doc.get("last_run"), dict) and doc.get("last_run") else None
        ing_s, ing_tgt, ing_m = loops.effective_ingest(store, loop, cfg.reserved_sources)
        extra_i = {
            k: v
            for k, v in (
                ("description", loop.description),
                ("trigger", _loop_trigger_label(loop)),
                ("ingest_target", ing_tgt if ing_s else ""),
                ("ingest_mode", ing_m if ing_s else ""),
                ("last_start", doc.get("last_start", "")),
                ("last_end", doc.get("last_end", "")),
                ("duration_ms", doc.get("duration_ms", 0) or None),
                ("last_run", lr),
                ("error", err_s),
            )
            if v
        }
        base = {"enabled": loop.enabled, "mode": mode, "status": st, "last_success": doc.get("last_success", "")}
        loops_out[name] = {**base, "consecutive_failures": consec, **extra_i}

    events_seq = records.latest_event_seq(store)
    orphaned_states = loops.find_orphans(store, set(cfg.loops))
    notes_set = frozenset(cfg.notes)
    authored_list = markdown.authored_notes(store, cfg.notes)
    notes_total = len(authored_list)
    unbuilt_notes = unbuilt_note_dirs(store, cfg.notes, authored_list)
    rec_total = usage_total = 0
    rec_sources: set[str] = set()
    for bucket in models.RECORD_DIRS:
        for src in records.valid_record_sources(store, bucket, cfg.notes):
            if bucket == "memories" and src in notes_set:
                continue
            cnt = sum(1 for ln in store.iter_lines(records.record_file(src, bucket), models.MAX_FILE) if ln.strip())
            if cnt:
                if bucket == "usages":
                    usage_total += cnt
                else:
                    rec_total += cnt
                if rec_total + usage_total > models.MAX_TOTAL_FILES:
                    raise Error(f"records exceed the {models.MAX_TOTAL_FILES:,}-record total scan limit")
                rec_sources.add(f"{bucket}/{src}")

    inside = False
    with suppress(Error):
        enclosing = config.inside_brain()
        inside = enclosing is not None and enclosing.root == store.root
    counts = {
        "notes": notes_total,
        "records": rec_total,
        "usages": usage_total,
        "sources": len(rec_sources),
        "loops": len(cfg.loops),
    }
    desc_d = {"description": cfg.description} if cfg.description else {}
    head = {"brain": cfg.name, **desc_d, "ok": not problems, "inside_brain": inside, "events_seq": events_seq}
    tail = {
        "counts": counts,
        "attention": attention,
        "orphaned_states": orphaned_states,
        "unbuilt_notes": unbuilt_notes,
        "problems": problems,
    }
    return {**head, **tail, **({"loops": loops_out} if all_units else {})}


def _fmt_age(raw_iso: str, now: datetime, *, live: bool = False) -> str:
    with suppress(ValueError, TypeError):
        if raw_iso:
            sec = max(0, int((now - datetime.fromisoformat(raw_iso)).total_seconds()))
            if sec < 5 and not live:
                return "now"
            for size, unit in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
                if sec >= size or size == 1:
                    return f"up {sec // size}{unit}" if live else f"{sec // size}{unit} ago"
    return "-"


def _fmt_detail(entry: dict[str, Any]) -> str:
    st = str(entry.get("status") or "idle")
    if st == "failed":
        consec = int(entry.get("consecutive_failures") or 0)
        err = printable(" ".join(str(entry.get("error") or "failed").split()))
        return f"({consec}x) {err[:72]}" if consec > 1 else err[:72]
    if st == "ok" and isinstance(lr := entry.get("last_run"), dict):
        if "records" in lr:
            diffs = " ".join(
                f"{s}{printable(str(v))}"
                for s, k in (("+", "added"), ("~", "updated"), ("-", "removed"))
                if (v := lr.get(k))
            )
            tot = printable(str(lr.get("records") or 0))
            return f"{diffs} ({tot} total)" if diffs else f"{tot} unchanged"
        if "emitted" in lr:
            return f"{printable(str(lr['emitted']))} emitted"
        if "notes" in lr:
            return f"{printable(str(lr['notes']))} notes ({printable(str(lr.get('written', 0)))} built)"
    return printable(" ".join(str(entry.get("description") or "").split()))[:64]


def format_status_text(summary: dict[str, Any], *, color: bool = False, now: datetime | None = None) -> str:
    """Format a compact, calm bird's-eye overview table for humans (zero external dependencies)."""
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    c_ok, c_err, c_run, c_dim, c_rst = (
        ("\033[32m", "\033[31;1m", "\033[36;1m", "\033[2m", "\033[0m") if color else ("", "", "", "", "")
    )
    cnt = summary.get("counts") or {}
    ok = bool(summary.get("ok", True))
    badge = f"{c_ok}HEALTHY{c_rst}" if ok else f"{c_err}ATTENTION ({len(summary.get('attention') or [])}){c_rst}"
    lines = [
        f"Brain: {printable(str(summary.get('brain', '')))}  [{badge}]",
        (
            f"  notes={cnt.get('notes', 0)}  memories={cnt.get('records', 0)}  "
            f"usages={cnt.get('usages', 0)}  sources={cnt.get('sources', 0)}  "
            f"loops={cnt.get('loops', 0)}  events={summary.get('events_seq', 0)}"
        ),
    ]
    loops_map = summary.get("loops")
    if isinstance(loops_map, dict) and loops_map:
        order = {"failed": 0, "running": 1, "ok": 2, "stopped": 3, "idle": 4}
        rows: list[tuple[int, str, dict[str, Any]]] = [
            (order.get(str(d.get("status", "idle")), 4), name, d)
            for name, d in loops_map.items()
            if d.get("enabled", True)
        ]
        rows.sort(key=lambda r: (r[0], r[1]))
        if rows:
            w_n = max((len(n) for _, n, _ in rows), default=4)
            w_t = max((len(printable(str(d.get("trigger") or d.get("mode") or ""))) for _, _, d in rows), default=7)
            lines.append("")
            lines.append(f"  {c_dim}{'LOOP':<{w_n}}  {'TRIGGER':<{w_t}}  {'STATE':<8}  {'WHEN':<8}  DETAIL{c_rst}")
            for _, name, d in rows:
                st = str(d.get("status") or "idle")
                st_c = c_err if st == "failed" else (c_run if st == "running" else (c_ok if st == "ok" else c_dim))
                ts = str(d.get("last_start" if st == "running" else "last_end") or d.get("last_success") or "")
                when = _fmt_age(ts, instant, live=st == "running")
                trig = printable(str(d.get("trigger") or d.get("mode") or ""))
                st_disp = printable(st)[:16]
                lines.append(
                    f"  {printable(name):<{w_n}}  {trig:<{w_t}}  {st_c}{st_disp:<8}{c_rst}  {when:<8}  {_fmt_detail(d)}"
                )
        disabled = sum(1 for d in loops_map.values() if not d.get("enabled", True))
        if disabled:
            lines.append(f"  {c_dim}({disabled} disabled loop(s)){c_rst}")
    if unbuilt := summary.get("unbuilt_notes"):
        lines.append(f"  unbuilt notes: {', '.join(printable(str(u)) for u in unbuilt)} (run: bf build)")
    if orphans := summary.get("orphaned_states"):
        lines.append(f"  orphaned states: {', '.join(printable(str(o)) for o in orphans)} (run: bf run --prune)")
    return "\n".join(lines) + "\n"


def format_check_text(res: dict[str, Any], *, color: bool = False, notes_only: bool = False) -> str:
    """Format a compact, calm human summary for `bf check --format text`."""
    c_ok, c_err, c_dim, c_rst = ("\033[32m", "\033[31;1m", "\033[2m", "\033[0m") if color else ("", "", "", "")
    ok = bool(res.get("ok"))
    errs, warns = int(res.get("errors_count") or 0), int(res.get("warnings_count") or 0)
    badge = f"{c_ok}OK{c_rst}" if ok else f"{c_err}FAILED ({errs} errors, {warns} warnings){c_rst}"
    mem_v = "skipped" if notes_only else res.get("records", 0)
    use_v = "skipped" if notes_only else res.get("usages", 0)
    lines = [
        f"Check: {printable(str(res.get('brain', '')))}  [{badge}]",
        f"  notes={res.get('notes', 0)}  memories={mem_v}  usages={use_v}  errors={errs}  warnings={warns}",
    ]
    if probs := res.get("problems"):
        lines.append("")
        lines.extend(f"  - {printable(str(p))}" for p in probs)
    sm = res.get("summary") or {}
    if sm.get("truncated"):
        lines.append(f"  {c_dim}(truncated; pass --all for full list){c_rst}")
    return "\n".join(lines) + "\n"


def format_review_text(res: dict[str, Any], *, color: bool = False) -> str:
    """Format a compact, calm human summary for `bf review --format text`."""
    c_ok, c_err, c_dim, c_rst = ("\033[32m", "\033[31;1m", "\033[2m", "\033[0m") if color else ("", "", "", "")
    ok = bool(res.get("ok", True))
    probs = res.get("problems") or []
    stale = res.get("stale") or []
    total_stale, open_tasks = int(res.get("total_stale") or 0), int(res.get("total_tasks_open") or 0)
    if not ok:
        badge = f"{c_err}FAILED ({len(probs)} problems){c_rst}" if probs else f"{c_err}FAILED{c_rst}"
    elif total_stale == 0:
        badge = f"{c_ok}FRESH{c_rst}"
    else:
        badge = f"{c_err}STALE ({total_stale}){c_rst}"
    lines = [
        f"Review: {printable(str(res.get('brain', '')))}  [{badge}]",
        f"  stale={total_stale}  open_tasks={open_tasks}",
    ]
    if stale:
        lines.append("")
        w_r = max((len(printable(str(it.get("ref", "")))) for it in stale), default=3)
        lines.append(f"  {c_dim}{'REF':<{w_r}}  {'REASONS':<22}  DETAIL{c_rst}")
        for it in stale:
            ref = printable(str(it.get("ref", "")))
            reasons = ",".join(str(r) for r in (it.get("reasons") or ()))
            newer = [printable(str(n)) for n in (it.get("newer") or ())]
            detail = f"newer: {', '.join(newer)}" if newer else printable(str(it.get("title") or ""))
            lines.append(f"  {ref:<{w_r}}  {reasons:<22}  {detail}")
    if res.get("truncated"):
        lines.append(f"  {c_dim}(truncated; pass --all for full list){c_rst}")
    if probs:
        lines.append("")
        lines.extend(f"  ! {printable(str(p))}" for p in probs)
    return "\n".join(lines) + "\n"
