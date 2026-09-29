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
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
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

from bf.collect import next_due
from bf.config import load
from bf.history import ROUTINES, environment, log_path, state
from bf.models import Error, WatchSettings, decode, encode, present, terminal, timestamp
from bf.storage import BusyError, Store, collecting
from bf.update import selection
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


def snapshot(store: Store, sensors: tuple[str, ...] = (), routines: tuple[str, ...] = ()) -> list[Row]:
    """Read only configuration and small local history files; never scan evidence or run providers."""
    config = load(store)
    selected = selection(config, sensors, routines)
    now = datetime.now(UTC)
    rows = []
    for kind, programs, file, included in (
        ("sensor", config.sensors, "sensors.json", selected[0]),
        ("routine", config.routines, ROUTINES, selected[1]),
    ):
        # state() validates every entry, so each present field already has its history type.
        history = state(store, file)
        for name, program in sorted(programs.items()):
            entry = history.get(name, {})
            success, error = str(entry.get("success", "")), str(entry.get("error", ""))
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
    message: str = "Ready"
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
                f"Check: {duration(self.preferences.interval)} · history: {poll} · alerts: "
                + self.preferences.notifications,
                "Configure watch in bf.yaml; restart to apply.",
                "f (or u) reloads bf.yaml and checks due programs now.",
                "While updating, refresh queues one check; pause still applies.",
                "Selection, refresh intervals and retry backoff still apply.",
                "Space pauses future checks; q cancels an active update.",
            ]
        lines = [
            "s cycles: " + ", ".join(Sort) + ".",
            "n name · t last OK · i items · r reverse · g/G first/last",
            "j/k or arrows select · Tab attention · ? close guide · q quit",
            "Newest/largest first; name, state, due, refresh and kind ascend.",
            "Unknown values stay last; ties use name. Sorting is session-only.",
            "Items: records returned by the last successful sensor run, not total stored.",
            "+ added / ~ updated / - removed; changes sorts their sum. * marks routines.",
            "Failed runs keep prior counts. — means unknown or not applicable.",
            *mode,
            "Fresh is local success, not proof the provider is unchanged.",
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
        detail.append(f"Refresh: {duration(row.refresh) if row.refresh else 'manual only'}\n")
        detail.append("Last success: " + (row.success or "never") + "\n")
        detail.append("Last run items: " + ("—" if row.records is None else f"{row.records:,}") + "\n")
        if row.elapsed_seconds is not None and row.output_bytes is not None:
            detail.append(f"Last run: {row.elapsed_seconds:.2f}s · {row.output_bytes:,} bytes\n")
            if row.reconcile:
                detail.append("Wider reconciliation window\n", style="cyan")
        if not row.error and not row.action:
            detail.append("Only due programs run. Use bf collect for an intentional manual collection.")
        detail.rstrip()
        return detail

    def render(self, width: int, height: int, now: datetime | None = None) -> Layout:
        """Responsive table, selected-program details, and always-visible operating boundaries."""
        now = now or datetime.now(UTC)
        mode = "OBSERVE · no execution" if self.observe else "PAUSED" if self.paused else "WATCH · collection enabled"
        working = "Updating…" if self.running else f"Next check in {duration(self.next_check - time.monotonic())}"
        header = Text.assemble(
            (" BF ", "bold white on blue"),
            (f"  {clean(self.brain)}", "bold cyan"),
            (f"    {mode}", "yellow" if self.paused else "cyan"),
            no_wrap=True,
            overflow="ellipsis",
        )
        summary = Text(
            f"{len(self.rows)} programs   "
            + ("" if self.observe else f"{sum(row.included for row in self.rows)} selected   ")
            + f"{sum(row.status in ATTENTION for row in self.rows)} need attention   "
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
                Text(("> " if index == self.cursor else "  ") + ("* " if row.kind == "routine" else "") + row.name),
                Text(row.status, style=COLORS.get(row.status, "dim")) if row.included else Text("excluded", "dim"),
                Text(duration((now - success).total_seconds()) + " ago" if success else "never"),
                Text("—" if due is None else "now" if due <= now else duration((due - now).total_seconds())),
                Text("—" if row.records is None else str(row.records)),
                Text("/".join("—" if value is None else str(value) for value in (row.added, row.updated, row.removed))),
                style="on #183040" if index == self.cursor else "",
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
            detail = Text("Add a reviewed sensor or routine in bf.yaml. Press ? for controls and timing.")
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

    def __init__(self, store: Store, sensors: tuple[str, ...], routines: tuple[str, ...]) -> None:
        self.argv = [sys.executable, "-I", "-m", "bf", "update", "--brain", str(store.root)]
        for flag, names in (("--sensor", sensors), ("--routine", routines)):
            for name in names:
                self.argv.extend((flag, name))
        self.process: subprocess.Popen[bytes] | None = None
        self.reply: IO[bytes] | None = None

    def start(self) -> None:
        self.discard()
        # BF's own JSON reply, in an unnamed private file: it tells skipped files apart from failed programs.
        self.reply = tempfile.TemporaryFile()  # noqa: SIM115 - kept until the next start or close
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
        programs = [*cast(list[object], value.get("sensors") or []), *cast(list[object], value.get("routines") or [])]
        cache = value.get("index")
        if any(not isinstance(item, dict) or item.get("status") == "failed" for item in programs):
            return 0
        skipped = cache.get("skipped") if isinstance(cache, dict) and "error" not in cache else None
        return skipped if type(skipped) is int and skipped > 0 else 0

    def discard(self) -> None:
        if self.reply is not None:
            self.reply.close()
            self.reply = None

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=STOP)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.discard()


@contextmanager
def keyboard(stream: TextIO) -> Iterator[int]:
    descriptor = stream.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        yield descriptor
    finally:
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
    interval: int | None = None,
    poll_interval: float | None = None,
    notifications: str | None = None,
    observe: bool = False,
    json_output: bool = False,
) -> None:
    """A foreground controller; no daemon, scheduler installation or provider credentials are created."""
    preferences = settings(store, interval=interval, poll_interval=poll_interval, notifications=notifications)
    if not json_output and (not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM") == "dumb"):
        # Never suggest an executing command to someone who asked only to observe.
        raise Error(
            "status --watch needs an interactive terminal; use bf status for a JSON snapshot"
            if observe
            else "watch needs an interactive terminal; use bf watch --json or bf status"
        )
    dashboard = Dashboard(
        load(store).name, observe=observe, rows=snapshot(store, sensors, routines), preferences=preferences
    )
    job = Job(store, sensors, routines)
    with ExitStack() as stack:
        if not observe:
            try:
                stack.enter_context(collecting(store, ".watch"))
            except BusyError:
                if json_output:
                    raise Error("another watcher owns this brain; open bf watch in a terminal to observe it") from None
                dashboard.observe = True
                dashboard.message = "Observing an active watcher; its selection and settings control collection"
        stack.callback(job.close)
        if json_output:
            _loop(store, dashboard, job, sensors, routines)
        else:
            console = Console(markup=False, highlight=False)
            with (
                keyboard(sys.stdin) as descriptor,
                Live(
                    console=console, screen=True, auto_refresh=False, redirect_stdout=False, redirect_stderr=False
                ) as live,
            ):
                _loop(store, dashboard, job, sensors, routines, live=live, descriptor=descriptor)


def _loop(
    store: Store,
    dashboard: Dashboard,
    job: Job,
    sensors: tuple[str, ...],
    routines: tuple[str, ...],
    *,
    live: Live | None = None,
    descriptor: int = -1,
) -> None:
    preferences = dashboard.preferences
    next_read = next_frame = 0.0
    size = (0, 0)
    notifications = Notifications(preferences)
    previous: dict[tuple[str, str], str] = {}
    completed: int | None = None
    refresh_requested = False
    while True:
        now = time.monotonic()
        if dashboard.running and (code := job.poll()) is not None:
            dashboard.running = False
            skipped = job.skipped() if code else 0
            job.discard()
            dashboard.message = (
                "Update complete"
                if code == 0
                # Programs ran; the search cache skipped files, which bf validate names. No collection failed.
                else f"Search cache skipped {skipped} files; run bf validate"
                if skipped
                else "Update failed; inspect program details or run bf update for diagnostics"
            )
            dashboard.next_check = now + preferences.interval
            completed = 0 if skipped else code
            next_read = 0
        if now >= next_read:
            try:
                dashboard.replace_rows(snapshot(store, sensors, routines))
                valid = True
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
                    if warning:
                        dashboard.message = warning
                        # The dashboard shows it; stderr text written under the alternate screen is lost.
                        if live is None:
                            sys.stderr.write(encode({"warning": warning}).decode())
                if dashboard.message.startswith("Cannot read"):
                    dashboard.message = "Configuration recovered"
            except Error, OSError, UnicodeError:
                dashboard.rows = []
                dashboard.message = "Cannot read bf.yaml or history; run bf status for diagnostics"
                valid = False
            if (
                valid
                and not dashboard.observe
                and not dashboard.paused
                and not dashboard.running
                and (refresh_requested or now >= dashboard.next_check)
            ):
                previous = {(row.kind, row.name): row.success for row in dashboard.rows}
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
                                "message": dashboard.message,
                                "programs": [asdict(row) for row in dashboard.rows],
                            }
                        )
                    )
                )
                sys.stdout.flush()
            next_read = now + preferences.poll_interval
            next_frame = 0
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
