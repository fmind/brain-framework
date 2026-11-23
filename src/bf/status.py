"""Strictly read-only brain status summary and bird's-eye text table formatter (zero external UI deps)."""

from __future__ import annotations

import os
import re
import select
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from bf import config, loops, markdown, models, records
from bf.config import LoopSpec
from bf.models import Error, decode, printable
from bf.storage import Store, private_dir

_ANSI_RE = re.compile(r"(\033\[[0-9;]*m)")
_STATE_ORDER = {"failed": 0, "running": 1, "ok": 2, "stopped": 3, "idle": 4}
_E1 = {"[A": "up", "[B": "down", "[5~": "pgup", "[6~": "pgdn"}
_ESC_KEYS = _E1 | dict.fromkeys(("[H", "OH", "[1~"), "home") | dict.fromkeys(("[F", "OF", "[4~"), "end")
_C1 = {"k": "up", "j": "down", "b": "pgup", " ": "pgdn", "g": "home", "G": "end"}
_CHAR_KEYS = _C1 | {"f": "filter", "s": "sort", "e": "errors", "r": "refresh", "q": "quit"}


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


def unbuilt_note_dirs(store: Store, folders: Iterable[str], authored: Iterable[str] | None = None) -> list[str]:
    """Return sorted note folders whose `memories/<dir>.jsonl` is missing or out of sync with Markdown files."""
    dirs = tuple(dict.fromkeys(folders))
    by_dir: dict[str, list[str]] = {d: [] for d in dirs}
    for path in authored or ():
        if (d := path.split("/", 1)[0]) in by_dir:
            by_dir[d].append(path)
    out: list[str] = []
    for folder in dirs:
        try:
            notes = markdown.authored_notes(store, (folder,), skipped=[]) if authored is None else by_dir[folder]
            mem = records.memory_file(folder)
            if not notes or not store.exists(mem):
                if bool(notes) != store.exists(mem):
                    out.append(folder)
                continue
            built: dict[str, str] = {}
            for _, obj in records.iter_jsonl(store, mem):
                if not isinstance(ref := obj.get("ref"), str) or ref in built:
                    raise Error("bad")
                built[ref] = str(obj.get("sha256") or "")
            if set(built) != set(notes) or any(
                models.digest(store.read(p, models.MAX_NOTE)) != built[p] for p in notes
            ):
                out.append(folder)
        except Error, OSError:
            out.append(folder)
    return sorted(out)


def corpus_counts(store: Store, cfg: config.Config) -> tuple[dict[str, int], list[str]]:
    """Compute corpus counts and out-of-sync note folders without mutating disk."""
    notes_set, authored_list = frozenset(cfg.notes), markdown.authored_notes(store, cfg.notes)
    rec_total, usage_total, rec_sources = 0, 0, set()
    for bucket in models.RECORD_DIRS:
        for src in records.valid_record_sources(store, bucket, cfg.notes):
            if bucket == "memories" and src in notes_set:
                continue
            if cnt := sum(
                1 for ln in store.iter_lines(records.record_file(src, bucket), models.MAX_FILE) if ln.strip()
            ):
                if bucket == "usages":
                    usage_total += cnt
                else:
                    rec_total += cnt
                if rec_total + usage_total > models.MAX_TOTAL_FILES:
                    raise Error(f"records exceed the {models.MAX_TOTAL_FILES:,}-record total scan limit")
                rec_sources.add(f"{bucket}/{src}")
    n, s, lp = len(authored_list), len(rec_sources), len(cfg.loops)
    counts = {"notes": n, "records": rec_total, "usages": usage_total, "sources": s, "loops": lp}
    return counts, unbuilt_note_dirs(store, cfg.notes, authored_list)


def status_summary(
    store: Store, *, all_units: bool = False, counts_cache: tuple[dict[str, int], list[str]] | None = None
) -> dict[str, Any]:
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
            why = "last run failed" if st == "failed" else f"{consec} consecutive failure(s)"
            problems.append(f"loops.{name}: {why}")
            att = {"kind": "loop", "mode": mode, "name": name, "status": st, "failed": st == "failed"}
            attention.append({**att, "consecutive_failures": consec, **({"error": err_s} if err_s else {})})
        lr = doc.get("last_run") if isinstance(doc.get("last_run"), dict) and doc.get("last_run") else None
        ing_s, ing_tgt, ing_m = loops.effective_ingest(store, loop, cfg.reserved_sources)
        ing_d = {"ingest_target": ing_tgt, "ingest_mode": ing_m} if ing_s else {}
        doc_d = {"last_start": doc.get("last_start", ""), "last_end": doc.get("last_end", "")}
        opt_d = {"description": loop.description, "trigger": _loop_trigger_label(loop), **ing_d, **doc_d}
        opt_d |= {"duration_ms": doc.get("duration_ms", 0) or None, "last_run": lr, "error": err_s}
        base_l = {"enabled": loop.enabled, "mode": mode, "status": st, "last_success": doc.get("last_success", "")}
        loops_out[name] = {**base_l, "consecutive_failures": consec, **{k: v for k, v in opt_d.items() if v}}
    if counts_cache is None:
        counts, unbuilt_notes = corpus_counts(store, cfg)
    else:
        counts, unbuilt_notes = {**counts_cache[0], "loops": len(cfg.loops)}, list(counts_cache[1])
    inside = False
    with suppress(Error):
        inside = (enclosing := config.inside_brain()) is not None and enclosing.root == store.root
    desc_d = {"description": cfg.description} if cfg.description else {}
    head_d = {"brain": cfg.name, **desc_d, "ok": not problems, "inside_brain": inside}
    mid_d = {"events_seq": records.latest_event_seq(store), "counts": counts, "attention": attention}
    tail_d = {"orphaned_states": loops.find_orphans(store, set(cfg.loops)), "unbuilt_notes": unbuilt_notes}
    return {**head_d, **mid_d, **tail_d, "problems": problems, **({"loops": loops_out} if all_units else {})}


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


def format_run_detail(lr: Mapping[str, Any], *, compact: bool = False) -> str:
    """Render sanitized `+added ~updated -removed` / `emitted` / `notes` detail from a loop run summary."""
    if "records" in lr:
        diffs = " ".join(
            f"{s}{printable(str(v))[:16]}"
            for s, k in (("+", "added"), ("~", "updated"), ("-", "removed"))
            if (v := lr.get(k))
        )
        tot = printable(str(lr.get("records") or 0))[:16]
        if diffs:
            return f"{diffs} · {tot} records" if compact else f"{diffs} ({tot} total)"
        return f"{tot} unchanged"
    if "emitted" in lr:
        return f"{printable(str(lr['emitted']))[:16]} emitted"
    if "notes" in lr:
        return f"{printable(str(lr['notes']))[:16]} notes ({printable(str(lr.get('written', 0)))[:16]} built)"
    return ""


def _fmt_detail(entry: dict[str, Any]) -> str:
    st = str(entry.get("status") or "idle")
    if st == "failed":
        consec = int(entry.get("consecutive_failures") or 0)
        err = printable(" ".join(str(entry.get("error") or "failed").split()))
        return f"({consec}x) {err[:72]}" if consec > 1 else err[:72]
    if st == "ok" and isinstance(lr := entry.get("last_run"), dict) and (detail := format_run_detail(lr)):
        return detail
    return printable(" ".join(str(entry.get("description") or "").split()))[:64]


def ansi_codes(color: bool) -> tuple[str, str, str, str, str]:
    """Return `(ok, err, running, dim, reset)` ANSI SGR codes when `color` is enabled."""
    return ("\033[32m", "\033[31;1m", "\033[36;1m", "\033[2m", "\033[0m") if color else ("", "", "", "", "")


def clip_ansi(line: str, width: int) -> str:
    """Sanitize non-SGR escapes via `printable()` and clip to `width` visible columns without color bleed."""
    if width < 1:
        return ""
    parts = [p if i % 2 else printable(p) for i, p in enumerate(_ANSI_RE.split(line))]
    if sum(len(p) for i, p in enumerate(parts) if i % 2 == 0) <= width:
        last_sgr = next((parts[i] for i in range(len(parts) - 1, -1, -1) if i % 2), "")
        return "".join(parts) + ("\033[0m" if last_sgr and last_sgr != "\033[0m" else "")
    keep, out, vis, has_esc = (width - 1 if width > 1 else 1), [], 0, False
    for i, part in enumerate(parts):
        if i % 2:
            out.append(part)
            has_esc = True
        elif part:
            take = min(len(part), keep - vis)
            out.append(part[:take])
            vis += take
            if vis >= keep:
                break
    return "".join(out) + ("…" if width > 1 else "") + ("\033[0m" if has_esc else "")


def read_tui_key(fd: int) -> str:
    """Decode one keypress, ANSI arrow/page/SGR-wheel sequence, or `'eof'` from a TTY descriptor."""
    if not (ch := os.read(fd, 1)):
        return "eof"
    if ch == b"\x03":
        return "quit"
    if ch != b"\x1b":
        return _CHAR_KEYS.get(ch.decode("latin1"), "")
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    seq = b""
    while len(seq) < 20 and (evs := poller.poll(20)) and (evs[0][1] & select.POLLIN) and (part := os.read(fd, 1)):
        seq += part
        if hit := _ESC_KEYS.get(seq.decode("latin1")):
            return hit
        if part == b"M" and (wheel := {b"[<64;": "up", b"[<65;": "down"}.get(seq[:5])):
            return wheel
        if len(seq) >= 2 and 0x40 <= part[0] <= 0x7E:
            break
    return ""


def apply_tui_key(
    key: str, offset: int, page: int, only_active: bool, sort_recent: bool, show_errors: bool = False
) -> tuple[int, bool, bool, bool, str]:
    """Apply one interactive shortcut and return `(offset, only_active, sort_recent, show_errors, action)`."""
    step = max(1, page)
    if (delta := {"up": -1, "down": 1, "pgup": -step, "pgdn": step}.get(key)) is not None:
        return max(0, offset + delta), only_active, sort_recent, show_errors, ""
    if key in {"home", "end"}:
        return (0 if key == "home" else 10**9), only_active, sort_recent, show_errors, ""
    if key in {"filter", "sort"}:
        return 0, only_active ^ (key == "filter"), sort_recent ^ (key == "sort"), show_errors, ""
    if key == "errors":
        return offset, only_active, sort_recent, not show_errors, ""
    return offset, only_active, sort_recent, show_errors, key if key in {"refresh", "quit"} else ""


def _neg_epoch(entry: Mapping[str, Any]) -> float:
    with suppress(ValueError, TypeError):
        if raw := str(entry.get("last_end") or entry.get("last_start") or entry.get("last_success") or ""):
            return -datetime.fromisoformat(raw).timestamp()
    return 0.0


def _summary_head(summary: Mapping[str, Any], *, color: bool, suffix: str = "") -> list[str]:
    c_ok, c_err, _, _, c_rst = ansi_codes(color)
    cnt, ok = summary.get("counts") or {}, bool(summary.get("ok", True))
    badge = f"{c_ok}HEALTHY{c_rst}" if ok else f"{c_err}ATTENTION ({len(summary.get('attention') or [])}){c_rst}"
    c_ln = f"  notes={cnt.get('notes', 0)}  memories={cnt.get('records', 0)}  usages={cnt.get('usages', 0)}  sources={cnt.get('sources', 0)}  loops={cnt.get('loops', 0)}  events={summary.get('events_seq', 0)}"
    return [f"Brain: {printable(str(summary.get('brain', '')))}  [{badge}]{suffix}", c_ln]


def _is_active_loop(d: Mapping[str, Any]) -> bool:
    lr = d.get("last_run")
    changed = isinstance(lr, Mapping) and any(lr.get(k) for k in ("added", "updated", "removed", "emitted", "written"))
    return str(d.get("status") or "") in {"failed", "running"} or int(d.get("consecutive_failures") or 0) > 0 or changed


def _build_loop_rows(
    loops_map: Mapping[str, Any],
    instant: datetime,
    *,
    color: bool,
    only_active: bool = False,
    sort_recent: bool = False,
) -> tuple[str, list[str], int, int]:
    c_ok, c_err, c_run, c_dim, c_rst = ansi_codes(color)
    enabled = [(n, d) for n, d in loops_map.items() if isinstance(d, dict) and d.get("enabled", True)]
    picked = [(n, d) for n, d in enabled if not only_active or _is_active_loop(d)]
    picked.sort(
        key=lambda p: (
            _STATE_ORDER.get(str(p[1].get("status", "idle")), 4),
            _neg_epoch(p[1]) if sort_recent else 0.0,
            p[0],
        )
    )
    if not picked:
        return "", [], len(enabled), len(loops_map) - len(enabled)
    w_n = max(len(printable(n)) for n, _ in picked)
    w_t = max(len(printable(str(d.get("trigger") or d.get("mode") or ""))) for _, d in picked)
    hdr = f"  {c_dim}{'LOOP':<{w_n}}  {'TRIGGER':<{w_t}}  {'STATE':<8}  {'WHEN':<8}  DETAIL{c_rst}"
    rows = []
    for name, d in picked:
        st = str(d.get("status") or "idle")
        st_c = c_err if st == "failed" else (c_run if st == "running" else (c_ok if st == "ok" else c_dim))
        ts = str(d.get("last_start" if st == "running" else "last_end") or d.get("last_success") or "")
        trig, age = printable(str(d.get("trigger") or d.get("mode") or "")), _fmt_age(ts, instant, live=st == "running")
        rows.append(
            f"  {printable(name):<{w_n}}  {trig:<{w_t}}  {st_c}{printable(st)[:16]:<8}{c_rst}  {age:<8}  {_fmt_detail(d)}"
        )
    return hdr, rows, len(enabled), len(loops_map) - len(enabled)


def _alert_lines(summary: Mapping[str, Any], *, short: bool = False) -> list[str]:
    pfx = "" if short else "run: "
    u_l, o_l = ("unbuilt", "orphans") if short else ("unbuilt notes", "orphaned states")
    rows = ((u_l, summary.get("unbuilt_notes"), "bf build"), (o_l, summary.get("orphaned_states"), "bf run --prune"))
    return [f"  {lbl}: {', '.join(printable(str(v)) for v in vals)} ({pfx}{cmd})" for lbl, vals, cmd in rows if vals]


def format_status_text(summary: dict[str, Any], *, color: bool = False, now: datetime | None = None) -> str:
    """Format a compact, calm bird's-eye overview table for humans (zero external dependencies)."""
    instant, (_, _, _, c_dim, c_rst) = datetime.now(UTC) if now is None else now.astimezone(UTC), ansi_codes(color)
    lines = _summary_head(summary, color=color)
    if isinstance(loops_map := summary.get("loops"), dict) and loops_map:
        hdr, rows, _, disabled = _build_loop_rows(loops_map, instant, color=color)
        if rows:
            lines.extend(["", hdr, *rows])
        if disabled:
            lines.append(f"  {c_dim}({disabled} disabled loop(s)){c_rst}")
    lines.extend(_alert_lines(summary))
    return "\n".join(lines) + "\n"


def format_watch_screen(
    summary: dict[str, Any],
    recent: Sequence[str],
    *,
    cols: int = 100,
    rows: int = 30,
    offset: int = 0,
    only_active: bool = False,
    sort_recent: bool = True,
    show_errors: bool = False,
    concurrency: int = 4,
    color: bool = False,
    now: datetime | None = None,
) -> tuple[str, int, int]:
    """Render one flicker-free scrollable ANSI frame for `bf watch`, returning `(frame, clamped_offset, page)`."""
    instant, (_, c_err, _, c_dim, c_rst) = datetime.now(UTC) if now is None else now.astimezone(UTC), ansi_codes(color)
    t_spec = (("active", only_active), ("A-Z", not sort_recent), ("errors", show_errors))
    tags = "".join(f"  {c_dim}[{t}]{c_rst}" for t, on in t_spec if on)
    sfx = f"{tags}  {c_dim}{concurrency}w · {instant.strftime('%H:%M:%S')} UTC{c_rst}"
    head = [*_summary_head(summary, color=color, suffix=sfx), *_alert_lines(summary, short=True)]
    loops_map: Mapping[str, Any] = raw if isinstance(raw := summary.get("loops"), dict) else {}
    hdr, r_lines, en_n, dis_n = _build_loop_rows(
        loops_map, instant, color=color, only_active=only_active, sort_recent=sort_recent
    )
    w, rec_n, rec_blk = max(20, cols), (5 if rows >= 14 else (2 if rows >= 10 else 0)), []
    if rec_n and show_errors:
        att = [a for a in (summary.get("attention") or ()) if isinstance(a, Mapping)]
        if att:
            top, span = att[0], max(20, w - 4)
            more = f" · +{len(att) - 1} more" if len(att) > 1 else ""
            err_s = printable(" ".join(str(top.get("error") or "failed").split()))
            wrapped = textwrap.wrap(err_s, span)[:rec_n] or ["failed"]
            t_lbl = f"{printable(str(top.get('name') or ''))} · {int(top.get('consecutive_failures') or 0)}x{more}"
            rec_blk = ["", f"  {c_err}Error ({t_lbl}):{c_rst}", *(f"  {ln}" for ln in wrapped)]
        else:
            rec_blk = ["", f"  {c_dim}Errors: (no failing loops){c_rst}"]
    elif rec_n and recent:
        rec_blk = ["", f"  {c_dim}Recent activity:{c_rst}", *(f"  {r}" for r in list(recent)[-rec_n:])]
    f_l, s_l = ("active" if only_active else "all"), ("recent" if sort_recent else "name")
    keys_bar = f"  {c_dim}↑/↓ scroll · PgUp/PgDn page · g/G top/end · f filter({f_l}) · s sort({s_l}) · e errors · r refresh · q quit{c_rst}"
    avail = max(1, rows - len(head) - len(rec_blk) - 5)
    clamped = max(0, min(offset, max(0, len(r_lines) - avail)))
    body = [""]
    if r_lines:
        shown = r_lines[clamped : clamped + avail]
        body.extend([hdr, *shown])
        if len(r_lines) > avail or only_active or dis_n:
            extra = (f" of {en_n} enabled" if only_active else "") + (f" · {dis_n} disabled" if dis_n else "")
            body.append(f"  {c_dim}showing {clamped + 1}-{clamped + len(shown)}/{len(r_lines)}{extra}{c_rst}")
    else:
        empty = "no active/failing loops (press f for all)" if only_active and en_n else "no enabled loops in bf.yaml"
        body.append(f"  {c_dim}({empty}){c_rst}")
    out = [*head, *body, *rec_blk, "", keys_bar][: max(1, rows)]
    return "\n".join(clip_ansi(ln, w) + "\033[K" for ln in out), clamped, avail


def format_check_text(res: dict[str, Any], *, color: bool = False, notes_only: bool = False) -> str:
    """Format a compact, calm human summary for `bf check --format text`."""
    c_ok, c_err, _, c_dim, c_rst = ansi_codes(color)
    errs, warns = int(res.get("errors_count") or 0), int(res.get("warnings_count") or 0)
    badge = f"{c_ok}OK{c_rst}" if res.get("ok") else f"{c_err}FAILED ({errs} errors, {warns} warnings){c_rst}"
    mem_v, use_v = ("skipped", "skipped") if notes_only else (res.get("records", 0), res.get("usages", 0))
    c_head = f"Check: {printable(str(res.get('brain', '')))}  [{badge}]"
    c_cnt = f"  notes={res.get('notes', 0)}  memories={mem_v}  usages={use_v}  errors={errs}  warnings={warns}"
    lines = [c_head, c_cnt]
    if probs := res.get("problems"):
        lines.extend(["", *(f"  - {printable(str(p))}" for p in probs)])
    if (res.get("summary") or {}).get("truncated"):
        lines.append(f"  {c_dim}(truncated; pass --all for full list){c_rst}")
    return "\n".join(lines) + "\n"


def format_review_text(res: dict[str, Any], *, color: bool = False) -> str:
    """Format a compact, calm human summary for `bf review --format text`."""
    c_ok, c_err, _, c_dim, c_rst = ansi_codes(color)
    probs, stale = res.get("problems") or [], res.get("stale") or []
    total_stale, open_tasks = int(res.get("total_stale") or 0), int(res.get("total_tasks_open") or 0)
    ok_b = f"{c_ok}FRESH{c_rst}" if total_stale == 0 else f"{c_err}STALE ({total_stale}){c_rst}"
    badge = ok_b if res.get("ok", True) else f"{c_err}FAILED{f' ({len(probs)} problems)' if probs else ''}{c_rst}"
    b_head = f"Review: {printable(str(res.get('brain', '')))}  [{badge}]"
    lines = [b_head, f"  stale={total_stale}  open_tasks={open_tasks}"]
    if stale:
        w_r = max((len(printable(str(it.get("ref", "")))) for it in stale), default=3)
        lines.extend(["", f"  {c_dim}{'REF':<{w_r}}  {'REASONS':<22}  DETAIL{c_rst}"])
        for it in stale:
            newer = [printable(str(n)) for n in (it.get("newer") or ())]
            detail = f"newer: {', '.join(newer)}" if newer else printable(str(it.get("title") or ""))
            rsn = ",".join(str(r) for r in (it.get("reasons") or ()))
            lines.append(f"  {printable(str(it.get('ref', ''))):<{w_r}}  {rsn:<22}  {detail}")
    if res.get("truncated"):
        lines.append(f"  {c_dim}(truncated; pass --all for full list){c_rst}")
    if probs:
        lines.extend(["", *(f"  ! {printable(str(p))}" for p in probs)])
    return "\n".join(lines) + "\n"
