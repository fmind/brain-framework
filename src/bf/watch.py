"""A small terminal dashboard over local run history and cancellable update processes."""

from __future__ import annotations

import os
import select
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TextIO

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bf.collect import ROUTINES, environment, log_path, state
from bf.config import load
from bf.models import Error, encode
from bf.storage import BusyError, Store, collecting
from bf.update import selection
from bf.watch_settings import Notifications, Settings, settings


@dataclass
class Row:
    kind: str
    name: str
    status: str
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
        history = state(store, file)
        for name, program in sorted(programs.items()):
            entry = history.get(name, {})
            success = str(entry.get("success", ""))
            next_due = (
                datetime.fromisoformat(success) + timedelta(seconds=program.refresh)
                if success and not entry.get("error")
                else now
            )
            status = (
                "disabled"
                if not program.enabled
                else "manual"
                if not program.refresh
                else "failed"
                if entry.get("error")
                else "never"
                if not success
                else "due"
                if next_due <= now
                else "fresh"
            )
            rows.append(
                Row(
                    kind=kind,
                    name=name,
                    status=status,
                    included=name in included,
                    refresh=program.refresh,
                    success=success,
                    next_due=next_due.isoformat() if program.enabled and program.refresh else "",
                    added=int(str(entry["added"])) if "added" in entry else None,
                    updated=int(str(entry["updated"])) if "updated" in entry else None,
                    removed=int(str(entry["removed"])) if "removed" in entry else None,
                    elapsed_seconds=float(str(entry["elapsed_seconds"])) if "elapsed_seconds" in entry else None,
                    output_bytes=int(str(entry["output_bytes"])) if "output_bytes" in entry else None,
                    reconcile=bool(entry.get("reconcile", False)),
                    error=str(entry.get("error", "")),
                    log=str(log_path(store, name)) if entry.get("error") else "",
                    action=str(entry.get("action", "")),
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
    preferences: Settings = field(default_factory=Settings)

    def visible(self) -> list[Row]:
        return [row for row in self.rows if not self.attention or row.status in {"failed", "never", "due"}]

    def key(self, key: str) -> str:
        """Return a controller action without touching configuration or executing programs."""
        if key in {"q", "Q"}:
            return "quit"
        if key in {"j", "\x1b[B"}:
            self.cursor += 1
        elif key in {"k", "\x1b[A"}:
            self.cursor -= 1
        elif key == " ":
            if not self.observe:
                self.paused = not self.paused
        elif key == "\t":
            self.attention = not self.attention
            self.cursor = 0
        elif key == "?":
            self.help = not self.help
        elif key == "u":
            return "refresh"
        self.cursor = max(0, min(self.cursor, len(self.visible()) - 1))
        return ""

    def render(self, width: int, height: int) -> Layout:
        """Responsive table, selected-program details, and always-visible operating boundaries."""
        now = datetime.now(UTC)
        mode = "OBSERVE · no execution" if self.observe else "PAUSED" if self.paused else "WATCH · collection enabled"
        working = "Updating…" if self.running else f"Next check in {duration(self.next_check - time.monotonic())}"
        header = Text.assemble(
            (" BF ", "bold white on blue"),
            (f"  {clean(self.brain)}", "bold cyan"),
            (f"    {mode}", "yellow" if self.paused else "cyan"),
        )
        attention = sum(row.status in {"failed", "never", "due"} for row in self.rows)
        selected = sum(row.included for row in self.rows)
        summary = Text(
            f"{len(self.rows)} programs   {selected} selected   {attention} need attention   "
            + ("Reading local history" if self.observe else working),
            style="dim",
        )
        visible = self.visible()
        self.cursor = max(0, min(self.cursor, len(visible) - 1))
        limit = max(1, height - (18 if width < 100 else 10))
        offset = max(0, self.cursor - limit + 1)
        table = Table(expand=True, box=box.SIMPLE, header_style="bold cyan", padding=(0, 1))
        for label in ("Program", "State", "Last OK", "Next due", "+ / ~ / -"):
            table.add_column(label, no_wrap=True, overflow="ellipsis")
        for index, row in enumerate(visible[offset : offset + limit], offset):
            color = (
                "red"
                if row.status == "failed"
                else "yellow"
                if row.status in {"never", "due"}
                else "green"
                if row.status == "fresh"
                else "dim"
            )
            age = (
                duration((now - datetime.fromisoformat(row.success)).total_seconds()) + " ago"
                if row.success
                else "never"
            )
            due = (
                (
                    "now"
                    if datetime.fromisoformat(row.next_due) <= now
                    else duration((datetime.fromisoformat(row.next_due) - now).total_seconds())
                )
                if row.next_due
                else "—"
            )
            counts = " / ".join("—" if value is None else str(value) for value in (row.added, row.updated, row.removed))
            table.add_row(
                Text(("> " if index == self.cursor else "  ") + row.name + (" *" if row.kind == "routine" else "")),
                Text(row.status if row.included else "excluded", style=color if row.included else "dim"),
                Text(age),
                Text(due),
                Text(counts),
                style="on #183040" if index == self.cursor else "",
            )
        if not visible:
            table.add_row("No matching programs", "", "", "", "")
        title = f"{'Attention' if self.attention else 'Programs'} · {min(offset + 1, len(visible))}-{min(offset + limit, len(visible))}/{len(visible)}"
        detail = Text()
        if self.help:
            detail.append("HOW IT WORKS\n", style="bold cyan")
            detail.append(
                f"Check: {duration(self.preferences.interval)} · history: {self.preferences.poll_interval:g}s\n"
                f"Notifications: {'off (observer)' if self.observe else self.preferences.notifications}\n"
                "Configure settings/watch.yaml; restart to apply.\n\n"
            )
            detail.append("Watch runs only enabled, due programs. refresh: 0 stays manual.\n\n")
            detail.append(
                "+ added  ~ updated  - removed\nCounts describe the last successful run. * marks routines.\n\n"
            )
            detail.append("Space pauses future checks; an active update finishes. q cancels an active update.\n\n")
            detail.append("Fresh means local collection succeeded. It does not prove the provider is unchanged.")
        elif visible:
            row = visible[self.cursor]
            detail.append(clean(row.name) + "\n", style="bold cyan")
            detail.append(f"{row.kind} · {row.status} · {'selected' if row.included else 'excluded'}\n\n")
            detail.append(f"Refresh: {duration(row.refresh) if row.refresh else 'manual only'}\n")
            detail.append("Last success: " + (row.success or "never") + "\n")
            if row.elapsed_seconds is not None and row.output_bytes is not None:
                detail.append(f"Last run: {row.elapsed_seconds:.2f}s · {row.output_bytes:,} bytes\n")
                if row.reconcile:
                    detail.append("Wider reconciliation window\n", style="cyan")
            if row.error:
                detail.append("\n" + clean(row.error) + "\n", style="red")
                detail.append("Private log: " + clean(row.log), style="dim")
            elif row.action:
                detail.append("\nLatest action: " + clean(row.action))
            else:
                detail.append("\nOnly due programs run. Use bf collect for an intentional manual collection.")
        else:
            detail.append("Add a reviewed sensor or routine in bf.yaml. Press ? for controls and timing.")
        layout = Layout()
        layout.split_column(
            Layout(Group(header, summary), size=3),
            Layout(name="body"),
            Layout(Text(clean(self.message), style="yellow"), size=1),
            Layout(
                Text(
                    "j/k ↑/↓ select  Tab attention  u check  "
                    + ("" if self.observe else "Space pause  ")
                    + "? help  q quit",
                    style="bold cyan",
                ),
                size=1,
            ),
            Layout(
                Text(
                    "Local run history · no provider text · "
                    + ("observation only" if self.observe else "closing watch stops its collection"),
                    style="dim",
                ),
                size=1,
            ),
        )
        programs = Layout(Panel(table, title=title, border_style="blue"))
        details = Layout(Panel(detail, title="Guide" if self.help else "Details", border_style="cyan"))
        if width >= 100:
            layout["body"].split_row(programs, Layout(details, size=max(32, width // 3)))
        else:
            layout["body"].split_column(programs, Layout(details, size=8))
        return layout


class Job:
    """A separate BF process keeps keyboard input responsive and reuses provider cancellation."""

    def __init__(self, store: Store, sensors: tuple[str, ...], routines: tuple[str, ...]) -> None:
        self.argv = [sys.executable, "-I", "-m", "bf", "update", "--brain", str(store.root)]
        for flag, names in (("--sensor", sensors), ("--routine", routines)):
            for name in names:
                self.argv.extend((flag, name))
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(  # noqa: S603 - fixed BF entry point, literal validated selection
            self.argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment(),
            start_new_session=True,
        )

    def poll(self) -> int | None:
        return self.process.poll() if self.process is not None else None

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


@contextmanager
def keyboard(stream: TextIO) -> Iterator[int]:
    descriptor = stream.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        yield descriptor
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def read_key(descriptor: int) -> str:
    if not select.select([descriptor], [], [], 0.1)[0]:
        return ""
    data = os.read(descriptor, 32)
    if not data:
        return "q"
    # Arrow sequences may arrive in separate reads.
    if data == b"\x1b" and select.select([descriptor], [], [], 0.03)[0]:
        data += os.read(descriptor, 2)
    return data.decode("utf-8", errors="replace")


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
        raise Error("watch needs an interactive terminal; use bf watch --json or bf status")
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
            _loop(store, dashboard, job, sensors, routines, preferences)
        else:
            console = Console(markup=False, highlight=False)
            with (
                keyboard(sys.stdin) as descriptor,
                Live(
                    console=console, screen=True, auto_refresh=False, redirect_stdout=False, redirect_stderr=False
                ) as live,
            ):
                _loop(store, dashboard, job, sensors, routines, preferences, live=live, descriptor=descriptor)


def _loop(
    store: Store,
    dashboard: Dashboard,
    job: Job,
    sensors: tuple[str, ...],
    routines: tuple[str, ...],
    preferences: Settings,
    *,
    live: Live | None = None,
    descriptor: int = -1,
) -> None:
    next_read = 0.0
    notifications = Notifications(preferences)
    previous: dict[tuple[str, str], str] = {}
    completed: int | None = None
    while True:
        now = time.monotonic()
        if dashboard.running and (code := job.poll()) is not None:
            dashboard.running = False
            dashboard.message = (
                "Update complete"
                if code == 0
                else "Update failed; inspect program details or run bf update for diagnostics"
            )
            dashboard.next_check = now + preferences.interval
            completed = code
            next_read = 0
        if now >= next_read:
            try:
                dashboard.rows = snapshot(store, sensors, routines)
                valid = True
                if completed is not None:
                    failures = tuple(
                        sorted(
                            f"{row.kind}:{row.name}:{row.error}"
                            for row in dashboard.rows
                            if row.included and row.status == "failed"
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
                        sys.stderr.write(encode({"warning": warning}).decode())
                if dashboard.message.startswith("Cannot read"):
                    dashboard.message = "Configuration recovered"
            except Error, OSError, UnicodeError:
                dashboard.rows = []
                dashboard.message = (
                    "Cannot read configuration or history; fix bf.yaml and run bf status for diagnostics"
                )
                valid = False
            if (
                valid
                and not dashboard.observe
                and not dashboard.paused
                and not dashboard.running
                and now >= dashboard.next_check
            ):
                previous = {(row.kind, row.name): row.success for row in dashboard.rows}
                job.start()
                dashboard.running = True
                dashboard.message = "Running due sensors, then routines; existing per-program limits apply"
            if live is None:
                sys.stdout.write(
                    encode(
                        {
                            "brain": dashboard.brain,
                            "observing": dashboard.observe,
                            "running": dashboard.running,
                            "message": dashboard.message,
                            "programs": [asdict(row) for row in dashboard.rows],
                        }
                    ).decode()
                )
                sys.stdout.flush()
            next_read = now + preferences.poll_interval
        if live is not None:
            live.update(dashboard.render(live.console.width, live.console.height), refresh=True)
            action = dashboard.key(read_key(descriptor))
            if action == "quit":
                return
            if action == "refresh":
                dashboard.next_check = 0
                next_read = 0
        else:
            time.sleep(0.1)
