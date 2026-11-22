"""Live UNIX supervisor (`bf watch`): startup bird's-eye status + real-time text/JSONL stream, zero TUI deps."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
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
    ts = str(payload.get("time") or now_utc())[11:19]
    c_ok, c_err, c_dim, c_cyn, c_rst = (
        ("\033[32m", "\033[31;1m", "\033[2m", "\033[36m", "\033[0m") if color else ("", "", "", "", "")
    )
    if event == "watch.build":
        ok = bool(payload.get("ok", True))
        tag = f"{c_ok}build  {c_rst}" if ok else f"{c_err}build  {c_rst}"
        return f"{c_dim}{ts}{c_rst}  {tag}  {payload.get('notes', 0)} notes ({payload.get('written', 0)} built)\n"
    if event == "watch.loop":
        name = printable(str(payload.get("loop") or ""))
        st = str(payload.get("status") or "")
        dur = _fmt_ms(int(payload.get("duration_ms") or 0))
        if st == "ok":
            detail = ""
            if isinstance(sm := payload.get("summary"), dict):
                if "records" in sm:
                    diffs = " ".join(
                        f"{s}{v}" for s, k in (("+", "added"), ("~", "updated"), ("-", "removed")) if (v := sm.get(k))
                    )
                    detail = f"{diffs} · {sm['records']} records" if diffs else f"{sm['records']} unchanged"
                elif "emitted" in sm:
                    detail = f"{sm['emitted']} emitted"
            return f"{c_dim}{ts}{c_rst}  {c_ok}ok     {c_rst}  {name}  {detail}  {c_dim}{dur}{c_rst}\n"
        err = printable(str(payload.get("error") or st))
        return f"{c_dim}{ts}{c_rst}  {c_err}fail   {c_rst}  {name}  {err}  {c_dim}{dur}{c_rst}\n"
    if event == "watch.daemon":
        name = printable(str(payload.get("loop") or ""))
        state = str(payload.get("state") or "")
        detail = printable(str(payload.get("error") or ""))
        tag = f"{c_cyn}daemon {c_rst}" if state in {"started", "healthy", "stopped"} else f"{c_err}daemon {c_rst}"
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

    def _mark(self, status_val: str, ts_iso: str, **extra: str) -> None:
        loops.mark_unit_state(self.store, self.unit.name, status=status_val, updated=ts_iso, **extra)

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
        backoff, label = 1.0, f"loops.{self.unit.name}"
        while not self.stop_event.is_set():
            now_dt = datetime.now(UTC)
            now_iso, t0, healthy = now_utc(now_dt), time.monotonic(), False
            run_id = f"{self.unit.name}-{int(now_dt.timestamp())}-{os.urandom(3).hex()}"
            try:
                cmd, _, _, _, _ = loops.resolve_loop(self.store, self.unit)
                argv, env, _ = loops.prepare_unit(
                    self.store,
                    self.unit.name,
                    cmd,
                    run_id,
                    now_iso,
                    trigger="daemon",
                    base_env=self.base_env,
                    unit_env=self.unit.env,
                )
                with subprocess.Popen(  # noqa: S603
                    argv,
                    cwd=self.store.root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                ) as child:
                    err_stop = threading.Event()
                    err_t, err_buf = proc.drain_tail(
                        cast("IO[bytes]", child.stderr),
                        65536,
                        err_stop,
                        on_line=records.make_stderr_logger(self.store, self.unit.name, run_id),
                    )
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
                        self._notify("healthy")

                    health_timer = threading.Timer(_HEALTHY_SECS, _mark_healthy)
                    health_timer.daemon = True
                    try:
                        with self._lock:
                            self.proc, stopping = child, self.stop_event.is_set()
                        if stopping:
                            proc.stop_proc(child)
                            continue
                        self._mark("running", now_iso, last_start=now_iso)
                        self._notify("started")
                        health_timer.start()
                        overflow_err, out_s = "", child.stdout
                        while out_s and not self.stop_event.is_set() and (ln := out_s.readline(models.MAX_BATCH + 1)):
                            if len(ln) > models.MAX_BATCH:
                                overflow_err = f"output line exceeds {models.MAX_BATCH} bytes"
                                records.append_loop_stderr(self.store, self.unit.name, run_id, overflow_err.encode())
                                proc.stop_proc(child)
                                break
                            if not (stripped := ln.strip()) or not self.emit:
                                continue
                            try:
                                ev_k, payload = loops.parse_emitted_event(stripped, self.emit, label)
                                records.append_event(self.store, ev_k, self.unit.name, payload, dedupe=True)
                                self.wake_event.set()
                            except Error as err:
                                msg = printable(failure(err))
                                records.append_loop_stderr(self.store, self.unit.name, run_id, msg.encode())
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
                        self.store.append_log(
                            self.unit.name,
                            models.log_line(
                                "loop.stopped",
                                severity="INFO",
                                time_iso=t_stop,
                                loop=self.unit.name,
                                run=run_id,
                                trigger="daemon",
                            ),
                        )
                    self._mark("stopped", t_stop, last_end=t_stop)
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
    fmt: Literal["auto", "text", "jsonl"] = "auto",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Supervise notes, events, daemon loops, and concurrent cycle loops with text or JSONL streaming."""
    cfg = config.load(store)
    busy = "another bf watch is already running for this brain; stop it before starting a new one"
    with writer(store, wait=0.0, busy=busy, name="watch.lock"):
        return _serve_locked(store, cfg, fmt=fmt, stop_event=stop_event)


def _serve_locked(
    store: Store,
    cfg: config.Config,
    *,
    fmt: Literal["auto", "text", "jsonl"] = "auto",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    stop_ev = stop_event if stop_event is not None else threading.Event()
    wake_event, note_dirty, cfg_dirty = threading.Event(), threading.Event(), threading.Event()
    reconcile_lock, daemons = threading.Lock(), {}
    flight_lock, in_flight = threading.Lock(), set()
    rate_lock = threading.Lock()
    event_rates: dict[str, deque[float]] = {}
    emit_lock = threading.Lock()
    is_tty = sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"
    use_text = (fmt == "text") or (fmt == "auto" and is_tty)
    use_color = use_text and is_tty and not os.environ.get("NO_COLOR", "")
    last_summary = {"brain": cfg.name, "time": now_utc(), "built": 0, "loops_ran": 0, "ok": True}
    root_pfx = f"{store.root}/"

    def write_out(data: bytes) -> None:
        with emit_lock:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            except BrokenPipeError:
                stop_ev.set(), wake_event.set()
            except OSError:
                pass

    def emit_event(kind: str, payload: dict[str, Any]) -> None:
        ts = str(payload.get("time") or now_utc())
        body = {"brain": cfg.name, **payload}
        if use_text:
            write_out(format_watch_line(kind, {"time": ts, **body}, color=use_color).encode("utf-8"))
        else:
            sev = (
                "ERROR"
                if (body.get("ok") is False or body.get("status") == "failed" or kind == "watch.error")
                else "INFO"
            )
            write_out(models.log_line(kind, severity=sev, time_iso=ts, **body))

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

    def reconcile_config() -> None:
        nonlocal cfg
        prev_key = (cfg.name, cfg.notes)
        try:
            new_cfg = config.load(store)
        except models.FAILURES as err:
            emit_event("watch.error", {"ok": False, "error": printable(failure(err))})
            return
        if (new_cfg.name, new_cfg.notes) != prev_key:
            note_dirty.set()
        desired = {u.name: (u, new_cfg.env) for u in new_cfg.loops.values() if u.enabled and u.daemon}
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
                    stream = DaemonStream(store, unit, u_stop, wake_event, base_env=base_env, on_event=emit_event)
                    daemons[key] = (spec_pair, u_stop, stream)
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
        if rel == "bf.yaml":
            return "config"
        if rel == "logs/events.jsonl":
            return "wake"
        return ""

    def watch_files() -> None:
        if (wf := watch_fs) is None:
            from watchfiles import watch as wf
        try:
            for changes in wf(
                store.root,
                watch_filter=lambda _, p: bool(_classify(p)),
                debounce=_DEBOUNCE_MS,
                stop_event=stop_ev,
                raise_interrupt=False,
            ):
                if kinds := {_classify(p) for _, p in changes} - {""}:
                    for k, ev in (("note", note_dirty), ("config", cfg_dirty)):
                        if k in kinds:
                            ev.set()
                    wake_event.set()
        except (*models.FAILURES, RuntimeError) as err:
            emit_event("watch.error", {"ok": False, "error": type(err).__name__})

    pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="bf-worker")

    def _record_summary(summary: dict[str, Any]) -> None:
        nonlocal last_summary
        with emit_lock:
            last_summary = summary

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
            _record_summary({"brain": cfg.name, "time": now_utc(), "built": 0, "loops_ran": 1, "ok": ok_r})
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
            tick_ok = True
            wait_secs = 5.0
            try:
                cur_cfg = cfg
                built = engine.build(store) if (first or dirty) else None
                with flight_lock:
                    busy = frozenset(in_flight)
                now_dt = datetime.now(UTC)
                due_items, pre_fails = loops.select_due_loops(store, cur_cfg, sched_now=now_dt, exclude=busy)
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
                            emit_event(
                                "watch.error",
                                {
                                    "ok": False,
                                    "loop": it[0].name,
                                    "error": "event rate limit reached; pausing triggers",
                                },
                            )
                            continue
                        in_flight.add(it[0].name)
                        to_run.append(it)
                    busy_after = frozenset(in_flight) | rate_limited
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
                wait_secs = (
                    5.0
                    if slots == 0 and due_items
                    else next_due_delay(store, cur_cfg, datetime.now(UTC), exclude=busy_after)
                )
                b_cnt = built["written"] if built else 0
                ok_t = (built is None or bool(built["ok"])) and not pre_fails
                summary = {
                    "brain": cur_cfg.name,
                    "time": now_utc(),
                    "built": b_cnt,
                    "loops_ran": len(pre_fails),
                    "ok": ok_t,
                }
                _record_summary(summary)
                if built is not None and (b_cnt or not built["ok"]):
                    emit_event(
                        "watch.build",
                        {"ok": built["ok"], "notes": built["notes"], "written": b_cnt, "problems": built["problems"]},
                    )
            except (*models.FAILURES, RuntimeError, ValueError, KeyError, TypeError) as err:
                tick_ok = False
                msg = printable(failure(err)) if isinstance(err, models.FAILURES) else type(err).__name__
                with emit_lock:
                    base_b = last_summary["brain"]
                _record_summary({"brain": base_b, "time": now_utc(), "built": 0, "loops_ran": 0, "ok": False})
                emit_event("watch.error", {"ok": False, "error": msg})
            if not tick_ok and dirty:
                note_dirty.set()
            first = False if tick_ok else first
            if not stop_ev.is_set():
                wake_event.wait(timeout=wait_secs)

    tick_thread: threading.Thread | None = None
    on_main = threading.current_thread() is threading.main_thread()

    def _on_term(*_a: object) -> None:
        stop_ev.set()
        wake_event.set()

    prev_term = signal.signal(signal.SIGTERM, _on_term) if on_main else None
    try:
        if use_text:
            with suppress(Error):
                write_out(
                    status.format_status_text(status.status_summary(store, all_units=True), color=use_color).encode()
                )
                write_out(f"Watching {cfg.name} ({cfg.concurrency}w) — Ctrl+C to stop\n".encode())
        else:
            emit_event("watch.started", {"ok": True, "concurrency": cfg.concurrency, "loops": len(cfg.loops)})
        reconcile_config()
        threading.Thread(target=watch_files, name="bf-watchfiles", daemon=True).start()
        tick_thread = threading.Thread(target=run_ticks, name="bf-tick", daemon=True)
        tick_thread.start()
        stop_ev.wait()
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
        for _, _, daemon in active_daemons:
            daemon.terminate()
    return last_summary
