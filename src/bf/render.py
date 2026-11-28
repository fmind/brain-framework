"""Human text formatters and zero-termios live screen renderer for `bf status`, `bf watch`, `bf check`, `bf review`, `bf query`, and `bf events`."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from bf.models import now_utc, printable

ALT_ENTER = b"\033[?1049h\033[?25l\033[H\033[2J"
ALT_EXIT = b"\033[?25h\033[?1049l"
_ANSI_SGR = re.compile(r"\033\[[0-9;]*m")
_STATE_ORDER = {"failed": 0, "running": 5, "ok": 6, "paused": 7, "stopped": 8, "idle": 9}


def _ansi_codes(color: bool) -> tuple[str, str, str, str, str]:
    """Return `(ok, err, running, dim, reset)` ANSI SGR codes when `color` is enabled."""
    if not color:
        return ("", "", "", "", "")
    return ("\033[32m", "\033[31;1m", "\033[36;1m", "\033[90m", "\033[0m")


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


def _fmt_ms(dur_ms: int) -> str:
    return f"{dur_ms}ms" if dur_ms < 1000 else f"{dur_ms / 1000:.1f}s"


def _format_run_detail(last_run: Mapping[str, Any], *, compact: bool = False) -> str:
    """Render sanitized `+added ~updated -removed` / `emitted` / `notes` detail from a loop run summary."""
    if "records" in last_run:
        diffs = " ".join(
            f"{sym}{printable(str(val))[:16]}"
            for sym, key in (("+", "added"), ("~", "updated"), ("-", "removed"))
            if (val := last_run.get(key))
        )
        total = printable(str(last_run.get("records") or 0))[:16]
        inc = " [incomplete]" if last_run.get("incomplete") else ""
        if diffs:
            return (f"{diffs} · {total} records{inc}") if compact else f"{diffs} ({total} total){inc}"
        return f"{total} unchanged{inc}"
    if "emitted" in last_run:
        return f"{printable(str(last_run['emitted']))[:16]} emitted"
    if "notes" in last_run:
        notes_c = printable(str(last_run["notes"]))[:16]
        built_c = printable(str(last_run.get("written", 0)))[:16]
        return f"{notes_c} notes ({built_c} built)"
    return ""


def _fmt_until(raw_iso: str, now: datetime) -> str:
    with suppress(ValueError, TypeError):
        if raw_iso:
            sec = max(0, int((datetime.fromisoformat(raw_iso) - now).total_seconds()))
            for size, unit in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
                if sec >= size or size == 1:
                    return f"{sec // size}{unit} left"
    return ""


def _fmt_detail(entry: Mapping[str, Any], *, now: datetime | None = None) -> str:
    state = str(entry.get("status") or "idle")
    consec = int(entry.get("consecutive_failures") or 0)
    pool_tag = f"[{printable(str(entry['pool']))}] " if entry.get("pool") else ""
    dl_n = int(entry.get("dead_lettered") or 0)
    ev_m = int(entry.get("events_missed") or 0)
    dl_suffix = f" ({dl_n} dead-lettered)" if dl_n else ""
    lag_suffix = f" (missed {ev_m} compacted)" if ev_m else ""
    if state == "paused":
        if (until := str(entry.get("paused_until") or "")) and now is not None and (left := _fmt_until(until, now)):
            return f"{pool_tag}paused ({left})"
        return f"{pool_tag}paused"
    if state == "failed" or consec > 0:
        err = printable(" ".join(str(entry.get("error") or "failed").split()))
        detail = f"({consec}x) {err}" if consec > 1 else err
        return f"{pool_tag}{detail if state == 'failed' else f'{state} {detail}'}{dl_suffix}{lag_suffix}"
    if entry.get("crashed"):
        return f"{pool_tag}crashed (lock not held){lag_suffix}"
    if ev_m:
        return f"{pool_tag}missed {ev_m} compacted event(s)"
    if entry.get("degraded"):
        return f"{pool_tag}degraded (incomplete collection)"
    if entry.get("overdue"):
        return f"{pool_tag}overdue schedule"
    if state == "ok" and isinstance(lr := entry.get("last_run"), Mapping) and (detail := _format_run_detail(lr)):
        return f"{pool_tag}{detail}{dl_suffix}"
    return f"{pool_tag}{printable(' '.join(str(entry.get('description') or '').split()))}{dl_suffix}"


def _summary_head(summary: Mapping[str, Any], *, color: bool) -> list[str]:
    c_ok, c_err, _, _, c_rst = _ansi_codes(color)
    cnt = summary.get("counts") or {}
    ok = bool(summary.get("ok", True))
    att_n = len(summary.get("attention") or [])
    badge = (
        f"{c_ok}HEALTHY{c_rst}" if ok else f"{c_err}ATTENTION ({att_n or len(summary.get('problems') or [])}){c_rst}"
    )
    timers_part = f"  timers={int(t):,}" if (t := summary.get("pending_timers")) else ""
    paused_part = f"  paused={len(p)}" if (p := summary.get("paused")) else ""
    counts_line = (
        f"  notes={int(cnt.get('notes', 0)):,}"
        f"  memories={int(cnt.get('memories', 0)):,}"
        f"  loops={int(cnt.get('loops', 0)):,}"
        f"  events={int(summary.get('events_seq') or 0):,}"
        f"{timers_part}{paused_part}"
    )
    return [f"Brain: {printable(str(summary.get('brain', '')))}  [{badge}]", counts_line]


def _neg_epoch(raw_iso: str) -> float:
    with suppress(ValueError, TypeError):
        if raw_iso:
            return -datetime.fromisoformat(raw_iso).timestamp()
    return 0.0


def _loop_sort_key(item: tuple[str, dict[str, Any]]) -> tuple[Any, ...]:
    name, data = item
    st = str(data.get("status") or "idle")
    consec = int(data.get("consecutive_failures") or 0)
    ts = str(data.get("last_start" if st == "running" else "last_end") or data.get("last_success") or "")
    if st == "paused":
        return (7, 0, 0.0, name)
    if st == "failed" or consec > 0:
        return (0, -consec, _neg_epoch(ts), name)
    if data.get("crashed"):
        return (1, 0, _neg_epoch(ts), name)
    if ev_m := int(data.get("events_missed") or 0):
        return (2, -ev_m, _neg_epoch(ts), name)
    if data.get("degraded"):
        return (3, 0, _neg_epoch(ts), name)
    if data.get("overdue"):
        return (4, 0, _neg_epoch(ts), name)
    return (_STATE_ORDER.get(st, 9), 0, _neg_epoch(ts), name)


def _build_loop_rows(loops_map: Mapping[str, Any], instant: datetime, *, color: bool) -> tuple[str, list[str], int]:
    c_ok, c_err, c_run, c_dim, c_rst = _ansi_codes(color)
    enabled = [(name, data) for name, data in loops_map.items() if isinstance(data, dict) and data.get("enabled", True)]
    enabled.sort(key=_loop_sort_key)
    disabled = len(loops_map) - len(enabled)
    if not enabled:
        return "", [], disabled
    name_width = max(4, *(len(printable(name)) for name, _ in enabled))
    trig_width = max(7, *(len(printable(str(data.get("trigger") or data.get("mode") or ""))) for _, data in enabled))
    hdr = f"  {c_dim}{'LOOP':<{name_width}}  {'TRIGGER':<{trig_width}}  {'STATE':<8}  {'WHEN':<8}  DETAIL{c_rst}"
    rows: list[str] = []
    for name, data in enabled:
        st = str(data.get("status") or "idle")
        consec = int(data.get("consecutive_failures") or 0)
        if st == "paused":
            st_lbl, st_color = "paused", c_dim
        elif st == "failed" or consec > 0:
            st_lbl, st_color = ("failed" if st == "failed" else st), c_err
        elif data.get("crashed"):
            st_lbl, st_color = "crashed", c_err
        elif data.get("events_missed"):
            st_lbl, st_color = "lagging", c_err
        elif data.get("degraded"):
            st_lbl, st_color = "degraded", c_err
        elif data.get("overdue"):
            st_lbl, st_color = "overdue", c_err
        else:
            st_lbl = st
            st_color = c_run if st == "running" else (c_ok if st == "ok" else c_dim)
        ts = str(data.get("last_start" if st == "running" else "last_end") or data.get("last_success") or "")
        trig = printable(str(data.get("trigger") or data.get("mode") or ""))
        age = _fmt_age(ts, instant, live=st == "running")
        rows.append(
            f"  {printable(name):<{name_width}}  {trig:<{trig_width}}"
            f"  {st_color}{printable(st_lbl)[:16]:<8}{c_rst}  {age:<8}  {_fmt_detail(data, now=instant)}"
        )
    return hdr, rows, disabled


def _alert_lines(summary: Mapping[str, Any]) -> list[str]:
    alerts: list[str] = []
    if unbuilt := summary.get("unbuilt_notes"):
        alerts.append(f"  unbuilt notes: {', '.join(printable(str(v)) for v in unbuilt)} (run: bf build)")
    orphans = summary.get("orphans")
    if isinstance(orphans, Mapping):
        flat = sorted({str(v) for key in ("states", "dirs", "logs") for v in (orphans.get(key) or ())})
        if flat:
            alerts.append(f"  orphans: {', '.join(printable(v) for v in flat)} (run: bf state prune)")
    if summary.get("index_stale"):
        alerts.append("  index: stale (run: bf build)")
    if scale := summary.get("scale_warnings"):
        alerts.append(f"  scale warnings: {'; '.join(printable(str(w)) for w in scale[:3])}")
    if (probs := summary.get("problems")) and not summary.get("attention"):
        alerts.extend(f"  ! {printable(str(p))}" for p in probs[:5])
    return alerts


def format_status_text(summary: Mapping[str, Any], *, color: bool = False, now: datetime | None = None) -> str:
    """Format a compact, calm bird's-eye overview table for humans."""
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    _, _, _, c_dim, c_rst = _ansi_codes(color)
    lines = _summary_head(summary, color=color)
    if isinstance(loops_map := summary.get("loops"), Mapping) and loops_map:
        hdr, rows, disabled = _build_loop_rows(loops_map, instant, color=color)
        if rows:
            lines.extend(["", hdr, *rows])
        if disabled:
            lines.append(f"  {c_dim}({disabled} disabled loop(s)){c_rst}")
    lines.extend(_alert_lines(summary))
    return "\n".join(lines) + "\n"


def _clip_ansi(line: str, width: int) -> str:
    """Clamp one line containing SGR escapes to `width` display columns without slicing escape sequences."""
    if width < 4 or len(_ANSI_SGR.sub("", line)) <= width:
        return line
    out: list[str] = []
    vis = pos = 0
    limit = width - 1
    had_ansi = False
    for m in _ANSI_SGR.finditer(line):
        seg = line[pos : m.start()]
        if vis + len(seg) >= limit:
            out.append(seg[: max(0, limit - vis)])
            break
        out.append(seg)
        vis += len(seg)
        out.append(m.group(0))
        had_ansi = True
        pos = m.end()
    else:
        out.append(line[pos : pos + max(0, limit - vis)])
    out.append("…")
    if had_ansi:
        out.append("\033[0m")
    return "".join(out)


def format_live_screen(
    summary: Mapping[str, Any],
    *,
    recent: Sequence[str] = (),
    footer: str = "",
    color: bool = False,
    now: datetime | None = None,
    cols: int | None = None,
    rows: int | None = None,
) -> str:
    """Format a failure-first live status screen with optional recent activity lines and terminal bounds."""
    _, _, _, c_dim, c_rst = _ansi_codes(color)
    body = format_status_text(summary, color=color, now=now).rstrip("\n").splitlines()
    foot_lines = ["", f"{c_dim}{printable(footer)}{c_rst}"] if footer else []
    recent_clean = [f"  {ln.rstrip()}" for ln in recent[-5:] if ln.strip()]

    if rows is not None and rows >= 6:
        avail_body = max(3, rows - len(foot_lines))
        if len(body) > avail_body:
            hidden = len(body) - (avail_body - 1)
            body = [*body[: avail_body - 1], f"  {c_dim}… (+{hidden} more; run bf status --format text){c_rst}"]
            recent_clean = []
        elif recent_clean:
            spare = avail_body - len(body) - 2
            recent_clean = recent_clean[-spare:] if spare > 0 else []

    out = list(body)
    if recent_clean:
        out.extend(["", f"  {c_dim}RECENT{c_rst}", *recent_clean])
    out.extend(foot_lines)
    if cols is not None and cols >= 16:
        out = [_clip_ansi(ln, cols) for ln in out]
    return "\n".join(out) + "\n"


def paint_frame(frame: str, *, tty: bool) -> bytes:
    """Render `frame` as a flicker-free ANSI in-place repaint on TTYs or plain UTF-8 text otherwise."""
    if not tty:
        return frame.encode("utf-8")
    lines = frame.rstrip("\n").splitlines()
    return ("\033[H" + "\n".join(f"{ln}\033[K" for ln in lines) + "\033[J").encode("utf-8")


def format_watch_line(event: str, payload: Mapping[str, Any], *, color: bool = False) -> str:
    """Format one human-readable scrolling event line for `bf watch --format text`."""
    ts = printable(str(payload.get("time") or now_utc())[11:19])
    c_ok, c_err, c_cyn, c_dim, c_rst = _ansi_codes(color)
    if event == "watch.build":
        ok = bool(payload.get("ok", True))
        probs = payload.get("problems") or []
        tail = ""
        if not ok and probs:
            first = printable(" ".join(str(probs[0]).split()))[:100]
            tail = f"  {first}" + (f" (+{len(probs) - 1} more; run bf check)" if len(probs) > 1 else "")
        elif idx_w := printable(" ".join(str(payload.get("index_warning") or "").split()))[:100]:
            tail = f"  index warning: {idx_w}"
        notes_c = printable(str(payload.get("notes", 0)))
        built_c = printable(str(payload.get("written", 0)))
        rem_n = int(payload.get("removed") or 0)
        counts_part = f"{built_c} built, {rem_n} removed" if rem_n else f"{built_c} built"
        return f"{c_dim}{ts}{c_rst}  {c_ok if ok else c_err}build  {c_rst}  {notes_c} notes ({counts_part}){tail}\n"
    if event == "watch.loop":
        name = printable(str(payload.get("loop") or ""))
        state = str(payload.get("status") or "")
        dur = _fmt_ms(int(payload.get("duration_ms") or 0))
        summary = payload.get("summary")
        if state == "ok":
            tag = f"{c_ok}ok     {c_rst}"
            detail = _format_run_detail(summary, compact=True) if isinstance(summary, dict) else ""
        elif state == "stopped":
            tag = f"{c_cyn}stop   {c_rst}"
            detail = "stopped"
        else:
            tag = f"{c_err}fail   {c_rst}"
            detail = printable(str(payload.get("error") or state))
        return f"{c_dim}{ts}{c_rst}  {tag}  {name}  {detail}  {c_dim}{dur}{c_rst}\n"
    if event == "watch.daemon":
        name = printable(str(payload.get("loop") or ""))
        state = printable(str(payload.get("state") or ""))
        detail = printable(str(payload.get("error") or ""))
        is_ok = payload.get("ok", state in {"started", "healthy", "stopped"} and not detail)
        tag = f"{c_cyn if is_ok else c_err}daemon {c_rst}"
        return f"{c_dim}{ts}{c_rst}  {tag}  {name} {state}{f': {detail}' if detail else ''}\n"
    if event in {"watch.started", "watch.stopped"}:
        lbl = "start  " if event == "watch.started" else "stop   "
        return f"{c_dim}{ts}{c_rst}  {c_cyn}{lbl}{c_rst}  {printable(str(payload.get('brain') or ''))}\n"
    msg = printable(str(payload.get("error") or payload.get("message") or event))
    if loop_name := printable(str(payload.get("loop") or "")):
        msg = f"{loop_name}: {msg}"
    return f"{c_dim}{ts}{c_rst}  {c_err}error  {c_rst}  {msg}\n"


def format_check_text(res: Mapping[str, Any], *, color: bool = False) -> str:
    """Format a compact, calm human summary for `bf check --format text`."""
    c_ok, c_err, _, c_dim, c_rst = _ansi_codes(color)
    sm = res.get("summary") or {}
    cnt = res.get("counts") or {}
    errs = int(sm.get("errors") or 0)
    warns = int(sm.get("warnings") or 0)
    badge = f"{c_ok}OK{c_rst}" if res.get("ok") else f"{c_err}FAILED ({errs} errors, {warns} warnings){c_rst}"
    mem_v = "skipped" if res.get("notes_only") or cnt.get("memories") is None else f"{int(cnt['memories']):,}"
    head = f"Check: {printable(str(res.get('brain', '')))}  [{badge}]"
    counts = f"  notes={int(cnt.get('notes') or 0):,}  memories={mem_v}  errors={errs}  warnings={warns}"
    lines = [head, counts]
    if probs := res.get("problems"):
        lines.extend(["", *(f"  - {printable(str(p))}" for p in probs)])
    if res.get("truncated"):
        lines.append(f"  {c_dim}(truncated; pass -n/--limit or --all){c_rst}")
    return "\n".join(lines) + "\n"


def format_review_text(res: dict[str, Any], *, color: bool = False) -> str:
    """Format a compact, calm human summary for `bf review --format text`."""
    c_ok, c_err, _, c_dim, c_rst = _ansi_codes(color)
    probs = res.get("problems") or []
    stale = res.get("stale") or []
    total_stale = int(res.get("total_stale") or 0)
    open_tasks = int(res.get("total_tasks_open") or 0)
    ok_badge = f"{c_ok}FRESH{c_rst}" if total_stale == 0 else f"{c_err}STALE ({total_stale}){c_rst}"
    badge = ok_badge if res.get("ok", True) else f"{c_err}FAILED{f' ({len(probs)} problems)' if probs else ''}{c_rst}"
    lines = [
        f"Review: {printable(str(res.get('brain', '')))}  [{badge}]",
        f"  stale={total_stale}  open_tasks={open_tasks}",
    ]
    if stale:
        ref_w = max(3, *(len(printable(str(it.get("ref", "")))) for it in stale))
        lines.extend(["", f"  {c_dim}{'REF':<{ref_w}}  {'REASONS':<22}  DETAIL{c_rst}"])
        for it in stale:
            newer = [printable(str(n)) for n in (it.get("newer") or ())]
            detail = f"newer: {', '.join(newer)}" if newer else printable(str(it.get("title") or ""))
            rsn = printable(",".join(str(r) for r in (it.get("reasons") or ())))
            lines.append(f"  {printable(str(it.get('ref', ''))):<{ref_w}}  {rsn:<22}  {detail}")
    if res.get("truncated"):
        lines.append(f"  {c_dim}(truncated; pass -n/--limit or --all){c_rst}")
    if unbuilt := res.get("unbuilt_notes"):
        lines.append(f"  {c_dim}unbuilt notes: {', '.join(printable(str(u)) for u in unbuilt)} (run: bf build){c_rst}")
    if res.get("index_stale"):
        lines.append(f"  {c_dim}(index stale; run bf build){c_rst}")
    if probs:
        lines.extend(["", *(f"  ! {printable(str(p))}" for p in probs)])
    return "\n".join(lines) + "\n"


def format_query_text(res: dict[str, Any], *, color: bool = False) -> str:
    """Format a compact ranked human listing for `bf query --format text`."""
    _, _, c_cyn, c_dim, c_rst = _ansi_codes(color)
    items = res.get("items") or []
    hdr = f"Query ({printable(str(res.get('engine', '')))}): {len(items)} result(s)"
    if q := res.get("query"):
        hdr += f" for {printable(str(q))!r}"
    lines = [hdr]
    if items:
        lines.append("")
        for it in items:
            ref = printable(str(it.get("ref") or ""))
            title = printable(str(it.get("title") or ""))
            snip = printable(str(it.get("snippet") or it.get("description") or ""))
            anc = f"#{printable(str(it['anchor']))}" if it.get("anchor") else ""
            lines.append(f"  {c_cyn}{ref}{anc}{c_rst}  {title}")
            if snip and snip != title:
                lines.append(f"    {c_dim}{snip[:140]}{c_rst}")
    if res.get("truncated"):
        lines.append(f"  {c_dim}(truncated; pass -n/--limit or --all){c_rst}")
    if excl := res.get("excluded_sources"):
        lines.append(
            f"  {c_dim}(excluded by bf.yaml: {', '.join(printable(str(s)) for s in excl)}; pass --all-sources){c_rst}"
        )
    if unbuilt := res.get("unbuilt_notes"):
        lines.append(f"  {c_dim}unbuilt notes: {', '.join(printable(str(u)) for u in unbuilt)} (run: bf build){c_rst}")
    if res.get("index_stale"):
        lines.append(f"  {c_dim}(index stale; run bf build for fast BM25 FTS5 search){c_rst}")
    return "\n".join(lines) + "\n"


def format_events_text(res: dict[str, Any], *, color: bool = False) -> str:
    """Format a compact read-only listing for `bf events --format text`."""
    _, _, c_cyn, c_dim, c_rst = _ansi_codes(color)
    mode = str(res.get("mode") or "events")
    items = res.get("items") or []
    hdr_lbl = "Timers" if mode == "timers" else "Events"
    timers_n = int(res.get("pending_timers") or 0)
    extra = f"  pending_timers={timers_n}" if mode == "events" and timers_n else ""
    lines = [
        f"{hdr_lbl}: {printable(str(res.get('brain', '')))}  (showing {len(items)} of {int(res.get('total') or 0)}{extra})"
    ]
    if items:
        lines.append("")
        for it in items:
            kind = printable(str(it.get("kind") or ""))
            src = printable(str(it.get("source") or ""))
            data = it.get("data")
            preview = printable(json.dumps(data, ensure_ascii=False, separators=(",", ":")))[:100] if data else ""
            if mode == "timers":
                when_s = printable(str(it.get("deliver_at") or ""))
                tid = printable(str(it.get("id") or ""))
                lines.append(f"  {c_dim}{when_s}{c_rst}  {c_cyn}{kind}{c_rst}  {src}  {c_dim}{tid}{c_rst}  {preview}")
            else:
                seq = int(it.get("seq") or 0)
                when_s = printable(str(it.get("time") or ""))
                lines.append(f"  {c_dim}#{seq:<6} {when_s}{c_rst}  {c_cyn}{kind}{c_rst}  {src}  {preview}")
    if res.get("truncated"):
        lines.append(f"  {c_dim}(truncated; pass -n/--limit or --all){c_rst}")
    return "\n".join(lines) + "\n"
