"""Generated native files preserve selection, arguments and user-owned configuration."""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from bf.cli import app
from bf.models import Error, Program
from bf.schedule import backend_name, generate
from bf.storage import Store, collecting
from bf.update import update

CONFIG = b"""version: 7
name: fixture
sensors:
  mail:
    command: [echo]
    refresh: 3600
  git:
    command: [echo]
    refresh: 300
  manual:
    command: [echo]
  disabled:
    command: [echo]
    refresh: 300
    enabled: false
routines:
  review:
    command: [echo]
    refresh: 86400
"""


@pytest.fixture
def scheduled(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    return brain


def test_update_selects_only_named_programs_and_keeps_due_rules(scheduled: Store) -> None:
    preview: dict = update(scheduled, sensors=("mail", "manual", "disabled"), dry_run=True)
    assert [s["sensor"] for s in preview["sensors"]] == ["mail"]
    assert preview["routines"] == []
    preview: dict = update(scheduled, routines=("review",), dry_run=True)
    assert preview["sensors"] == []
    assert [r["routine"] for r in preview["routines"]] == ["review"]
    executed = []

    def fake(*args: object) -> bytes:
        executed.append(args)
        return b'[{"id":"one","title":"One"}]'

    assert update(scheduled, sensors=("mail",), runner=fake)["ok"]
    assert len(executed) == 1
    assert update(scheduled, sensors=("mail",), runner=fake)["ok"]
    assert len(executed) == 1


def test_invalid_selection_is_rejected_before_any_execution(scheduled: Store) -> None:
    calls = []
    with pytest.raises(Error, match="unknown sensor"):
        update(scheduled, sensors=("mail", "absent"), runner=lambda *args: (calls.append(args), b"[]")[1])
    assert not calls
    # Like a busy collect, a cycle that cannot start fails instead of reporting an empty run.
    with collecting(scheduled, ".update"), pytest.raises(Error, match="another update is still active"):
        update(scheduled, wait=0.1)


def test_schedules_for_other_selections_queue_instead_of_failing(
    scheduled: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Named schedules fire at the same minutes; a second cycle waits for the first, then runs its own due list.
    entered, release, queued = Event(), Event(), Event()
    released: list[bool] = []
    lock = collecting

    def asking(*args: Any, **kwargs: Any) -> Any:
        if entered.is_set():
            queued.set()
        return lock(*args, **kwargs)

    def slow(_argv: list[str], program: Program, *_: object) -> bytes:
        if program.refresh == 3600:
            entered.set()
            assert release.wait(5)
        else:
            released.append(release.is_set())
        return b"[]"

    monkeypatch.setattr("bf.update.collecting", asking)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(update, scheduled, sensors=("mail",), runner=slow)
        assert entered.wait(5)
        second = executor.submit(update, scheduled, sensors=("git",), runner=slow)
        # Once the second cycle asks for the brain, an unserialized one would reach its program well within this.
        assert queued.wait(5)
        time.sleep(0.3)
        release.set()
        reports: list[Any] = [first.result(timeout=10), second.result(timeout=10)]
    # The second cycle ran its program only after the first one finished.
    assert released == [True]
    assert [report["ok"] for report in reports] == [True, True]
    assert [report["sensors"][0]["status"] for report in reports] == ["collected", "collected"]


def test_cli_selection_and_preview_do_not_execute(scheduled: Store, tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["update", "--brain", str(scheduled.root), "--sensor", "mail", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["sensors"]) == 1
    result = CliRunner().invoke(
        app,
        [
            "schedule",
            "--brain",
            str(scheduled.root),
            "--backend",
            "cron",
            "--sensor",
            "git",
            "--output",
            str(tmp_path / "schedules"),
            "--executable",
            sys.executable,
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["written"]) == 1
    result = CliRunner().invoke(app, ["schedule", "--backend", "invalid"])
    assert result.exit_code == 2


def test_systemd_quotes_arguments_and_captures_only_safe_environment(
    scheduled: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "bf space%$"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", "/usr/bin:/path with space/$dollars%:relative:")
    monkeypatch.setenv("PRIVATE_API_KEY", "never-copy-this")
    result: dict = generate(scheduled, backend="systemd", sensors=("mail",), executable=executable)
    files = cast("dict[str, str]", result["files"])
    service = next(value for name, value in files.items() if name.endswith(".service"))
    timer = next(value for name, value in files.items() if name.endswith(".timer"))
    assert '"--sensor" "mail"' in service
    assert 'space%%$"' in service
    assert "PATH=/usr/bin:/path with space/$dollars%%" in service
    assert "relative" not in service
    assert "never-copy-this" not in service
    assert "OnCalendar=*-*-* *:00,15,30,45:00" in timer
    # Per-program timeouts bound each run; a total limit would kill long legitimate cycles.
    assert "TimeoutStartSec" not in service
    # A stop leaves time to roll back an interrupted record commit before systemd kills the cycle.
    assert "TimeoutStopSec=60s" in service
    assert "Persistent=true" in timer
    assert result["written"] == []
    # The install commands copy files a preview did not write: say so before anyone runs them, and copy them from
    # where the rerun writes them, outside the brain, since they hold this machine's paths.
    suggested = Path.home() / ".config/bf-schedules"
    preview = (
        "Preview only: no file was written; rerun with --output ~/.config/bf-schedules before the install commands."
    )
    assert preview in result["warnings"]
    units = str(Path.home() / ".config/systemd/user")
    assert result["install"][1] == ["cp", "-i", *(str(suggested / name) for name in files), units]
    assert result["remove"][0][:4] == ["systemctl", "--user", "disable", "--now"]


def test_launchd_preserves_literal_arguments_and_calendar_catchup(scheduled: Store) -> None:
    result: dict = generate(
        scheduled, backend="launchd", every=30, routines=("review",), executable=Path(sys.executable)
    )
    content = next(iter(cast("dict[str, str]", result["files"]).values()))
    plist = plistlib.loads(content.encode())
    assert plist["ProgramArguments"][-2:] == ["--routine", "review"]
    assert plist["WorkingDirectory"] == str(scheduled.root)
    assert plist["StartCalendarInterval"] == [{"Minute": 0}, {"Minute": 30}]
    assert "KeepAlive" not in plist
    assert "RunAtLoad" not in plist
    assert plist["ExitTimeOut"] == 60
    assert result["install"][-1][0:2] == ["launchctl", "bootstrap"]
    (filename,) = result["files"]
    copied = str(Path.home() / ".config/bf-schedules" / filename)
    assert result["install"][1:3] == [
        ["plutil", "-lint", copied],
        ["cp", "-i", copied, str(Path.home() / "Library/LaunchAgents" / filename)],
    ]


def test_cron_keeps_existing_crontab_and_escapes_percent(scheduled: Store, tmp_path: Path) -> None:
    executable = tmp_path / "bf % weird'"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    result: dict = generate(scheduled, backend="cron", executable=executable, every=60)
    line = next(iter(result["files"].values())).splitlines()[-1]
    assert line.startswith("0 * * * * ")
    assert "\\%" in line
    command = shlex.split(line.split(" ", 5)[5].replace("\\%", "%"))
    assert str(executable) in command
    assert result["install"] == [["crontab", "-e"]]
    assert result["remove"] == [["crontab", "-e"]]


def test_generated_files_are_idempotent_but_never_overwrite_edits(scheduled: Store, tmp_path: Path) -> None:
    for folder in (scheduled.root / "settings/schedules", tmp_path / "schedules"):
        first: dict = generate(scheduled, backend="systemd", executable=Path(sys.executable), output=folder)
        assert first == generate(scheduled, backend="systemd", executable=Path(sys.executable), output=folder)
        target = Path(first["written"][0])
        target.write_text("user edit")
        with pytest.raises(Error, match="already exists"):
            generate(scheduled, backend="systemd", executable=Path(sys.executable), output=folder)
        assert target.read_text() == "user edit"
    symlink = tmp_path / "link"
    symlink.symlink_to(tmp_path / "schedules", target_is_directory=True)
    with pytest.raises(Error, match="symlink"):
        generate(scheduled, executable=Path(sys.executable), output=symlink)


def test_relative_output_resolves_against_the_brain(
    scheduled: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Run from elsewhere, as an agent working in another repository would.
    monkeypatch.chdir(tmp_path)
    result: dict = generate(
        scheduled, backend="cron", executable=Path(sys.executable), output=Path("settings/schedules")
    )
    assert [Path(path).parent for path in result["written"]] == [scheduled.root / "settings/schedules"]
    assert not (tmp_path / "settings").exists()
    outside: dict = generate(scheduled, backend="cron", executable=Path(sys.executable), output=Path("../jobs"))
    assert Path(outside["written"][0]).parent == scheduled.root.parent / "jobs"


def test_a_check_interval_reaching_a_refresh_warns(scheduled: Store) -> None:
    def warned(**kwargs: Any) -> list[str]:
        result: dict = generate(scheduled, backend="cron", executable=Path(sys.executable), **kwargs)
        return [warning for warning in result["warnings"] if "refreshes every" in warning]

    # Every program is selected by default; git has the shortest refresh: a 15-minute check runs it late.
    late = (
        "git refreshes every 300s, but the timer checks every 15 minutes: it will run late and status can report it "
        "overdue. Choose an --every shorter than the shortest refresh."
    )
    assert warned() == [late]
    # Equal intervals skip about every other check: scheduler jitter lands it just before the program is due.
    assert warned(every=5)
    assert not warned(every=4)
    assert not warned(sensors=("mail",))
    assert warned(sensors=("mail",), every=60)


def test_an_output_folder_holding_the_state_directory_is_not_a_brain(scheduled: Store) -> None:
    # The default state directory lies below home: before, --output ~ failed as if home were a brain holding it.
    result: dict = generate(scheduled, backend="cron", executable=Path(sys.executable), output=Path("~"))
    assert [Path(path).parent for path in result["written"]] == [Path.home()]
    # Generation took the brain's own lock: no lock follows the output folder.
    locks = Path(os.environ["XDG_STATE_HOME"]) / "bf/locks"
    assert not (locks / f"{Store(Path.home()).identity}.lock").exists()
    assert (locks / f"{scheduled.identity}.lock").exists()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"every": 7}, "divide an hour"),
        ({"name": "../bad"}, "schedule name"),
        ({"sensors": ("unknown",)}, "unknown sensor"),
        ({"sensors": ("manual",)}, "no enabled"),
        ({"sensors": ("disabled",)}, "no enabled"),
        ({"executable": Path("/missing/bf")}, "executable is unavailable"),
    ],
)
def test_invalid_schedules_fail_visibly(scheduled: Store, kwargs: dict, match: str) -> None:
    with pytest.raises(Error, match=match):
        generate(scheduled, **kwargs)


def test_schedule_environment_validation_and_backend_detection(
    scheduled: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Like every command, generation ignores a relative value and expands ~, so the job uses the same state.
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    monkeypatch.setenv("XDG_STATE_HOME", "~/state")
    files = cast("dict[str, str]", generate(scheduled, backend="cron", executable=Path(sys.executable))["files"])
    job = next(iter(files.values()))
    assert "XDG_CONFIG_HOME" not in job
    assert f"XDG_STATE_HOME={Path.home() / 'state'}" in job
    monkeypatch.setenv("XDG_CONFIG_HOME", str(scheduled.root / "invalid\npath"))
    with pytest.raises(Error, match="control characters"):
        generate(scheduled)
    monkeypatch.setattr("bf.schedule.sys.platform", "darwin")
    assert backend_name("auto") == "launchd"
    monkeypatch.setattr("bf.schedule.sys.platform", "linux")
    monkeypatch.setattr("bf.schedule.shutil.which", lambda _: "/bin/systemctl")
    assert backend_name("auto") == "systemd"
    monkeypatch.setattr("bf.schedule.shutil.which", lambda _: None)
    assert backend_name("auto") == "cron"


def test_native_systemd_accepts_generated_files(scheduled: Store, tmp_path: Path) -> None:
    import shutil
    import subprocess

    validator = shutil.which("systemd-analyze")
    if validator is None:
        pytest.skip("systemd native validation is available only with systemd tools")
    result: dict = generate(scheduled, backend="systemd", executable=Path(sys.executable), output=tmp_path / "units")
    checked = subprocess.run(  # noqa: S603 - validate synthetic units without activating them
        # Check the units themselves: man pages and generators vary with the runner image.
        [validator, "--user", "--man=no", "--generators=no", "verify", *result["written"]],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


def test_backend_unrepresentable_paths_fail_before_writing(scheduled: Store, tmp_path: Path) -> None:
    for backend, filename, message in (
        ("systemd", 'bf"', "cannot contain quotes"),
        ("cron", "bf\\%", "cannot safely represent"),
    ):
        executable = tmp_path / filename
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        with pytest.raises(Error, match=message):
            generate(scheduled, backend=backend, executable=executable)
