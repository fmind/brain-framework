"""Live UNIX supervisor (`bf watch`): zero-dep scrollable ANSI dashboard + text/JSONL stream."""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from collections import deque
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal, cast

from bf import config, engine, loops, models, proc, records, status
from bf.config import Config, LoopSpec, ScheduleSpec
from bf.models import Error, failure, now_utc, printable
from bf.storage import Store, writer

watch_fs: Any = None

_DEBOUNCE_MS = 300
_HEALTHY_SECS = 30.0
_EVENT_RATE_WINDOW = 60.0
_EVENT_RATE_MAX = 10
_SKIP_DIRS = frozenset(
    (".git", "memories", "usages", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".ty_cache", "site", "dist")
)


def _fmt_ms(dur_ms: int) -> str:
    return f"{dur_ms}ms" if dur_ms < 1000 else f"{dur_ms / 1000:.1f}s"


def format_watch_line(event: str, payload: dict[str, Any], *, color: bool = False) -> str:
    ts = printable(str(payload.get("time") or now_utc())[11:19])
    c_ok, c_err, c_cyn, c_dim, c_rst = status.ansi_codes(color)
    if event == "watch.build":
        ok, probs = bool(payload.get("ok", True)), payload.get("problems") or []
        tail = ""
        if not ok and probs:
            first = printable(" ".join(str(probs[0]).split()))[:100]
            tail = f"  {first}" + (f" (+{len(probs) - 1} more; run bf check)" if len(probs) > 1 else "")
        n_c, w_c = printable(str(payload.get("notes", 0))), printable(str(payload.get("written", 0)))
        return f"{c_dim}{ts}{c_rst}  {c_ok if ok else c_err}build  {c_rst}  {n_c} notes ({w_c} built){tail}\n"
    if event == "watch.loop":
        name, st = printable(str(payload.get("loop") or "")), str(payload.get("status") or "")
        dur = _fmt_ms(int(payload.get("duration_ms") or 0))
        sm = payload.get("summary")
        if st == "ok":
            tag = f"{c_ok}ok     {c_rst}"
            detail = status.format_run_detail(sm, compact=True) if isinstance(sm, dict) else ""
        else:
            tag, detail = f"{c_err}fail   {c_rst}", printable(str(payload.get("error") or st))
        return f"{c_dim}{ts}{c_rst}  {tag}  {name}  {detail}  {c_dim}{dur}{c_rst}\n"
    if event == "watch.daemon":
        name, state = printable(str(payload.get("loop") or "")), printable(str(payload.get("state") or ""))
        detail = printable(str(payload.get("error") or ""))
        tag = f"{c_cyn if state in {'started', 'healthy', 'stopped'} else c_err}daemon {c_rst}"
        return f"{c_dim}{ts}{c_rst}  {tag}  {name} {state}{f': {detail}' if detail else ''}\n"
    msg = printable(str(payload.get("error") or payload.get("message") or event))
    return f"{c_dim}{ts}{c_rst}  {c_err}error  {c_rst}  {msg}\n"


class DaemonStream:
    """Supervise one long-running `loops.<name>` with `when: daemon`."""

    def __init__(
        self,
        store: Store,
        unit: LoopSpec,
        stop_event: threading.Event,
        wake_event: threading.Event,
        *,
        base_env: Mapping[str, str] | None = None,
        on_event: Any = None,
    ) -> None:
        self.store, self.unit, self.base_env = store, unit, dict(base_env or {})
        self.stop_event, self.wake_event, self.on_event = stop_event, wake_event, on_event
        self.emit = unit.emit
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

    def _notify(self, state: str, error: str = "") -> None:
        if self.on_event is not None:
            self.on_event(
                "watch.daemon", {"loop": self.unit.name, "state": state, **({"error": error} if error else {})}
            )

    def _record(self, run_id: str, now_iso: str, t0: float, code: int, err: str, stderr: str = "") -> int:
        ms = max(0, int((time.monotonic() - t0) * 1000))
        loops.record_run(
            self.store,
            self.unit.name,
            run_id,
            now_iso,
            ended=datetime.now(UTC),
            dur_ms=ms,
            code=code,
            err_msg=err,
            stderr=stderr,
            trigger="daemon",
        )
        return ms

    def _loop(self) -> None:
        st, u, name = self.store, self.unit, self.unit.name
        backoff, label = 1.0, f"loops.{name}"
        while not self.stop_event.is_set():
            now_dt = datetime.now(UTC)
            now_iso, t0, healthy = now_utc(now_dt), time.monotonic(), False
            run_id = f"{name}-{int(now_dt.timestamp())}-{os.urandom(3).hex()}"
            try:
                cmd, _, _, _, _ = loops.resolve_loop(st, u)
                argv, env, _ = proc.prepare_unit(
                    st, name, cmd, run_id, now_iso, trigger="daemon", base_env=self.base_env, unit_env=u.env
                )
                pipe = subprocess.PIPE
                with subprocess.Popen(  # noqa: S603
                    argv,
                    cwd=st.root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=pipe,
                    stderr=pipe,
                    start_new_session=True,
                ) as child:
                    err_stop = threading.Event()
                    err_t, err_buf = proc.drain_tail(
                        cast("IO[bytes]", child.stderr),
                        65536,
                        err_stop,
                        on_line=records.make_stderr_logger(st, name, run_id),
                    )
                    run_lock, exited = threading.Lock(), threading.Event()

                    def _mark_healthy(
                        c: subprocess.Popen[bytes] = child, lk: threading.Lock = run_lock, ex: threading.Event = exited
                    ) -> None:
                        with lk:
                            if ex.is_set() or self.stop_event.is_set() or c.poll() is not None:
                                return
                            ts = now_utc()
                            with suppress(Error, OSError):
                                loops.mark_unit_state(
                                    st,
                                    name,
                                    status="running",
                                    updated=ts,
                                    last_success=ts,
                                    consecutive_failures=0,
                                    error="",
                                )
                        self._notify("healthy")

                    health_timer = threading.Timer(_HEALTHY_SECS, _mark_healthy)
                    health_timer.daemon = True
                    try:
                        with self._lock:
                            self.proc, stopping = child, self.stop_event.is_set()
                        if stopping:
                            proc.stop_proc(child)
                            continue
                        loops.mark_unit_state(st, name, status="running", updated=now_iso, last_start=now_iso)
                        self._notify("started")
                        health_timer.start()
                        overflow_err, out_s = "", child.stdout
                        while out_s and not self.stop_event.is_set() and (ln := out_s.readline(models.MAX_BATCH + 1)):
                            if len(ln) > models.MAX_BATCH:
                                overflow_err = f"output line exceeds {models.MAX_BATCH} bytes"
                                records.append_loop_stderr(st, name, run_id, overflow_err.encode())
                                proc.stop_proc(child)
                                break
                            if not (stripped := ln.strip()) or not self.emit:
                                continue
                            try:
                                ev_k, payload = records.parse_emitted_event(stripped, self.emit, label)
                                records.append_event(st, ev_k, name, payload, dedupe=True)
                                self.wake_event.set()
                            except Error as err:
                                records.append_loop_stderr(st, name, run_id, printable(failure(err)).encode())
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
                    t_stop = now_utc()
                    with suppress(Error, OSError):
                        st.append_log(
                            name,
                            models.log_line(
                                "loop.stopped",
                                severity="INFO",
                                time_iso=t_stop,
                                loop=name,
                                run=run_id,
                                trigger="daemon",
                            ),
                        )
                    loops.mark_unit_state(st, name, status="stopped", updated=t_stop, last_end=t_stop)
                    self._notify("stopped")
                    break
                rc = (rc or 1) if overflow_err else rc
                tail_err = "" if rc == 0 else models.redact_secrets(err_txt or f"exit code {rc}")[-1024:]
                dur_ms = self._record(run_id, now_iso, t0, rc, overflow_err or tail_err, err_txt)
                self._notify("exited", overflow_err or tail_err)
                healthy = not overflow_err and dur_ms >= int(_HEALTHY_SECS * 1000)
            except models.FAILURES as err:
                msg = models.redact_secrets(printable(failure(err)))
                with suppress(Error, OSError):
                    self._record(run_id, now_iso, t0, 1, msg[:1024])
                self._notify("failed", msg[:256])
            finally:
                with self._lock:
                    self.proc = None
            if self.stop_event.wait(1.0 if healthy else backoff):
                break
            backoff = 1.0 if healthy else min(backoff * 2.0, 30.0)


def next_due_delay(
    store: Store, cfg: Config, now: datetime, *, exclude: frozenset[str] = frozenset(), max_wait: float = 60.0
) -> float:
    """Compute seconds until the earliest next scheduled loop or backoff expiry, clamped to `[0.5, max_wait]`."""
    soonest = max_wait
    for name, loop in cfg.loops.items():
        if not loop.enabled or loop.daemon or name in exclude or not (loop.schedules or loop.events):
            continue
        try:
            state_doc = loops.load_unit_state(store, name)
        except Error:
            continue
        if (rem := config.next_schedule_delay(loop, state_doc, now, tz_name=cfg.timezone)) is not None:
            soonest = min(soonest, rem)
    return max(0.5, min(soonest, max_wait))


def serve(
    store: Store,
    *,
    fmt: Literal["auto", "tui", "text", "jsonl"] = "auto",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Supervise notes, events, daemon loops, and concurrent cycle loops with TUI, text, or JSONL output."""
    cfg = config.load(store)
    busy = "another bf watch is already running for this brain; stop it before starting a new one"
    with writer(store, wait=0.0, busy=busy, name="watch.lock"):
        return _serve_locked(store, cfg, fmt=fmt, stop_event=stop_event)


def _serve_locked(
    store: Store,
    cfg: config.Config,
    *,
    fmt: Literal["auto", "tui", "text", "jsonl"] = "auto",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    stop_ev = stop_event if stop_event is not None else threading.Event()
    wake_event, note_dirty, cfg_dirty = threading.Event(), threading.Event(), threading.Event()
    ui_dirty, counts_dirty = threading.Event(), threading.Event()
    reconcile_lock, flight_lock, rate_lock, emit_lock = (threading.Lock() for _ in range(4))
    daemons, in_flight, event_rates, recent = {}, set(), {}, deque(maxlen=8)
    is_tty, term_dumb = sys.stdout.isatty(), os.environ.get("TERM", "") == "dumb"
    use_tui = fmt == "tui" or (fmt == "auto" and is_tty and sys.stdin.isatty() and not term_dumb)
    use_text = fmt == "text"
    use_color = (use_tui or use_text) and is_tty and not term_dumb and not os.environ.get("NO_COLOR", "")
    last_summary = {"brain": cfg.name, "time": now_utc(), "built": 0, "loops_ran": 0, "ok": True}
    root_pfx, term_hit = f"{store.root}/", False

    def _stop_all() -> None:
        stop_ev.set()
        wake_event.set()

    def write_out(data: bytes) -> None:
        with emit_lock:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            except BrokenPipeError:
                with suppress(OSError):
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, 1)
                    os.close(devnull)
                _stop_all()
            except OSError:
                pass

    def emit_event(kind: str, payload: dict[str, Any]) -> None:
        ts, body = str(payload.get("time") or now_utc()), {"brain": cfg.name, **payload}
        if use_tui:
            line = format_watch_line(kind, {"time": ts, **body}, color=use_color).rstrip("\n")
            with emit_lock:
                recent.append(line)
            if kind == "watch.build" or (isinstance(sm := payload.get("summary"), dict) and "records" in sm):
                counts_dirty.set()
            ui_dirty.set()
        elif use_text:
            write_out(format_watch_line(kind, {"time": ts, **body}, color=use_color).encode("utf-8"))
        else:
            is_err = body.get("ok") is False or body.get("status") == "failed" or kind == "watch.error"
            write_out(models.log_line(kind, severity="ERROR" if is_err else "INFO", time_iso=ts, **body))

    def _allow_event_trigger(name: str) -> bool:
        now_m = time.monotonic()
        with rate_lock:
            q = event_rates.setdefault(name, deque())
            while q and now_m - q[0] > _EVENT_RATE_WINDOW:
                q.popleft()
            if len(q) >= _EVENT_RATE_MAX:
                return False
            q.append(now_m)
            return True

    def _daemon_sig(unit: LoopSpec, base_env: Mapping[str, str], dotenv_sig: str) -> object:
        resolved_argv: tuple[str, ...] = ()
        scripts: list[tuple[str, str]] = []
        with suppress(Error, OSError):
            resolved_argv, _, _, _, _ = loops.resolve_loop(store, unit)
            for tok in resolved_argv:
                cleaned = tok.removeprefix("{{brain}}/").removeprefix(f"{store.root}/")
                while cleaned.startswith("./"):
                    cleaned = cleaned[2:]
                if cleaned.startswith(("sensors/", "routines/", "agents/")):
                    sig = models.digest(store.read(cleaned, models.MAX_NOTE)) if store.exists(cleaned) else "invalid"
                    scripts.append((cleaned, sig))
        return (unit, models.digest(models.encode(dict(base_env))), dotenv_sig, resolved_argv, tuple(scripts))

    def reconcile_config() -> None:
        nonlocal cfg
        prev_key = (cfg.name, cfg.notes)
        try:
            new_cfg = config.load(store)
            dotenv_sig = models.digest(models.encode(config.load_dotenv(store)))
        except models.FAILURES as err:
            emit_event("watch.error", {"ok": False, "error": printable(failure(err))})
            return
        if (new_cfg.name, new_cfg.notes) != prev_key:
            note_dirty.set()
        counts_dirty.set()
        ui_dirty.set()
        desired = {
            u.name: (_daemon_sig(u, new_cfg.env, dotenv_sig), u, new_cfg.env)
            for u in new_cfg.loops.values()
            if u.enabled and u.daemon
        }
        with reconcile_lock:
            cfg = new_cfg
            stale = (
                []
                if stop_ev.is_set()
                else [daemons.pop(k) for k in list(daemons) if k not in desired or desired[k][0] != daemons[k][0]]
            )
        for _, u_stop, d_stream in stale:
            u_stop.set()
            d_stream.terminate()
        with reconcile_lock:
            for key, (sig, unit, base_env) in () if stop_ev.is_set() else desired.items():
                if key not in daemons:
                    u_stop = threading.Event()
                    stream = DaemonStream(store, unit, u_stop, wake_event, base_env=base_env, on_event=emit_event)
                    daemons[key] = (sig, u_stop, stream)
                    stream.start()

    def _classify(raw_path: str) -> str:
        rel = raw_path[len(root_pfx) :] if raw_path.startswith(root_pfx) else ""
        if not rel and raw_path != store.root.as_posix():
            with suppress(OSError, ValueError):
                rel = Path(raw_path).resolve().relative_to(store.root).as_posix()
        parts = rel.split("/") if rel else []
        if not parts or _SKIP_DIRS.intersection(parts) or any(p.startswith((".write-", ".#")) for p in parts):
            return ""
        if (parts[0] in cfg.notes and rel.endswith(".md")) or (parts[0] == "schemas" and rel.endswith(".json")):
            return "note"
        if rel in {"bf.yaml", ".env"} or parts[0] in {"sensors", "routines", "agents"}:
            return "config"
        if rel == "logs/events.jsonl":
            return "wake"
        return ""

    def watch_files() -> None:
        if (wf := watch_fs) is None:
            from watchfiles import watch as wf
        try:
            wf_kw: dict[str, Any] = {"watch_filter": lambda _, p: bool(_classify(p)), "debounce": _DEBOUNCE_MS}
            for changes in wf(store.root, **wf_kw, stop_event=stop_ev, raise_interrupt=False):
                if kinds := {_classify(p) for _, p in changes} - {""}:
                    for k, ev in (("note", note_dirty), ("config", cfg_dirty)):
                        if k in kinds:
                            ev.set()
                    wake_event.set()
        except (*models.FAILURES, RuntimeError) as err:
            emit_event("watch.error", {"ok": False, "error": type(err).__name__})

    pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="bf-worker")

    def _record_summary(brain: str, *, built: int = 0, loops_ran: int = 0, ok: bool = True) -> None:
        nonlocal last_summary
        with emit_lock:
            last_summary = {"brain": brain, "time": now_utc(), "built": built, "loops_ran": loops_ran, "ok": ok}

    def _run_bg(
        item: tuple[LoopSpec, str, list[dict[str, Any]], int | None, ScheduleSpec | None], base_env: Mapping[str, str]
    ) -> None:
        loop, trig, evs, seq_target, sched = item
        try:
            try:
                res = loops.execute_one_loop(
                    store,
                    loop,
                    trigger=trig,
                    events=evs,
                    cursor_target_seq=seq_target,
                    schedule=sched,
                    stop_event=stop_ev,
                    base_env=base_env,
                )
            except models.FAILURES as err:
                res = loops.record_failure(store, loop.name, trig, err)
            ok_r = res["status"] in {"ok", "dry_run", "stopped"}
            _record_summary(cfg.name, loops_ran=1, ok=ok_r)
            emit_event("watch.loop", {**res, "ok": ok_r, "trigger": trig})
        finally:
            with flight_lock:
                in_flight.discard(loop.name)
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
            tick_ok, wait_secs = True, 5.0
            try:
                cur_cfg = cfg
                built = engine.build(store) if (first or dirty) else None
                with flight_lock:
                    busy = frozenset(in_flight)
                due_items, pre_fails = loops.select_due_loops(store, cur_cfg, sched_now=datetime.now(UTC), exclude=busy)
                for pf in pre_fails:
                    emit_event("watch.loop", {**pf, "ok": False})
                rate_limited: set[str] = set()
                with flight_lock:
                    slots = max(0, cur_cfg.concurrency - len(in_flight))
                    to_run: list[tuple[LoopSpec, str, list[dict[str, Any]], int | None, ScheduleSpec | None]] = []
                    for it in due_items:
                        if len(to_run) >= slots:
                            break
                        if it[0].name in in_flight:
                            continue
                        if it[1] == "event" and not _allow_event_trigger(it[0].name):
                            rate_limited.add(it[0].name)
                            err_d = {
                                "ok": False,
                                "loop": it[0].name,
                                "error": "event rate limit reached; pausing triggers",
                            }
                            emit_event("watch.error", err_d)
                            continue
                        in_flight.add(it[0].name)
                        to_run.append(it)
                    busy_after = frozenset(in_flight) | rate_limited
                if to_run:
                    ui_dirty.set()
                for idx_i, it in enumerate(to_run):
                    try:
                        if stop_ev.is_set():
                            raise StopIteration
                        pool.submit(_run_bg, it, cur_cfg.env)
                    except BaseException as exc:
                        with flight_lock:
                            for rem_it in to_run[idx_i:]:
                                in_flight.discard(rem_it[0].name)
                        if isinstance(exc, StopIteration):
                            break
                        raise
                nxt = next_due_delay(store, cur_cfg, datetime.now(UTC), exclude=busy_after)
                wait_secs = 5.0 if slots == 0 and due_items else nxt
                b_cnt = built["written"] if built else 0
                ok_t = (built is None or bool(built["ok"])) and not pre_fails
                _record_summary(cur_cfg.name, built=b_cnt, loops_ran=len(pre_fails), ok=ok_t)
                if built is not None and (b_cnt or not built["ok"]):
                    b_ev = {"ok": built["ok"], "notes": built["notes"], "written": b_cnt, "problems": built["problems"]}
                    emit_event("watch.build", b_ev)
            except (*models.FAILURES, RuntimeError, ValueError, KeyError, TypeError) as err:
                tick_ok = False
                msg = printable(failure(err)) if isinstance(err, models.FAILURES) else type(err).__name__
                with emit_lock:
                    base_b = str(last_summary["brain"])
                _record_summary(base_b, ok=False)
                emit_event("watch.error", {"ok": False, "error": msg})
            if not tick_ok and dirty:
                note_dirty.set()
            first = False if tick_ok else first
            if not stop_ev.is_set():
                wake_event.wait(timeout=wait_secs)

    tick_thread: threading.Thread | None = None
    on_main = threading.current_thread() is threading.main_thread()

    def _on_term_sig(*_a: object) -> None:
        nonlocal term_hit
        term_hit = True

    def _run_tui() -> None:
        offset, page, only_active, sort_recent, show_errors = 0, 10, False, True, False
        last_draw, last_fail, winch_hit, alt_on = 0.0, -10.0, False, False
        counts_cache: tuple[dict[str, int], list[str]] | None = None
        term_fd: int | None = None
        read_fd: int | None = None
        old_term: Any = None
        prev_winch: Any = None
        poller: select.poll | None = None

        def _on_winch(*_a: object) -> None:
            nonlocal winch_hit
            winch_hit = True

        try:
            if on_main:
                prev_winch = signal.signal(signal.SIGWINCH, _on_winch)
            if sys.stdin.isatty():
                with suppress(OSError, ValueError, termios.error):
                    fd = sys.stdin.fileno()
                    old_term, term_fd = termios.tcgetattr(fd), fd
                    tty.setcbreak(fd)
                    poller = select.poll()
                    poller.register(fd, select.POLLIN)
                    read_fd = fd
            alt_on = True
            write_out(b"\033[?1049h\033[?25l\033[?1000h\033[?1006h")
            counts_dirty.set(), ui_dirty.set()
            while not stop_ev.is_set():
                if term_hit:
                    _stop_all()
                    break
                now_m = time.monotonic()
                if winch_hit or ui_dirty.is_set() or (now_m - last_draw) >= 1.0:
                    winch_hit = False
                    ui_dirty.clear()
                    cur_cfg = cfg
                    if counts_dirty.is_set() or (counts_cache is None and (now_m - last_fail) >= 10.0):
                        counts_dirty.clear()
                        try:
                            counts_cache = status.corpus_counts(store, cur_cfg)
                        except Error, OSError:
                            last_fail = now_m
                    z_c = {"notes": 0, "records": 0, "usages": 0, "sources": 0, "loops": len(cur_cfg.loops)}
                    with suppress(Error, OSError):
                        sm = status.status_summary(store, all_units=True, counts_cache=counts_cache or (z_c, []))
                        with emit_lock:
                            rec_snap = list(recent)
                        cols, lines = shutil.get_terminal_size((100, 30))
                        frame, offset, page = status.format_watch_screen(
                            sm,
                            rec_snap,
                            cols=cols,
                            rows=lines,
                            offset=offset,
                            only_active=only_active,
                            sort_recent=sort_recent,
                            show_errors=show_errors,
                            concurrency=cur_cfg.concurrency,
                            color=use_color,
                        )
                        write_out(b"\033[H" + frame.encode("utf-8") + b"\033[J")
                    last_draw = now_m
                if read_fd is not None and poller is not None:
                    try:
                        if evs := poller.poll(150):
                            mask = evs[0][1]
                            bad_ev = bool(mask & (select.POLLERR | select.POLLNVAL)) or (
                                bool(mask & select.POLLHUP) and not (mask & select.POLLIN)
                            )
                            if bad_ev or (key := status.read_tui_key(read_fd)) == "eof":
                                read_fd = None
                            elif key:
                                offset, only_active, sort_recent, show_errors, act = status.apply_tui_key(
                                    key, offset, page, only_active, sort_recent, show_errors
                                )
                                if act == "quit":
                                    _stop_all()
                                elif act == "refresh":
                                    counts_dirty.set(), wake_event.set()
                                ui_dirty.set()
                    except OSError, ValueError:
                        read_fd = None
                else:
                    stop_ev.wait(0.15)
        finally:
            if alt_on:
                write_out(b"\033[?1006l\033[?1000l\033[?25h\033[?1049l")
            if term_fd is not None and old_term is not None:
                with suppress(OSError, termios.error):
                    termios.tcsetattr(term_fd, termios.TCSADRAIN, old_term)
            if on_main and prev_winch is not None:
                with suppress(OSError, ValueError):
                    signal.signal(signal.SIGWINCH, prev_winch)

    prev_term = signal.signal(signal.SIGTERM, _on_term_sig) if on_main else None
    try:
        if use_text:
            with suppress(Error):
                st_txt = status.format_status_text(status.status_summary(store, all_units=True), color=use_color)
                write_out(f"{st_txt}Watching {cfg.name} ({cfg.concurrency}w) — Ctrl+C to stop\n".encode())
        elif not use_tui:
            emit_event("watch.started", {"ok": True, "concurrency": cfg.concurrency, "loops": len(cfg.loops)})
        reconcile_config()
        threading.Thread(target=watch_files, name="bf-watchfiles", daemon=True).start()
        tick_thread = threading.Thread(target=run_ticks, name="bf-tick", daemon=True)
        tick_thread.start()
        if use_tui:
            _run_tui()
        else:
            while not stop_ev.is_set():
                if term_hit:
                    _stop_all()
                    break
                stop_ev.wait(0.2)
    finally:
        _stop_all()
        with reconcile_lock:
            active_daemons = list(daemons.values())
            daemons.clear()
        for _, u_stop, _ in active_daemons:
            u_stop.set()
        if tick_thread is not None and tick_thread.is_alive():
            tick_thread.join(timeout=3)
        pool.shutdown(wait=True, cancel_futures=True)
        with suppress(OSError, ValueError):
            if on_main and prev_term is not None:
                signal.signal(signal.SIGTERM, prev_term)
        for _, _, daemon in active_daemons:
            daemon.terminate()
    return last_summary
