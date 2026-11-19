"""Live supervisor and Rich TUI dashboard (`bf watch`) backed by `watchfiles`."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from watchfiles import watch as watch_fs

from bf import config, engine, jobs, models, query, records
from bf.config import JobSpec, ServiceSpec
from bf.models import Error, decode, failure, now_utc, printable
from bf.storage import Store

_SKIP_A = (".git", "memories", ".venv", "__pycache__", ".pytest_cache")
_SKIP_DIRS = frozenset((*_SKIP_A, ".ruff_cache", ".ty_cache", "site", "dist", "logs"))
_B_A = {"failed": ("✗ failed", "bold red", 0), "running": ("● running", "bold green", 1)}
_B_B = {"ok": ("✓ ok", "green", 2), "stopped": ("○ stopped", "yellow", 3)}
_STATE_BADGE: dict[str, tuple[str, str, int]] = {**_B_A, **_B_B}


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


def _unit_trigger(unit: JobSpec | ServiceSpec) -> Text:
    if isinstance(unit, ServiceSpec):
        mode = unit.emit if unit.emit != "none" else "daemon"
        return Text(f"service · {mode}", style="magenta")
    parts: list[str] = []
    if unit.every:
        parts.append(f"every {_fmt_span(unit.every)}")
    elif unit.cron:
        parts.append(f"cron {printable(unit.cron)}")
    if unit.events:
        ev0 = printable(unit.events[0].kind or unit.events[0].source or "event")
        parts.append(f"on {ev0}" if len(unit.events) == 1 else f"on {len(unit.events)} events")
    if unit.hooks:
        parts.append(f"hook {','.join(unit.hooks)}")
    return Text(" · ".join(parts) or "manual", style="dim")


def _ok_label(s: dict[str, Any]) -> str:
    if "written" in s and "notes" in s:
        return f"{_int(s.get('notes'))} notes ({_int(s.get('written'))} built)"
    if "notes" in s and "records" in s:
        return f"valid ({_int(s.get('notes'))} notes, {_int(s.get('records'))} records)"
    if "passed" in s and "failed" in s:
        p = _int(s.get("passed"))
        return f"{p}/{p + _int(s.get('failed'))} passed"
    if "stale" in s and "tasks_open" in s:
        n_st = len(st_l) if isinstance(st_l := s.get("stale"), list) else 0
        return f"{n_st} stale · {_int(s.get('tasks_open'))} tasks"
    if isinstance(act := s.get("action"), str) and act:
        return f"→ {printable(act)}"
    return "completed"


def _unit_detail(unit: JobSpec | ServiceSpec, doc: dict[str, Any]) -> Text:
    st = str(doc.get("status", "idle"))
    dur = f" · {_fmt_span(dms / 1000)}" if type(dms := doc.get("duration_ms")) is int and doc.get("last_end") else ""
    if st == "failed":
        consec = _int(doc.get("consecutive_failures"))
        raw_err = str(doc.get("error") or "")
        tail = next((ln.strip() for ln in reversed(raw_err.splitlines()) if ln.strip()), "last run failed")
        pfx = [(f"({consec}x) ", "bold red")] if consec > 1 else []
        return Text.assemble(*pfx, (printable(" ".join(tail.split())), "red"))
    if st == "running":
        msg = ("listening" if unit.emit == "messages" else "supervising") if isinstance(unit, ServiceSpec) else "busy"
        return Text(msg, style="dim")
    if st == "ok":
        s = doc["last_run"] if isinstance(doc.get("last_run"), dict) else {}
        if isinstance(unit, JobSpec) and unit.ingest:
            pfx_s = f"{unit.ingest}: " if unit.ingest != unit.name else ""
            d_k = (("+", "added"), ("~", "updated"), ("-", "removed"))
            diffs = " ".join(f"{sym}{v}" for sym, k in d_k if (v := _int(s.get(k))) > 0)
            total = _int(s.get("records"))
            rec_lbl = f"{total} record{'' if total == 1 else 's'}"
            body = (f"{diffs} · {rec_lbl}", "green") if diffs else (f"{rec_lbl} unchanged", "")
            inc = [(" · incomplete", "yellow")] if s.get("incomplete") is True else []
            return Text.assemble((pfx_s, "dim"), body, *inc, (dur, "dim"))
        return Text.assemble((_ok_label(s), ""), (dur, "dim"))
    if isinstance(unit, JobSpec):
        act = f"action · keep {unit.action_keep}" if unit.action_keep else "ready"
        return Text(f"→ {unit.ingest} ({unit.mode})" if unit.ingest else act, style="dim")
    return Text("stopped" if st == "stopped" else "ready", style="dim")


def _make_table(first_col: str, second_col: str, cols: int) -> Table:
    w_name, w_trig = (24, 21) if cols >= 95 else (20, 16)
    table = Table(
        box=box.SIMPLE_HEAD, expand=True, show_edge=False, pad_edge=False, collapse_padding=True, padding=(0, 1)
    )
    table.add_column(first_col, header_style="bold blue", no_wrap=True, overflow="ellipsis", width=w_name)
    table.add_column(second_col, header_style="dim", no_wrap=True, overflow="ellipsis", width=w_trig)
    table.add_column("State", header_style="dim", no_wrap=True, width=9, min_width=9)
    table.add_column("When", style="dim", header_style="dim", justify="right", no_wrap=True, width=8, min_width=8)
    table.add_column("Detail", header_style="dim", ratio=1, no_wrap=True, overflow="ellipsis", min_width=14)
    return table


type _Entry = tuple[Any, str, JobSpec | ServiceSpec, dict[str, Any]]


def _add_rows(table: Table, items: list[_Entry], instant: datetime) -> None:
    for _, name, unit, doc in items:
        st = str(doc.get("status", "idle"))
        lbl, style, _ = _STATE_BADGE.get(st, ("· idle", "dim", 4))
        raw_ts = str(doc.get("last_start" if st == "running" else "last_end") or doc.get("last_success") or "")
        when = _fmt_when(raw_ts, instant, live=st == "running")
        row = (Text(printable(name), style="bold" if st in {"failed", "running"} else ""), _unit_trigger(unit))
        table.add_row(*row, Text(lbl, style=style), when, _unit_detail(unit, doc))


def _neg_ts(doc: dict[str, Any]) -> float:
    with suppress(ValueError, TypeError):
        if raw := str(doc.get("last_end") or doc.get("last_success") or ""):
            return -datetime.fromisoformat(raw).timestamp()
    return 0.0


def render_dashboard(store: Store, *, now: datetime | None = None, max_rows: int | None = None) -> Panel:
    """Render a calm, single-panel Rich dashboard from `.bf/` operational state."""
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    term = shutil.get_terminal_size((100, 40))
    cfg = config.load(store)
    events_seq = 0
    with suppress(Error):
        events_seq = records.latest_event_seq(store)

    svc_entries: list[_Entry] = []
    job_entries: list[_Entry] = []
    disabled = 0
    for kind, group in (("service", cfg.services), ("job", cfg.jobs)):
        for idx, (name, unit) in enumerate(group.items()):
            if not unit.enabled:
                disabled += 1
                continue
            try:
                doc = jobs.load_unit_state(store, name, kind=kind)
            except Error as err:
                doc = {"status": "failed", "error": str(err)}
            rank = _STATE_BADGE.get(str(doc.get("status", "idle")), ("", "", 4))[2]
            key = (rank, _neg_ts(doc), idx) if kind == "job" else (rank, idx)
            (job_entries if kind == "job" else svc_entries).append((key, name, unit, doc))
    svc_entries.sort(key=lambda item: item[0])
    job_entries.sort(key=lambda item: item[0])

    all_docs = [d for _, _, _, d in (*svc_entries, *job_entries)]
    n_fail, n_run, n_ok = (sum(d.get("status") == s for d in all_docs) for s in ("failed", "running", "ok"))
    chips = [(f"{n_fail} failed", "bold red")] if n_fail else [("All healthy", "green")]
    chips += [*([(f"{n_run} running", "green")] if n_run else ()), (f"{n_ok} ok", "dim")]
    chips += [(f"{events_seq} event{'' if events_seq == 1 else 's'}", "dim")]
    chips += [(f"{disabled} disabled", "dim")] if disabled else []
    summary = Text(" · ", style="dim").join(Text(txt, style=st_style) for txt, st_style in chips)
    summary.no_wrap, summary.overflow = True, "ellipsis"

    sections: list[Any] = [summary]
    if svc_entries:
        s_tbl = _make_table(f"Services ({len(svc_entries)})", "Mode", term.columns)
        _add_rows(s_tbl, svc_entries, instant)
        sections.extend((Text(""), s_tbl))
    if job_entries:
        j_tbl = _make_table(f"Jobs ({len(job_entries)})", "Trigger", term.columns)
        auto_rows = max(4, term.lines - (10 + len(svc_entries) if svc_entries else 7))
        avail = max(1, max_rows) if max_rows is not None else auto_rows
        shown = job_entries if len(job_entries) <= avail else job_entries[: max(1, avail - 1)]
        _add_rows(j_tbl, shown, instant)
        if hidden := job_entries[len(shown) :]:
            m_styles = {"failed": "bold red", "running": "green"}
            st_all = ("failed", "running", "ok", "stopped", "idle")
            counts = ((s, sum(1 for *_, d in hidden if str(d.get("status", "idle")) == s)) for s in st_all)
            more = Text(" · ", style="dim").join(Text(f"{c} {s}", style=m_styles.get(s, "dim")) for s, c in counts if c)
            j_tbl.add_row(Text(f"+ {len(hidden)} more", style="dim"), "", "", "", more)
        sections.extend((Text(""), j_tbl))
    if not svc_entries and not job_entries:
        msg = "No enabled jobs or services in bf.yaml — watching notes for live rebuilds."
        sections.extend((Rule(style="dim"), Text(msg, style="dim")))

    b_col = "bold red" if n_fail else "bold green"
    title = Text.assemble(("● ", b_col), (printable(cfg.name), "bold blue"), (" · watch", "dim"))
    sub_txt = f"every {_fmt_span(cfg.watch.interval)} · {instant.strftime('%H:%M:%S')} UTC · Ctrl+C to stop"
    p_kw: dict[str, Any] = {"title": title, "title_align": "left", "subtitle": Text(sub_txt, style="dim")}
    return Panel(Group(*sections), **p_kw, subtitle_align="right", box=box.ROUNDED, border_style="dim", padding=(0, 1))


def tick(
    store: Store, *, rebuild_notes: bool = True, now: datetime | None = None, stop_event: threading.Event | None = None
) -> dict[str, Any]:
    """Execute one supervisor cycle: optional note build and due/event jobs."""
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    built = engine.build(store, now=now, incremental=True) if rebuild_notes else None
    job_res = jobs.run_jobs(store, due_only=True, rebuild=False, now=now, stop_event=stop_event)
    res = {"brain": config.load(store).name, "time": now_utc(instant), "built": built["written"] if built else 0}
    b_ok = built is None or bool(built["ok"])
    return {**res, "jobs_ran": job_res["ran"], "ok": b_ok and bool(job_res["ok"])}


class DaemonStream:
    """Supervise one long-running `services.<name>.run` daemon."""

    def __init__(self, store: Store, unit: ServiceSpec, *evs: threading.Event) -> None:
        self.store, self.unit = store, unit
        self.stop_event, self.wake_event, self.ui_event = evs[0], evs[1], (evs[2] if len(evs) > 2 else None)
        self.kind, self.cmd, self.emit = "service", unit.run, unit.emit
        self._lock = threading.Lock()
        self.proc: subprocess.Popen[bytes] | None = None
        self.thread = threading.Thread(target=self._loop, name=f"bf-service-{unit.name}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def terminate(self) -> None:
        with self._lock:
            proc = self.proc
        if proc is not None and proc.poll() is None:
            jobs.stop_proc(proc)
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=3)

    def _mark(self, status: str, ts_iso: str, **extra: str) -> None:
        doc = jobs.load_unit_state(self.store, self.unit.name, kind=self.kind)
        jobs.save_unit_state(self.store, {**doc, "status": status, "updated": ts_iso, **extra})
        if self.ui_event is not None:
            self.ui_event.set()

    def _record(self, run_id: str, now_iso: str, t0: float, code: int, err: str, stderr: str = "") -> int:
        ms, dt = max(0, int((time.monotonic() - t0) * 1000)), datetime.now(UTC)
        pos = (self.store, self.kind, self.unit.name, run_id, now_iso)
        jobs.record_run(*pos, ended=dt, dur_ms=ms, code=code, err_msg=err, stderr=stderr)
        return ms

    def _loop(self) -> None:
        backoff = 1.0
        label = f"{self.kind}s.{self.unit.name}"
        while not self.stop_event.is_set():
            now_dt = datetime.now(UTC)
            now_iso, t0 = now_utc(now_dt), time.monotonic()
            run_id = f"{self.unit.name}-{int(now_dt.timestamp())}-{os.urandom(3).hex()}"
            healthy = False
            try:
                if missing := jobs.check_requires(self.unit.requires, label):
                    raise Error("; ".join(missing))
                argv, env = jobs.prepare_unit(
                    self.store, self.unit.name, self.cmd, run_id, now_iso, trigger=self.kind, unit_env=self.unit.env
                )
                dn, pipe = subprocess.DEVNULL, subprocess.PIPE
                with subprocess.Popen(  # noqa: S603
                    argv, cwd=self.store.root, env=env, stdin=dn, stdout=pipe, stderr=pipe, start_new_session=True
                ) as proc:
                    err_stop = threading.Event()
                    err_t, err_buf = query.drain_tail(cast("IO[bytes]", proc.stderr), 65536, err_stop)
                    try:
                        with self._lock:
                            self.proc, stopping = proc, self.stop_event.is_set()
                        if stopping:
                            jobs.stop_proc(proc)
                            continue
                        self._mark("running", now_iso, last_start=now_iso)
                        overflow_err = ""
                        out_s = proc.stdout
                        while out_s and not self.stop_event.is_set() and (ln := out_s.readline(models.MAX_BATCH + 1)):
                            if len(ln) > models.MAX_BATCH:
                                overflow_err = f"output line exceeds {models.MAX_BATCH} bytes"
                                sys.stderr.write(f"bf: {label}: {overflow_err}\n")
                                jobs.stop_proc(proc)
                                break
                            if not (stripped := ln.strip()) or self.emit == "none":
                                continue
                            try:
                                if isinstance(parsed := decode(stripped, label), dict):
                                    p_kind = str(parsed.get("kind") or "service.event")
                                    ev_k = "service.message" if self.emit == "messages" else p_kind
                                    records.append_event(self.store, ev_k, self.unit.name, dict(parsed), dedupe=True)
                                    self.wake_event.set()
                            except Error as err:
                                sys.stderr.write(f"bf: {label}: {printable(failure(err))}\n")
                        if self.stop_event.is_set() and proc.poll() is None:
                            jobs.stop_proc(proc)
                        rc = proc.wait()
                    except BaseException:
                        jobs.stop_proc(proc)
                        raise
                    finally:
                        err_stop.set()
                        err_t.join(timeout=2)
                    err_txt = bytes(err_buf).decode("utf-8", "replace").strip()
                if self.stop_event.is_set():
                    self._mark("stopped", (t_stop := now_utc()), last_end=t_stop)
                    break
                rc = (rc or 1) if overflow_err else rc
                tail_err = "" if rc == 0 else models.redact_secrets(err_txt or f"exit code {rc}")[-1024:]
                err_msg = overflow_err or tail_err
                dur_ms = self._record(run_id, now_iso, t0, rc, err_msg, err_txt)
                healthy = not overflow_err and (rc == 0 or dur_ms >= 30_000)
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
            backoff = 1.0 if healthy else backoff
            if self.stop_event.wait(backoff):
                break
            backoff = 1.0 if healthy else min(backoff * 2.0, 30.0)


def _safe_tick(
    store: Store, *, rebuild_notes: bool, fallback: dict[str, Any], stop_event: threading.Event
) -> tuple[dict[str, Any], bool]:
    try:
        return tick(store, rebuild_notes=rebuild_notes, stop_event=stop_event), True
    except (*models.FAILURES, RuntimeError, ValueError, KeyError, TypeError) as err:
        msg = printable(failure(err)) if isinstance(err, models.FAILURES) else type(err).__name__
        sys.stderr.write(f"bf: watch: {msg}\n")
        return {**fallback, "time": now_utc(), "built": 0, "jobs_ran": 0, "ok": False}, False


def serve(store: Store, *, json_stream: bool = False, stop_event: threading.Event | None = None) -> dict[str, Any]:
    """Supervise files, events, jobs, and services (`--json` or non-TTY emits `structlog` JSON)."""
    cfg = config.load(store)
    stop_ev = stop_event if stop_event is not None else threading.Event()
    wake_event, ui_event, note_dirty, cfg_dirty = (threading.Event() for _ in range(4))
    reconcile_lock = threading.Lock()
    daemons: dict[str, tuple[ServiceSpec, threading.Event, DaemonStream]] = {}
    is_tty = sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"
    use_tui, emit_json = not json_stream and is_tty, json_stream or not is_tty
    con = Console(no_color=bool(os.environ.get("NO_COLOR", "")), highlight=False, emoji=False)
    live = Live(console=con, auto_refresh=False) if use_tui else None
    last_summary = {"brain": cfg.name, "time": now_utc(), "built": 0, "jobs_ran": 0, "ok": True}
    authored_pfx, root_pfx = tuple(f"{f}/" for f in models.AUTHORED), f"{store.root}/"

    def reconcile_config() -> None:
        nonlocal cfg
        try:
            new_cfg = config.load(store)
        except models.FAILURES as err:
            sys.stderr.write(f"bf: watch: {printable(failure(err))}\n")
            return
        desired = {u.name: u for u in new_cfg.services.values() if u.enabled and u.run}
        with reconcile_lock:
            cfg = new_cfg
            stale = (
                [] if stop_ev.is_set() else [daemons.pop(k) for k in list(daemons) if desired.get(k) != daemons[k][0]]
            )
        for _, u_stop, d_stream in stale:
            u_stop.set()
            d_stream.terminate()
        with reconcile_lock:
            for key, unit in () if stop_ev.is_set() else desired.items():
                if key not in daemons:
                    u_stop = threading.Event()
                    daemons[key] = (unit, u_stop, DaemonStream(store, unit, u_stop, wake_event, ui_event))
                    daemons[key][2].start()

    def emit_line(payload: dict[str, Any]) -> None:
        fields = {k: payload[k] for k in ("brain", "built", "jobs_ran", "ok")}
        sev, tm = "INFO" if payload["ok"] else "ERROR", str(payload["time"])
        with suppress(BrokenPipeError, OSError):
            sys.stdout.buffer.write(models.log_line("watch.tick", severity=sev, time_iso=tm, **fields))
            sys.stdout.flush()

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
        if len(parts) == 2 and parts[0] == ".bf" and (p1 := parts[1]).endswith(".json"):
            return "" if p1.endswith(".context.json") else "ui"
        return ""

    def watch_files() -> None:
        w_kw: dict[str, Any] = {"watch_filter": lambda _, p: bool(_classify(p)), "debounce": cfg.watch.debounce_ms}
        try:
            for changes in watch_fs(store.root, **w_kw, stop_event=stop_ev, raise_interrupt=False):
                if kinds := {_classify(p) for _, p in changes} - {""}:
                    for k, ev in (("note", note_dirty), ("config", cfg_dirty)):
                        if k in kinds:
                            ev.set()
                    if kinds & {"note", "config", "wake"}:
                        wake_event.set()
                    ui_event.set()
        except (*models.FAILURES, OSError, RuntimeError) as err:
            sys.stderr.write(f"bf: watch: {type(err).__name__}\n")

    def run_ticks() -> None:
        nonlocal last_summary
        first = True
        while not stop_ev.is_set():
            wake_event.clear()
            if cfg_dirty.is_set():
                cfg_dirty.clear()
                reconcile_config()
            if dirty := note_dirty.is_set():
                note_dirty.clear()
            last_summary, tick_ok = _safe_tick(
                store, rebuild_notes=first or dirty, fallback=last_summary, stop_event=stop_ev
            )
            if not tick_ok and dirty:
                note_dirty.set()
            if emit_json and (first or last_summary["built"] or last_summary["jobs_ran"] or not last_summary["ok"]):
                emit_line(last_summary)
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
            active_daemons, _ = list(daemons.values()), daemons.clear()
        for _, u_stop, _ in active_daemons:
            u_stop.set()
        if tick_thread is not None and tick_thread.is_alive():
            tick_thread.join(timeout=3)
        if on_main and prev_term is not None:
            with suppress(OSError, ValueError):
                signal.signal(signal.SIGTERM, prev_term)
        if live is not None:
            with suppress(Exception):
                live.stop()
        for _, _, daemon in active_daemons:
            daemon.terminate()
    return last_summary
