"""Live supervisor and Rich TUI dashboard (`bf watch`) backed by `watchfiles`."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.table import Table
from watchfiles import watch as watch_fs

from bf import config, engine, jobs, records
from bf.config import ChannelSpec, ServerSpec
from bf.models import AUTHORED, Error, decode, failure, now_utc, printable, terminal
from bf.storage import Store, private_dir

_IGNORED_PREFIXES = (".bf/", ".git/", "memories/", ".venv/", "__pycache__/")


def render_dashboard(store: Store) -> Table:
    stat = jobs.status_summary(store)
    rev = engine.review(store)
    header = (
        f"Brain: {stat['brain']}  |  Events: {stat['events_seq']}  |  "
        f"Stale: {len(rev['stale'])}  |  Open tasks: {rev['tasks_open']}"
    )
    table = Table(title=header)
    for col, style in (("Kind", "cyan"), ("Name", "bold"), ("Status", ""), ("Last Success", "")):
        table.add_column(col, style=style or None)
    for kind, group in (("job", stat["jobs"]), ("channel", stat["channels"]), ("server", stat["servers"])):
        for name, info in group.items():
            if info["enabled"]:
                st = info["status"]
                color = "green" if st in {"ok", "running"} else ("red" if st == "failed" else "dim")
                table.add_row(kind, name, f"[{color}]{st}[/{color}]", str(info["last_success"] or "-"))
    return table


def tick(store: Store, *, rebuild_notes: bool = True, now: datetime | None = None) -> dict[str, Any]:
    """Execute one supervisor cycle: optional note build and due/event jobs."""
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    built = engine.build(store, now=now) if rebuild_notes else None
    job_res = jobs.run_jobs(store, due_only=True, now=now)
    res = {"brain": config.load(store).name, "time": now_utc(instant), "built": built["written"] if built else 0}
    return {**res, "jobs_ran": job_res["ran"], "ok": job_res["ok"]}


class DaemonStream:
    """Supervise one long-running `channels.<name>.listen` or `servers.<name>.run` daemon."""

    def __init__(
        self, store: Store, unit: ChannelSpec | ServerSpec, stop_event: threading.Event, wake_event: threading.Event
    ) -> None:
        self.store, self.unit, self.stop_event, self.wake_event = store, unit, stop_event, wake_event
        self.kind = "channel" if isinstance(unit, ChannelSpec) else "server"
        self.cmd = unit.listen if isinstance(unit, ChannelSpec) else unit.run
        self.emit = "messages" if isinstance(unit, ChannelSpec) else unit.emit
        self.proc: subprocess.Popen[bytes] | None = None
        self.thread = threading.Thread(target=self._loop, name=f"bf-{self.kind}-{unit.name}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def terminate(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            with suppress(OSError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=2)
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=3)

    def _loop(self) -> None:
        backoff = 1.0
        err_flags = os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
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
                with private_dir(self.store, ".bf", "state").parent(f"{self.unit.name}.stderr") as (pfd, leaf):
                    efd = os.open(leaf, err_flags, 0o600, dir_fd=pfd)
                dn, pipe = subprocess.DEVNULL, subprocess.PIPE
                with (
                    os.fdopen(efd, "w+b") as err_f,
                    subprocess.Popen(  # noqa: S603
                        argv, cwd=self.store.root, env=env, stdin=dn, stdout=pipe, stderr=err_f, start_new_session=True
                    ) as proc,
                ):
                    self.proc = proc
                    doc = jobs.load_run_log(self.store, self.unit.name, kind=self.kind)
                    doc.update({"status": "running", "updated": now_iso, "last_start": now_iso})
                    jobs.save_run_log(self.store, doc)
                    if proc.stdout is not None:
                        for raw_line in proc.stdout:
                            if self.stop_event.is_set():
                                break
                            if not (stripped := raw_line.strip()) or self.emit == "none":
                                continue
                            try:
                                if isinstance(parsed := decode(stripped, label), dict):
                                    def_ev = str(parsed.get("kind") or "server.event")
                                    ev_kind = "channel.message" if self.emit == "messages" else def_ev
                                    records.append_event(self.store, ev_kind, self.unit.name, dict(parsed), dedupe=True)
                                    self.wake_event.set()
                            except Error as err:
                                sys.stderr.write(f"bf: {label}: {printable(failure(err))}\n")
                    if self.stop_event.is_set():
                        self.terminate()
                    rc = proc.wait()
                    err_f.seek(max(0, err_f.seek(0, os.SEEK_END) - 4096))
                    err_txt = err_f.read().decode("utf-8", "replace").strip()
                dur_ms = max(0, int((time.monotonic() - t0) * 1000))
                end_dt = datetime.now(UTC)
                if self.stop_event.is_set():
                    doc = jobs.load_run_log(self.store, self.unit.name, kind=self.kind)
                    doc.update({"status": "stopped", "updated": now_utc(end_dt), "last_end": now_utc(end_dt)})
                    jobs.save_run_log(self.store, doc)
                    break
                err_msg = "" if rc == 0 else (err_txt or f"exit code {rc}")[-1024:]
                jobs.record_run(
                    self.store,
                    self.kind,
                    self.unit.name,
                    run_id,
                    now_iso,
                    ended=end_dt,
                    dur_ms=dur_ms,
                    code=rc,
                    err_msg=err_msg,
                    stderr=err_txt,
                )
                healthy = rc == 0 or dur_ms >= 30_000
            except (Error, OSError) as err:
                msg = printable(failure(err))
                sys.stderr.write(f"bf: {label}: {msg}\n")
                dur_ms = max(0, int((time.monotonic() - t0) * 1000))
                with suppress(Error, OSError):
                    jobs.record_run(
                        self.store,
                        self.kind,
                        self.unit.name,
                        run_id,
                        now_iso,
                        ended=datetime.now(UTC),
                        dur_ms=dur_ms,
                        code=1,
                        err_msg=msg[:1024],
                    )
            backoff = 1.0 if healthy else backoff
            if self.stop_event.wait(backoff):
                break
            backoff = 1.0 if healthy else min(backoff * 2.0, 30.0)


def _safe_tick(store: Store, *, rebuild_notes: bool, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        return tick(store, rebuild_notes=rebuild_notes)
    except (Error, OSError) as err:
        sys.stderr.write(f"bf: watch: {printable(failure(err))}\n")
        return {**fallback, "time": now_utc(), "built": 0, "jobs_ran": 0, "ok": False}


def serve(
    store: Store, *, once: bool = False, json_stream: bool = False, stop_event: threading.Event | None = None
) -> dict[str, Any]:
    """Run `bf watch` once (`--once`) or supervise files, events, jobs, channels, and servers."""
    if once:
        return tick(store, rebuild_notes=True)

    cfg = config.load(store)
    stop_ev = stop_event if stop_event is not None else threading.Event()
    wake_event, note_dirty = threading.Event(), threading.Event()
    daemons: list[DaemonStream] = []
    for unit in (*cfg.channels.values(), *cfg.servers.values()):
        cmd = unit.listen if isinstance(unit, ChannelSpec) else unit.run
        if unit.enabled and cmd:
            daemon = DaemonStream(store, unit, stop_ev, wake_event)
            daemons.append(daemon)
            daemon.start()

    def watch_files() -> None:
        with suppress(Exception):
            for changes in watch_fs(
                store.root, debounce=cfg.watch.debounce_ms, stop_event=stop_ev, raise_interrupt=False
            ):
                relevant = False
                for _, raw_path in changes:
                    try:
                        rel = Path(raw_path).resolve().relative_to(store.root).as_posix()
                    except ValueError:
                        continue
                    if rel.startswith(_IGNORED_PREFIXES):
                        continue
                    if rel.startswith(tuple(f"{f}/" for f in AUTHORED)) and rel.endswith(".md"):
                        note_dirty.set()
                        relevant = True
                    elif rel in {"bf.yaml", "logs/events.json"}:
                        relevant = True
                if relevant:
                    wake_event.set()

    threading.Thread(target=watch_files, name="bf-watchfiles", daemon=True).start()
    use_tui = not json_stream and sys.stdout.isatty()
    live = Live(render_dashboard(store), console=Console(), refresh_per_second=2) if use_tui else None
    empty = {"brain": cfg.name, "time": now_utc(), "built": 0, "jobs_ran": 0, "ok": True}
    last_summary = _safe_tick(store, rebuild_notes=True, fallback=empty)
    if json_stream:
        sys.stdout.buffer.write(terminal(last_summary))
        sys.stdout.flush()
    try:
        if live is not None:
            live.start()
        while not stop_ev.is_set():
            wake_event.wait(timeout=cfg.watch.interval)
            wake_event.clear()
            if stop_ev.is_set():
                break
            do_build = note_dirty.is_set()
            note_dirty.clear()
            last_summary = _safe_tick(store, rebuild_notes=do_build, fallback=last_summary)
            if json_stream and (last_summary["built"] or last_summary["jobs_ran"]):
                sys.stdout.buffer.write(terminal(last_summary))
                sys.stdout.flush()
            elif live is not None:
                with suppress(Error, OSError):
                    live.update(render_dashboard(store))
            time.sleep(0.05)
    finally:
        stop_ev.set()
        if live is not None:
            with suppress(Exception):
                live.stop()
        for daemon in daemons:
            daemon.terminate()
    return last_summary
