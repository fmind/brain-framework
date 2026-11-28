"""UNIX supervisor (`bf watch`): zero-dep stdlib polling watcher, live/text/jsonl output, daemon loops, note rebuilds, and cycle loops."""

from __future__ import annotations

import os
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Generator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal

from bf import config, cron, engine, loops, markdown, models, proc, render, state, status
from bf.config import Config, LoopSpec
from bf.cron import ScheduleSpec
from bf.models import BusyError, Error, failure, now_utc, printable, redact_secrets
from bf.storage import Store, loop_lock, private_dir, writer

type _EventCallback = Callable[[str, dict[str, Any]], None]

_POLL_INTERVAL = 0.5
_BUSY_COOLDOWN = 5.0
_HEALTHY_SECS = 30.0
_DAEMON_MAX_BACKOFF = 300.0
_EVENT_RATE_WINDOW = 60.0
_EVENT_RATE_MAX = 10
_CORPUS_CACHE_TTL = 15.0
_LIVE_REFRESH_SECS = 1.0


class DaemonStream:
    """Supervise one long-running `loops.<name>` with `when: daemon` under `.bf/locks/loop-<name>.lock`."""

    def __init__(
        self,
        store: Store,
        unit: LoopSpec,
        stop_event: threading.Event,
        wake_event: threading.Event,
        *,
        base_env: Mapping[str, str] | None = None,
        on_event: _EventCallback | None = None,
    ) -> None:
        self.store, self.unit = store, unit
        self.base_env = dict(base_env or {})
        self.stop_event, self.wake_event, self.on_event = stop_event, wake_event, on_event
        self.emit = unit.emit
        self._lock, self.proc = threading.Lock(), None
        self.thread = threading.Thread(target=self._loop, name=f"bf-loop-{unit.name}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def signal_stop(self) -> None:
        """Send SIGTERM to the active daemon process group without blocking."""
        with self._lock:
            child = self.proc
        if child is not None and child.poll() is None:
            with suppress(OSError):
                os.killpg(child.pid, signal.SIGTERM)

    def terminate(self) -> None:
        with self._lock:
            child = self.proc
        if child is not None and child.poll() is None:
            proc.stop_proc(child)
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=6)

    def _notify(self, state_lbl: str, error: str = "") -> None:
        if self.on_event is not None:
            ok = state_lbl in {"started", "healthy", "stopped"} and not error
            self.on_event(
                "watch.daemon",
                {"loop": self.unit.name, "state": state_lbl, "ok": ok, **({"error": error} if error else {})},
            )

    def _record(
        self, run_id: str, now_iso: str, t0: float, code: int, err: str, stderr: str = "", secrets: Sequence[str] = ()
    ) -> int:
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
            emit_loop_event=True,
            trigger="daemon",
            secrets=secrets,
        )
        return ms

    def _loop(self) -> None:
        st, u, name = self.store, self.unit, self.unit.name
        backoff, label = 1.0, f"loops.{name}"
        while not self.stop_event.is_set():
            now_dt = datetime.now(UTC)
            now_iso, t0, healthy = now_utc(now_dt), time.monotonic(), False
            run_id = f"{name}-{int(now_dt.timestamp())}-{os.urandom(3).hex()}"
            secret_vals: tuple[str, ...] = ()
            try:
                cmd, _, _, _, _ = config.resolve_loop(st, u)
                argv, env, _, secret_vals = proc.prepare_unit(
                    st,
                    name,
                    cmd,
                    run_id,
                    now_iso,
                    trigger="daemon",
                    base_env=self.base_env,
                    unit_env=u.env,
                    unit_env_from=u.env_from,
                )
                pipe = subprocess.PIPE
                with loop_lock(st, name, wait=1.0):
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
                        err_t: threading.Thread | None = None
                        err_snap: Callable[[], str] | None = None
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
                                    state.mark_unit_state(
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
                            if child.stderr is not None:
                                err_t, err_snap = proc.drain_tail(
                                    child.stderr,
                                    65536,
                                    err_stop,
                                    on_line=state.make_stderr_logger(st, name, run_id, secrets=secret_vals),
                                    secrets=secret_vals,
                                )
                            with self._lock:
                                self.proc, stopping = child, self.stop_event.is_set()
                            if stopping:
                                proc.stop_proc(child)
                                continue
                            state.mark_unit_state(st, name, status="running", updated=now_iso, last_start=now_iso)
                            self._notify("started")
                            health_timer.start()
                            overflow_err, out_s = "", child.stdout
                            while (
                                out_s and not self.stop_event.is_set() and (ln := out_s.readline(models.MAX_BATCH + 1))
                            ):
                                if len(ln) > models.MAX_BATCH:
                                    overflow_err = f"output line exceeds {models.MAX_BATCH} bytes"
                                    state.append_loop_stderr(
                                        st, name, run_id, overflow_err.encode(), secrets=secret_vals
                                    )
                                    proc.stop_proc(child)
                                    break
                                if not (stripped := ln.strip()) or not self.emit:
                                    continue
                                try:
                                    ev_k, payload = state.parse_emitted_event(stripped, self.emit, label)
                                    state.append_event(st, ev_k, name, payload, dedupe=True, secrets=secret_vals)
                                    self.wake_event.set()
                                except Error as err:
                                    state.append_loop_stderr(
                                        st,
                                        name,
                                        run_id,
                                        redact_secrets(printable(failure(err)), secrets=secret_vals).encode(),
                                    )
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
                            if err_t is not None:
                                err_t.join(timeout=2)
                        err_txt = err_snap() if err_snap is not None else ""
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
                            state.mark_unit_state(st, name, status="stopped", updated=t_stop, last_end=t_stop)
                        self._notify("stopped")
                        break
                    rc = (rc or 1) if overflow_err else rc
                    tail_err = (
                        "" if rc == 0 else redact_secrets(err_txt or f"exit code {rc}", secrets=secret_vals)[-1024:]
                    )
                    dur_ms = self._record(
                        run_id, now_iso, t0, rc, overflow_err or tail_err, err_txt, secrets=secret_vals
                    )
                    self._notify("exited", overflow_err or tail_err)
                    healthy = not overflow_err and dur_ms >= int(_HEALTHY_SECS * 1000)
            except models.FAILURES as err:
                msg = redact_secrets(printable(failure(err)), secrets=secret_vals)
                with suppress(Error, OSError):
                    self._record(run_id, now_iso, t0, 1, msg[:1024], secrets=secret_vals)
                self._notify("failed", msg[:256])
            finally:
                with self._lock:
                    self.proc = None
            wait_s = 1.0 if healthy else backoff * (0.85 + 0.15 * random.random())  # noqa: S311 - retry jitter
            if self.stop_event.wait(wait_s):
                break
            backoff = 1.0 if healthy else min(backoff * 2.0, _DAEMON_MAX_BACKOFF)


def next_due_delay(
    store: Store,
    cfg: Config,
    now: datetime,
    *,
    exclude: frozenset[str] = frozenset(),
    max_wait: float = 60.0,
    snapshot: loops.DueSnapshot | None = None,
) -> float:
    """Compute seconds until the earliest next schedule, delayed timer, debounce, event backoff, or pause expiry."""
    soonest = max_wait
    if (tmr_rem := state.next_timer_delay(store, now)) is not None:
        soonest = min(soonest, tmr_rem)
    latest_seq: int | None = snapshot.latest_seq if snapshot is not None else None
    cached_events: tuple[list[dict[str, Any]], int, int] | None = snapshot.events if snapshot is not None else None
    active_loops: list[tuple[str, LoopSpec, dict[str, Any]]] = []
    for name, loop in cfg.loops.items():
        if not loop.enabled or name in exclude:
            continue
        if loop.events and latest_seq is None:
            _, latest_seq = state.event_bounds(store)
        if snapshot is not None and name in snapshot.states:
            state_doc = snapshot.states[name]
        else:
            try:
                state_doc = state.load_unit_state(store, name, default_event_seq=latest_seq if loop.events else 0)
            except Error:
                continue
        if state.is_paused(state_doc, now):
            if (p_rem := state.pause_remaining(state_doc, now)) is not None:
                soonest = min(soonest, p_rem)
            continue
        if loop.daemon or not (loop.schedules or loop.events):
            continue
        active_loops.append((name, loop, state_doc))

    if latest_seq is not None and latest_seq > 0:
        lag_floors = [
            0 if cur_seq > latest_seq else cur_seq
            for _, lp, doc in active_loops
            if lp.events and (cur_seq := state.int_field(doc, "event_seq")) != latest_seq
        ]
        if lag_floors:
            need_floor = min(lag_floors)
            if cached_events is None or (snapshot is not None and need_floor < snapshot.cached_floor):
                cached_events = state.read_events_meta(store, after_seq=need_floor)

    for name, loop, state_doc in active_loops:
        if (rem := cron.next_schedule_delay(loop, state_doc, now, tz_name=cfg.timezone)) is not None:
            soonest = min(soonest, rem)
        if loop.events:
            assert latest_seq is not None  # noqa: S101
            cur_seq = state.int_field(state_doc, "event_seq")
            if cur_seq != latest_seq and latest_seq > 0:
                matched_evs, _ = loops.pending_events(
                    store, cur_seq, loop.events, self_name=name, events_data=cached_events
                )
                if matched_evs:
                    if (ev_bo := cron.event_backoff_remaining(state_doc, now)) > 0:
                        soonest = min(soonest, ev_bo)
                    elif (deb := loops.event_debounce_remaining(matched_evs, loop.events, now, self_name=name)) > 0:
                        soonest = min(soonest, deb)
                    else:
                        soonest = 0.5
    return max(0.5, min(soonest, max_wait))


def _scan_watch_snapshot(
    store: Store, note_dirs: tuple[str, ...], state_names: tuple[str, ...] = ()
) -> dict[str, tuple[int, int, int]]:
    """Bounded `O_NOFOLLOW` snapshot of config, authored notes, schemas, loop scripts, events, timers, and pause states."""
    snap: dict[str, tuple[int, int, int]] = {}
    for top in ("bf.yaml", ".env"):
        with suppress(FileNotFoundError, Error, OSError):
            st = store.stat(top)
            snap[top] = (st.st_mtime_ns, st.st_size, st.st_ino)
    for note_path in markdown.authored_notes(store, note_dirs, skipped=[]):
        with suppress(FileNotFoundError, Error, OSError):
            st = store.stat(note_path)
            snap[note_path] = (st.st_mtime_ns, st.st_size, st.st_ino)
    for folder, ext in (("schemas", ".json"), ("sensors", ""), ("routines", ""), ("agents", "")):
        with suppress(Error, OSError):
            _, files = store.subdirs(folder, skipped=[])
            for rel in files:
                if ext and not rel.endswith(ext):
                    continue
                with suppress(FileNotFoundError, Error, OSError):
                    st = store.stat(rel)
                    snap[rel] = (st.st_mtime_ns, st.st_size, st.st_ino)
    with suppress(FileNotFoundError, Error, OSError):
        st_dir = private_dir(store, ".bf", "state", create=False)
        for state_file in (state.EVENTS_FILE, state.TIMERS_FILE, *(f"{dn}.json" for dn in state_names)):
            with suppress(FileNotFoundError, Error, OSError):
                st_ev = st_dir.stat(state_file)
                snap[f".bf/state/{state_file}"] = (st_ev.st_mtime_ns, st_ev.st_size, st_ev.st_ino)
    return snap


def _poll_changes(
    store: Store,
    get_notes: Callable[[], tuple[str, ...]],
    stop_ev: threading.Event,
    *,
    get_state_names: Callable[[], tuple[str, ...]] | None = None,
    initial_snap: dict[str, tuple[int, int, int]] | None = None,
    interval: float = _POLL_INTERVAL,
) -> Generator[set[str]]:
    """Zero-dependency stdlib polling watcher yielding changed brain-relative paths without recursive OS watchers."""
    states_fn = get_state_names or (lambda: ())
    prev = _scan_watch_snapshot(store, get_notes(), states_fn()) if initial_snap is None else dict(initial_snap)
    while not stop_ev.wait(interval):
        cur = _scan_watch_snapshot(store, get_notes(), states_fn())
        changed = {rel for rel, sig in cur.items() if prev.get(rel) != sig} | {rel for rel in prev if rel not in cur}
        prev = cur
        if changed:
            yield changed


def serve(
    store: Store,
    *,
    fmt: Literal["auto", "live", "text", "jsonl"] = "auto",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Supervise notes, events, daemon loops, and concurrent cycle loops (`live`, `text`, or `jsonl`)."""
    cfg = config.load(store)
    busy = "another bf watch is already running for this brain; stop it before starting a new one"
    with writer(store, wait=0.0, busy=busy, name="watch.lock"):
        return _serve_locked(store, cfg, fmt=fmt, stop_event=stop_event)


def _serve_locked(
    store: Store,
    cfg: config.Config,
    *,
    fmt: Literal["auto", "live", "text", "jsonl"] = "auto",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    stop_ev = stop_event if stop_event is not None else threading.Event()
    term_ev, wake_event, note_dirty, cfg_dirty, render_dirty = (threading.Event() for _ in range(5))
    reconcile_lock, flight_lock, rate_lock, emit_lock = (threading.Lock() for _ in range(4))
    daemons: dict[str, tuple[object, threading.Event, DaemonStream]] = {}
    terminating_daemons: set[DaemonStream] = set()
    in_flight: dict[str, str] = {}
    busy_until: dict[str, float] = {}
    event_rates: dict[str, deque[float]] = {}
    recent_lines: deque[str] = deque(maxlen=5)
    corpus_cache: tuple[float, tuple[dict[str, int], list[str], bool, list[str], list[str]]] | None = None
    corpus_gen = 0
    pool_closed = False
    is_tty = sys.stdout.isatty()
    term_dumb = os.environ.get("TERM", "") in {"", "dumb"}
    use_live = fmt == "live" or (fmt == "auto" and is_tty and not term_dumb)
    use_text = fmt == "text"
    use_alt = use_live and is_tty and not term_dumb
    use_color = (use_live or use_text) and is_tty and not term_dumb and not os.environ.get("NO_COLOR", "")
    last_summary = {"brain": cfg.name, "time": now_utc(), "built": 0, "loops_ran": 0, "ok": True, "exit_code": 0}
    term_hit = False

    def _stop_all() -> None:
        stop_ev.set()
        wake_event.set()
        render_dirty.set()

    def write_out(data: bytes) -> None:
        with emit_lock:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            except BrokenPipeError:
                models.silence_broken_pipe()
                _stop_all()
            except OSError:
                pass

    def emit_event(kind: str, payload: dict[str, Any]) -> None:
        nonlocal corpus_cache, corpus_gen
        ts, body = str(payload.get("time") or now_utc()), {"brain": cfg.name, **payload}
        if use_live:
            if kind not in {"watch.started", "watch.stopped"}:
                line = render.format_watch_line(kind, {"time": ts, **body}, color=use_color).rstrip("\n")
                with emit_lock:
                    recent_lines.append(line)
                    if kind in {"watch.build", "watch.loop"}:
                        corpus_cache = None
                        corpus_gen += 1
                render_dirty.set()
            return
        if use_text:
            write_out(render.format_watch_line(kind, {"time": ts, **body}, color=use_color).encode("utf-8"))
        else:
            is_err = (
                body.get("ok") is False
                or body.get("status") == "failed"
                or kind == "watch.error"
                or (kind == "watch.daemon" and bool(body.get("error")))
            )
            write_out(models.log_line(kind, severity="ERROR" if is_err else "INFO", time_iso=ts, **body))

    def _paint_live() -> None:
        nonlocal corpus_cache
        cur_cfg = cfg
        now_m = time.monotonic()
        with emit_lock:
            cached, snap_gen, recent_snap = corpus_cache, corpus_gen, list(recent_lines)
        if cached is None or (now_m - cached[0]) >= _CORPUS_CACHE_TTL:
            sw: list[str] = []
            c_probs: list[str] = []
            try:
                cnts, unb, stale_i = status.corpus_counts(store, cur_cfg, scale_warnings=sw, problems=c_probs)
                cached_tuple = (cnts, unb, stale_i, sw, c_probs)
                with emit_lock:
                    if corpus_gen == snap_gen:
                        corpus_cache = (now_m, cached_tuple)
            except models.FAILURES:
                cached_tuple = None
        else:
            cached_tuple = cached[1]
        eff_p = cur_cfg.effective_pools()
        pool_lbl = ", ".join(f"{k}:{v}" for k, v in eff_p.items()) if len(eff_p) > 1 else f"{cur_cfg.concurrency}w"
        with suppress(*models.FAILURES):
            summary = status.status_summary(
                store, include_loops=True, cfg=cur_cfg, check_inside=False, cached_corpus=cached_tuple
            )
            cols, rows = shutil.get_terminal_size((100, 30)) if use_alt else (None, None)
            frame = render.format_live_screen(
                summary,
                recent=recent_snap,
                footer=f"Watching {cur_cfg.name} ({pool_lbl}) · Ctrl+C to stop",
                color=use_color,
                cols=cols,
                rows=rows,
            )
            write_out(render.paint_frame(frame, tty=use_alt))

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

    def _next_rate_limit_delay(names: set[str]) -> float | None:
        if not names:
            return None
        now_m = time.monotonic()
        with rate_lock:
            waits = [max(0.5, _EVENT_RATE_WINDOW - (now_m - q[0])) for n in names if (q := event_rates.get(n))]
        return min(waits) if waits else None

    def _daemon_sig(unit: LoopSpec, base_env: Mapping[str, str], dotenv: Mapping[str, str]) -> object:
        resolved_argv: tuple[str, ...] = ()
        scripts: list[tuple[str, str]] = []
        with suppress(Error, OSError):
            resolved_argv, _, _, _, _ = config.resolve_loop(store, unit)
            for tok in resolved_argv:
                if rel := config.normalize_brain_script(tok, store.root):
                    sig = models.digest(store.read(rel, models.MAX_NOTE)) if store.exists(rel) else "invalid"
                    scripts.append((rel, sig))
        scoped_dotenv = {k: dotenv[k] for k in unit.env_from if k in dotenv}
        return (
            unit,
            models.digest(models.encode(dict(base_env))),
            models.digest(models.encode(scoped_dotenv)),
            resolved_argv,
            tuple(scripts),
        )

    pause_lock = threading.Lock()
    pause_cache: dict[str, tuple[tuple[int, int, int], bool, str]] = {}

    def _is_paused_cached(name: str, now_dt: datetime, st_dir: Store | None = None) -> bool:
        try:
            sdir = private_dir(store, ".bf", "state", create=False) if st_dir is None else st_dir
            info = sdir.stat(f"{name}.json")
            sig = (info.st_mtime_ns, info.st_size, info.st_ino)
        except (FileNotFoundError, Error, OSError):
            with pause_lock:
                pause_cache.pop(name, None)
            return False
        with pause_lock:
            cached = pause_cache.get(name)
        if cached is not None and cached[0] == sig:
            return state.is_paused({"paused": cached[1], "paused_until": cached[2]}, now_dt)
        with suppress(Error, OSError):
            doc = state.load_unit_state(store, name, default_event_seq=0)
            paused_f, until_s = bool(doc.get("paused")), str(doc.get("paused_until") or "")
            with pause_lock:
                if len(pause_cache) >= 256 and name not in pause_cache:
                    pause_cache.clear()
                pause_cache[name] = (sig, paused_f, until_s)
            return state.is_paused(doc, now_dt)
        with pause_lock:
            pause_cache.pop(name, None)
        return False

    def _watched_state_names() -> tuple[str, ...]:
        now_dt = datetime.now(UTC)
        try:
            st_dir: Store | None = private_dir(store, ".bf", "state", create=False)
        except (FileNotFoundError, Error, OSError):
            st_dir = None
        names: list[str] = []
        for n, lp in cfg.loops.items():
            if not lp.enabled:
                continue
            if lp.daemon or (st_dir is not None and _is_paused_cached(n, now_dt, st_dir)):
                names.append(n)
        return tuple(names)

    def reconcile_config() -> bool:
        nonlocal cfg, corpus_cache, corpus_gen
        prev_key = (cfg.name, cfg.notes)
        try:
            new_cfg = config.load(store)
            dotenv = config.load_dotenv(store)
        except models.FAILURES as err:
            emit_event("watch.error", {"ok": False, "error": redact_secrets(printable(failure(err)))})
            return False
        notes_changed = (new_cfg.name, new_cfg.notes) != prev_key
        now_dt = datetime.now(UTC)
        desired = {
            u.name: (_daemon_sig(u, new_cfg.env, dotenv), u, new_cfg.env)
            for u in new_cfg.loops.values()
            if u.enabled and u.daemon and not _is_paused_cached(u.name, now_dt)
        }
        with reconcile_lock:
            cfg = new_cfg
            stale = (
                []
                if stop_ev.is_set()
                else [daemons.pop(k) for k in list(daemons) if k not in desired or desired[k][0] != daemons[k][0]]
            )
            for _, _, d_stream in stale:
                terminating_daemons.add(d_stream)
        if notes_changed:
            note_dirty.set()
            with emit_lock:
                corpus_cache = None
                corpus_gen += 1
        for _, u_stop, d_stream in stale:
            u_stop.set()
            d_stream.signal_stop()
        for _, _, d_stream in stale:
            try:
                d_stream.terminate()
            finally:
                with reconcile_lock:
                    terminating_daemons.discard(d_stream)
        with reconcile_lock:
            for key, (sig, unit, base_env) in () if stop_ev.is_set() else desired.items():
                if key not in daemons:
                    u_stop = threading.Event()
                    stream = DaemonStream(
                        store,
                        unit,
                        u_stop,
                        wake_event,
                        base_env=base_env,
                        on_event=emit_event,
                    )
                    daemons[key] = (sig, u_stop, stream)
                    stream.start()
        if use_live:
            render_dirty.set()
        return True

    def _classify(rel: str) -> str:
        parts = rel.split("/") if rel else []
        if not parts or any(p.startswith((".write-", ".#")) for p in parts):
            return ""
        cur_notes = cfg.notes
        if parts[0] in cur_notes:
            return "note" if rel.endswith(".md") and markdown.is_authored_note(store, rel, cur_notes) else ""
        if parts[0] == "schemas" and rel.endswith(".json"):
            return "note"
        if rel in {"bf.yaml", ".env"} or parts[0] in {"sensors", "routines", "agents"}:
            return "config"
        if rel in {f".bf/state/{state.EVENTS_FILE}", f".bf/state/{state.TIMERS_FILE}"}:
            return "wake"
        if rel.startswith(".bf/state/") and rel.endswith(".json"):
            return "wake"
        return ""

    def _handle_changes(changes: set[str]) -> None:
        if kinds := {_classify(p) for p in changes} - {""}:
            for k, ev in (("note", note_dirty), ("config", cfg_dirty)):
                if k in kinds:
                    ev.set()
            wake_event.set()
            if use_live:
                render_dirty.set()

    init_snap: dict[str, tuple[int, int, int]] | None = None

    def watch_files() -> None:
        nonlocal init_snap
        snap = init_snap
        init_snap = None
        while not stop_ev.is_set():
            try:
                for changes in _poll_changes(
                    store,
                    lambda: cfg.notes,
                    stop_ev,
                    get_state_names=_watched_state_names,
                    initial_snap=snap,
                ):
                    _handle_changes(changes)
                break
            except (*models.FAILURES, RuntimeError) as err:
                snap = None
                emit_event(
                    "watch.error", {"ok": False, "error": f"filesystem watcher unavailable ({type(err).__name__})"}
                )
                if stop_ev.wait(1.0):
                    break
                cfg_dirty.set()
                wake_event.set()

    pool: ThreadPoolExecutor | None = None

    def _record_summary(brain: str, *, built: int = 0, loops_ran: int = 0, ok: bool = True) -> None:
        nonlocal last_summary
        with emit_lock:
            last_summary = {
                "brain": brain,
                "time": now_utc(),
                "built": built,
                "loops_ran": loops_ran,
                "ok": ok,
                "exit_code": last_summary.get("exit_code", 0),
            }

    def _run_bg(
        item: tuple[LoopSpec, str, list[dict[str, Any]], int | None, tuple[ScheduleSpec, ...], int],
        base_env: Mapping[str, str],
    ) -> None:
        loop, trig, evs, seq_target, scheds, missed = item
        try:
            try:
                res = loops.execute_one_loop(
                    store,
                    loop,
                    trigger=trig,
                    events=evs,
                    events_missed=missed,
                    cursor_target_seq=seq_target,
                    schedules=scheds,
                    stop_event=stop_ev,
                    term_event=term_ev,
                    base_env=base_env,
                )
            except BusyError:
                with flight_lock:
                    busy_until[loop.name] = time.monotonic() + _BUSY_COOLDOWN
                return
            except models.FAILURES as err:
                res = loops.record_failure(store, loop.name, trig, err)
            ok_r = res["status"] in {"ok", "dry_run", "stopped"}
            _record_summary(cfg.name, loops_ran=1, ok=ok_r)
            emit_event("watch.loop", {**res, "ok": ok_r, "trigger": trig})
        finally:
            with flight_lock:
                in_flight.pop(loop.name, None)
            wake_event.set()

    def run_ticks() -> None:
        first = True
        while not stop_ev.is_set():
            wake_event.clear()
            tick_now = datetime.now(UTC)
            with reconcile_lock:
                running_daemons = frozenset(daemons)
                cur_loops = tuple(cfg.loops.values())
            daemon_drift = any(
                (u.name in running_daemons) == _is_paused_cached(u.name, tick_now)
                for u in cur_loops
                if u.enabled and u.daemon
            )
            if cfg_dirty.is_set() or daemon_drift:
                cfg_dirty.clear()
                if not reconcile_config():
                    cfg_dirty.set()
            if stop_ev.is_set():
                break
            if dirty := note_dirty.is_set():
                note_dirty.clear()
            tick_ok, wait_secs = True, 5.0
            try:
                cur_cfg = cfg
                built = engine.build(store) if (first or dirty) else None
                if stop_ev.is_set():
                    break
                now_m = time.monotonic()
                with flight_lock:
                    for k in [k for k, exp in busy_until.items() if exp <= now_m]:
                        busy_until.pop(k, None)
                    busy = frozenset(in_flight) | frozenset(busy_until)
                    soonest_busy = min((exp - now_m for exp in busy_until.values()), default=None)
                due_items, pre_fails, due_snap = loops.select_due_with_snapshot(
                    store, cur_cfg, sched_now=datetime.now(UTC), exclude=busy
                )
                for pf in pre_fails:
                    emit_event("watch.loop", {**pf, "ok": False})
                rate_limited: set[str] = set()
                pool_skipped: set[str] = set()
                rl_msgs: list[str] = []
                pools_map = cur_cfg.effective_pools()
                total_cap = cur_cfg.total_concurrency()
                with flight_lock:
                    pool_counts = Counter(in_flight.values())
                    for it in due_items:
                        if it[0].name in in_flight:
                            continue
                        p_name = it[0].pool
                        p_cap = pools_map.get(p_name, cur_cfg.concurrency)
                        if pool_counts[p_name] >= p_cap or len(in_flight) >= total_cap:
                            pool_skipped.add(it[0].name)
                            continue
                        if it[1] == "event" and not _allow_event_trigger(it[0].name):
                            rate_limited.add(it[0].name)
                            rl_msgs.append(it[0].name)
                            continue
                        if stop_ev.is_set() or pool_closed or pool is None:
                            break
                        in_flight[it[0].name] = p_name
                        pool_counts[p_name] += 1
                        try:
                            pool.submit(_run_bg, it, cur_cfg.env)
                        except BaseException:
                            in_flight.pop(it[0].name, None)
                            pool_counts[p_name] -= 1
                            raise
                    busy_after = frozenset(in_flight) | frozenset(busy_until) | rate_limited | pool_skipped
                for rl_name in rl_msgs:
                    emit_event(
                        "watch.error",
                        {
                            "ok": False,
                            "loop": rl_name,
                            "error": "event rate limit reached; pausing triggers",
                        },
                    )
                nxt = next_due_delay(store, cur_cfg, datetime.now(UTC), exclude=busy_after, snapshot=due_snap)
                if soonest_busy is not None:
                    nxt = min(nxt, max(0.5, soonest_busy))
                if (rl_wait := _next_rate_limit_delay(rate_limited)) is not None:
                    nxt = min(nxt, rl_wait)
                wait_secs = nxt
                b_cnt = built["written"] if built else 0
                r_cnt = built["removed"] if built else 0
                ok_t = (built is None or bool(built["ok"])) and not pre_fails
                _record_summary(cur_cfg.name, built=b_cnt, loops_ran=len(pre_fails), ok=ok_t)
                idx_warn = str(built.get("index_warning") or "") if built else ""
                if built is not None and (b_cnt or r_cnt or not built["ok"] or idx_warn):
                    emit_event(
                        "watch.build",
                        {
                            "ok": built["ok"],
                            "notes": built["notes"],
                            "written": b_cnt,
                            "removed": r_cnt,
                            "problems": built["problems"],
                            **({"index_warning": idx_warn} if idx_warn else {}),
                        },
                    )
            except (*models.FAILURES, RuntimeError, ValueError, KeyError, TypeError) as err:
                tick_ok = False
                msg = (
                    redact_secrets(printable(failure(err))) if isinstance(err, models.FAILURES) else type(err).__name__
                )
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
    files_thread: threading.Thread | None = None
    on_main = threading.current_thread() is threading.main_thread()

    def _on_term_sig(*_a: object) -> None:
        nonlocal term_hit
        term_hit = True
        render_dirty.set()

    prev_term = signal.signal(signal.SIGTERM, _on_term_sig) if on_main else None
    try:
        eff_pools = cfg.effective_pools()
        pool = ThreadPoolExecutor(max_workers=max(64, cfg.total_concurrency()), thread_name_prefix="bf-worker")
        if use_live:
            if use_alt:
                write_out(render.ALT_ENTER)
            _paint_live()
        elif use_text:
            with suppress(Error):
                st_txt = render.format_status_text(status.status_summary(store, include_loops=True), color=use_color)
                pool_lbl = (
                    ", ".join(f"{k}:{v}" for k, v in eff_pools.items()) if len(eff_pools) > 1 else f"{cfg.concurrency}w"
                )
                write_out(f"{st_txt}Watching {cfg.name} ({pool_lbl}) — Ctrl+C to stop\n".encode())
        else:
            emit_event(
                "watch.started",
                {"ok": True, "concurrency": cfg.concurrency, "pools": eff_pools, "loops": len(cfg.loops)},
            )
        init_snap = _scan_watch_snapshot(store, cfg.notes, _watched_state_names())
        reconcile_config()
        files_thread = threading.Thread(target=watch_files, name="bf-watch-poll", daemon=True)
        files_thread.start()
        tick_thread = threading.Thread(target=run_ticks, name="bf-tick", daemon=True)
        tick_thread.start()
        last_paint = time.monotonic()
        while not stop_ev.is_set():
            if term_hit:
                break
            if use_live:
                render_dirty.wait(timeout=0.2)
                if stop_ev.is_set() or term_hit:
                    break
                if render_dirty.is_set() or (time.monotonic() - last_paint) >= _LIVE_REFRESH_SECS:
                    render_dirty.clear()
                    _paint_live()
                    last_paint = time.monotonic()
            else:
                stop_ev.wait(0.2)
    finally:
        alt_exited = False
        try:
            with flight_lock:
                pool_closed = True
            if term_hit:
                term_ev.set()
            _stop_all()
            with reconcile_lock:
                active_daemons = list(daemons.values())
                daemons.clear()
                extra_daemons = list(terminating_daemons)
            for _, u_stop, daemon in active_daemons:
                u_stop.set()
                daemon.signal_stop()
            for daemon in extra_daemons:
                daemon.signal_stop()
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
            if tick_thread is not None and tick_thread.is_alive():
                tick_thread.join(timeout=12)
            for _, _, daemon in active_daemons:
                daemon.terminate()
            for daemon in extra_daemons:
                daemon.terminate()
            if files_thread is not None and files_thread.is_alive():
                files_thread.join(timeout=2)
            if use_alt:
                write_out(render.ALT_EXIT)
                alt_exited = True
            if use_live:
                write_out(
                    render.format_watch_line(
                        "watch.stopped", {"time": now_utc(), "brain": cfg.name}, color=use_color
                    ).encode("utf-8")
                )
            else:
                emit_event("watch.stopped", {"ok": True})
        finally:
            if use_alt and not alt_exited:
                write_out(render.ALT_EXIT)
            with suppress(OSError, ValueError):
                if on_main and prev_term is not None:
                    signal.signal(signal.SIGTERM, prev_term)
    last_summary["exit_code"] = 143 if term_hit else 0
    return last_summary
