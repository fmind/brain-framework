"""Terminal rendering, keyboard behavior, read-only observation and process cancellation."""

from __future__ import annotations

import io
import json
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from rich.console import Console
from typer.testing import CliRunner

import bf.collect as collector
from bf.cli import app
from bf.models import Error, encode
from bf.storage import Store, state_store
from bf.watch import Dashboard, Job, Row, clean, duration, keyboard, read_key, snapshot, watch

CONFIG = b"""version: 6
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
                },
                "git": {"success": (now - timedelta(days=1)).isoformat(), "error": "program exited with status 1"},
            }
        ),
    )
    state_store(brain.root).write(
        "routines.json", encode({"review": {"success": now.isoformat(), "action": "actions/review/ACTION.md"}})
    )
    rows = {row.name: row for row in snapshot(brain, ("calendar",))}
    assert rows["calendar"].status == "fresh"
    assert rows["calendar"].added == 3
    assert rows["calendar"].elapsed_seconds == 0.25
    assert rows["calendar"].output_bytes == 128
    assert rows["calendar"].reconcile
    assert rows["git"].status == "failed"
    assert not rows["git"].included
    assert rows["git"].log.endswith("git.log")
    assert rows["manual"].status == "manual"
    assert not rows["manual"].next_due
    assert rows["disabled"].status == "disabled"
    assert rows["review"].action.endswith("ACTION.md")
    state_store(brain.root).write(
        "sensors.json", encode({"calendar": {"success": (now - timedelta(days=1)).isoformat()}})
    )
    assert {row.name: row.status for row in snapshot(brain)}["calendar"] == "due"
    assert {row.name: row.status for row in snapshot(brain)}["git"] == "never"


@pytest.mark.parametrize(("width", "height"), [(140, 36), (80, 24), (40, 12)])
def test_dashboard_layout_keeps_data_literal_and_handles_resize(width: int, height: int) -> None:
    dashboard = Dashboard(
        "[red]fixture\x1b]0;bad",
        rows=[
            Row(
                "sensor",
                "calendar",
                "fresh",
                True,
                3600,
                success=datetime.now(UTC).isoformat(),
                added=3,
                updated=2,
                removed=0,
            ),
            Row("sensor", "git", "failed", True, 300, error="[bold]literal\x1b[2J", log="/private/log"),
            Row("routine", "review", "never", False, 86400),
        ],
    )
    output = io.StringIO()
    console = Console(file=output, width=width, height=height, color_system=None)
    console.print(dashboard.render(width, height))
    assert "BF" in output.getvalue()
    assert "\x1b" not in output.getvalue()
    dashboard.cursor = 1
    console.print(dashboard.render(width, height))
    dashboard.help = True
    console.print(dashboard.render(width, height))
    if width >= 80:
        assert "q quit" in output.getvalue()
    assert clean("x\n\x1b\t") == "x   "


def test_empty_many_rows_and_controls() -> None:
    dashboard = Dashboard("fixture")
    dashboard.render(100, 24)
    assert dashboard.key("q") == "quit"
    assert dashboard.key("u") == "refresh"
    assert dashboard.key("j") == ""
    assert dashboard.cursor == 0
    dashboard.rows = [Row("sensor", f"source-{i}", "failed" if i == 25 else "fresh", True, 60) for i in range(40)]
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


def test_watch_rejects_non_tty_and_cli_invalid_options(brain: Store) -> None:
    with pytest.raises(Error, match="interactive terminal"):
        watch(brain)
    with pytest.raises(Error, match="greater than or equal to 5"):
        watch(brain, interval=1)
    assert CliRunner().invoke(app, ["watch", "--interval", "1"]).exit_code == 2
    assert CliRunner().invoke(app, ["status", "--watch", "--check"]).exit_code == 2
    for command in ("watch", "schedule"):
        result = CliRunner().invoke(app, [command, "--help"])
        assert result.exit_code == 0


def test_keyboard_restores_terminal_after_failure() -> None:
    master, slave = pty.openpty()
    try:
        with os.fdopen(os.dup(slave), "r") as stream:
            previous = termios.tcgetattr(slave)
            with pytest.raises(RuntimeError, match="stop"), keyboard(stream) as descriptor:  # noqa: PT012 - terminal restoration after failure is the behavior under test
                assert not termios.tcgetattr(slave)[3] & termios.ICANON
                assert read_key(descriptor) == ""
                os.write(master, b"\x1b[B")
                assert read_key(descriptor) == "\x1b[B"
                raise RuntimeError("stop")
            assert termios.tcgetattr(slave) == previous
    finally:
        os.close(master)
        os.close(slave)


def test_json_watch_observes_without_execution_and_recovers_config_error(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0
    original = snapshot

    def changing(*_args: object) -> list[Row]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise Error("bad configuration")
        return original(brain)

    ticks = iter([0, 1, 4, 7])
    monkeypatch.setattr("bf.watch.snapshot", changing)
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    monkeypatch.setattr(Job, "start", lambda _: pytest.fail("observation executed a program"))
    with pytest.raises(StopIteration):
        watch(brain, observe=True, json_output=True)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["programs"] == []
    assert all(event["observing"] for event in events)
    assert "Cannot read" in events[0]["message"]


def test_job_runs_selected_program_and_retains_state(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 6\nname: fixture\nsensors:\n  sample:\n    command: [python3, sensors/sample.py]\n    refresh: 60\n",
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


def test_tui_quit_restores_terminal_and_cancels_sensor(brain: Store, tmp_path: Path) -> None:
    brain.write(
        "bf.yaml",
        b"version: 6\nname: fixture\nsensors:\n  slow:\n    command: [python3, sensors/slow.py]\n    refresh: 60\n",
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
        os.write(master, b"q")
        # Drain output while the terminal restores; never leave a full PTY blocking exit.
        while child.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))
        assert child.wait(timeout=5) == 0
        assert termios.tcgetattr(slave) == original
        pid = int(marker.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert b"\x1b[?1049h" in output
        assert b"\x1b[?1049l" in output
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=10)
        os.close(master)
        os.close(slave)


def test_shipped_offline_watch_example(tmp_path: Path) -> None:
    import shutil

    from bf.update import update

    source = Path(__file__).resolve().parents[1] / "examples/watch"
    store = Store(Path(shutil.copytree(source, tmp_path / "watch-demo")))
    first: dict = update([store])
    assert not first["ok"]
    results = {entry["sensor"]: entry for entry in first["brains"][0]["sensors"]}
    assert set(results) == {"calendar", "git", "unavailable"}
    assert results["calendar"]["added"] == 1
    assert results["git"]["added"] == 1
    assert results["unavailable"]["status"] == "failed"
    second: dict = update([store], sensors=("calendar", "git"))
    assert second["ok"]
    assert second["brains"][0]["sensors"] == []


def test_json_watch_reports_completion_and_spaces_retries(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    starts: list[float] = []
    ticks = iter([0.0, 1.0, 4.0, 7.0, 8.0])
    results = iter([0, 1])
    monkeypatch.setattr("bf.watch.time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    monkeypatch.setattr(Job, "start", lambda _: starts.append(1))
    monkeypatch.setattr(Job, "poll", lambda _: next(results))
    with pytest.raises(StopIteration):
        watch(brain, interval=5, json_output=True)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(starts) == 2
    assert any(event["message"] == "Update complete" for event in events)
    assert any(event["message"].startswith("Update failed") for event in events)
    assert not events[-1]["running"]


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
