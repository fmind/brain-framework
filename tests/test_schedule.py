"""Generated native files preserve selection, arguments and user-owned configuration."""

from __future__ import annotations

import json
import plistlib
import shlex
import sys
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from bf.cli import app
from bf.models import Error
from bf.schedule import backend_name, generate
from bf.storage import Store, collecting
from bf.update import update

CONFIG = b"""version: 6
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
    preview: dict = update([scheduled], sensors=("mail", "manual", "disabled"), dry_run=True)
    assert [s["sensor"] for s in preview["brains"][0]["sensors"]] == ["mail"]
    assert "routines" not in preview["brains"][0]
    preview: dict = update([scheduled], routines=("review",), dry_run=True)
    assert preview["brains"][0]["sensors"] == []
    assert [r["routine"] for r in preview["brains"][0]["routines"]] == ["review"]
    executed = []

    def fake(*args: object) -> bytes:
        executed.append(args)
        return b'[{"id":"one","title":"One"}]'

    assert update([scheduled], sensors=("mail",), runner=fake)["ok"]
    assert len(executed) == 1
    assert update([scheduled], sensors=("mail",), runner=fake)["ok"]
    assert len(executed) == 1


def test_invalid_selection_is_rejected_before_any_execution(scheduled: Store, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    second = Store(other)
    second.write("bf.yaml", b"version: 6\nname: other\n")
    calls = []
    with pytest.raises(Error, match="unknown sensor"):
        update([scheduled, second], sensors=("mail",), runner=lambda *args: (calls.append(args), b"[]")[1])
    assert not calls
    with collecting(scheduled, ".update"):
        result: dict = update([scheduled])
    assert not result["ok"]
    assert "another update" in result["brains"][0]["error"]


def test_cli_selection_and_preview_do_not_execute(scheduled: Store, tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["update", "--brain", str(scheduled.root), "--sensor", "mail", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["brains"][0]["sensors"]) == 1
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
    assert "TimeoutStartSec=45min" in service
    assert "Persistent=true" in timer
    assert result["written"] == []
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
    assert result["install"][-1][0:2] == ["launchctl", "bootstrap"]


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
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    with pytest.raises(Error, match="must be absolute"):
        generate(scheduled)
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
        [validator, "--user", "verify", *result["written"]],
        capture_output=True,
        text=True,
        timeout=10,
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
