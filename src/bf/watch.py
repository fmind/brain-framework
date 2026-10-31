"""A small terminal dashboard over local run history and cancellable update processes."""

from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import tempfile
import termios
import time
import tty
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import IO, TextIO, cast

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bf.assistants import Dispatcher, StreamStoppedError
from bf.collect import next_due, starting
from bf.config import load
from bf.health import assistant_health
from bf.history import ROUTINES, SENSORS, environment, log_path, state
from bf.models import Error, WatchSettings, decode, local, present, printable, terminal, timestamp
from bf.runtime import Queue
from bf.storage import BusyError, Store, collecting
from bf.update import KINDS, ProgramKind, late, selection
from bf.watch_settings import Notifications, settings


class State(StrEnum):
    """A program's local state; declaration order is the state sort order."""

    FAILED = "failed"
    NEVER = "never"
    DUE = "due"
    FRESH = "fresh"
    MANUAL = "manual"
    DISABLED = "disabled"


ATTENTION = frozenset({State.FAILED, State.NEVER, State.DUE})
COLORS = {State.FAILED: "red", State.NEVER: "yellow", State.DUE: "yellow", State.FRESH: "green"}


@dataclass
class Row:
    kind: str
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
    # An assistant's successful runs and the record its latest run wrote.
    runs: int | None = None
    record: str = ""


# The table marks each kind beside its name; sensors carry none.
MARKERS = {"routine": "* ", "assistant": "@ "}


def snapshot(
    store: Store, sensors: tuple[str, ...] = (), routines: tuple[str, ...] = (), assistants: tuple[str, ...] = ()
) -> list[Row]:
    """Read only configuration and small local history files; never scan evidence or run providers."""
    config = load(store)
    selected = selection(config, sensors, routines, assistants)
    now = datetime.now(UTC)
    rows = []
    for kind, programs, file, included in (
        ("sensor", config.sensors, SENSORS, selected[0]),
        ("routine", config.routines, ROUTINES, selected[1]),
    ):
        # state() validates every entry, so each present field already has its history type.
        history = state(store, file)
        for name, program in sorted(programs.items()):
            entry = history.get(name, {})
            # A disabled program's last failure is history, as in bf status: it shows no error.
            success, error = str(entry.get("success", "")), str(entry.get("error", "")) if program.enabled else ""
            # The same due rule as update, including failure backoff.
            due_at = next_due(program, entry, now)
            status = (
                State.DISABLED
                if not program.enabled
                else State.MANUAL
                if not program.refresh
                else State.FAILED
                if error
                else State.NEVER
                if not success
                else State.DUE
                if due_at is not None and due_at <= now
                else State.FRESH
            )
            rows.append(
                Row(
                    kind=kind,
                    name=name,
                    status=status,
                    included=name in included,
                    refresh=program.refresh,
                    # Run history already holds canonical UTC instants.
                    success=success,
                    next_due=timestamp(due_at.isoformat()) if due_at else "",
                    added=cast(int | None, entry.get("added")),
                    updated=cast(int | None, entry.get("updated")),
                    removed=cast(int | None, entry.get("removed")),
                    elapsed_seconds=cast(float | None, entry.get("elapsed_seconds")),
                    output_bytes=cast(int | None, entry.get("output_bytes")),
                    reconcile=bool(entry.get("reconcile", False)),
                    error=error,
                    log=log_path(name) if error else "",
                    action=str(entry.get("action", "")),
                    records=cast(int | None, entry.get("records")),
                )
            )
    # The same bf.yaml as the rows above, even when an editor saves it meanwhile.
    for name, health in assistant_health(store, now=now, config=config).items():
        settings = config.assistants[name]
        schedules = cast(dict[str, dict[str, object]], health["schedules"])
        # A queue that cannot be read states no deadline.
        deadlines = [str(item["next_due"]) for item in schedules.values() if "next_due" in item]
        due = min(deadlines) if deadlines else ""
        success = str(health.get("last_success", ""))
        error = str(health.get("error", ""))
        status = (
            State.DISABLED
            if not settings.enabled
            else State.FAILED
            if error
            else State.MANUAL
            if not schedules
            else State.NEVER
            if health["freshness"] == "never"
            else State.DUE
            if due and datetime.fromisoformat(due) <= now
            else State.FRESH
        )
        rows.append(
            Row(
                kind="assistant",
                name=name,
                status=status,
                included=name in selected[2],
                refresh=min((spec.every for spec in settings.schedules.values()), default=0),
                success=success,
                next_due=timestamp(due) if due else "",
                error=error,
                log=log_path(name) if error else "",
                runs=cast(int, health["runs"]),
                record=str(health.get("record", "")),
            )
        )
    return rows


def clean(value: str) -> str:
    """Terminal control characters in paths or errors are data, never terminal instructions."""
    return "".join(char if char.isprintable() else " " for char in value)


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def period(seconds: int) -> str:
    """A configured period in its largest exact unit: unlike an age, 90 seconds is 90s, never 1m."""
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size and not seconds % size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


class Sort(StrEnum):
    NAME = "name"
    UPDATED = "last OK"
    ITEMS = "items"
    STATE = "state"
    DUE = "next due"
    CHANGES = "changes"
    DURATION = "duration"
    OUTPUT = "output bytes"
    REFRESH = "refresh"
    KIND = "kind"

    @property
    def descending(self) -> bool:
        return self in {Sort.UPDATED, Sort.ITEMS, Sort.CHANGES, Sort.DURATION, Sort.OUTPUT}

    def value_for(self, row: Row) -> str | float | None:
        match self:
            case Sort.NAME:
                return row.name
            case Sort.UPDATED:
                return datetime.fromisoformat(row.success).timestamp() if row.success else None
            case Sort.ITEMS:
                return row.records
            case Sort.STATE:
                return list(State).index(row.status)
            case Sort.DUE:
                return datetime.fromisoformat(row.next_due).timestamp() if row.next_due else None
            case Sort.CHANGES:
                if row.added is None or row.updated is None or row.removed is None:
                    return None
                return row.added + row.updated + row.removed
            case Sort.DURATION:
                return row.elapsed_seconds
            case Sort.OUTPUT:
                return row.output_bytes
            case Sort.REFRESH:
                return row.refresh
            case Sort.KIND:
                return row.kind


# Terminals send different sequences for one key: CSI (ESC [), application mode (ESC O) and rxvt forms.
KEYS = {
    "Q": "q",
    "\x1b[A": "k",
    "\x1bOA": "k",
    "\x1b[B": "j",
    "\x1bOB": "j",
    "\x1b[H": "g",
    "\x1bOH": "g",
    "\x1b[1~": "g",
    "\x1b[7~": "g",
    "\x1b[F": "G",
    "\x1bOF": "G",
    "\x1b[4~": "G",
    "\x1b[8~": "G",
}
SEQUENCE = re.compile(r"\x1b\[[0-9;]*[~A-Za-z]|\x1bO[A-Za-z]|\x1b|[^\x1b]")
PARTIAL = re.compile(rb"\x1b(?:\[[0-9;]*|O)?\Z")
# Screen lines outside the program rows: header and summary; message, two key lines and boundary;
# panel borders plus the column header and its rule; details stacked below a long table on narrow terminals.
HEADER, FOOTER, FRAME, DETAILS = 2, 4, 4, 8
# Details sit beside the table only when it keeps at least 80 columns for its six columns.
WIDE = 120
# Seconds a stopped update may take to roll back an interrupted record commit before it is killed.
STOP = 60
# Seconds between dispatcher passes in either mode: streamed input starts within about a second, and an idle watcher
# leaves the queue alone in between.
TICK = 1.0


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
    # Assistant work runs in the watcher's dispatcher beside the collection cycle.
    assisting: bool = False
    # Why the dispatcher cannot run assistant work now; it retries on its next pass.
    waiting: str = ""
    message: str = "Ready"
    # Why the check interval runs a selected program late, shown in the header and the guide.
    late: str = ""
    next_check: float = 0
    preferences: WatchSettings = field(default_factory=WatchSettings)
    sort: Sort = Sort.NAME
    descending: bool = False

    def visible(self) -> list[Row]:
        rows = sorted(
            (row for row in self.rows if not self.attention or row.status in ATTENTION),
            key=lambda row: (row.name, row.kind),
        )
        # Stable ties stay alphabetical, and unknown values stay last in either direction.
        known = [(row, value) for row in rows if (value := self.sort.value_for(row)) is not None]
        unknown = [row for row in rows if self.sort.value_for(row) is None]
        return [row for row, _ in sorted(known, key=lambda item: item[1], reverse=self.descending)] + unknown

    def selected(self) -> Row | None:
        visible = self.visible()
        return visible[min(self.cursor, len(visible) - 1)] if visible else None

    def select(self, selected: Row | None) -> None:
        visible = self.visible()
        self.cursor = next(
            (i for i, row in enumerate(visible) if selected and (row.kind, row.name) == (selected.kind, selected.name)),
            max(0, min(self.cursor, len(visible) - 1)),
        )

    def replace_rows(self, rows: list[Row]) -> None:
        """Keep the same program selected when polling changes its position or state."""
        selected = self.selected()
        self.rows = rows
        self.select(selected)

    def key(self, key: str) -> str:
        """Return a controller action without touching configuration or executing programs."""
        key = KEYS.get(key, key)
        if key == "q":
            return "quit"
        if key == "j":
            self.cursor += 1
        elif key == "k":
            self.cursor -= 1
        elif key == " ":
            if not self.observe:
                self.paused = not self.paused
        elif key == "\t":
            self.attention = not self.attention
            self.cursor = 0
        elif key == "?":
            self.help = not self.help
        elif key in {"f", "u"}:
            return "refresh"
        elif key in {"s", "n", "t", "i", "r"}:
            selected = self.selected()
            if key == "r":
                self.descending = not self.descending
            else:
                sorts = list(Sort)
                self.sort = (
                    sorts[(sorts.index(self.sort) + 1) % len(sorts)]
                    if key == "s"
                    else {"n": Sort.NAME, "t": Sort.UPDATED, "i": Sort.ITEMS}[key]
                )
                self.descending = self.sort.descending
            self.select(selected)
        elif key == "g":
            self.cursor = 0
        elif key == "G":
            self.cursor = len(self.visible()) - 1
        self.cursor = max(0, min(self.cursor, len(self.visible()) - 1))
        return ""

    def guide(self) -> Text:
        poll = f"{self.preferences.poll_interval:g}s"
        if self.observe:
            # An observer neither executes nor pauses anything; `u` only rereads local history.
            mode = [
                f"History: {poll} · this view never runs programs or sends alerts.",
                "u rereads history; q closes this view, not the active collector.",
            ]
        else:
            mode = [
                f"Check: {period(self.preferences.interval)} · history: {poll} · alerts: "
                + self.preferences.notifications,
                *([self.late + "."] if self.late else []),
                "Configure watch in bf.yaml; restart to apply.",
                "f (or u) reloads bf.yaml and checks due programs now.",
                "While updating, refresh queues one check; pause still applies.",
                "Selection, refresh and backoff apply; assistants run beside collection.",
                "Space pauses future checks; q cancels an active update.",
            ]
        # The footer stays visible below the guide and lists every key. At 80 by 24 these lines fill the panel, so the
        # freshness boundary precedes the mode lines, whose late warning can wrap onto a third line.
        lines = [
            "s cycles: " + ", ".join(Sort) + ".",
            "Newest/largest first; name, state, due, refresh and kind ascend.",
            "Unknown values stay last; ties use name. Sorting is session-only.",
            "Items: records the last committed collection returned, not total stored.",
            "+ added / ~ updated / - removed; changes = sum. * routine, @ assistant",
            "Failed runs keep prior counts. — means unknown or not applicable.",
            "Fresh is local success, not proof the provider is unchanged.",
            *mode,
        ]
        return Text("\n".join(lines))

    def details(self, row: Row) -> Text:
        detail = Text()
        detail.append(clean(row.name) + "\n", style="bold cyan")
        # An observer's selectors filter only its own view; the active collector keeps its selection.
        scope = (
            ("" if row.included else "excluded from this view")
            if self.observe
            else ("selected" if row.included else "excluded")
        )
        detail.append(" · ".join(part for part in (row.kind, row.status, scope) if part) + "\n")
        # Diagnostics come first: details stacked below a long table show only their first lines.
        if row.error:
            detail.append(clean(row.error) + "\n", style="red")
            detail.append("Log: " + clean(row.log) + "\n", style="dim")
        elif row.action:
            detail.append("Latest action: " + clean(row.action) + "\n")
        if row.runs is not None:
            detail.append(f"Runs: {row.runs:,}\n")
        if row.record and not row.error:
            detail.append("Latest record: " + clean(row.record) + "\n")
        detail.append(f"Refresh: {period(row.refresh) if row.refresh else 'manual only'}\n")
        detail.append("Last OK: " + (local(row.success) if row.success else "never") + "\n")
        detail.append("Last run items: " + ("—" if row.records is None else f"{row.records:,}") + "\n")
        if row.elapsed_seconds is not None and row.output_bytes is not None:
            detail.append(f"Last run: {row.elapsed_seconds:.2f}s · {row.output_bytes:,} bytes\n")
            if row.reconcile:
                detail.append("Wider reconciliation window\n", style="cyan")
        # An observer suggests no executing command, and neither bf collect nor bf run runs a disabled program.
        if row.kind == "assistant" and not (self.observe or row.error) and row.status is not State.DISABLED:
            detail.append(f"Inputs and named schedules queue work. Use bf run {clean(row.name)} to ask now.")
        elif not (self.observe or row.error or row.action) and row.status is not State.DISABLED:
            # bf collect runs only sensors and bf run routines.
            command = "bf collect" if row.kind == "sensor" else "bf run"
            detail.append(f"Only due programs run here; {command} {clean(row.name)} runs it now.")
        detail.rstrip()
        return detail

    def render(self, width: int, height: int, now: datetime | None = None) -> Layout:
        """Responsive table, selected-program details, and always-visible operating boundaries."""
        now = now or datetime.now(UTC)
        mode = "OBSERVE · no execution" if self.observe else "PAUSED" if self.paused else "WATCH · collection enabled"
        if self.late:
            # The header keeps the hint whole at 80 columns, where the summary's counts and countdown fill the line.
            mode += " · interval too slow, press ?"
        # A paused watcher starts no check, so a countdown would claim one that waits for Space.
        working = (
            "Updating…"
            if self.running
            else "Checks paused"
            if self.paused
            else f"Next check in {duration(self.next_check - time.monotonic())}"
        )
        if self.assisting:
            working += " · assistant running"
        if self.waiting:
            working += " · assistants waiting"
        header = Text.assemble(
            (" BF ", "bold white on blue"),
            (f"  {clean(self.brain)}", "bold cyan"),
            (f"    {mode}", "yellow" if self.paused else "cyan"),
            no_wrap=True,
            overflow="ellipsis",
        )
        count, needing = len(self.rows), sum(row.status in ATTENTION for row in self.rows)
        summary = Text(
            f"{count} program{'' if count == 1 else 's'}   "
            + ("" if self.observe else f"{sum(row.included for row in self.rows)} selected   ")
            + f"{needing} need{'s' if needing == 1 else ''} attention   "
            + ("Reading local history" if self.observe else working),
            style="dim",
            no_wrap=True,
            overflow="ellipsis",
        )
        visible = self.visible()
        self.cursor = max(0, min(self.cursor, len(visible) - 1))
        wide = width >= WIDE
        body = height - HEADER - FOOTER
        # Stacked details keep DETAILS lines below a long table and take the space a short table leaves.
        table_height = body if wide else min(body, FRAME + max(1, len(visible)), max(FRAME + 1, body - DETAILS))
        limit = max(0, table_height - FRAME)
        offset = max(0, self.cursor - max(1, limit) + 1)
        shown = visible[offset : offset + limit]
        # Without an outer edge, every table line is counted by FRAME; the panel already draws the border.
        table = Table(
            expand=True,
            box=box.SIMPLE,
            show_edge=False,
            collapse_padding=True,
            pad_edge=False,
            header_style="bold cyan",
            padding=(0, 1),
        )
        # Short columns keep their values whole; only the program name shortens on narrow terminals.
        table.add_column("Program", no_wrap=True, overflow="ellipsis", ratio=1, min_width=12)
        table.add_column("State", no_wrap=True, min_width=8)
        table.add_column("Last OK", no_wrap=True, min_width=8)
        table.add_column("Next due", no_wrap=True)
        table.add_column("Items", no_wrap=True, justify="right")
        table.add_column("+/~/-", no_wrap=True)
        for index, row in enumerate(shown, offset):
            success = datetime.fromisoformat(row.success) if row.success else None
            due = datetime.fromisoformat(row.next_due) if row.next_due else None
            table.add_row(
                # The routine marker leads so that shortening a long name never hides it.
                Text(("> " if index == self.cursor else "  ") + MARKERS.get(row.kind, "") + row.name),
                Text(row.status, style=COLORS.get(row.status, "dim")) if row.included else Text("excluded", "dim"),
                Text(duration((now - success).total_seconds()) + " ago" if success else "never"),
                Text("—" if due is None else "now" if due <= now else duration((due - now).total_seconds())),
                Text("—" if row.records is None else str(row.records)),
                Text("/".join("—" if value is None else str(value) for value in (row.added, row.updated, row.removed))),
                # A light theme's dark default text would be unreadable on this background.
                style="white on #183040" if index == self.cursor else "",
            )
        if not visible:
            table.add_row("No matching programs", "", "", "", "", "")
        title = (
            f"{'Attention' if self.attention else 'Programs'} · "
            f"{f'{offset + 1}-{offset + len(shown)}' if shown else '0-0'}/{len(visible)} · "
            f"{self.sort} {'↓' if self.descending else '↑'}"
        )
        if self.help:
            detail = self.guide()
        elif visible:
            detail = self.details(visible[self.cursor])
        else:
            # An attention filter that hides every program is the healthy case, not a brain without programs.
            detail = Text(
                "Nothing needs attention; press Tab to show every program."
                if self.attention and self.rows
                else "Add a reviewed sensor, routine or assistant in bf.yaml. Press ? for controls and timing."
            )
        keys = (
            "s sort  r reverse  n name  t last OK  i items  ? help  q quit\nj/k ↑/↓ select  g/G first/last  Tab attention  "
            + ("u reread" if self.observe else "f refresh  Space pause")
        )
        boundary = "Local run history · no provider text · " + (
            "observation only" if self.observe else "closing watch stops its collection"
        )
        layout = Layout()
        # Fixed-height lines truncate instead of wrapping, so long names or messages never hide other lines.
        layout.split_column(
            Layout(Group(header, summary), size=HEADER),
            Layout(name="body"),
            Layout(Text(clean(self.message), style="yellow", no_wrap=True, overflow="ellipsis"), size=1),
            Layout(Text(keys, style="bold cyan", no_wrap=True, overflow="ellipsis"), size=2),
            Layout(Text(boundary, style="dim", no_wrap=True, overflow="ellipsis"), size=1),
        )
        programs = Panel(table, title=title, border_style="blue")
        details = Panel(detail, title="Guide" if self.help else "Details", border_style="cyan")
        if self.help:
            layout["body"].update(details)
        elif wide:
            layout["body"].split_row(Layout(programs), Layout(details, size=width // 3))
        elif body - table_height > 2:
            layout["body"].split_column(Layout(programs, size=table_height), Layout(details))
        else:
            # Too short for a single detail line: the program rows matter more.
            layout["body"].update(programs)
        return layout


class Job:
    """A separate BF process keeps keyboard input responsive and reuses provider cancellation."""

    def __init__(
        self,
        store: Store,
        sensors: tuple[str, ...],
        routines: tuple[str, ...],
        assistants: tuple[str, ...] = (),
        kinds: frozenset[ProgramKind] = KINDS,
    ) -> None:
        # Like -I without -s: PYTHON* variables and the working directory never change what runs, while a user-site
        # installation, such as pip install --user, still imports bf.
        self.argv = [sys.executable, "-E", "-P", "-m", "bf", "update", "--brain", str(store.root)]
        for flag, names in (("--sensor", sensors), ("--routine", routines), ("--assistant", assistants)):
            for name in names:
                self.argv.extend((flag, name))
        if kinds != KINDS:
            for kind in sorted(kinds):
                self.argv.extend(("--kind", kind))
        self.process: subprocess.Popen[bytes] | None = None
        self.reply: IO[bytes] | None = None
        self.stopping = False

    def start(self) -> None:
        self.discard()
        self.stopping = False
        # BF's own JSON reply, in an unnamed private file: it tells skipped files apart from failed programs.
        self.reply = tempfile.TemporaryFile()  # noqa: SIM115 - kept until the next start or close
        # A cancellation arriving while the child starts waits until close() can find and stop it.
        with starting():
            self.process = subprocess.Popen(  # noqa: S603 - fixed BF entry point, literal validated selection
                self.argv,
                stdin=subprocess.DEVNULL,
                stdout=self.reply,
                stderr=subprocess.DEVNULL,
                env=environment(),
                start_new_session=True,
            )

    def poll(self) -> int | None:
        return self.process.poll() if self.process is not None else None

    def skipped(self) -> int:
        """Files the refreshed search cache skipped when they alone failed a finished update; otherwise 0."""
        if self.reply is None:
            return 0
        try:
            self.reply.seek(0)
            value = decode(self.reply.read(1 << 20))
        except Error, OSError:
            return 0
        if not isinstance(value, dict):
            return 0
        programs = [
            *cast(list[object], value.get("sensors") or []),
            *cast(list[object], value.get("routines") or []),
            *cast(list[object], value.get("assistants") or []),
        ]
        cache = value.get("index")
        if any(not isinstance(item, dict) or item.get("status") == "failed" for item in programs):
            return 0
        skipped = cache.get("skipped") if isinstance(cache, dict) and "error" not in cache else None
        return skipped if type(skipped) is int and skipped > 0 else 0

    def discard(self) -> None:
        if self.reply is not None:
            self.reply.close()
            self.reply = None

    def terminate(self) -> None:
        """Ask a running update to stop without waiting, so it stops while the dispatcher stops its own work.

        One request only: a second SIGTERM would interrupt the update while it stops its own programs.
        """
        if self.process is not None and self.process.poll() is None and not self.stopping:
            self.stopping = True
            self.process.terminate()

    def close(self) -> None:
        """Stop a running update within STOP seconds, then kill it."""
        if self.process is not None and self.process.poll() is None:
            self.terminate()
            end = time.monotonic() + STOP
            try:
                try:
                    self.process.wait(timeout=max(0, min(1, end - time.monotonic())))
                except subprocess.TimeoutExpired:
                    # The dashboard is gone: say why the terminal waits instead of seeming to hang. A closed
                    # terminal must not skip the wait and kill below, which would leave the update unsupervised.
                    with suppress(OSError, ValueError):
                        sys.stderr.write(f"bf: stopping the active update; this can take up to {STOP} seconds\n")
                        sys.stderr.flush()
                    self.process.wait(timeout=max(0, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.discard()


def _settle(store: Store) -> None:
    """Stream, read and lane health describes a running dispatcher: clear what one left once it stopped or crashed.

    Never creates the queue; a queue execution refuses keeps its entries for bf status to show.
    """
    queue = Queue(store)
    with suppress(Error, OSError):
        if queue.exists():
            with queue.connect() as db:
                db.execute("DELETE FROM checkpoints WHERE key LIKE 'health:%'")


def _failing(dispatcher: Dispatcher) -> str:
    """Why assistant work waits, or "": a failing pass, an assistant whose schedules cannot start, a
    failing read or stream input, then a delivery that failed or is uncertain.

    Read failures are the dispatcher's own; stream workers and deliveries record theirs in the queue.
    """
    health = dispatcher.health
    for prefix in ("dispatcher", "assistant:"):
        if error := next((error for key, error in sorted(health.items()) if key.startswith(prefix) and error), ""):
            return error
    for key, error in sorted(health.items()):
        if key.startswith("read:") and error:
            channel = key.split(":")[2]
            failed = f"channel {channel}: read failed: "
            if error.startswith(failed):
                return error
            return f"{failed}{error.removeprefix(f'channel {channel}: ')}; see {log_path(f'channel.{channel}.read')}"
    for channel in sorted(dispatcher.transports):
        if error := str(dispatcher.queue.progress(f"health:channel:{channel}").get("error") or ""):
            return error if error.startswith(f"channel {channel}: ") else f"channel {channel}: {error}"
    if failed := sorted(
        str(item["channel"]) for item in dispatcher.queue.outstanding(dispatcher.names) if item["error"]
    ):
        return f"channel {failed[0]}: delivery failed or uncertain; inspect bf queue"
    return ""


class Assistants:
    """Assistant work beside collection: the dispatcher starts once it can, and a pass that fails is shown and retried.

    Only a stopped stream worker stays fatal, as the dispatcher reports it: restarting watch reconnects it.
    """

    def __init__(
        self, store: Store, selected: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], stack: ExitStack
    ) -> None:
        self.store, self.selected, self.stack = store, selected, stack
        self.dispatcher: Dispatcher | None = None
        self.due = 0.0
        self.reason = ""
        _settle(store)
        # Registered first, so it runs once the dispatcher has stopped its streams and workers.
        stack.callback(_settle, store)

    @property
    def running(self) -> bool:
        return self.dispatcher is not None and bool({"reactive", "proactive"} & self.dispatcher.tasks.keys())

    def tick(self, now: float) -> str | None:
        """One pass at most every TICK seconds: why it failed, "" once passes succeed again, or None when unchanged."""
        if now < self.due:
            return None
        self.due = now + TICK
        try:
            if self.dispatcher is None:
                # The current selection: a retry must not start an assistant that bf.yaml no longer declares.
                names = selection(load(self.store), *self.selected)[2]
                self.dispatcher = self.stack.enter_context(Dispatcher(self.store, names))
            self.dispatcher.tick()
            # The dispatcher keeps running past a failing pass, input or delivery and records why.
            reason = _failing(self.dispatcher)
        except StreamStoppedError:
            raise
        except (Error, OSError, UnicodeError) as error:
            reason = (
                "Another bf update owns assistant work; assistants start once it finishes"
                if isinstance(error, BusyError) and self.dispatcher is None
                # BF's own diagnostic names the invalid field, file or capacity without quoting content.
                else clean(str(error))
                if isinstance(error, Error)
                else "Assistant work cannot read its files; run bf status for diagnostics"
            )
        return self.report(reason)

    def report(self, reason: str) -> str | None:
        """Why assistant work waits, "" once it resumes, or None when unchanged: each reason is shown once."""
        if reason == self.reason:
            return None
        self.reason = reason
        return reason


@contextmanager
def keyboard(stream: TextIO) -> Generator[int]:
    descriptor = stream.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        yield descriptor
    finally:
        # A closed terminal needs no restoring, and its error must not replace the interrupt that closing it raised.
        with suppress(termios.error):
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def read_keys(descriptor: int, timeout: float) -> list[str]:
    """Split each read into keys: held keys, pastes and remote terminals deliver several at once."""
    if not select.select([descriptor], [], [], timeout)[0]:
        return []
    data = os.read(descriptor, 64)
    if not data:
        return ["q"]
    # An escape sequence may arrive split across reads; wait briefly for its end.
    if PARTIAL.search(data) and select.select([descriptor], [], [], 0.03)[0]:
        data += os.read(descriptor, 64)
    return SEQUENCE.findall(data.decode("utf-8", errors="replace"))


def watch(
    store: Store,
    *,
    sensors: tuple[str, ...] = (),
    routines: tuple[str, ...] = (),
    assistants: tuple[str, ...] = (),
    interval: int | None = None,
    poll_interval: float | None = None,
    notifications: str | None = None,
    observe: bool = False,
    json_output: bool = False,
) -> None:
    """A foreground controller; no daemon, scheduler installation or provider credentials are created."""
    preferences = settings(store, interval=interval, poll_interval=poll_interval, notifications=notifications)
    # Python sets sys.stdin to None when descriptor 0 was closed at startup: no terminal either.
    if not json_output and (
        sys.stdin is None or not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM") == "dumb"
    ):
        # Never suggest an executing command to someone who asked only to observe.
        raise Error(
            "status --watch needs an interactive terminal; use bf status for a JSON snapshot"
            if observe
            else "watch needs an interactive terminal; use bf watch --json or bf status"
        )
    dashboard = Dashboard(
        load(store).name, observe=observe, rows=snapshot(store, sensors, routines, assistants), preferences=preferences
    )
    selected = (sensors, routines, assistants)
    job = Job(store, *selected, kinds=frozenset({"sensor", "routine"}))
    with ExitStack() as stack:
        if not observe:
            try:
                stack.enter_context(collecting(store, ".watch"))
            except BusyError:
                if json_output:
                    raise Error("another watcher owns this brain; open bf watch in a terminal to observe it") from None
                dashboard.observe = True
                dashboard.message = "Observing an active watcher; its selection and settings control collection"
        # Due programs run only at checks: a slower check runs them late, so status reports them overdue. Only the
        # collecting watcher's interval matters.
        if not dashboard.observe and (
            reason := late(load(store), preferences.interval, sensors, routines, assistants, include_assistants=False)
        ):
            dashboard.late = f"{reason}; lower the watch interval"
            if json_output:
                sys.stderr.write(f"bf: {printable(dashboard.late)}\n")
        stack.callback(_stop, job)
        # Exit callbacks run in reverse: the update is signalled first, then the dispatcher stops its streams and
        # workers, then watch waits for the update.
        dispatcher = (
            Assistants(store, selected, stack.enter_context(ExitStack()))
            if not dashboard.observe and selection(load(store), *selected)[2]
            else None
        )
        stack.callback(job.terminate)
        if json_output:
            _loop(store, dashboard, job, *selected, dispatcher=dispatcher)
        else:
            console = Console(markup=False, highlight=False)
            with (
                keyboard(sys.stdin) as descriptor,
                Live(
                    console=console, screen=True, auto_refresh=False, redirect_stdout=False, redirect_stderr=False
                ) as live,
            ):
                _loop(store, dashboard, job, *selected, dispatcher=dispatcher, live=live, descriptor=descriptor)


def _stop(job: Job) -> None:
    """Wait for the signalled update to stop, or kill it.

    A cancellation arriving meanwhile, such as a second Ctrl-C or the hangup of a closing terminal, is raised once the
    update stopped or was killed.
    """
    with starting():
        job.close()


def _finished(job: Job, code: int, done: str, failed: str) -> tuple[str, int]:
    """A finished cycle's message and exit code, 0 when only the search cache failed: its programs all ran."""
    skipped = job.skipped() if code else 0
    job.discard()
    if skipped:
        # Programs ran; the search cache skipped files, which bf validate names. No program failed.
        return f"Search cache skipped {skipped} file{'' if skipped == 1 else 's'}; run bf validate", 0
    return failed if code else done, code


def _loop(
    store: Store,
    dashboard: Dashboard,
    job: Job,
    sensors: tuple[str, ...],
    routines: tuple[str, ...],
    assistants: tuple[str, ...] = (),
    *,
    dispatcher: Assistants | None = None,
    live: Live | None = None,
    descriptor: int = -1,
) -> None:
    preferences = dashboard.preferences
    next_read = next_frame = 0.0
    size = (0, 0)
    notifications = Notifications(preferences)
    previous: dict[tuple[str, str], str] = {}
    completed: int | None = None
    refresh_requested = invalid = False
    while True:
        now = time.monotonic()
        # Why assistant work waits, when that changed.
        reason: str | None = None
        if dispatcher is not None:
            dashboard.assisting = dispatcher.running
        if dashboard.running and (code := job.poll()) is not None:
            dashboard.running = False
            dashboard.message, code = _finished(
                job, code, "Update complete", "Update failed; inspect program details or run bf update for diagnostics"
            )
            # A failure not reported yet stays a failure.
            completed = completed or code
            dashboard.next_check = now + preferences.interval
            next_read = 0
        if now >= next_read:
            try:
                dashboard.replace_rows(snapshot(store, sensors, routines, assistants))
                if invalid:
                    dashboard.message = "Configuration recovered"
                invalid = False
                if completed is not None:
                    failures = tuple(
                        sorted(
                            f"{row.kind}:{row.name}:{row.error}"
                            for row in dashboard.rows
                            if row.included and row.status == State.FAILED
                        )
                    )
                    warning = notifications.completed(
                        failures=failures or (("update failed",) if completed else ()),
                        worked=any(
                            row.included and row.success and row.success != previous.get((row.kind, row.name), "")
                            for row in dashboard.rows
                        ),
                        now=now,
                    )
                    completed = None
                    # Each success counts as work once, whichever process finishes next.
                    previous = {(row.kind, row.name): row.success for row in dashboard.rows}
                    if warning:
                        dashboard.message = warning
                        # The dashboard shows it; stderr text written under the alternate screen is lost.
                        if live is None:
                            # One diagnostic line, like every other bf: line on stderr.
                            sys.stderr.write(f"bf: {printable(warning)}\n")
            except (Error, OSError, UnicodeError) as error:
                dashboard.rows = []
                # BF's own diagnostic names the invalid field, YAML position or unknown selected program without
                # quoting content: bf status cannot diagnose this watcher's selection.
                dashboard.message = (
                    clean(str(error))
                    if isinstance(error, Error)
                    else "Cannot read bf.yaml or history; run bf status for diagnostics"
                )
                invalid = True
                # No assistant work starts either: say so even when this snapshot reads the failure first.
                if dispatcher is not None:
                    reason = dispatcher.report(dashboard.message)
            if (
                not invalid
                and not dashboard.observe
                and not dashboard.paused
                and not dashboard.running
                and (refresh_requested or now >= dashboard.next_check)
            ):
                # A collection start resets only its own rows: an assistant success recorded while its work still
                # finishes counts at the next completion. A row not seen yet, such as an assistant with no due work,
                # starts from its current success.
                previous.update(
                    {
                        (row.kind, row.name): row.success
                        for row in dashboard.rows
                        if row.kind != "assistant" or (row.kind, row.name) not in previous
                    }
                )
                job.start()
                refresh_requested = False
                dashboard.running = True
                dashboard.message = "Running due sensors, then routines; existing per-program limits apply"
            if live is None:
                # Bytes, like every command reply: JSON Lines stay UTF-8 whatever the terminal's locale.
                sys.stdout.buffer.write(
                    terminal(
                        present(
                            {
                                "brain": dashboard.brain,
                                "running": dashboard.running,
                                "assisting": dashboard.assisting,
                                "message": dashboard.message,
                                "programs": [asdict(row) for row in dashboard.rows],
                            }
                        )
                    )
                )
                sys.stdout.flush()
            next_read = now + preferences.poll_interval
            next_frame = 0
        # After collection starts, so the message line keeps a new reason until the next snapshot shows it. Nothing new
        # starts while the snapshot fails, which reported its own reason.
        if dispatcher is not None and not dashboard.paused and not invalid:
            reason = dispatcher.tick(now)
        if reason is not None:
            dashboard.waiting = reason
            dashboard.message = reason or "Assistant work resumed"
            next_frame = 0
            if reason and live is None:
                # One diagnostic line per new reason, like every other bf: line on stderr.
                sys.stderr.write(f"bf: {printable(reason)}\n")
        if live is None:
            time.sleep(0.1)
            continue
        # Redraw after a change, a resize or each second for relative ages; idle frames waste CPU and bandwidth.
        current = (live.console.width, live.console.height)
        if now >= next_frame or current != size:
            live.update(dashboard.render(*current), refresh=True)
            size, next_frame = current, now + 1
        # Check a running update often enough to report its completion promptly.
        wait = min(next_frame, next_read, now + (0.25 if dashboard.running else 1)) - now
        for key in read_keys(descriptor, max(0.0, wait)):
            action = dashboard.key(key)
            if action == "quit":
                return
            if action == "refresh":
                refresh_requested = True
                next_read = 0
                dashboard.message = (
                    "Rereading configuration and history"
                    if dashboard.observe
                    else "Refresh queued; resume with Space to check due programs"
                    if dashboard.paused
                    else "Refresh queued after the active update"
                    if dashboard.running
                    else "Reloading bf.yaml and checking due programs"
                )
            next_frame = 0
