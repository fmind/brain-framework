"""Terminal dashboard and foreground daemon/reactive supervisor over local runtime state and cancellable updates."""

from __future__ import annotations

import os
import re
import select
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import termios
import time
import tty
from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from threading import Event, Lock, Thread
from typing import IO, TextIO, cast

from pydantic import ValidationError
from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bf import collect, config, index, runtime, update
from bf import jobs as job_ops
from bf.health import DEGRADED, _load_states, degradation, is_late
from bf.models import Error, WatchSettings, decode, local, present, printable, terminal, timestamp
from bf.runtime import Queue, sync_item_events
from bf.storage import BusyError, Store, collecting, wakeup_mtime
from bf.watch_settings import Notifications, settings


class State(StrEnum):
    """A job's local state; declaration order is the state sort order."""

    FAILED, NEVER, DUE, FRESH, MANUAL, DISABLED = "failed", "never", "due", "fresh", "manual", "disabled"


class StreamStoppedError(Error):
    """A streaming daemon exited repeatedly before establishing."""


ATTENTION = frozenset({State.FAILED, State.NEVER, State.DUE})
COLORS = {State.FAILED: "red", State.NEVER: "yellow", State.DUE: "yellow", State.FRESH: "green"}
MARKERS = {"action": "* ", "message": "@ ", "messages": "~ "}
_MAX_STREAM_LINE, _MAX_STREAM_FAILS, _COMPACT_EVERY = 1 << 20, 5, 3600.0
_STATE_ORDER = list(State)
_REFRESH_MSG = (
    ("observe", "Rereading configuration and runtime state"),
    ("paused", "Refresh queued; resume with Space to check due jobs"),
    ("running", "Refresh queued after the active update"),
)


@dataclass
class Row:
    emit: str
    name: str
    status: State
    included: bool
    refresh: int
    success: str = ""
    next_due: str = ""
    added: int | None = None
    updated: int | None = None
    removed: int | None = None
    elapsed_seconds: float | None = None
    output_bytes: int | None = None
    reconcile: bool = False
    error: str = ""
    log: str = ""
    action: str = ""
    records: int | None = None
    runs: int | None = None
    record: str = ""
    daemon: bool = False
    early: bool = False
    degraded: dict[str, object] | None = None
    incident_count: int = 0


def needs_attention(row: Row) -> bool:
    """Whether the attention count and Tab filter include the row."""
    runs = int(cast(int, row.degraded["runs"])) if row.degraded is not None else 0
    return (row.status in ATTENTION and not row.early) or (
        runs >= DEGRADED and row.status not in {State.MANUAL, State.DISABLED}
    )


def _incidents(store: Store) -> list[dict[str, object]]:
    with suppress(Error, OSError, sqlite3.DatabaseError):
        return Queue(store).incidents()
    return []


def snapshot(store: Store, jobs: tuple[str, ...] = (), *, now: datetime | None = None) -> list[Row]:
    """Read only configuration and `.bf/runtime.sqlite3`; never execute commands or create missing state."""
    cfg, now = config.load(store), now or datetime.now(UTC)
    selected, tz = update.selection(cfg, jobs), cfg.defaults.timezone
    states, _, failing, daemon_errors, _ = _load_states(store)
    inc_by_job = {str(inc["job"]): inc for inc in _incidents(store)}
    rows: list[Row] = []
    for name, job in sorted(cfg.jobs.items()):
        entry, daemon, inc = states.get(name, {}), job.is_daemon, inc_by_job.get(name)
        timed = [t for t in job.triggers if t.every is not None or t.at is not None]
        q_err = "unresolved work in runtime queue; inspect bf queue" if name in failing else ""
        inc_err = (inc or {}).get("last_error") or ""
        error = str(entry.get("last_error") or daemon_errors.get(name, "") or inc_err or q_err) if job.enabled else ""
        success, sched_ok = str(entry.get("last_success", "")), runtime.scheduled_anchor(entry)
        due_at = collect.next_due(job, entry, now, default_tz=tz)
        status = State.DISABLED if not job.enabled else State.FAILED if error else State.MANUAL
        if status is State.MANUAL and (timed or daemon):
            status = (
                State.NEVER if not sched_ok and not daemon else State.DUE if due_at and due_at <= now else State.FRESH
            )
        keys = ("added", "updated", "removed", "output_bytes", "records", "runs")
        added, updated, removed, out_b, recs, runs = (cast("int | None", entry.get(k)) for k in keys)
        refresh = min((t.every for t in timed if t.every), default=0)
        due_s = timestamp(due_at.isoformat()) if due_at else ""
        early = status is State.NEVER and (daemon or not is_late(job, {"freshness": "never"}, now, default_tz=tz))
        r = Row(job.emit, name, status, name in selected, refresh, success, due_s, added, updated, removed)
        r.elapsed_seconds, r.output_bytes = cast("float | None", entry.get("elapsed_seconds")), out_b
        r.reconcile, r.error = bool(entry.get("reconcile", False)), error
        r.log = runtime.log_path(name) if error else ""
        r.action, r.records, r.runs, r.record = str(entry.get("action", "")), recs, runs, str(entry.get("record", ""))
        r.daemon, r.early = daemon, early
        r.degraded = degradation(entry) if job.emit == "records" else None
        r.incident_count = int(cast("int", inc["count"])) if inc and job.enabled else 0
        rows.append(r)
    return rows


def clean(value: str) -> str:
    """Terminal control characters in paths or errors are data, never terminal instructions."""
    return "".join(c if c.isprintable() else " " for c in value)


def duration(seconds: float) -> str:
    s = max(0, int(seconds))
    return next((f"{s // n}{u}" for n, u in ((86400, "d"), (3600, "h"), (60, "m")) if s >= n), f"{s}s")


def period(seconds: int) -> str:
    """A configured period in its largest exact unit: unlike an age, 90 seconds is 90s, never 1m."""
    units = ((86400, "d"), (3600, "h"), (60, "m"))
    return next((f"{seconds // n}{u}" for n, u in units if seconds >= n and not seconds % n), f"{seconds}s")


class Sort(StrEnum):
    NAME, UPDATED, ITEMS, STATE, DUE = "name", "last OK", "items", "state", "next due"
    CHANGES, DURATION, OUTPUT, REFRESH, EMIT = "changes", "duration", "output bytes", "refresh", "emit"

    @property
    def descending(self) -> bool:
        return self in {Sort.UPDATED, Sort.ITEMS, Sort.CHANGES, Sort.DURATION, Sort.OUTPUT}

    def value_for(self, r: Row) -> str | float | None:
        if self is Sort.STATE:
            return _STATE_ORDER.index(r.status)
        if self in {Sort.UPDATED, Sort.DUE}:
            raw = r.success if self is Sort.UPDATED else r.next_due
            return datetime.fromisoformat(raw).timestamp() if raw else None
        if self is Sort.CHANGES:
            if None in (r.added, r.updated, r.removed):
                return None
            return (r.added or 0) + (r.updated or 0) + (r.removed or 0)
        m = {"items": "records", "duration": "elapsed_seconds", "output bytes": "output_bytes"}
        return cast("str | float | None", getattr(r, m.get(self.value, self.value)))


KEYS = {"Q": "q", **dict.fromkeys(("\x1b[A", "\x1bOA"), "k"), **dict.fromkeys(("\x1b[B", "\x1bOB"), "j")}
KEYS |= dict.fromkeys(("\x1b[H", "\x1bOH", "\x1b[1~", "\x1b[7~"), "g")
KEYS |= dict.fromkeys(("\x1b[F", "\x1bOF", "\x1b[4~", "\x1b[8~"), "G")
SEQUENCE = re.compile(r"\x1b\[[0-9;]*[~A-Za-z]|\x1bO[A-Za-z]|\x1b|[^\x1b]")
PARTIAL = re.compile(rb"\x1b(?:\[[0-9;]*|O)?\Z")
HEADER, FOOTER, FRAME, DETAILS, WIDE, STOP, TICK = 2, 4, 4, 8, 120, 60, 1.0


def _line(content: str | Text, style: str = "") -> Text:
    txt = content if isinstance(content, Text) else Text(content, style=style)
    txt.no_wrap, txt.overflow = True, "ellipsis"
    return txt


@dataclass
class Dashboard:
    brain: str
    observe: bool = False
    rows: list[Row] = field(default_factory=list)
    cursor: int = 0
    paused: bool = False
    attention: bool = False
    help: bool = False
    running: bool = False
    assisting: bool = False
    waiting: str = ""
    message: str = "Ready"
    late: str = ""
    next_check: float = 0
    preferences: WatchSettings = field(default_factory=WatchSettings)
    sort: Sort = Sort.NAME
    descending: bool = False
    incidents: list[dict[str, object]] = field(default_factory=list)

    def visible(self) -> list[Row]:
        rows = sorted(
            (r for r in self.rows if not self.attention or needs_attention(r)), key=lambda r: (r.name, r.emit)
        )
        known = [(r, v) for r in rows if (v := self.sort.value_for(r)) is not None]
        unknown = [r for r in rows if self.sort.value_for(r) is None]
        return [r for r, _ in sorted(known, key=lambda x: x[1], reverse=self.descending)] + unknown

    def selected(self) -> Row | None:
        return v[min(self.cursor, len(v) - 1)] if (v := self.visible()) else None

    def select(self, selected: Row | None) -> None:
        v = self.visible()
        fb = max(0, min(self.cursor, len(v) - 1))
        self.cursor = next((i for i, r in enumerate(v) if selected and r.name == selected.name), fb)

    def replace_rows(self, rows: list[Row]) -> None:
        sel = self.selected()
        self.rows = rows
        self.select(sel)

    def key(self, key: str) -> str:
        """Return a controller action without touching configuration or executing jobs."""
        k = KEYS.get(key, key)
        if k in {"q", "f", "u"}:
            return "quit" if k == "q" else "refresh"
        if k in {"j", "k"}:
            self.cursor += 1 if k == "j" else -1
        elif k == " " and not self.observe:
            self.paused = not self.paused
        elif k == "\t":
            self.attention, self.cursor = not self.attention, 0
        elif k == "?":
            self.help = not self.help
        elif k in {"s", "n", "t", "i", "r"}:
            sel, s = self.selected(), list(Sort)
            if k == "r":
                self.descending = not self.descending
            else:
                fixed = {"n": Sort.NAME, "t": Sort.UPDATED, "i": Sort.ITEMS}
                self.sort = fixed[k] if k in fixed else s[(s.index(self.sort) + 1) % len(s)]
                self.descending = self.sort.descending
            self.select(sel)
        elif k in {"g", "G"}:
            self.cursor = 0 if k == "g" else len(self.visible()) - 1
        self.cursor = max(0, min(self.cursor, len(self.visible()) - 1))
        return ""

    def guide(self) -> Text:
        p = self.preferences
        lines = [
            "s cycles: " + ", ".join(Sort) + ". Newest/largest first; name/state/due/refresh/emit ascend.",
            "Items: records returned by last committed run. +/~/- = added/updated/removed (* action, @ msg, ~ msgs).",
            "Failed runs keep prior counts. Fresh is local success, not proof provider is unchanged.",
        ]
        if self.observe:
            lines.append(f"History: {p.poll_interval:g}s · view never runs jobs or sends alerts. u rereads; q quits.")
        else:
            lines += [
                f"Check: {period(p.interval)} · history: {p.poll_interval:g}s · alerts: {p.notifications}",
                *([self.late + "."] if self.late else []),
                "Configure watch in bf.yaml; restart to apply. f/u refreshes; Space pauses; q stops watch.",
                "Daemons, events, wakes and deliveries run beside collection.",
            ]
        return Text("\n".join(lines))

    def details(self, row: Row) -> Text:
        d = Text()
        scope = "" if self.observe and row.included else "selected" if row.included else "excluded"
        sched = "daemon stream" if row.daemon else period(row.refresh) if row.refresh else "manual only"
        sched = "scheduled" if sched == "manual only" and row.status not in {State.MANUAL, State.DISABLED} else sched
        d.append(clean(row.name) + "\n", style="bold cyan")
        d.append(" · ".join(p for p in (f"emit:{row.emit}", row.status, scope) if p) + "\n")
        if row.error:
            d.append(f"{clean(row.error)}\n", style="red")
            if row.incident_count:
                d.append(f"Incident: {row.incident_count} consecutive failure(s)\n", style="red")
            d.append(f"Log: {clean(row.log)}\n", style="dim")
        if (deg := row.degraded) is not None:
            m = f"Last run missed {deg['missed']:,} of {deg['of']:,}; {int(cast(int, deg['runs']))} incomplete in a row\n"
            d.append(m, style="yellow")
            if not row.error:
                d.append(f"Log: {clean(runtime.log_path(row.name))}\n", style="dim")
        if not row.error:
            for lbl, val in (("Latest action: ", row.action), ("Latest record: ", row.record)):
                if val:
                    d.append(f"{lbl}{clean(val)}\n")
        runs_s = f"Runs: {row.runs:,}\n" if row.runs is not None else ""
        ok, items = local(row.success) if row.success else "never", "—" if row.records is None else f"{row.records:,}"
        d.append(f"{runs_s}Schedule: {sched}\nLast OK: {ok}\nLast run items: {items}\n")
        if row.elapsed_seconds is not None and row.output_bytes is not None:
            d.append(f"Last run: {row.elapsed_seconds:.2f}s · {row.output_bytes:,} bytes\n")
            if row.reconcile:
                d.append("Wider reconciliation window\n", style="cyan")
        if not (self.observe or row.error) and row.status is not State.DISABLED and (row.daemon or not row.action):
            cmd = "bf collect" if row.emit == "records" else "bf run"
            hint = f"Only due jobs run automatically; {cmd} {clean(row.name)} runs it now."
            d.append("Runs continuously as a daemon stream while bf watch is active." if row.daemon else hint)
        d.rstrip()
        return d

    def render(self, width: int, height: int, now: datetime | None = None) -> Layout:
        now = now or datetime.now(UTC)
        mode = "OBSERVE · no execution" if self.observe else "PAUSED" if self.paused else "WATCH · collection enabled"
        mode += " · interval too slow, press ?" if self.late else ""
        nxt = duration(self.next_check - time.monotonic())
        w = "Updating…" if self.running else "Checks paused" if self.paused else f"Next check in {nxt}"
        w += (" · reactive active" if self.assisting else "") + (" · dispatcher waiting" if self.waiting else "")
        p_hdr = ((" BF ", "bold white on blue"), (f"  {clean(self.brain)}", "bold cyan"))
        header = _line(Text.assemble(*p_hdr, (f"    {mode}", "yellow" if self.paused else "cyan")))
        att = sum(needs_attention(r) for r in self.rows)
        sel = "" if self.observe else f"{sum(r.included for r in self.rows)} selected   "
        n, w = len(self.rows), "Reading local history" if self.observe else w
        inc_n = len(self.incidents) or sum(r.incident_count > 0 for r in self.rows)
        inc_s = f" · {inc_n} active incident{'' if inc_n == 1 else 's'}" if inc_n else ""
        s_txt = f"{n} job{'' if n == 1 else 's'}   {sel}{att} need{'s' if att == 1 else ''} attention   {w}{inc_s}"
        summary, visible = _line(s_txt, "dim"), self.visible()
        self.cursor = max(0, min(self.cursor, len(visible) - 1))
        wide, body = width >= WIDE, height - HEADER - FOOTER
        t_h = body if wide else min(body, FRAME + max(1, len(visible)), max(FRAME + 1, body - DETAILS))
        limit = max(0, t_h - FRAME)
        offset = max(0, self.cursor - max(1, limit) + 1)
        shown = visible[offset : offset + limit]
        table = Table(expand=True, box=box.SIMPLE, show_edge=False, collapse_padding=True, pad_edge=False)
        table.header_style, table.padding = "bold cyan", (0, 1)
        table.add_column("Job", no_wrap=True, overflow="ellipsis", ratio=1, min_width=12)
        for col, mw in (("State", 8), ("Last OK", 8), ("Next due", None), ("Items", None), ("+/~/-", None)):
            table.add_column(col, no_wrap=True, min_width=mw, justify="right" if col == "Items" else "left")
        for idx, r in enumerate(shown, offset):
            succ = datetime.fromisoformat(r.success) if r.success else None
            due = datetime.fromisoformat(r.next_due) if r.next_due else None
            rem = "now" if due and due <= now else duration((due - now).total_seconds()) if due else "—"
            name = Text(("> " if idx == self.cursor else "  ") + MARKERS.get(r.emit, "") + r.name)
            st_t = Text(r.status, style=COLORS.get(r.status, "dim")) if r.included else Text("excluded", "dim")
            ok_s = duration((now - succ).total_seconds()) + " ago" if succ else "never"
            delta = "/".join("—" if v is None else str(v) for v in (r.added, r.updated, r.removed))
            due_s, hl = "daemon" if r.daemon and r.included else rem, "white on #183040" if idx == self.cursor else ""
            table.add_row(name, st_t, ok_s, due_s, "—" if r.records is None else str(r.records), delta, style=hl)
        if not visible:
            table.add_row("No matching jobs", "", "", "", "", "")
        rng, arrow = f"{offset + 1}-{offset + len(shown)}" if shown else "0-0", "↓" if self.descending else "↑"
        title = f"{'Attention' if self.attention else 'Jobs'} · {rng}/{len(visible)} · {self.sort} {arrow}"
        e_att = "Nothing needs attention; press Tab to show every job."
        empty = (
            e_att if self.attention and self.rows else "Add a reviewed job in bf.yaml. Press ? for controls and timing."
        )
        detail = self.guide() if self.help else self.details(visible[self.cursor]) if visible else Text(empty)
        k_end = "u reread" if self.observe else "f refresh  Space pause"
        b_end = "observation only" if self.observe else "closing watch stops its collection"
        keys = f"s sort  r reverse  n name  t last OK  i items  ? help  q quit\nj/k ↑/↓ select  g/G first/last  Tab attention  {k_end}"
        jp = Panel(table, title=title, border_style="blue")
        dp = Panel(detail, title="Guide" if self.help else "Details", border_style="cyan")
        body_lay = Layout(dp if self.help else jp)
        if not self.help and wide:
            body_lay.split_row(Layout(jp), Layout(dp, size=width // 3))
        elif not self.help and body - t_h > 2:
            body_lay.split_column(Layout(jp, size=t_h), Layout(dp))
        layout = Layout()
        foot = Layout(_line(f"Local run history · no provider text · {b_end}", "dim"), size=1)
        msg_l, key_l = Layout(_line(clean(self.message), "yellow"), size=1), Layout(_line(keys, "bold cyan"), size=2)
        layout.split_column(Layout(Group(header, summary), size=HEADER), body_lay, msg_l, key_l, foot)
        return layout


class Job:
    """A separate BF `update` subprocess keeps keyboard input responsive and reuses cancellation."""

    def __init__(self, store: Store, jobs: tuple[str, ...] = ()) -> None:
        flags = [a for n in jobs for a in ("--job", n)]
        self.argv = [sys.executable, "-E", "-P", "-m", "bf", "update", "--brain", str(store.root), *flags]
        self.process: subprocess.Popen[bytes] | None = None
        self.reply: IO[bytes] | None = None
        self.stopping = False

    def start(self) -> None:
        self.discard()
        self.stopping, self.reply = False, tempfile.TemporaryFile()  # noqa: SIM115
        with job_ops.starting():
            n, env = subprocess.DEVNULL, runtime.environment()
            self.process = subprocess.Popen(  # noqa: S603
                self.argv, stdin=n, stdout=self.reply, stderr=n, env=env, start_new_session=True
            )

    def poll(self) -> int | None:
        return self.process.poll() if self.process is not None else None

    def skipped(self) -> int:
        if self.reply is None:
            return 0
        try:
            self.reply.seek(0)
            val = decode(self.reply.read(1 << 20))
        except Error, OSError:
            return 0
        if not isinstance(val, dict) or not isinstance(cache := val.get("index"), dict) or "error" in cache:
            return 0
        jobs_v = cast("list[object]", val.get("jobs") or [])
        if any(not isinstance(i, dict) or i.get("status") == "failed" for i in jobs_v):
            return 0
        return s if type(s := cache.get("skipped")) is int and s > 0 else 0

    def discard(self) -> None:
        if self.reply is not None:
            self.reply.close()
            self.reply = None

    def terminate(self) -> None:
        if self.process is not None and self.process.poll() is None and not self.stopping:
            self.stopping = True
            self.process.terminate()

    def close(self) -> None:
        if (proc := self.process) is not None and proc.poll() is None:
            self.terminate()
            end = time.monotonic() + STOP
            try:
                try:
                    proc.wait(timeout=min(1.0, STOP))
                except subprocess.TimeoutExpired:
                    with suppress(OSError, ValueError):
                        sys.stderr.write(f"bf: stopping the active update; this can take up to {STOP} seconds\n")
                        sys.stderr.flush()
                    proc.wait(timeout=max(0.0, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.discard()


def _drain_stderr(store: Store, name: str, pipe: IO[bytes] | None) -> None:
    if pipe is None:
        return
    buf = bytearray()
    with suppress(OSError, ValueError):
        while len(buf) < 65_536 and (chunk := pipe.read(4096)):
            buf.extend(chunk[: 65_536 - len(buf)])
        pipe.close()
    if buf:
        with suppress(Error, OSError):
            runtime.append_log(store, name, f"{runtime.now()} daemon", bytes(buf))


def _set_daemon_health(store: Store, name: str, err: str) -> None:
    with suppress(Error, OSError):
        if err:
            Queue(store).set_progress(f"health:daemon:{name}", {"error": err})
        elif (q := Queue(store)).exists():
            with q.connect() as db:
                db.execute(
                    "DELETE FROM checkpoints WHERE key=?"
                    if name
                    else "DELETE FROM checkpoints WHERE key LIKE 'health:daemon:%'",
                    (f"health:daemon:{name}",) if name else (),
                )


def _pump_stdout(store: Store, name: str, proc: subprocess.Popen[bytes], stopped: Event, started: float) -> None:
    if proc.stdout is None:
        raise Error(f"daemon {name}: missing stdout pipe")
    sel, buf, queue, cleared = selectors.DefaultSelector(), b"", Queue(store), False
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        while not stopped.is_set():
            if not cleared and time.monotonic() - started >= job_ops.ESTABLISHED:
                cleared = True
                _set_daemon_health(store, name, "")
            if not sel.select(0.5):
                continue
            if not (chunk := os.read(proc.stdout.fileno(), 65_536)):
                break
            buf += chunk
            if len(buf) > _MAX_STREAM_LINE:
                raise Error(f"daemon {name}: output line exceeds {_MAX_STREAM_LINE} bytes")
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    try:
                        msgs, cursor = runtime.parse_messages(decode(line), f"daemon {name}: batch", allow_single=True)
                    except ValidationError as error:
                        raise Error(f"daemon {name}: invalid stream message") from error
                    queue.admit(name, msgs, checkpoint=(f"cursor:{name}", {"cursor": cursor}) if cursor else None)
                    ts = runtime.now()
                    queue.remember(name, last_run=ts, last_success=ts, last_error="", error_count=0)
                    _set_daemon_health(store, name, "")
    finally:
        sel.close()


_Procs = dict[str, subprocess.Popen[bytes]]


def _stream_job(store: Store, name: str, stopped: Event, procs: _Procs, lock: Lock, fatal: list[str]) -> None:
    failures = 0
    while not stopped.is_set():
        started, failed_msg, proc = time.monotonic(), "", None
        try:
            job = config.load(store).jobs.get(name)
            if job is None or not job.enabled or not job.is_daemon:
                return
            env, argv = job_ops.job_env(store, job), job_ops.expand_argv(store, job.run, name)
            cmd, cwd = [job_ops.executable(store, argv[0], env), *argv[1:]], job_ops.cwd_path(store, job.cwd)
            with job_ops.starting():
                if stopped.is_set():
                    return
                dn, p = subprocess.DEVNULL, subprocess.PIPE
                proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=dn, stdout=p, stderr=p, start_new_session=True)  # noqa: S603
            with lock:
                procs[name] = proc
            started = time.monotonic()
            err_reader = Thread(target=_drain_stderr, args=(store, name, proc.stderr), daemon=True)
            err_reader.start()
            try:
                _pump_stdout(store, name, proc, stopped, started)
                code = proc.poll()
                if code is None and not stopped.is_set():
                    with suppress(subprocess.TimeoutExpired):
                        code = proc.wait(timeout=0.5)
                if not stopped.is_set() and code not in {None, 0}:
                    failed_msg = f"daemon {name}: exited with status {code}; see {runtime.log_path(name)}"
            finally:
                with lock:
                    procs.pop(name, None)
                job_ops.stop(proc)
                err_reader.join(timeout=2)
                for p in (proc.stdout, proc.stderr):
                    if p is not None and not p.closed:
                        p.close()
        except (Error, OSError, UnicodeError) as error:
            failed_msg = f"daemon {name}: {error}" if isinstance(error, Error) else f"daemon {name}: stream failed"
        if stopped.is_set():
            return
        established = proc is not None and time.monotonic() - started >= job_ops.ESTABLISHED
        failures = (0 if not failed_msg else 1) if established else failures + 1
        msg = failed_msg or f"daemon {name}: stream closed unexpectedly"
        _set_daemon_health(store, name, msg)
        with suppress(Error):
            job_ops.record_failure(store, name, Error(msg), msg, dry_run=False)
        if failures >= _MAX_STREAM_FAILS:
            fatal.append(msg)
            return
        stopped.wait(min(300.0, job_ops.BACKOFF * 2 ** max(0, failures - 1)))


class Dispatcher:
    """Manage daemon stream jobs and run non-blocking event/wake/delivery passes."""

    def __init__(self, store: Store, names: set[str]) -> None:
        self.store, self.names, self.stopped, self._lock = store, names, Event(), Lock()
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self.daemons: dict[str, Thread] = {}
        self.tasks: dict[str, Future[object]] = {}
        self.health: dict[str, str] = {}
        self._fatal: list[str] = []
        self._pool, self._next_compact = ThreadPoolExecutor(max_workers=2), time.monotonic() + _COMPACT_EVERY

    def __enter__(self) -> Dispatcher:
        cfg = config.load(self.store)
        if (q := Queue(self.store)).exists():
            q.recover_attempts()
        for name in sorted(self.names):
            if (job := cfg.jobs.get(name)) is not None and job.enabled and job.is_daemon:
                args = (self.store, name, self.stopped, self._procs, self._lock, self._fatal)
                (t := Thread(target=_stream_job, args=args, daemon=True)).start()
                self.daemons[name] = t
        return self

    def __exit__(self, *_: object) -> None:
        self.stopped.set()
        with self._lock:
            active = list(self._procs.values())
        for proc in active:
            job_ops.stop(proc)
        for t in self.daemons.values():
            t.join(timeout=5)
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _reactive_pass(self, *, sync: bool = False) -> None:
        cfg, queue = config.load(self.store), Queue(self.store)
        if sync:
            try:
                index.refresh(self.store, wait=10, recover=True)
                sync_item_events(self.store)
                self.health.pop("sync", None)
            except (Error, OSError, UnicodeError) as error:
                self.health["sync"] = str(error) if isinstance(error, Error) else "index sync failed; run bf status"
        if not queue.exists():
            return
        non_daemon = [n for n in sorted(self.names) if (j := cfg.jobs.get(n)) and j.enabled and not j.is_daemon]
        try:
            job_ops.dispatch_events(self.store, non_daemon)
            self.health.pop("events", None)
        except (Error, OSError, UnicodeError) as error:
            self.health["events"] = str(error) if isinstance(error, Error) else "event dispatch failed; run bf status"
        update.run_wakes(self.store, cfg, non_daemon, stopped=self.stopped)
        update.run_deliveries(self.store, stopped=self.stopped)
        if time.monotonic() >= self._next_compact:
            self._next_compact = time.monotonic() + _COMPACT_EVERY
            with suppress(Error, OSError):
                queue.compact()

    def tick(self, *, force: bool = False) -> None:
        if self._fatal:
            raise StreamStoppedError(self._fatal[0])
        for key in [k for k, f in self.tasks.items() if f.done()]:
            if err := self.tasks.pop(key).exception():
                self.health[key] = str(err) if isinstance(err, Error) else f"{key} pass failed; inspect bf status"
            else:
                self.health.pop(key, None)
        if "reactive" not in self.tasks and (force or Queue(self.store).exists()):
            self.tasks["reactive"] = self._pool.submit(self._reactive_pass, sync=force)


class _Supervisor:
    def __init__(self, store: Store, jobs: tuple[str, ...], stack: ExitStack) -> None:
        self.store, self.jobs, self.stack, self.due, self.reason, self.woke = store, jobs, stack, 0.0, "", False
        self.dispatcher: Dispatcher | None = None
        _set_daemon_health(store, "", "")
        stack.callback(_set_daemon_health, store, "", "")

    def tick(self, now: float) -> str | None:
        if now < self.due and not self.woke:
            return None
        force, self.due, self.woke = self.woke, now + TICK, False
        try:
            if (d := self.dispatcher) is None:
                sel = update.selection(config.load(self.store), self.jobs)
                d = self.dispatcher = self.stack.enter_context(Dispatcher(self.store, sel))
            d.tick(force=force)
            err = next((e for e in d.health.values() if e), "")
            if not err and d.daemons and (q := Queue(d.store)).exists():
                daemons = sorted(d.daemons)
                err = next((str(e) for n in daemons if (e := q.progress(f"health:daemon:{n}").get("error"))), "")
        except StreamStoppedError:
            raise
        except (Error, OSError, UnicodeError) as error:
            msg = "Dispatcher cannot read runtime state; run bf status for diagnostics"
            err = clean(str(error)) if isinstance(error, Error) else msg
        if err == self.reason:
            return None
        self.reason = err
        return err


@contextmanager
def keyboard(stream: TextIO) -> Generator[int]:
    descriptor = stream.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        yield descriptor
    finally:
        with suppress(termios.error):
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def read_keys(descriptor: int, timeout: float) -> list[str]:
    if not select.select([descriptor], [], [], timeout)[0]:
        return []
    if not (data := os.read(descriptor, 64)):
        return ["q"]
    if PARTIAL.search(data) and select.select([descriptor], [], [], 0.03)[0]:
        data += os.read(descriptor, 64)
    return SEQUENCE.findall(data.decode("utf-8", errors="replace"))


def _stop(job: Job) -> None:
    with job_ops.starting():
        job.close()


def watch(
    store: Store,
    *,
    jobs: tuple[str, ...] = (),
    interval: int | None = None,
    poll_interval: float | None = None,
    notifications: str | None = None,
    observe: bool = False,
    json_output: bool = False,
) -> None:
    """A foreground controller; no OS scheduler installation or background daemon outside watch."""
    prefs = settings(store, interval=interval, poll_interval=poll_interval, notifications=notifications)
    no_tty = sys.stdin is None or not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM") == "dumb"
    if not json_output and no_tty:
        obs = "status --watch needs an interactive terminal; use bf status for a JSON snapshot"
        raise Error(obs if observe else "watch needs an interactive terminal; use bf watch --json or bf status")
    rows, inc = snapshot(store, jobs), _incidents(store)
    dashboard = Dashboard(config.load(store).name, observe=observe, rows=rows, preferences=prefs, incidents=inc)
    job = Job(store, jobs)
    with ExitStack() as stack:
        if not observe:
            try:
                stack.enter_context(collecting(store, ".watch"))
            except BusyError:
                if json_output:
                    raise Error("another watcher owns this brain; open bf watch in a terminal to observe it") from None
                dashboard.observe = True
                dashboard.message = "Observing an active watcher; its selection and settings control collection"
        if not dashboard.observe and (reason := update.late(config.load(store), prefs.interval, jobs)):
            dashboard.late = f"{reason}; lower the watch interval"
            if json_output:
                sys.stderr.write(f"bf: {printable(dashboard.late)}\n")
        stack.callback(_stop, job)
        sup = _Supervisor(store, jobs, stack.enter_context(ExitStack())) if not dashboard.observe else None
        stack.callback(job.terminate)
        if json_output:
            _loop(store, dashboard, job, jobs, supervisor=sup)
            return
        c = Console(markup=False, highlight=False)
        live_ctx = Live(console=c, screen=True, auto_refresh=False, redirect_stdout=False, redirect_stderr=False)
        with keyboard(sys.stdin) as desc, live_ctx as live:
            _loop(store, dashboard, job, jobs, supervisor=sup, live=live, descriptor=desc)


def _loop(
    store: Store,
    dashboard: Dashboard,
    job: Job,
    jobs: tuple[str, ...] = (),
    *,
    supervisor: _Supervisor | None = None,
    live: Live | None = None,
    descriptor: int = -1,
) -> None:
    prefs, next_read, next_frame = dashboard.preferences, 0.0, 0.0
    size, alerts, prev, completed, refresh_req, invalid = (0, 0), Notifications(prefs), {}, None, False, False
    wake_mtime = wakeup_mtime(store)
    while True:
        now, reason = time.monotonic(), None
        if (wm := wakeup_mtime(store)) != wake_mtime:
            wake_mtime, next_read = wm, 0.0
            if supervisor is not None:
                supervisor.due, supervisor.woke = 0.0, True
        if supervisor is not None and (d := supervisor.dispatcher) is not None:
            dashboard.assisting = "reactive" in d.tasks or bool(d.daemons)
        if dashboard.running and (code := job.poll()) is not None:
            skipped = job.skipped() if code else 0
            job.discard()
            dashboard.running, code = False, 0 if skipped else code
            fail = "Update failed; inspect job details or run bf update for diagnostics"
            sk_msg = f"Search cache skipped {skipped} file{'' if skipped == 1 else 's'}; run bf validate"
            dashboard.message = sk_msg if skipped else fail if code else "Update complete"
            completed, dashboard.next_check, next_read = completed or code, now + prefs.interval, 0
        if now >= next_read:
            try:
                dashboard.replace_rows(snapshot(store, jobs))
                dashboard.incidents = _incidents(store)
                if invalid:
                    dashboard.message = "Configuration recovered"
                invalid = False
                if completed is not None:
                    inc = [r for r in dashboard.rows if r.included]
                    fails = tuple(sorted(f"{r.emit}:{r.name}:{r.error}" for r in inc if r.status == State.FAILED))
                    worked = any(r.success and r.success != prev.get(r.name, "") for r in inc)
                    err = fails or (("update failed",) if completed else ())
                    completed, prev = None, {r.name: r.success for r in dashboard.rows}
                    if warning := alerts.completed(failures=err, worked=worked, now=now):
                        dashboard.message = warning
                        if live is None:
                            sys.stderr.write(f"bf: {printable(warning)}\n")
            except (Error, OSError, UnicodeError) as error:
                dashboard.rows, dashboard.incidents, invalid = [], [], True
                msg = "Cannot read bf.yaml or runtime state; run bf status for diagnostics"
                dashboard.message = clean(str(error)) if isinstance(error, Error) else msg
                if supervisor is not None and dashboard.message != supervisor.reason:
                    reason = supervisor.reason = dashboard.message
            due = refresh_req or now >= dashboard.next_check
            if due and not (invalid or dashboard.observe or dashboard.paused or dashboard.running):
                prev.update({r.name: r.success for r in dashboard.rows if not r.daemon or r.name not in prev})
                job.start()
                refresh_req, dashboard.running = False, True
                dashboard.message = "Running due jobs; existing per-job limits apply"
            if live is None:
                d = dashboard
                head = {"brain": d.brain, "running": d.running, "assisting": d.assisting, "message": d.message}
                if d.incidents:
                    head["incidents"] = d.incidents
                sys.stdout.buffer.write(terminal(present({**head, "jobs": [asdict(r) for r in d.rows]})))
                sys.stdout.flush()
            next_read, next_frame = now + prefs.poll_interval, 0
        if supervisor is not None and not dashboard.paused and not invalid:
            reason = supervisor.tick(now)
        if reason is not None:
            dashboard.waiting, dashboard.message, next_frame = reason, reason or "Dispatcher resumed", 0
            if reason and live is None:
                sys.stderr.write(f"bf: {printable(reason)}\n")
        if live is None:
            time.sleep(0.1)
            continue
        current = (live.console.width, live.console.height)
        if now >= next_frame or current != size:
            live.update(dashboard.render(*current), refresh=True)
            size, next_frame = current, now + 1
        wait = max(0.0, min(next_frame, next_read, now + (0.25 if dashboard.running else 1)) - now)
        for key in read_keys(descriptor, wait):
            if (action := dashboard.key(key)) == "quit":
                return
            if action == "refresh":
                refresh_req, next_read = True, 0
                fb = "Reloading bf.yaml and checking due jobs"
                dashboard.message = next((m for a, m in _REFRESH_MSG if getattr(dashboard, a)), fb)
            next_frame = 0
