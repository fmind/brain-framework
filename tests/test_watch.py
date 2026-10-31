"""Terminal rendering, keyboard behavior, read-only observation and process cancellation."""

from __future__ import annotations

import io
import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import termios
import time
from collections.abc import Callable
from concurrent import futures
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import UUID

import pytest
from rich.console import Console
from typer.testing import CliRunner

import bf.collect as collector
import bf.watch as watching
from bf.assistants import Dispatcher
from bf.cli import _cancel, app
from bf.config import load
from bf.history import state
from bf.models import Error, WatchSettings, encode, local, timestamp
from bf.runtime import Queue
from bf.storage import Store, collecting, state_store
from bf.watch import (
    Dashboard,
    Job,
    Row,
    Sort,
    State,
    _loop,
    _stop,
    clean,
    duration,
    keyboard,
    period,
    read_keys,
    snapshot,
    watch,
)

if TYPE_CHECKING:
    from rich.live import Live

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
CONFIG = b"""version: 8
name: fixture
sensors:
  calendar:
    command: [echo]
    refresh: 3600
  git:
    command: [echo]
    refresh: 300
  manual:
    command: [echo]
  disabled:
    command: [echo]
    enabled: false
    refresh: 300
routines:
  review:
    command: [echo]
    refresh: 86400
"""


def screen(dashboard: Dashboard, width: int, height: int) -> str:
    output = io.StringIO()
    Console(file=output, width=width, height=height, color_system=None).print(dashboard.render(width, height, NOW))
    return output.getvalue()


class FakeLive:
    """Records frames instead of drawing them; the loop reads only the console size."""

    def __init__(self) -> None:
        self.console = SimpleNamespace(width=80, height=24)
        self.frames = 0

    def update(self, _layout: object, *, refresh: bool) -> None:
        assert refresh
        self.frames += 1


def test_snapshot_reads_counts_and_status_without_executing(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    now = datetime.now(UTC)
    state_store(brain.root).write(
        "sensors.json",
        encode(
            {
                "calendar": {
                    "success": now.isoformat(),
                    "added": 3,
                    "updated": 2,
                    "removed": 1,
                    "elapsed_seconds": 0.25,
                    "output_bytes": 128,
                    "reconcile": True,
                    "records": 10,
                },
                "git": {
                    "success": (now - timedelta(days=1)).isoformat(),
                    "run": (now - timedelta(minutes=1)).isoformat(),
                    "failures": 2,
                    "error": "program exited with status 1",
                },
            }
        ),
    )
    state_store(brain.root).write(
        "routines.json", encode({"review": {"success": now.isoformat(), "action": "actions/review/ACTION.md"}})
    )
    rows = {row.name: row for row in snapshot(brain, ("calendar",))}
    assert rows["calendar"].status is State.FRESH
    assert rows["calendar"].added == 3
    assert rows["calendar"].elapsed_seconds == 0.25
    assert rows["calendar"].output_bytes == 128
    assert rows["calendar"].reconcile
    assert rows["calendar"].records == 10
    # Reply instants are canonical UTC, whatever offset the history used.
    assert rows["calendar"].success.endswith("Z")
    assert rows["calendar"].next_due.endswith("Z")
    assert rows["git"].records is None
    assert rows["git"].status is State.FAILED
    # A second consecutive failure waits two minutes from its run, not a refresh after the last success.
    assert rows["git"].next_due == timestamp((now + timedelta(minutes=1)).isoformat())
    assert not rows["git"].included
    assert rows["git"].log.endswith("git.log")
    assert rows["manual"].status is State.MANUAL
    assert not rows["manual"].next_due
    assert rows["disabled"].status is State.DISABLED
    assert rows["review"].action.endswith("ACTION.md")
    state_store(brain.root).write(
        "sensors.json", encode({"calendar": {"success": (now - timedelta(days=1)).isoformat()}})
    )
    assert {row.name: row.status for row in snapshot(brain)}["calendar"] is State.DUE
    assert {row.name: row.status for row in snapshot(brain)}["git"] is State.NEVER


@pytest.mark.parametrize(("width", "height"), [(140, 36), (80, 24), (40, 12)])
def test_dashboard_layout_keeps_data_literal_and_handles_resize(width: int, height: int) -> None:
    dashboard = Dashboard(
        "[red]fixture\x1b]0;bad",
        rows=[
            Row("assistant", "assistant", State.FRESH, True, 1800, runs=1, record="memories/\x1b[2Jassistant"),
            Row(
                "sensor",
                "calendar",
                State.FRESH,
                True,
                3600,
                success=datetime.now(UTC).isoformat(),
                added=3,
                updated=2,
                removed=0,
            ),
            Row("sensor", "git", State.FAILED, True, 300, error="[bold]literal\x1b[2J", log="/private/\x1b[2Jlog"),
            Row("routine", "review", State.NEVER, False, 86400, action="actions/\x1b[2Jreview"),
        ],
    )
    output = io.StringIO()
    console = Console(file=output, width=width, height=height, color_system=None)
    console.print(dashboard.render(width, height))
    assert "BF" in output.getvalue()
    # Every row's details, then the guide: control characters in history are data, never terminal instructions.
    for cursor in range(1, len(dashboard.rows)):
        dashboard.cursor = cursor
        console.print(dashboard.render(width, height))
    dashboard.help = True
    console.print(dashboard.render(width, height))
    assert "\x1b" not in output.getvalue()
    # Brackets stay text, never markup; details need 80 columns.
    assert "[red]fixture" in output.getvalue()
    if width >= 80:
        assert "@ assistant" in output.getvalue()
        assert "[bold]literal" in output.getvalue()
        assert "q quit" in output.getvalue()
    assert clean("x\n\x1b\t") == "x   "


def test_empty_many_rows_and_controls() -> None:
    dashboard = Dashboard("fixture")
    dashboard.render(100, 24)
    assert dashboard.key("q") == "quit"
    assert dashboard.key("Q") == "quit"
    assert dashboard.key("u") == "refresh"
    assert dashboard.key("f") == "refresh"
    assert dashboard.key("j") == ""
    assert dashboard.cursor == 0
    dashboard.rows = [
        Row("sensor", f"source-{i:02}", State.FAILED if i == 25 else State.FRESH, True, 60) for i in range(40)
    ]
    for _ in range(26):
        dashboard.key("\x1b[B")
    assert dashboard.cursor == 26
    dashboard.render(120, 25)
    dashboard.key("\x1b[A")
    assert dashboard.cursor == 25
    dashboard.key("\t")
    assert len(dashboard.visible()) == 1
    assert dashboard.cursor == 0
    dashboard.key(" ")
    assert dashboard.paused
    dashboard.key("?")
    assert dashboard.help
    dashboard.key("?")
    assert not dashboard.help
    dashboard.observe = True
    dashboard.key(" ")
    assert dashboard.paused
    assert [duration(n) for n in (-1, 15, 120, 7200, 172800)] == ["0s", "15s", "2m", "2h", "2d"]


def test_an_empty_attention_filter_says_nothing_needs_attention() -> None:
    dashboard = Dashboard("fixture", attention=True, rows=[Row("sensor", "calendar", State.FRESH, True, 60)])
    output = screen(dashboard, 80, 24)
    assert "No matching programs" in output
    assert "Nothing needs attention; press Tab to show every program." in output
    assert "Add a reviewed sensor" not in output
    # Without any program, the filter is not why the table is empty.
    dashboard.rows = []
    assert "Add a reviewed sensor, routine or assistant in bf.yaml." in screen(dashboard, 80, 24)


@pytest.mark.parametrize(
    ("states", "running", "paused", "summary"),
    [
        # No check starts until Space, so a countdown would announce one.
        ([State.FAILED], False, True, "1 program   1 selected   1 needs attention   Checks paused"),
        # An active update finishes while paused.
        ([State.FAILED], True, True, "1 program   1 selected   1 needs attention   Updating…"),
        (
            [State.FAILED, State.DUE, State.FRESH],
            False,
            False,
            "3 programs   3 selected   2 need attention   Next check in ",
        ),
    ],
)
def test_summary_agrees_with_its_counts_and_shows_no_countdown_while_paused(
    states: list[State], summary: str, *, running: bool, paused: bool
) -> None:
    rows = [Row("sensor", f"source-{index}", status, True, 60) for index, status in enumerate(states)]
    dashboard = Dashboard("fixture", rows=rows, running=running, paused=paused)
    assert screen(dashboard, 80, 24).splitlines()[1].startswith(summary)


@pytest.mark.parametrize(
    ("keys", "cursor"),
    [
        (["\x1bOB", "\x1bOB", "\x1bOA"], 1),  # application-mode arrows
        (["\x1b[8~"], 4),  # rxvt End
        (["G", "\x1b[7~"], 0),  # rxvt Home
        (["\x1b[F", "\x1bOH"], 0),
        (["\x1bOF", "\x1b[1~", "\x1b[4~"], 4),
        (["\x1b", "[", "x"], 0),  # a lone Escape and unknown keys do nothing
    ],
)
def test_terminal_key_variants_share_one_action(keys: list[str], cursor: int) -> None:
    dashboard = Dashboard("fixture", rows=[Row("sensor", f"s{i}", State.FRESH, True, 60) for i in range(5)])
    for key in keys:
        assert dashboard.key(key) == ""
    assert dashboard.cursor == cursor


@pytest.mark.parametrize(
    ("width", "height"), [(80, 11), (80, 18), (80, 24), (100, 24), (120, 11), (120, 30), (160, 50), (80, 40)]
)
def test_selected_row_stays_visible_while_scrolling(width: int, height: int) -> None:
    dashboard = Dashboard("fixture", rows=[Row("sensor", f"source-{i:02}", State.FRESH, True, 60) for i in range(40)])
    dashboard.key("G")
    output = screen(dashboard, width, height)
    assert len(output.splitlines()) == height
    assert "> source-39" in output
    # The title range matches the rows actually drawn, including the last one.
    title = re.search(r"Programs · (\d+)-(\d+)/40", output)
    assert title
    assert title[2] == "40"
    drawn = [int(number) for number in re.findall(r"│ [> ] source-(\d+)", output)]
    assert drawn == list(range(int(title[1]) - 1, 40))
    dashboard.key("g")
    for index in range(1, 40):
        dashboard.key("j")
        assert f"> source-{index:02}" in screen(dashboard, width, height)


@pytest.mark.parametrize(
    ("width", "height", "count"), [(80, 24, 40), (80, 24, 5), (100, 30, 5), (119, 40, 40), (120, 24, 40)]
)
def test_details_show_diagnostics_and_routine_markers_at_every_width(width: int, height: int, count: int) -> None:
    routine = "a-very-long-routine-name-for-the-weekly-review"
    dashboard = Dashboard(
        "fixture",
        rows=[
            Row("routine", routine, State.FRESH, True, 60, action="actions/weekly-review"),
            Row("sensor", "broken", State.FAILED, True, 60, error="program exited with status 1", log="/state/b.log"),
            *(Row("sensor", f"source-{i:02}", State.FRESH, True, 60) for i in range(count - 2)),
        ],
    )
    output = screen(dashboard, width, height)
    assert "Latest action: actions/weekly-review" in output
    # The marker leads the name, so shortening a long routine name keeps it.
    assert "> * a-very-long-routine" in output
    dashboard.key("j")
    output = screen(dashboard, width, height)
    assert "program exited with status 1" in output
    assert "Log: /state/b.log" in output


@pytest.mark.parametrize(
    ("row", "command"),
    [
        (Row("sensor", "calendar", State.FRESH, True, 60), "bf collect calendar"),
        (Row("sensor", "manual", State.MANUAL, True, 0), "bf collect manual"),
        (Row("routine", "weekly-log", State.FRESH, True, 60), "bf run weekly-log"),
        (Row("assistant", "personal", State.NEVER, True, 1800, runs=0), "bf run personal"),
        (Row("assistant", "manual", State.MANUAL, True, 0, runs=0), "bf run manual"),
        (Row("sensor", "disabled", State.DISABLED, True, 60), ""),
        (Row("assistant", "paused", State.DISABLED, True, 1800, runs=0), ""),
    ],
)
def test_details_name_the_command_that_runs_the_program_now(row: Row, command: str) -> None:
    # bf collect runs only sensors and bf run routines and assistants; neither runs a disabled program.
    commands = re.compile(r"bf (?:collect|run) \S+")
    assert commands.findall(Dashboard("fixture", rows=[row]).details(row).plain) == ([command] if command else [])
    # An observer runs nothing and suggests no executing command.
    assert not commands.findall(Dashboard("fixture", observe=True, rows=[row]).details(row).plain)


def test_configured_periods_show_exactly() -> None:
    row = Row("sensor", "git", State.FRESH, True, 5400)
    dashboard = Dashboard("fixture", rows=[row], preferences=WatchSettings(interval=90))
    # Ages and countdowns round down; a configured period does not, so the guide matches the late warning's seconds.
    assert "Refresh: 90m" in dashboard.details(row).plain
    assert "Check: 90s" in dashboard.guide().plain
    assert [period(n) for n in (5, 60, 90, 3600, 5400, 86400, 604800)] == ["5s", "1m", "90s", "1h", "90m", "1d", "7d"]


def test_details_show_the_last_success_in_local_time() -> None:
    success = "2026-09-01T11:59:00.123456Z"
    dashboard = Dashboard("fixture", rows=[Row("sensor", "calendar", State.FRESH, True, 60, success=success)])
    # Like every reply, with the local offset and to the second, whole within the details panel.
    output = screen(dashboard, 120, 24)
    assert f"Last OK: {local(success)}" in output
    assert ".123456" not in output


def test_short_terminals_keep_program_rows_over_details() -> None:
    dashboard = Dashboard("fixture", rows=[Row("sensor", f"source-{i:02}", State.FRESH, True, 60) for i in range(40)])
    output = screen(dashboard, 80, 11)
    assert "> source-00" in output
    assert "Details" not in output
    # Below the frame's own height no row fits, and the title says so instead of claiming one.
    assert "Programs · 0-0/40" in screen(dashboard, 80, 10)


@pytest.mark.parametrize(("width", "height"), [(80, 24), (100, 24), (120, 30)])
def test_columns_keep_state_ages_and_counts_whole(width: int, height: int) -> None:
    dashboard = Dashboard(
        "fixture",
        rows=[
            Row(
                "sensor",
                "github-pull-requests",
                State.FAILED,
                True,
                60,
                success=(NOW - timedelta(minutes=59)).isoformat(),
                next_due=NOW.isoformat(),
                records=12345,
                added=1234,
                updated=5678,
                removed=9012,
            ),
            Row("sensor", "gmail-inbox", State.FRESH, True, 3600, next_due=(NOW + timedelta(seconds=59)).isoformat()),
            Row("routine", "weekly-review", State.DISABLED, False, 0),
            Row("sensor", "z-" + "very-" * 12 + "long-name", State.DISABLED, True, 300),
        ],
    )
    output = screen(dashboard, width, height)
    for value in ("github-pull-requests", "failed", "59m ago", "now", "12345", "1234/5678/9012", "excluded", "59s"):
        assert value in output
    assert "disabled" in output
    # Only the program name shortens.
    assert "z-very-very-" in output
    assert "long-name" not in output


def test_text_on_a_background_sets_its_own_color() -> None:
    dashboard = Dashboard(
        "fixture", rows=[Row("sensor", "calendar", State.FRESH, True, 60), Row("sensor", "git", State.DUE, True, 60)]
    )
    console = Console(file=io.StringIO(), width=120, height=24, color_system="truecolor", force_terminal=True)
    filled = [
        (segment.text, segment.style.color)
        for segment in console.render(dashboard.render(120, 24, NOW))
        if segment.text.strip() and segment.style is not None and segment.style.bgcolor is not None
    ]
    # A light theme's dark default text would be unreadable on the selected row's dark highlight.
    assert any(text.startswith("> calendar") for text, _ in filled)
    assert all(color is not None for _, color in filled)


def test_fixed_lines_truncate_instead_of_hiding_sort_and_keys() -> None:
    dashboard = Dashboard(
        "a-" + "very-" * 12 + "long-brain",
        rows=[Row("sensor", "calendar", State.FRESH, True, 60)],
        message="A long status message " * 5,
    )
    dashboard.key("i")
    output = screen(dashboard, 80, 24)
    lines = output.splitlines()
    assert len(lines) == 24
    assert lines[0].startswith(" BF   a-very-very-")
    assert lines[0].rstrip().endswith("…")
    assert "items ↓" in lines[2]
    assert lines[-4].rstrip().endswith("…")
    assert lines[-3].startswith("s sort")
    assert lines[-2].startswith("j/k")
    assert lines[-1].startswith("Local run history")


@pytest.mark.parametrize(
    ("sort", "expected", "reverse"),
    [
        (Sort.NAME, ["alpha", "beta", "gamma", "unknown"], ["unknown", "gamma", "beta", "alpha"]),
        (Sort.UPDATED, ["beta", "gamma", "alpha", "unknown"], ["alpha", "gamma", "beta", "unknown"]),
        (Sort.ITEMS, ["gamma", "beta", "alpha", "unknown"], ["alpha", "beta", "gamma", "unknown"]),
        (Sort.STATE, ["beta", "unknown", "gamma", "alpha"], ["alpha", "gamma", "unknown", "beta"]),
        (Sort.DUE, ["gamma", "alpha", "beta", "unknown"], ["beta", "alpha", "gamma", "unknown"]),
        (Sort.CHANGES, ["beta", "gamma", "alpha", "unknown"], ["alpha", "gamma", "beta", "unknown"]),
        (Sort.DURATION, ["alpha", "gamma", "beta", "unknown"], ["beta", "gamma", "alpha", "unknown"]),
        (Sort.OUTPUT, ["beta", "gamma", "alpha", "unknown"], ["alpha", "gamma", "beta", "unknown"]),
        (Sort.REFRESH, ["unknown", "gamma", "beta", "alpha"], ["alpha", "beta", "gamma", "unknown"]),
        # Sensors tie on kind, so they stay alphabetical in both directions.
        (Sort.KIND, ["unknown", "alpha", "beta", "gamma"], ["alpha", "beta", "gamma", "unknown"]),
    ],
)
def test_sort_orders_values_numerically_with_unknowns_last(sort: Sort, expected: list[str], reverse: list[str]) -> None:
    rows = [
        Row(
            "sensor",
            "alpha",
            State.FRESH,
            True,
            3600,
            success="2026-01-01T10:00:00+02:00",
            next_due="2026-01-01T12:00:00+00:00",
            records=0,
            added=0,
            updated=0,
            removed=0,
            elapsed_seconds=100,
            output_bytes=9,
        ),
        Row(
            "sensor",
            "beta",
            State.FAILED,
            True,
            300,
            success="2026-01-01T09:00:00+00:00",
            next_due="2026-01-01T13:00:00+00:00",
            records=9,
            added=2,
            updated=3,
            removed=10,
            elapsed_seconds=2,
            output_bytes=100,
        ),
        Row(
            "sensor",
            "gamma",
            State.DUE,
            False,
            60,
            success="2026-01-01T08:30:00+00:00",
            next_due="2026-01-01T11:00:00+00:00",
            records=100,
            added=1,
            updated=2,
            removed=0,
            elapsed_seconds=9,
            output_bytes=10,
        ),
        Row("routine", "unknown", State.NEVER, True, 0),
    ]
    dashboard = Dashboard("fixture", rows=rows, sort=sort, descending=sort.descending)
    assert [row.name for row in dashboard.visible()] == expected
    dashboard.key("r")
    assert [row.name for row in dashboard.visible()] == reverse
    assert f"{sort} {'↓' if dashboard.descending else '↑'}" in screen(dashboard, 120, 30)
    assert dashboard.rows == rows  # Sorting does not change execution/JSON order.


def test_sort_controls_keep_selection_across_polls_and_filters() -> None:
    alpha = Row("sensor", "alpha", State.FRESH, True, 60, records=2)
    beta = Row("sensor", "beta", State.FAILED, True, 60, records=10)
    routine = Row("routine", "beta", State.NEVER, True, 60)
    dashboard = Dashboard("fixture", rows=[beta, routine, alpha])
    dashboard.key("G")
    assert dashboard.selected() == beta
    dashboard.key("i")
    assert dashboard.selected() == beta
    assert dashboard.cursor == 0
    dashboard.key("r")
    assert dashboard.selected() == beta
    assert dashboard.cursor == 1
    dashboard.replace_rows([replace(alpha, records=100), beta, routine])
    assert dashboard.selected() == beta
    assert dashboard.cursor == 0
    dashboard.key("\t")
    assert {row.name for row in dashboard.visible()} == {"beta"}
    dashboard.key("n")
    assert not dashboard.descending
    assert dashboard.selected() == beta
    dashboard.key("t")
    assert dashboard.sort == Sort.UPDATED
    assert dashboard.descending
    for _ in Sort:
        dashboard.key("s")
    assert dashboard.sort == Sort.UPDATED
    dashboard.key("g")
    assert dashboard.selected() == routine
    dashboard.replace_rows([beta])
    assert dashboard.selected() == beta
    dashboard.replace_rows([])
    for key in ("s", "r", "g", "G"):
        dashboard.key(key)
    assert dashboard.cursor == 0
    assert dashboard.selected() is None


def test_ties_stay_alphabetical_in_both_directions_and_help_is_visible() -> None:
    dashboard = Dashboard(
        "fixture",
        rows=[
            Row("sensor", "zulu", State.FRESH, True, 60, records=2),
            Row("sensor", "alpha", State.FRESH, True, 60, records=2),
        ],
    )
    dashboard.key("i")
    for key in ("r", "r"):
        dashboard.key(key)
        assert [row.name for row in dashboard.visible()] == ["alpha", "zulu"]
    output = screen(dashboard, 80, 24)
    assert "Programs · 1-2/2 · items ↓" in output
    assert "Items" in output
    assert "s sort" in output
    assert "f refresh  Space pause" in output
    dashboard.key("?")
    output = screen(dashboard, 80, 24)
    assert "not total stored" in output
    assert "Unknown values stay last" in output
    assert "Space pauses future checks" in output
    assert "not proof the provider is unchanged" in output


def test_observer_offers_only_what_it_can_do() -> None:
    dashboard = Dashboard(
        "fixture",
        observe=True,
        rows=[Row("sensor", "calendar", State.FRESH, True, 60), Row("sensor", "git", State.DUE, False, 60)],
    )
    output = screen(dashboard, 80, 24)
    assert "OBSERVE · no execution" in output
    assert "u reread" in output
    assert "u check" not in output
    assert "Space pause" not in output
    # The observer's selection does not describe the active collector's selection.
    assert "selected" not in output
    dashboard.key("j")
    assert "excluded from this view" in screen(dashboard, 80, 24)
    dashboard.key("?")
    output = screen(dashboard, 80, 24)
    assert "u rereads history" in output
    assert "Space pauses" not in output
    assert "u checks" not in output


def test_watch_rejects_non_tty_and_cli_invalid_options(brain: Store) -> None:
    with pytest.raises(Error, match="interactive terminal"):
        watch(brain)
    # Observation never suggests a command that executes programs.
    with pytest.raises(Error, match="use bf status for a JSON snapshot") as caught:
        watch(brain, observe=True)
    assert "--json" not in str(caught.value)
    with pytest.raises(Error, match="greater than or equal to 5"):
        watch(brain, interval=1)
    assert CliRunner().invoke(app, ["watch", "--interval", "1"]).exit_code == 2
    assert CliRunner().invoke(app, ["status", "--watch", "--check"]).exit_code == 2
    assert CliRunner().invoke(app, ["watch", "--help"]).exit_code == 0


def assert_terminal_restored(master: int, slave: int, previous: list[object]) -> None:
    # Darwin sets transient PENDIN when returning to canonical mode (xnu/bsd/kern/tty.c).
    # Exercise the next input before comparing every setting; do not flush or discard user input.
    os.write(master, b"restored\n")
    assert select.select([slave], [], [], 1)[0], "restored terminal did not accept line input"
    assert os.read(slave, 1024) == b"restored\n"
    assert termios.tcgetattr(slave) == previous


def test_keyboard_restores_terminal_after_failure() -> None:
    master, slave = pty.openpty()
    try:
        with os.fdopen(os.dup(slave), "r") as stream:
            previous = termios.tcgetattr(slave)
            with pytest.raises(RuntimeError, match="stop"), keyboard(stream) as descriptor:  # noqa: PT012 - terminal restoration after failure is the behavior under test
                assert not termios.tcgetattr(slave)[3] & termios.ICANON
                assert read_keys(descriptor, 0.1) == []
                # Held keys, pastes and remote terminals deliver several keys in one read.
                os.write(master, b"jjq\x1b[B\x1b[B\x1bOB\x1b[7~")
                assert read_keys(descriptor, 1) == ["j", "j", "q", "\x1b[B", "\x1b[B", "\x1bOB", "\x1b[7~"]
                raise RuntimeError("stop")
            assert_terminal_restored(master, slave, previous)
    finally:
        os.close(master)
        os.close(slave)


def test_a_closed_terminal_cancels_like_ctrl_c() -> None:
    master, slave = pty.openpty()
    try:
        with os.fdopen(os.dup(slave), "r") as stream, pytest.raises(KeyboardInterrupt), keyboard(stream):  # noqa: PT012 - the hangup happens inside the context
            # Closing the terminal hangs it up; main() turns the SIGHUP that follows into an interrupt (exit 130).
            os.close(master)
            master = -1
            raise KeyboardInterrupt
    finally:
        if master >= 0:
            os.close(master)
        os.close(slave)


def test_read_keys_completes_an_escape_sequence_split_across_reads() -> None:
    reader, writer = os.pipe()
    try:
        # A 64-byte read ends inside the sequence; its remainder follows in the next read.
        os.write(writer, b"j" * 62 + b"\x1b[1~")
        assert read_keys(reader, 1) == ["j"] * 62 + ["\x1b[1~"]
        os.close(writer)
        writer = -1
        assert read_keys(reader, 1) == ["q"]  # End of input closes the dashboard.
    finally:
        os.close(reader)
        if writer >= 0:
            os.close(writer)


def test_dashboard_applies_batched_keys_in_order(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    reader, writer = os.pipe()
    live = FakeLive()
    dashboard = Dashboard("fixture", observe=True)
    try:
        os.write(writer, b"jujq")  # u rereads history without moving the selection
        _loop(brain, dashboard, Job(brain, (), ()), (), (), live=cast("Live", live), descriptor=reader)
    finally:
        os.close(reader)
        os.close(writer)
    assert dashboard.cursor == 2
    assert live.frames == 1


def test_idle_dashboard_redraws_once_a_second(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write("bf.yaml", CONFIG)
    clock, waits = [0.0], []

    def keys(_descriptor: int, wait: float) -> list[str]:
        waits.append(wait)
        clock[0] += wait
        return ["q"] if clock[0] >= 10 else []

    monkeypatch.setattr("bf.watch.read_keys", keys)
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _: None))
    live = FakeLive()
    _loop(brain, Dashboard("fixture", observe=True), Job(brain, (), ()), (), (), live=cast("Live", live))
    # Ten idle seconds: one frame per second for relative ages, not one per key poll.
    assert live.frames == 10
    assert min(waits) == 1


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (Error("invalid bf.yaml: sensors.git.refresh: Input should be a valid integer"), None),
        (PermissionError(13, "Permission denied"), "Cannot read bf.yaml or history; run bf status for diagnostics"),
    ],
)
def test_observation_loop_never_executes_and_recovers_config_error(
    brain: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    message: str | None,
) -> None:
    calls = 0
    original = snapshot

    def changing(*_args: object) -> list[Row]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        return original(brain)

    ticks = iter([0, 1, 4, 7])
    monkeypatch.setattr("bf.watch.snapshot", changing)
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    monkeypatch.setattr(Job, "start", lambda _: pytest.fail("observation executed a program"))
    with pytest.raises(StopIteration):
        _loop(brain, Dashboard("fixture", observe=True), Job(brain, (), ()), (), ())
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["programs"] == []
    # BF's own diagnostic names the field without quoting content; other failures point to bf status.
    assert events[0]["message"] == (message or str(failure))
    assert events[1]["message"] == "Configuration recovered"
    assert not any(event["running"] for event in events)


def test_watcher_names_a_selected_program_bf_yaml_no_longer_declares(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `bf watch --sensor git` runs while its owner renames git in a bf.yaml that stays valid.
    brain.write("bf.yaml", CONFIG.replace(b"  git:\n", b"  gitlab:\n"))
    ticks = iter([0.0, 4.0, 8.0])
    starts: list[set[str]] = []

    def clock() -> float:
        tick = next(ticks)
        if tick == 4.0:
            brain.write("bf.yaml", CONFIG)
        return tick

    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=clock, sleep=lambda _: None))
    dashboard = Dashboard("fixture")
    monkeypatch.setattr(Job, "start", lambda _: starts.append({row.name for row in dashboard.rows if row.included}))
    with pytest.raises(StopIteration):
        _loop(brain, dashboard, Job(brain, ("git",), ()), ("git",), ())
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    # bf status sees nothing wrong with bf.yaml: the message names the selection, and nothing runs until it is valid.
    assert events[0]["message"] == "unknown sensor git; check names in bf.yaml; did you mean gitlab?"
    assert (events[0]["programs"], events[0]["running"]) == ([], False)
    assert starts == [{"git"}]
    assert events[1]["running"]


def test_job_runs_selected_program_and_retains_state(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nsensors:\n  sample:\n    command: [python3, sensors/sample.py]\n    refresh: 60\n",
    )
    brain.write("sensors/sample.py", b'import json\nprint(json.dumps([{"id":"one","title":"Local fixture"}]))\n')
    job = Job(brain, ("sample",), ())
    assert job.poll() is None
    job.start()
    try:
        assert job.process is not None
        assert job.process.wait(timeout=10) == 0
        assert snapshot(brain)[0].added == 1
    finally:
        job.close()


@pytest.mark.parametrize("slow", [False, True])
def test_stopping_a_job_leaves_time_to_roll_back_a_commit(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *, slow: bool
) -> None:
    clock, signals = [0], []

    class Update:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            signals.append("terminate")

        def wait(self, timeout: float | None = None) -> int:
            signals.append(f"wait {timeout}")
            if slow and timeout == 1:
                clock[0] += 1
                raise subprocess.TimeoutExpired("bf", 1)
            return 130

    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: clock[0]))
    job = Job(brain, (), ())
    job.process = cast("subprocess.Popen[bytes]", Update())
    job.close()
    # A cancelled update rolls back its interrupted record commit before exiting; SIGKILL would leave the journal.
    # Only a stop that takes longer than a second says why the terminal waits.
    assert signals == (["terminate", "wait 1", "wait 59"] if slow else ["terminate", "wait 1"])
    stopping = "bf: stopping the active update; this can take up to 60 seconds\n"
    assert capsys.readouterr().err == (stopping if slow else "")


class Hung:
    """An update that exits only once killed; each timed wait advances the fake clock by its whole timeout."""

    def __init__(self, clock: list[int], signals: list[str], name: str = "") -> None:
        self.clock, self.signals, self.name = clock, signals, name

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.signals.append(f"terminate{self.name}")

    def kill(self) -> None:
        self.signals.append(f"kill{self.name}")

    def wait(self, timeout: int | None = None) -> int:
        self.signals.append(f"wait {timeout}{self.name}")
        if timeout is None:
            return -9
        self.clock[0] += timeout
        raise subprocess.TimeoutExpired("bf", timeout)


def test_stopping_a_job_after_the_terminal_closed_still_waits_then_kills(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, signals = [0], []

    class HungUp(io.StringIO):
        def write(self, _text: str) -> int:
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(sys, "stderr", HungUp())
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: clock[0]))
    job = Job(brain, (), ())
    job.process = cast("subprocess.Popen[bytes]", Hung(clock, signals))
    job.close()
    assert signals == ["terminate", "wait 1", "wait 59", "kill", "wait None"]


def test_a_cancellation_while_stopping_still_waits_then_kills(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    clock, signals = [0], []

    class Closing(Hung):
        def wait(self, timeout: int | None = None) -> int:
            if timeout == 59:
                # The closing terminal hangs watch up again, or its user presses Ctrl-C a second time.
                os.kill(os.getpid(), signal.SIGHUP)
            return super().wait(timeout)

    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: clock[0]))
    job = Job(brain, (), ())
    job.process = cast("subprocess.Popen[bytes]", Closing(clock, signals))
    # Like main(): a hangup cancels like Ctrl-C.
    previous = signal.signal(signal.SIGHUP, _cancel)
    try:
        with pytest.raises(KeyboardInterrupt):
            _stop(job)
    finally:
        signal.signal(signal.SIGHUP, previous)
    # The interrupt waits until the update was killed, so watch never exits leaving it unsupervised.
    assert signals == ["terminate", "wait 1", "wait 59", "kill", "wait None"]


def test_job_tells_skipped_cache_files_apart_from_failed_programs(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nsensors:\n"
        b"  sample:\n    command: [python3, sensors/sample.py]\n    refresh: 60\n"
        b"  failing:\n    command: [python3, sensors/failing.py]\n    refresh: 60\n",
    )
    brain.write("sensors/sample.py", b'import json\nprint(json.dumps([{"id":"one","title":"Local fixture"}]))\n')
    brain.write("sensors/failing.py", b"raise SystemExit(3)\n")
    brain.write("projects/broken.md", b"---\nstale_after: soon\n---\n# Broken\n")
    # Every selected program ran and only the search cache skipped a file, or a program also failed.
    for selected, skipped in ((("sample",), 1), (("failing",), 0)):
        job = Job(brain, selected, ())
        job.start()
        try:
            assert job.process is not None
            assert job.process.wait(timeout=30) == 1
            assert job.skipped() == skipped
        finally:
            job.close()


def test_json_watch_reports_skipped_files_without_a_collection_alert(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    brain.write("bf.yaml", CONFIG)
    ticks = iter([0.0, 1.0, 4.0])
    alerts: list[tuple[str, ...]] = []
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    monkeypatch.setattr(Job, "start", lambda _: None)
    monkeypatch.setattr(Job, "poll", lambda _: 1)
    monkeypatch.setattr(Job, "skipped", lambda _: 2)
    monkeypatch.setattr("bf.watch_settings.desktop", lambda *args: alerts.append(args) or True)
    with pytest.raises(StopIteration):
        watch(brain, interval=5, json_output=True)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["message"] == "Search cache skipped 2 files; run bf validate"
    assert alerts == []


# Ctrl-C makes the terminal send SIGINT to the dashboard's process group; signalling the child directly
# exercises the same cancellation without making the PTY a controlling terminal, which Darwin revokes on exit.
@pytest.mark.parametrize(("stop", "code"), [(b"q", 0), (signal.SIGINT, 130), (signal.SIGTERM, 130)])
def test_tui_exit_restores_terminal_and_cancels_sensor(
    brain: Store, tmp_path: Path, stop: bytes | signal.Signals, code: int
) -> None:
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nsensors:\n  slow:\n    command: [python3, sensors/slow.py]\n    refresh: 60\n",
    )
    marker = tmp_path / "started"
    brain.write(
        "sensors/slow.py",
        f"import os, time\nfrom pathlib import Path\nPath({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\nprint('[]')\n".encode(),
    )
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    child = subprocess.Popen(  # noqa: S603 - exercise BF with an isolated synthetic brain
        [sys.executable, "-m", "bf", "watch", "--brain", str(brain.root)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"},
        start_new_session=True,
    )
    output = bytearray()
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))
        assert marker.exists(), output.decode(errors="replace")
        if isinstance(stop, bytes):
            os.write(master, stop)
        else:
            child.send_signal(stop)
        # Drain output while the terminal restores; never leave a full PTY blocking exit.
        while child.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))
        assert child.wait(timeout=5) == code
        assert_terminal_restored(master, slave, original)
        pid = int(marker.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert b"\x1b[?1049h" in output
        assert b"\x1b[?1049l" in output
        assert b"\x1b[?25h" in output
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=10)
        os.close(master)
        os.close(slave)


def test_shipped_offline_watch_example(tmp_path: Path) -> None:
    import shutil

    from bf.update import update

    source = ROOT / "examples/watch"
    store = Store(Path(shutil.copytree(source, tmp_path / "watch-demo")))
    first: dict = update(store)
    assert not first["ok"]
    results = {entry["sensor"]: entry for entry in first["sensors"]}
    assert set(results) == {"calendar", "git", "unavailable"}
    assert results["calendar"]["added"] == 1
    assert results["git"]["added"] == 1
    assert results["unavailable"]["status"] == "failed"
    rows = {row.name: row for row in snapshot(store)}
    # The failed sensor retries after one minute of backoff, so the next five-second cycles skip it.
    failed_at = datetime.fromisoformat(str(state(store)["unavailable"]["run"]))
    assert datetime.fromisoformat(rows["unavailable"].next_due) == failed_at + timedelta(minutes=1)
    assert rows["unavailable"].status is State.FAILED
    dashboard = Dashboard("watch-demo", rows=snapshot(store))
    dashboard.key("i")
    assert [(row.name, row.records) for row in dashboard.visible()[:2]] == [("calendar", 1), ("git", 1)]
    output = screen(dashboard, 80, 24)
    assert "items ↓" in output
    assert "1/0/0" in output
    dashboard.key("r")
    assert "items ↑" in screen(dashboard, 80, 24)
    dashboard.key("s")
    assert dashboard.visible()[0].name == "unavailable"
    second: dict = update(store, sensors=("calendar", "git"))
    assert second["ok"]
    assert second["sensors"] == []
    # Follow the README's source-addition command, then reuse watch's actual update subprocess.
    example = (source / "README.md").read_text().split("```bash\npython3 - <<'PYTHON'\n", 1)[1].split("\nPYTHON", 1)[0]
    subprocess.run(  # noqa: S603 - repository-owned example, isolated fictional brain
        [sys.executable, "-c", example], cwd=store.root, check=True, timeout=60
    )
    job = Job(store, (), ())
    job.start()
    try:
        assert job.process is not None
        assert job.process.wait(timeout=10) == 0
        assert job.reply is not None
        job.reply.seek(0)
        refreshed = json.load(job.reply)
        assert [entry["sensor"] for entry in refreshed["sensors"]] == ["new-calendar"]
        assert refreshed["sensors"][0]["added"] == 1
        assert {row.name: row for row in snapshot(store)}["new-calendar"].records == 1
    finally:
        job.close()


@pytest.mark.parametrize("delivered", [True, False])
def test_json_watch_reports_completion_rows_and_delivery_warning(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], delivered: bool
) -> None:
    brain.write("bf.yaml", CONFIG)
    state_store(brain.root).write(
        "sensors.json", encode({"calendar": {"success": "2026-09-01T14:00:00+02:00", "records": 10, "added": 1}})
    )
    starts: list[float] = []
    ticks = iter([0.0, 1.0, 4.0, 7.0, 8.0])
    results = iter([0, 1])
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    monkeypatch.setattr(Job, "start", lambda _: starts.append(1))
    monkeypatch.setattr(Job, "poll", lambda _: next(results))
    monkeypatch.setattr("bf.watch_settings.desktop", lambda *_args: delivered)
    with pytest.raises(StopIteration):
        watch(brain, interval=5, json_output=True)
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert len(starts) == 2
    assert any(event["message"] == "Update complete" for event in events)
    assert not events[-1]["running"]
    assert set(events[0]) == {"brain", "running", "assisting", "message", "programs"}
    calendar = next(row for row in events[0]["programs"] if row["name"] == "calendar")
    assert (calendar["records"], calendar["added"], calendar["updated"]) == (10, 1, None)
    assert calendar["success"] == "2026-09-01T12:00:00+00:00"
    warnings = captured.err.splitlines()
    if delivered:
        assert events[-1]["message"].startswith("Update failed")
        assert warnings == []
    else:
        # An unavailable desktop warns once with a bf: line on stderr, and collection continues.
        assert events[-1]["message"].startswith("Desktop alerts unavailable")
        assert warnings == [f"bf: {events[-1]['message']}"]


def test_json_watch_writes_utf8_whatever_the_locale(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write("bf.yaml", CONFIG)
    state_store(brain.root).write(
        "sensors.json", encode({"calendar": {"run": "2026-09-01T12:00:00Z", "error": "échec du fournisseur"}})
    )
    raw = io.BytesIO()
    # Like `bf read`, the JSON Lines stream is UTF-8 even when the terminal's encoding is ASCII.
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii"))
    ticks = iter([0.0])
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    monkeypatch.setattr(Job, "start", lambda _: None)
    with pytest.raises(StopIteration):
        watch(brain, interval=5, json_output=True)
    event = json.loads(raw.getvalue().decode("utf-8").splitlines()[0])
    calendar = next(row for row in event["programs"] if row["name"] == "calendar")
    assert calendar["error"] == "échec du fournisseur"


def test_status_watch_routes_to_observation(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr("bf.watch.watch", lambda store, **kwargs: calls.append((store.root, kwargs)))
    result = CliRunner().invoke(app, ["status", "--watch", "--brain", str(brain.root)])
    assert result.exit_code == 0
    assert calls == [(brain.root, {"observe": True})]


@pytest.fixture(autouse=True)
def fixed_action_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bf.watch_settings.desktop", lambda *_args: True)
    monkeypatch.setattr(collector, "uuid4", lambda: UUID(int=1))


@pytest.mark.parametrize("key", ["f", "u"])
@pytest.mark.parametrize("mode", ["collect", "paused", "observe", "selected"])
def test_refresh_reloads_sources_and_preserves_execution_boundaries(
    brain: Store, monkeypatch: pytest.MonkeyPatch, key: str, mode: str
) -> None:
    brain.write("bf.yaml", CONFIG)
    sensors = ("calendar",) if mode == "selected" else ()
    dashboard = Dashboard("fixture", observe=mode == "observe", paused=mode == "paused", next_check=600)
    starts: list[set[str]] = []
    clock = [1.0]
    steps = 0

    def start(_job: Job) -> None:
        starts.append({row.name for row in dashboard.rows if row.included})

    def keys(_descriptor: int, _wait: float) -> list[str]:
        nonlocal steps
        steps += 1
        clock[0] += 0.25
        if steps == 1:
            brain.write(
                "bf.yaml",
                CONFIG.replace(b"sensors:\n", b"sensors:\n  new-source:\n    command: [echo]\n    refresh: 60\n"),
            )
            return [key]
        assert "new-source" in {row.name for row in dashboard.rows}
        if mode == "paused" and steps == 2:
            assert not starts
            assert "resume with Space" in dashboard.message
            return [" "]
        if mode == "paused" and not starts:
            return []
        return ["q"]

    monkeypatch.setattr(Job, "start", start)
    monkeypatch.setattr("bf.watch.read_keys", keys)
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: clock[0]))
    _loop(brain, dashboard, Job(brain, sensors, ()), sensors, (), live=cast("Live", FakeLive()))
    if mode == "observe":
        assert not starts
    else:
        assert len(starts) == 1
        assert ("new-source" in starts[0]) == (mode != "selected")


@pytest.mark.parametrize("invalid", [False, True])
def test_refresh_during_update_coalesces_and_waits_for_valid_configuration(
    brain: Store, monkeypatch: pytest.MonkeyPatch, invalid: bool
) -> None:
    brain.write("bf.yaml", CONFIG)
    dashboard = Dashboard("fixture")
    starts: list[set[str]] = []
    clock = [1.0]
    steps = 0
    changed = CONFIG.replace(b"sensors:\n", b"sensors:\n  new-source:\n    command: [echo]\n    refresh: 60\n")

    def start(_job: Job) -> None:
        starts.append({row.name for row in dashboard.rows})

    def keys(_descriptor: int, _wait: float) -> list[str]:
        nonlocal steps
        steps += 1
        clock[0] += 0.25
        if steps == 1:
            brain.write("bf.yaml", b"invalid: [" if invalid else changed)
            return ["f", "f", "u"]
        if steps == 2:
            assert len(starts) == 1  # The active update must finish before another starts.
            return []
        if invalid and steps == 3:
            assert len(starts) == 1
            assert dashboard.message == "bf.yaml: invalid YAML at line 2, column 1"
            brain.write("bf.yaml", changed)
            return []  # The pending request survives the error without pressing f again.
        if len(starts) == 1:
            assert steps < 20
            return []
        assert len(starts) == 2
        assert "new-source" in starts[1]
        # Finish the follow-up; no third update should be queued by the repeated keys.
        return ["q"] if not dashboard.running else []

    monkeypatch.setattr(Job, "start", start)
    monkeypatch.setattr(Job, "poll", lambda _: 0 if steps >= 2 else None)
    monkeypatch.setattr("bf.watch.read_keys", keys)
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: clock[0]))
    _loop(brain, dashboard, Job(brain, (), ()), (), (), live=cast("Live", FakeLive()))
    assert len(starts) == 2


def test_a_check_interval_slower_than_a_refresh_is_flagged(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    brain.write("bf.yaml", CONFIG)
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=iter([0.0]).__next__, sleep=lambda _: None))
    monkeypatch.setattr(Job, "start", lambda _: None)
    # A program is due only at a check, so checking slower than its refresh runs it late.
    with pytest.raises(StopIteration):
        watch(brain, interval=7200, json_output=True)
    late = capsys.readouterr().err
    assert late.startswith("bf: ")
    assert "but checks happen every 7200s" in late
    assert late.endswith("; lower the watch interval\n")
    # The dashboard keeps it in its header, whatever the message line says, and the guide explains it: both whole
    # at the 80 by 24 minimum, where the guide keeps its closing lines too.
    monkeypatch.setattr("bf.watch.time", time)
    dashboard = Dashboard("fixture", late=late.removeprefix("bf: ").strip())
    assert "interval too slow, press ?" in screen(dashboard, 80, 24)
    dashboard.help = True
    guide = screen(dashboard, 80, 24)
    for line in ("checks happen every 7200s", "Space pauses future checks", "not proof the provider is unchanged"):
        assert line in guide
    # A long program name wraps the warning onto a third line, which costs a line that the footer repeats.
    dashboard.late = dashboard.late.replace("git", "a-long-program-name-for-the-weekly-git-review")
    assert "not proof the provider is unchanged" in screen(dashboard, 80, 24)
    # An interval faster than every refresh says nothing.
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=iter([0.0]).__next__, sleep=lambda _: None))
    with pytest.raises(StopIteration):
        watch(brain, interval=60, json_output=True)
    assert not capsys.readouterr().err


HELPER = b"""assistants:
  helper:
    prompt: prompts/helper.md
    command: [harness]
"""


def json_watch(
    brain: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    seconds: float,
    step: Callable[[float], None] | None = None,
) -> tuple[list[dict], list[str]]:
    """Run `bf watch --json`, which snapshots every two seconds, for `seconds` of a fake clock that each 0.1-second
    sleep advances; `step` sees the time."""
    clock = [0.0]

    def sleep(delay: float) -> None:
        clock[0] = round(clock[0] + delay, 3)
        if step is not None:
            step(clock[0])
        if clock[0] >= seconds:
            raise StopIteration

    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(Job, "start", lambda _: None)
    with pytest.raises(StopIteration):
        watch(brain, interval=60, json_output=True)
    captured = capsys.readouterr()
    return [json.loads(line) for line in captured.out.splitlines()], captured.err.splitlines()


def test_snapshot_reads_one_bf_yaml_while_an_editor_saves_it(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write("bf.yaml", CONFIG + HELPER.replace(b"  helper:", b"  renamed:"))
    renamed = load(brain)
    brain.write("bf.yaml", CONFIG + HELPER)
    # The atomic save that renames the assistant lands right after the dashboard's own load.
    monkeypatch.setattr("bf.health.load", lambda _store: renamed)
    rows = {row.name: row for row in snapshot(brain)}
    assert rows["helper"].kind == "assistant"
    assert "renamed" not in rows


def test_watch_signals_the_update_then_stops_the_dispatcher_then_waits(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain.write("bf.yaml", CONFIG + HELPER)
    events: list[str] = []

    class Update:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            events.append("start")

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            events.append("terminate")

        def close(self) -> None:
            events.append("close")

    class Dispatching:
        def __init__(self, _store: Store, names: set[str]) -> None:
            assert names == {"helper"}
            self.names = names
            self.tasks: dict[str, object] = {}
            self.transports: dict[str, object] = {}
            self.health: dict[str, str] = {}
            self.queue = SimpleNamespace(outstanding=lambda _names: [])

        def __enter__(self) -> Dispatching:
            events.append("dispatcher start")
            return self

        def __exit__(self, *_: object) -> None:
            events.append("dispatcher close")

        def tick(self) -> None:
            pass

    monkeypatch.setattr("bf.watch.Job", Update)
    monkeypatch.setattr("bf.watch.Dispatcher", Dispatching)
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=iter([0.0]).__next__, sleep=lambda _: None))
    with pytest.raises(StopIteration):
        watch(brain, json_output=True)
    # Exit callbacks run in reverse: the update is signalled before the dispatcher waits for its harnesses, and watch
    # waits for the update only once the dispatcher stopped.
    assert events == ["start", "dispatcher start", "terminate", "dispatcher close", "close"]


@pytest.mark.parametrize("failure", ["invalid", "snapshot", "prompt", "capacity"])
def test_watch_shows_dispatcher_errors_and_keeps_collecting(
    brain: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    # A due schedule whose prompt is missing, a half-saved bf.yaml or a full queue never stops watch.
    config = CONFIG + HELPER + b"    schedules:\n      review: {every: 3600}\n"
    brain.write("bf.yaml", config)
    if failure != "prompt":
        brain.write("prompts/helper.md", b"Review.\n")
    if failure == "capacity":
        # The queue refuses the schedule's job: schedule() reports it per assistant, retried after a backoff.
        monkeypatch.setattr(
            "bf.assistants.Queue.enqueue",
            lambda *_: (_ for _ in ()).throw(Error("assistant queue is full; finish, retry or cancel unfinished work")),
        )
    ran: list[str] = []
    monkeypatch.setattr("bf.assistants.Dispatcher.runner", lambda *_: ran.append("harness") or b"Reviewed.")

    # Saved between two-second snapshots, so the next dispatcher pass reads it first, or right after a pass, so the
    # next snapshot does.
    saved = {"invalid": (0.5, 3.5), "snapshot": (1.5, 4.5)}.get(failure, ())

    def step(now: float) -> None:
        if saved and now == saved[0]:
            brain.write("bf.yaml", config + b"  broken: [\n")
        if saved and now == saved[1]:
            brain.write("bf.yaml", config)

    events, errors = json_watch(brain, monkeypatch, capsys, 9, step)
    reason = {
        "invalid": "bf.yaml: invalid YAML",
        "snapshot": "bf.yaml: invalid YAML",
        "prompt": "prompts/helper.md: cannot read assistant helper's prompt",
        "capacity": "assistant queue is full",
    }[failure]
    messages = [event["message"] for event in events]
    assert any(message.startswith(reason) for message in messages), messages
    # One diagnostic line on stderr for each new reason, not one per pass.
    assert [line for line in errors if reason in line] == [errors[0]]
    assert errors[0].startswith("bf: ")
    if saved:
        assert "Configuration recovered" in messages
        assert messages[-1] == "Assistant work resumed"
    else:
        # A schedule that cannot be admitted queues nothing, and collection keeps reporting its programs.
        assert ran == []
        assert all(event["programs"] for event in events)


@pytest.mark.parametrize("failure", ["read", "stream", "delivery"])
def test_watch_shows_failing_inputs_and_deliveries_until_they_recover(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    channel = {
        "read": b"  inbox:\n    read: {command: [reader]}\n",
        "stream": b"  chat:\n    send: {command: [send]}\n    stream: {command: [receive]}\n",
        "delivery": b"  chat:\n    send: {command: [send]}\n",
    }[failure]
    settings = {
        "read": b"    inputs:\n      - channel: {name: inbox, mode: read, every: 1}\n",
        "stream": b"    inputs:\n      - channel: {name: chat, mode: stream}\n",
        "delivery": b"    schedules:\n      review: {every: 3600}\n    outputs:\n      - channel: {name: chat}\n",
    }[failure]
    brain.write("bf.yaml", CONFIG + b"channels:\n" + channel + HELPER + settings)
    brain.write("prompts/helper.md", b"Review.\n")
    queue = Queue(brain)
    # The read's previous window ended an hour ago, so each poll reads.
    earlier = timestamp((datetime.now(UTC) - timedelta(hours=1)).isoformat())
    queue.set_progress("read:helper:inbox", {"since": earlier, "end": earlier})
    broken, connections = [True], []

    def reading(*_args: object, **_kwargs: object) -> list[object]:
        if broken[0]:
            raise Error("program exited with status 1")
        return []

    def streaming(*args: object, **_: object) -> None:
        # Stops once with an error, then stays connected until watch stops.
        connections.append(1)
        if len(connections) == 1:
            raise Error("channel chat: stream closed its output")
        cast("Event", args[-1]).wait()

    def runner(_self: Dispatcher, _argv: list[str], _command: object, _store: Store, name: str, _stdin: bytes) -> bytes:
        if name == "channel.chat.send":
            raise Error("program exited with status 1")
        return b"Reviewed."

    monkeypatch.setattr("bf.assistants.channels.read", reading)
    monkeypatch.setattr("bf.assistants.channels.stream", streaming)
    monkeypatch.setattr("bf.assistants.Dispatcher.runner", runner)
    # The dispatcher's read intervals follow watch's fake clock.
    monkeypatch.setattr("bf.assistants.time", SimpleNamespace(monotonic=lambda: watching.time.monotonic()))
    dispatchers: list[Dispatcher] = []
    enter = Dispatcher.__enter__
    monkeypatch.setattr(Dispatcher, "__enter__", lambda self: dispatchers.append(self) or enter(self))

    def step(now: float) -> None:
        # Each pass sees the outcome of the work the previous one started, and a stream's recorded failure.
        if dispatchers:
            futures.wait(list(dispatchers[0].tasks.values()), timeout=10)
        if failure == "stream" and now == 0.5:
            deadline = time.monotonic() + 10
            while not queue.progress("health:channel:chat").get("error") and time.monotonic() < deadline:
                time.sleep(0.01)
        if now == 4.5:
            # The input reads again, the stream accepts a message, or the owner cancels the uncertain delivery.
            broken[0] = False
            if failure == "stream":
                queue.set_progress("health:channel:chat", {"error": "", "at": timestamp(datetime.now(UTC).isoformat())})
            for delivery in cast("list[dict]", queue.report()["deliveries"]):
                queue.retry(str(delivery["id"]), cancel=True)

    events, errors = json_watch(brain, monkeypatch, capsys, 9, step)
    reason = {
        "read": "channel inbox: read failed: program exited with status 1; see logs/channel.inbox.read.log",
        "stream": "channel chat: stream closed its output",
        "delivery": "channel chat: delivery failed or uncertain; inspect bf queue",
    }[failure]
    messages = [event["message"] for event in events]
    assert reason in messages, messages
    # One diagnostic line for the failure, whichever pass sees it again; then work resumes.
    assert errors == [f"bf: {reason}"]
    assert messages[-1] == "Assistant work resumed"


def test_watch_waits_for_an_update_that_owns_assistant_work(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    brain.write("bf.yaml", CONFIG + HELPER)
    brain.write("prompts/helper.md", b"Review.\n")
    owner = collecting(brain, ".dispatch")
    owner.__enter__()

    def step(now: float) -> None:
        if now == 2.0:
            # bf update finishes its assistant cycle.
            owner.__exit__(None, None, None)

    try:
        events, errors = json_watch(brain, monkeypatch, capsys, 5, step)
    finally:
        owner.__exit__(None, None, None)
    waiting = "Another bf update owns assistant work; assistants start once it finishes"
    # The first snapshot precedes the first dispatcher pass.
    assert events[1]["message"] == waiting
    assert errors == [f"bf: {waiting}"]
    assert events[-1]["message"] == "Assistant work resumed"


def test_a_stopped_stream_worker_still_stops_watch(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write(
        "bf.yaml",
        CONFIG
        + b"channels:\n  chat:\n    send: {command: [send]}\n    stream: {command: [receive]}\n"
        + HELPER
        + b"    inputs:\n      - channel: {name: chat, mode: stream}\n",
    )
    brain.write("prompts/helper.md", b"Answer.\n")
    # A worker that returns without being stopped: watch exits so its service manager restarts it.
    monkeypatch.setattr("bf.assistants.Dispatcher._stream", lambda *_: None)
    clock = [0.0]

    def sleep(delay: float) -> None:
        clock[0] += delay
        assert clock[0] < 10, "watch kept running without its stream"

    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(Job, "start", lambda _: None)
    with pytest.raises(Error, match="stream worker stopped unexpectedly"):
        watch(brain, interval=3600, json_output=True)


def test_an_idle_dispatcher_ticks_once_a_second(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    brain.write("bf.yaml", CONFIG + HELPER)
    brain.write("prompts/helper.md", b"Review.\n")
    ticks: list[float] = []
    clock: list[float] = []
    original = Dispatcher.tick

    def counted(self: Dispatcher) -> None:
        ticks.append(1)
        original(self)

    monkeypatch.setattr(Dispatcher, "tick", counted)
    json_watch(brain, monkeypatch, capsys, 10, clock.append)
    # Ten seconds of 0.1-second passes reach the queue ten times, not a hundred.
    assert len(clock) == 100
    assert len(ticks) == 10


def test_watch_clears_the_transport_health_its_dispatcher_records(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    brain.write("bf.yaml", CONFIG + HELPER)
    brain.write("prompts/helper.md", b"Review.\n")
    queue = Queue(brain)
    # A watcher that crashed left a stream error; a running one records another.
    queue.set_progress("health:channel:chat", {"error": "stream exited", "at": "2026-10-17T00:00:00.000000Z"})
    seen: list[dict] = []

    def step(now: float) -> None:
        if now == 1.0:
            seen.append(cast("dict", queue.report()["transports"]))
            queue.set_progress("health:read:helper:chat", {"error": "read failed", "at": "2026-10-17T00:00:00.000000Z"})

    json_watch(brain, monkeypatch, capsys, 2, step)
    # Health describes a running dispatcher only: bf status --check recovers once watch stops.
    assert seen == [{}]
    assert queue.report()["transports"] == {}
