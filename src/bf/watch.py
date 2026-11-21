"""Live supervisor and Rich TUI dashboard (`bf watch`) backed by `watchfiles`."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast

from bf import config, engine, loops, models, proc, records
from bf.config import LoopSpec
from bf.models import Error, failure, now_utc, printable
from bf.storage import Store, writer

if TYPE_CHECKING:
    from rich.panel import Panel

Console: Any = None
watch_fs: Any = None

_DEBOUNCE_MS = 300
_HEALTHY_SECS = 30.0
_SKIP_DIRS = frozenset(
    (".git", "memories", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".ty_cache", "site", "dist", "logs")
)
_STATE_BADGE: dict[str, tuple[str, str, int]] = {
    "failed": ("✗ failed", "bold red", 0),
    "running": ("● running", "bold green", 1),
    "ok": ("✓ ok", "green", 2),
    "stopped": ("○ stopped", "yellow", 3),
}


def _int(val: object) -> int:
    return val if type(val) is int else 0


def _fmt_span(seconds: float) -> str:
    if seconds < 1:
        return f"{max(0, round(seconds * 1000))}ms"
    rem, parts = round(seconds), []
    for size, unit in ((604800, "w"), (86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        if rem >= size:
            val, rem = divmod(rem, size)
            parts.append(f"{val}{unit}")
    return "".join(parts[:2])


def _fmt_when(raw_iso: str, now: datetime, *, live: bool = False) -> str:
    with suppress(ValueError, TypeError):
        if raw_iso:
            sec = max(0, int((now - datetime.fromisoformat(raw_iso)).total_seconds()))
            if sec < 5 and not live:
                return "just now"
            for size, unit in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
                if sec >= size or size == 1:
                    return f"up {sec // size}{unit}" if live else f"{sec // size}{unit} ago"
    return "—"


def _loop_trigger(loop: LoopSpec) -> tuple[str, str]:
    if loop.daemon:
        return (f"daemon · {printable(loop.emit)}" if loop.emit else "daemon", "magenta")
    parts: list[str] = []
    if len(loop.schedules) == 1:
        sc = loop.schedules[0]
        parts.append(f"every {_fmt_span(sc.every)}" if sc.every else f"cron {printable(sc.cron)}")
    elif loop.schedules:
        parts.append(f"{len(loop.schedules)} schedules")
    if loop.events:
        ev0 = printable(loop.events[0].kind or loop.events[0].source or "event")
        parts.append(f"on {ev0}" if len(loop.events) == 1 else f"on {len(loop.events)} events")
    if loop.hooks:
        parts.append(f"hook {','.join(loop.hooks)}")
    return (" · ".join(parts) or "manual", "dim")


def _ok_label(s: dict[str, Any]) -> str:
    if "written" in s and "notes" in s:
        return f"{_int(s.get('notes'))} notes ({_int(s.get('written'))} built)"
    v, ev = s.get("validate"), s.get("eval")
    if isinstance(ev, dict) and "passed" in ev and "failed" in ev:
        ev_lbl = f"{(p := _int(ev.get('passed')))}/{p + _int(ev.get('failed'))} passed"
        return f"valid · {ev_lbl}" if isinstance(v, dict) else ev_lbl
    if isinstance(v, dict) and "notes" in v and "records" in v:
        return f"valid ({_int(v.get('notes'))} notes, {_int(v.get('records'))} records)"
    if "total_stale" in s or "stale" in s:
        st_l = s.get("stale")
        n_st = _int(s["total_stale"]) if "total_stale" in s else (len(st_l) if isinstance(st_l, list) else 0)
        n_tk = _int(s["total_tasks_open"]) if "total_tasks_open" in s else _int(s.get("tasks_open"))
        return f"{n_st} stale · {n_tk} tasks"
    if "emitted" in s:
        return f"{(n := _int(s.get('emitted')))} event{'' if n == 1 else 's'} emitted"
    return "completed"


def _loop_detail(loop: LoopSpec, doc: dict[str, Any]) -> list[tuple[str, str]]:
    st = str(doc.get("status", "idle"))
    dur = f" · {_fmt_span(dms / 1000)}" if type(dms := doc.get("duration_ms")) is int and doc.get("last_end") else ""
    if st == "failed":
        consec, raw_err = _int(doc.get("consecutive_failures")), str(doc.get("error") or "")
        tail = next((ln.strip() for ln in reversed(raw_err.splitlines()) if ln.strip()), "last run failed")
        pfx = [(f"({consec}x) ", "bold red")] if consec > 1 else []
        return [*pfx, (printable(" ".join(tail.split())), "red")]
    if st == "running":
        lbl = ("listening" if loop.emit else "supervising") if loop.daemon else "busy"
        return [(lbl, "dim")]
    if st == "ok":
        s = doc["last_run"] if isinstance(doc.get("last_run"), dict) else {}
        if loop.ingest:
            pfx_s = f"{loop.ingest}: " if loop.ingest != loop.name else ""
            d_k = (("+", "added"), ("~", "updated"), ("-", "removed"))
            diffs = " ".join(f"{sym}{v}" for sym, k in d_k if (v := _int(s.get(k))) > 0)
            rec_lbl = f"{(tot := _int(s.get('records')))} record{'' if tot == 1 else 's'}"
            body = (f"{diffs} · {rec_lbl}", "green") if diffs else (f"{rec_lbl} unchanged", "")
            inc = [(" · incomplete", "yellow")] if s.get("incomplete") is True else []
            return [(pfx_s, "dim"), body, *inc, (dur, "dim")]
        return [(_ok_label(s), ""), (dur, "dim")]
    if loop.daemon:
        return [("stopped" if st == "stopped" else "ready", "dim")]
    idle_lbl = f"emit · {printable(loop.emit)}" if loop.emit else "ready"
    return [(f"→ {loop.ingest} ({loop.mode})" if loop.ingest else idle_lbl, "dim")]


def _neg_ts(doc: dict[str, Any]) -> float:
    with suppress(ValueError, TypeError):
        if raw := str(doc.get("last_end") or doc.get("last_success") or ""):
            return -datetime.fromisoformat(raw).timestamp()
    return 0.0


type _Entry = tuple[Any, str, LoopSpec, dict[str, Any]]


def render_dashboard(store: Store, *, now: datetime | None = None, max_rows: int | None = None) -> Panel:
    """Render a calm, single-panel Rich dashboard from `.bf/` operational state."""
    from rich import box
    from rich.console import Group
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    term, cfg = shutil.get_terminal_size((100, 40)), config.load(store)
    events_seq = 0
    with suppress(Error):
        events_seq = records.latest_event_seq(store)

    w_n, w_t = (24, 22) if term.columns >= 95 else (20, 17)
    t_kw: dict[str, Any] = {"expand": True, "show_edge": False, "pad_edge": False, "collapse_padding": True}
    entries: list[_Entry] = []
    disabled = 0
    for idx, (name, loop) in enumerate(cfg.loops.items()):
        if not loop.enabled:
            disabled += 1
            continue
        try:
            doc = loops.load_unit_state(store, name)
        except Error as err:
            doc = {"status": "failed", "error": str(err)}
        rank = _STATE_BADGE.get(str(doc.get("status", "idle")), ("", "", 4))[2]
        key = (rank, 0 if loop.daemon else 1, 0.0 if loop.daemon else _neg_ts(doc), idx)
        entries.append((key, name, loop, doc))
    entries.sort(key=lambda item: item[0])

    all_docs = [d for _, _, _, d in entries]
    n_fail, n_run, n_ok = (sum(d.get("status") == s for d in all_docs) for s in ("failed", "running", "ok"))
    chips = [(f"{n_fail} failed", "bold red")] if n_fail else [("All healthy", "green")]
    if n_run:
        chips.append((f"{n_run} running", "green"))
    chips.extend([(f"{n_ok} ok", "dim"), (f"{events_seq} event{'' if events_seq == 1 else 's'}", "dim")])
    if disabled:
        chips.append((f"{disabled} disabled", "dim"))
    summary = Text(" · ", style="dim").join(Text(t, style=s) for t, s in chips)
    summary.no_wrap, summary.overflow = True, "ellipsis"

    sections: list[Any] = [summary]
    if entries:
        tbl = Table(box=box.SIMPLE_HEAD, **t_kw, padding=(0, 1))
        tbl.add_column(
            f"Loops ({len(entries)})", header_style="bold blue", no_wrap=True, overflow="ellipsis", width=w_n
        )
        tbl.add_column("Trigger", header_style="dim", no_wrap=True, overflow="ellipsis", width=w_t)
        tbl.add_column("State", header_style="dim", no_wrap=True, width=9, min_width=9)
        tbl.add_column("When", style="dim", header_style="dim", justify="right", no_wrap=True, width=8, min_width=8)
        tbl.add_column("Detail", header_style="dim", ratio=1, no_wrap=True, overflow="ellipsis", min_width=14)
        avail = max(1, max_rows) if max_rows is not None else max(4, term.lines - 7)
        shown = entries if len(entries) <= avail else entries[: max(1, avail - 1)]
        for _, name, loop, doc in shown:
            st = str(doc.get("status", "idle"))
            lbl, style, _ = _STATE_BADGE.get(st, ("· idle", "dim", 4))
            raw_ts = str(doc.get("last_start" if st == "running" else "last_end") or doc.get("last_success") or "")
            trig_t, trig_s = _loop_trigger(loop)
            n_txt = Text(printable(name), style="bold" if st in {"failed", "running"} else "")
            when = _fmt_when(raw_ts, instant, live=st == "running")
            tbl.add_row(
                n_txt, Text(trig_t, style=trig_s), Text(lbl, style=style), when, Text.assemble(*_loop_detail(loop, doc))
            )
        if hidden := entries[len(shown) :]:
            ms = {"failed": "bold red", "running": "green"}
            ks = ("failed", "running", "ok", "stopped", "idle")
            counts = ((s, sum(1 for *_, d in hidden if str(d.get("status", "idle")) == s)) for s in ks)
            more = Text(" · ", style="dim").join(Text(f"{c} {s}", style=ms.get(s, "dim")) for s, c in counts if c)
            tbl.add_row(Text(f"+ {len(hidden)} more", style="dim"), "", "", "", more)
        sections.extend((Text(""), tbl))
    else:
        empty_msg = "No enabled loops in bf.yaml — watching notes for live rebuilds."
        sections.extend((Rule(style="dim"), Text(empty_msg, style="dim")))

    dot = ("● ", "bold red" if n_fail else "bold green")
    title = Text.assemble(dot, (printable(cfg.name), "bold blue"), (" · watch", "dim"))
    sub_s = f"every {_fmt_span(cfg.watch.interval)} · {cfg.watch.concurrency}w · {instant.strftime('%H:%M:%S')} UTC · Ctrl+C to stop"
    p_kw: dict[str, Any] = {"title": title, "title_align": "left", "subtitle": Text(sub_s, style="dim")}
    return Panel(Group(*sections), **p_kw, subtitle_align="right", box=box.ROUNDED, border_style="dim", padding=(0, 1))


def tick(
    store: Store, *, rebuild_notes: bool = True, now: datetime | None = None, stop_event: threading.Event | None = None
) -> dict[str, Any]:
    """Execute one synchronous supervisor cycle: optional note build and due/event loops."""
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    built = engine.build(store, now=now, incremental=True) if rebuild_notes else None
    loop_res = loops.run_loops(store, due_only=True, rebuild=False, now=now, stop_event=stop_event)
    ok_all = (built is None or bool(built["ok"])) and bool(loop_res["ok"])
    out = {"brain": config.load(store).name, "time": now_utc(instant), "built": built["written"] if built else 0}
    return {**out, "loops_ran": loop_res["ran"], "ok": ok_all}


class DaemonStream:
    """Supervise one long-running `loops.<name>` with `on: daemon`."""

    def __init__(
        self,
        store: Store,
        unit: LoopSpec,
        stop_event: threading.Event,
        wake_event: threading.Event,
        ui_event: threading.Event | None = None,
        *,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        self.store, self.unit, self.base_env = store, unit, dict(base_env or {})
        self.stop_event, self.wake_event, self.ui_event = stop_event, wake_event, ui_event
        self.cmd, self.emit = unit.run, unit.emit
        self._lock, self.proc = threading.Lock(), None
        self.thread = threading.Thread(target=self._loop, name=f"bf-loop-{unit.name}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def terminate(self) -> None:
        with self._lock:
            child = self.proc
        if child is not None and child.poll() is None:
            proc.stop_proc(child)
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=6)

    def _mark(self, status: str, ts_iso: str, **extra: str) -> None:
        loops.mark_unit_state(self.store, self.unit.name, status=status, updated=ts_iso, **extra)
        if self.ui_event is not None:
            self.ui_event.set()

    def _record(self, run_id: str, now_iso: str, t0: float, code: int, err: str, stderr: str = "") -> int:
        ms = max(0, int((time.monotonic() - t0) * 1000))
        kw: dict[str, Any] = {"ended": datetime.now(UTC), "dur_ms": ms, "code": code, "err_msg": err}
        loops.record_run(self.store, self.unit.name, run_id, now_iso, **kw, stderr=stderr, trigger="daemon")
        return ms

    def _loop(self) -> None:
        backoff, label = 1.0, f"loops.{self.unit.name}"
        while not self.stop_event.is_set():
            now_dt = datetime.now(UTC)
            now_iso, t0, healthy = now_utc(now_dt), time.monotonic(), False
            run_id = f"{self.unit.name}-{int(now_dt.timestamp())}-{os.urandom(3).hex()}"
            try:
                if missing := loops.check_requires(self.unit.requires, label):
                    raise Error("; ".join(missing))
                argv, env = loops.prepare_unit(
                    self.store,
                    self.unit.name,
                    self.cmd,
                    run_id,
                    now_iso,
                    trigger="daemon",
                    base_env=self.base_env,
                    unit_env=self.unit.env,
                )
                dn, pipe = subprocess.DEVNULL, subprocess.PIPE
                with subprocess.Popen(  # noqa: S603
                    argv, cwd=self.store.root, env=env, stdin=dn, stdout=pipe, stderr=pipe, start_new_session=True
                ) as child:
                    err_stop = threading.Event()
                    err_t, err_buf = proc.drain_tail(cast("IO[bytes]", child.stderr), 65536, err_stop)

                    run_lock, exited = threading.Lock(), threading.Event()

                    def _mark_healthy(
                        c: subprocess.Popen[bytes] = child,
                        lk: threading.Lock = run_lock,
                        ex: threading.Event = exited,
                    ) -> None:
                        with lk:
                            if ex.is_set() or self.stop_event.is_set() or c.poll() is not None:
                                return
                            ts = now_utc()
                            with suppress(Error, OSError):
                                loops.mark_unit_state(
                                    self.store,
                                    self.unit.name,
                                    status="running",
                                    updated=ts,
                                    last_success=ts,
                                    consecutive_failures=0,
                                    error="",
                                )
                        if self.ui_event is not None:
                            self.ui_event.set()

                    health_timer = threading.Timer(_HEALTHY_SECS, _mark_healthy)
                    health_timer.daemon = True
                    try:
                        with self._lock:
                            self.proc, stopping = child, self.stop_event.is_set()
                        if stopping:
                            proc.stop_proc(child)
                            continue
                        self._mark("running", now_iso, last_start=now_iso)
                        health_timer.start()
                        overflow_err, out_s = "", child.stdout
                        while out_s and not self.stop_event.is_set() and (ln := out_s.readline(models.MAX_BATCH + 1)):
                            if len(ln) > models.MAX_BATCH:
                                overflow_err = f"output line exceeds {models.MAX_BATCH} bytes"
                                sys.stderr.write(f"bf: {label}: {overflow_err}\n")
                                proc.stop_proc(child)
                                break
                            if not (stripped := ln.strip()) or not self.emit:
                                continue
                            try:
                                ev_k, payload = loops.parse_emitted_event(stripped, self.emit, label)
                                records.append_event(self.store, ev_k, self.unit.name, payload, dedupe=True)
                                self.wake_event.set()
                            except Error as err:
                                sys.stderr.write(f"bf: {label}: {printable(failure(err))}\n")
                        if self.stop_event.is_set() and child.poll() is None:
                            proc.stop_proc(child)
                        rc = child.wait()
                    except BaseException:
                        proc.stop_proc(child)
                        raise
                    finally:
                        health_timer.cancel()
                        with run_lock:
                            exited.set()
                        err_stop.set()
                        err_t.join(timeout=2)
                    err_txt = bytes(err_buf).decode("utf-8", "replace").strip()
                if self.stop_event.is_set():
                    self._mark("stopped", (t_stop := now_utc()), last_end=t_stop)
                    break
                rc = (rc or 1) if overflow_err else rc
                tail_err = "" if rc == 0 else models.redact_secrets(err_txt or f"exit code {rc}")[-1024:]
                dur_ms = self._record(run_id, now_iso, t0, rc, overflow_err or tail_err, err_txt)
                # Only sustained uptime resets restart backoff; immediate exit-0 daemons still back off.
                healthy = not overflow_err and dur_ms >= int(_HEALTHY_SECS * 1000)
            except models.FAILURES as err:
                msg = models.redact_secrets(printable(failure(err)))
                sys.stderr.write(f"bf: {label}: {msg}\n")
                with suppress(Error, OSError):
                    self._record(run_id, now_iso, t0, 1, msg[:1024])
            finally:
                with self._lock:
                    self.proc = None
            if self.ui_event is not None:
                self.ui_event.set()
            if self.stop_event.wait(1.0 if healthy else backoff):
                break
            backoff = 1.0 if healthy else min(backoff * 2.0, 30.0)


def serve(store: Store, *, json_stream: bool = False, stop_event: threading.Event | None = None) -> dict[str, Any]:
    """Supervise notes, events, daemon loops, and concurrent cycle loops (`--json` or non-TTY emits JSONL)."""
    cfg = config.load(store)
    busy = "another bf watch is already running for this brain; stop it before starting a new one"
    with writer(store, wait=0.0, busy=busy, name="watch.lock"):
        return _serve_locked(store, cfg, json_stream=json_stream, stop_event=stop_event)


def _serve_locked(
    store: Store, cfg: config.Config, *, json_stream: bool = False, stop_event: threading.Event | None = None
) -> dict[str, Any]:
    stop_ev = stop_event if stop_event is not None else threading.Event()
    wake_event, ui_event, note_dirty, cfg_dirty = (threading.Event() for _ in range(4))
    reconcile_lock, daemons = threading.Lock(), {}
    flight_lock, in_flight = threading.Lock(), set()
    emit_lock = threading.Lock()
    is_tty = sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"
    use_tui, live = not json_stream and is_tty, None
    if use_tui:
        from rich.console import Console as RichConsole
        from rich.live import Live

        con = (Console or RichConsole)(no_color=bool(os.environ.get("NO_COLOR", "")), highlight=False, emoji=False)
        live = Live(console=con, auto_refresh=False)
    last_summary = {"brain": cfg.name, "time": now_utc(), "built": 0, "loops_ran": 0, "ok": True}
    authored_pfx, root_pfx = tuple(f"{f}/" for f in models.AUTHORED), f"{store.root}/"

    def reconcile_config() -> None:
        nonlocal cfg
        try:
            new_cfg = config.load(store)
        except models.FAILURES as err:
            sys.stderr.write(f"bf: watch: {printable(failure(err))}\n")
            return
        desired = {u.name: (u, new_cfg.env) for u in new_cfg.loops.values() if u.enabled and u.daemon and u.run}
        with reconcile_lock:
            cfg = new_cfg
            stale = (
                [] if stop_ev.is_set() else [daemons.pop(k) for k in list(daemons) if desired.get(k) != daemons[k][0]]
            )
        for _, u_stop, d_stream in stale:
            u_stop.set()
            d_stream.terminate()
        with reconcile_lock:
            for key, spec_pair in () if stop_ev.is_set() else desired.items():
                if key not in daemons:
                    unit, base_env = spec_pair
                    u_stop = threading.Event()
                    stream = DaemonStream(store, unit, u_stop, wake_event, ui_event, base_env=base_env)
                    daemons[key] = (spec_pair, u_stop, stream)
                    stream.start()

    def emit_line(payload: dict[str, Any]) -> None:
        fields = {k: payload[k] for k in ("brain", "built", "loops_ran", "ok")}
        line = models.log_line(
            "watch.tick", severity="INFO" if payload["ok"] else "ERROR", time_iso=str(payload["time"]), **fields
        )
        with emit_lock:
            try:
                sys.stdout.buffer.write(line)
                sys.stdout.flush()
            except BrokenPipeError:
                stop_ev.set(), wake_event.set(), ui_event.set()
            except OSError:
                pass

    def refresh_tui() -> None:
        if live is not None:
            with suppress(Error, OSError):
                live.update(render_dashboard(store), refresh=True)

    def _classify(raw_path: str) -> str:
        rel = raw_path[len(root_pfx) :] if raw_path.startswith(root_pfx) else ""
        if not rel and raw_path != store.root.as_posix():
            with suppress(OSError, ValueError):
                rel = Path(raw_path).resolve().relative_to(store.root).as_posix()
        parts = rel.split("/") if rel else []
        if not parts or _SKIP_DIRS.intersection(parts) or any(p.startswith((".write-", ".#")) for p in parts):
            return ""
        if rel.startswith(authored_pfx) and rel.endswith(".md"):
            return "note"
        if rel in {"bf.yaml", ".bf/events.jsonl"}:
            return "config" if rel == "bf.yaml" else "wake"
        is_st = len(parts) == 2 and parts[0] == ".bf" and parts[1].endswith(".json")
        if is_st and not parts[1].endswith(".context.json"):
            return "ui"
        return ""

    def watch_files() -> None:
        if (wf := watch_fs) is None:
            from watchfiles import watch as wf
        w_kw: dict[str, Any] = {"watch_filter": lambda _, p: bool(_classify(p)), "debounce": _DEBOUNCE_MS}
        try:
            for changes in wf(store.root, **w_kw, stop_event=stop_ev, raise_interrupt=False):
                if kinds := {_classify(p) for _, p in changes} - {""}:
                    for k, ev in (("note", note_dirty), ("config", cfg_dirty)):
                        if k in kinds:
                            ev.set()
                    if kinds & {"note", "config", "wake"}:
                        wake_event.set()
                    ui_event.set()
        except (*models.FAILURES, RuntimeError) as err:
            sys.stderr.write(f"bf: watch: {type(err).__name__}\n")

    pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="bf-worker")

    def _record_summary(summary: dict[str, Any], *, emit: bool) -> None:
        nonlocal last_summary
        with emit_lock:
            last_summary = summary
        if emit:
            emit_line(summary)

    def _run_bg(item: tuple[LoopSpec, str, list[dict[str, Any]], int | None], base_env: Mapping[str, str]) -> None:
        loop, trig, evs, seq_target = item
        try:
            try:
                kw: dict[str, Any] = {"trigger": trig, "events": evs, "cursor_target_seq": seq_target}
                res = loops.execute_one_loop(store, loop, **kw, stop_event=stop_ev, base_env=base_env)
            except models.FAILURES as err:
                res = loops.record_failure(store, loop.name, trig, err)
            ok_r = res["status"] in {"ok", "dry_run", "stopped"}
            row = {"brain": cfg.name, "time": now_utc(), "built": 0, "loops_ran": 1, "ok": ok_r}
            _record_summary(row, emit=not use_tui)
        finally:
            with flight_lock:
                in_flight.discard(loop.name)
            ui_event.set()
            wake_event.set()

    def run_ticks() -> None:
        first = True
        while not stop_ev.is_set():
            wake_event.clear()
            if cfg_dirty.is_set():
                cfg_dirty.clear()
                reconcile_config()
            if dirty := note_dirty.is_set():
                note_dirty.clear()
            tick_ok = True
            try:
                cur_cfg = cfg
                built = engine.build(store, incremental=True) if (first or dirty) else None
                with flight_lock:
                    busy = frozenset(in_flight)
                due_items, pre_fails = loops.select_due_loops(store, cur_cfg, sched_now=datetime.now(UTC), exclude=busy)
                with flight_lock:
                    slots = max(0, cur_cfg.watch.concurrency - len(in_flight))
                    to_run = [it for it in due_items if it[0].name not in in_flight][:slots]
                    for it in to_run:
                        in_flight.add(it[0].name)
                for idx_i, it in enumerate(to_run):
                    if stop_ev.is_set():
                        with flight_lock:
                            for rem_it in to_run[idx_i:]:
                                in_flight.discard(rem_it[0].name)
                        break
                    try:
                        pool.submit(_run_bg, it, cur_cfg.env)
                    except BaseException:
                        with flight_lock:
                            for rem_it in to_run[idx_i:]:
                                in_flight.discard(rem_it[0].name)
                        raise
                b_cnt = built["written"] if built else 0
                ok_t = (built is None or bool(built["ok"])) and not pre_fails
                summary = {"brain": cur_cfg.name, "time": now_utc(), "built": b_cnt, "loops_ran": len(pre_fails)}
                summary["ok"] = ok_t
                _record_summary(summary, emit=not use_tui and (first or bool(b_cnt) or bool(pre_fails) or not ok_t))
            except (*models.FAILURES, RuntimeError, ValueError, KeyError, TypeError) as err:
                tick_ok = False
                msg = printable(failure(err)) if isinstance(err, models.FAILURES) else type(err).__name__
                sys.stderr.write(f"bf: watch: {msg}\n")
                with emit_lock:
                    base_b = last_summary["brain"]
                _record_summary(
                    {"brain": base_b, "time": now_utc(), "built": 0, "loops_ran": 0, "ok": False}, emit=not use_tui
                )
            if not tick_ok and dirty:
                note_dirty.set()
            first = False if tick_ok else first
            ui_event.set()
            if not stop_ev.is_set():
                wake_event.wait(timeout=cfg.watch.interval)

    tick_thread: threading.Thread | None = None
    on_main = threading.current_thread() is threading.main_thread()

    def _on_term(*_a: object) -> None:
        for ev in (stop_ev, wake_event, ui_event):
            ev.set()

    prev_term = signal.signal(signal.SIGTERM, _on_term) if on_main else None
    try:
        reconcile_config()
        threading.Thread(target=watch_files, name="bf-watchfiles", daemon=True).start()
        if live is not None:
            live.start()
            refresh_tui()
        tick_thread = threading.Thread(target=run_ticks, name="bf-tick", daemon=True)
        tick_thread.start()
        while not stop_ev.is_set():
            if not use_tui:
                stop_ev.wait()
            else:
                ui_event.wait(timeout=1.0)
                ui_event.clear()
                if not stop_ev.is_set():
                    refresh_tui()
    finally:
        _on_term()
        with reconcile_lock:
            active_daemons = list(daemons.values())
            daemons.clear()
        for _, u_stop, _ in active_daemons:
            u_stop.set()
        if tick_thread is not None and tick_thread.is_alive():
            tick_thread.join(timeout=3)
        pool.shutdown(wait=True, cancel_futures=True)
        if on_main and prev_term is not None:
            with suppress(OSError, ValueError):
                signal.signal(signal.SIGTERM, prev_term)
        if live is not None:
            with suppress(Exception):
                live.stop()
        for _, _, daemon in active_daemons:
            daemon.terminate()
    return last_summary
