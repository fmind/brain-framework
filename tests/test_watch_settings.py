"""Watch preferences, notification transitions and native delivery failures."""

from __future__ import annotations

import re
import subprocess
from contextlib import nullcontext
from typing import Literal

import pytest
from typer.testing import CliRunner

from bf.cli import app
from bf.models import Error, WatchSettings
from bf.storage import Store, collecting
from bf.watch import Job, watch
from bf.watch_settings import Notifications, desktop, settings


def write_watch(brain: Store, data: bytes) -> None:
    brain.write(
        "bf.yaml",
        b"version: 7\nname: fixture\nwatch:\n" + b"\n".join(b"  " + line for line in data.splitlines()) + b"\n",
    )


def test_settings_defaults_precedence_and_file_validation(brain: Store) -> None:
    assert settings(brain) == WatchSettings()
    write_watch(brain, b"interval: 300\npoll_interval: 1\nnotifications: all\nnotification_cooldown: 60\n")
    result = settings(brain, interval=10, notifications="off")
    assert (result.interval, result.poll_interval, result.notifications, result.notification_cooldown) == (
        10,
        1,
        "off",
        60,
    )
    # YAML 1.2 reads an unquoted off as the string it looks like.
    write_watch(brain, b"notifications: off\n")
    assert settings(brain).notifications == "off"
    write_watch(brain, b"interval: 0\n")
    with pytest.raises(Error, match=r"bf\.yaml.*watch.interval"):
        settings(brain, interval=60)


@pytest.mark.parametrize(
    "data",
    [
        b"notifications: false",
        b"notifications: email",
        b"interval: '60'",
        b"poll_interval: .nan",
        b"notification_cooldown: -1",
        b"interval: 60\ninterval: 30",
        b"- bad",
    ],
)
def test_invalid_settings_fail_closed(brain: Store, data: bytes) -> None:
    write_watch(brain, data)
    with pytest.raises(Error, match=r"bf\.yaml") as caught:
        settings(brain)
    # Values are never quoted.
    assert not {"email", "bad", "30"} & set(re.findall(r"\w+", str(caught.value)))


def test_unknown_settings_keys_are_named_like_bf_yaml_keys(brain: Store) -> None:
    # The brain's owner writes this file: naming a typo, never its value, points at the line to fix.
    write_watch(brain, b"intervall: private-value\n")
    with pytest.raises(Error) as caught:
        settings(brain)
    assert str(caught.value) == "invalid bf.yaml: watch.intervall: Extra inputs are not permitted"


def test_settings_reject_redirected_files(brain: Store, tmp_path) -> None:
    target = tmp_path / "outside.yaml"
    target.write_text("interval: 5\n")
    (brain.root / "bf.yaml").unlink()
    (brain.root / "bf.yaml").symlink_to(target)
    with pytest.raises(Error, match="symlink"):
        settings(brain)


def test_invalid_override_and_cached_preferences(brain: Store) -> None:
    write_watch(brain, b"interval: 300\n")
    assert settings(brain, interval=10).interval == 10
    assert settings(brain).interval == 300
    with pytest.raises(Error, match="invalid watch overrides: interval"):
        settings(brain, interval=0)


def test_watch_validates_before_starting_even_with_cli_override(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    write_watch(brain, b"interval: 0\n")
    monkeypatch.setattr(Job, "start", lambda _: pytest.fail("invalid configuration executed"))
    with pytest.raises(Error, match=r"bf\.yaml.*watch.interval"):
        watch(brain, interval=60, json_output=True)


def test_notifications_deduplicate_failures_defer_recovery_and_ignore_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []
    monkeypatch.setattr("bf.watch_settings.desktop", lambda *args: sent.append(args) or True)
    notices = Notifications(WatchSettings())
    for now in (0, 60, 300):
        assert not notices.completed(failures=("private-error",), worked=False, now=now)
    assert len(sent) == 1
    notices.completed(failures=(), worked=False, now=310)
    notices.completed(failures=(), worked=False, now=610)
    assert [title for title, _ in sent] == ["Brain collection failed", "Brain collection recovered"]
    assert "private-error" not in str(sent)
    notices.completed(failures=("new-failure",), worked=False, now=620)
    assert len(sent) == 3
    notices.completed(failures=(), worked=True, now=630)
    assert len(sent) == 3
    notices.completed(failures=(), worked=False, now=920)
    assert len(sent) == 4


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("off", []),
        ("failure", ["failed", "recovered"]),
        ("success", ["completed", "recovered"]),
        ("all", ["completed", "failed", "recovered"]),
    ],
)
def test_notification_modes(
    monkeypatch: pytest.MonkeyPatch, mode: Literal["off", "failure", "success", "all"], expected: list[str]
) -> None:
    sent = []
    monkeypatch.setattr("bf.watch_settings.desktop", lambda title, _: sent.append(title.split()[-1]) or True)
    notices = Notifications(WatchSettings(notifications=mode, notification_cooldown=0))
    for now, (failures, worked) in enumerate(
        [((), False), ((), True), (("failed",), False), (("failed",), False), ((), True)]
    ):
        notices.completed(failures=failures, worked=worked, now=now)
    assert sent == expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("off", []),
        ("failure", ["failed"]),
        ("success", ["completed", "completed", "completed"]),
        ("all", ["failed", "completed", "completed"]),
    ],
)
def test_work_alerts_while_another_program_keeps_failing(
    monkeypatch: pytest.MonkeyPatch, mode: Literal["off", "failure", "success", "all"], expected: list[str]
) -> None:
    sent = []
    monkeypatch.setattr("bf.watch_settings.desktop", lambda title, _: sent.append(title.split()[-1]) or True)
    notices = Notifications(WatchSettings(notifications=mode, notification_cooldown=0))
    # One source stays failed, waiting out its retry backoff, while the others keep collecting.
    for now, worked in enumerate([True, True, False, True]):
        notices.completed(failures=("sensor:broken:program exited with status 1",), worked=worked, now=now)
    assert sent == expected


def test_unavailable_desktop_warns_once_and_does_not_stop_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bf.watch_settings.desktop", lambda *_: False)
    notices = Notifications(WatchSettings(notification_cooldown=0))
    assert "unavailable" in notices.completed(failures=("failed",), worked=False, now=0)
    assert not notices.completed(failures=(), worked=True, now=10)


@pytest.mark.parametrize(("platform", "tool"), [("darwin", "osascript"), ("linux", "notify-send"), ("linux", "gdbus")])
def test_native_delivery_uses_literal_bounded_argv(monkeypatch: pytest.MonkeyPatch, platform: str, tool: str) -> None:
    calls = []
    monkeypatch.setattr("bf.watch_settings.sys.platform", platform)
    monkeypatch.setattr("bf.watch_settings.shutil.which", lambda name: f"/usr/bin/{name}" if name == tool else None)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("bf.watch_settings.subprocess.run", run)
    assert desktop("Literal title", "Literal body")
    argv, kwargs = calls[0]
    assert argv[0] == f"/usr/bin/{tool}"
    assert "Literal title" in argv
    assert "Literal body" in argv
    assert kwargs["timeout"] == 5
    assert not kwargs.get("shell")
    if tool == "gdbus":
        assert "uint32 0" in argv
        assert "@as []" in argv
        assert "@a{sv} {}" in argv


@pytest.mark.parametrize("failure", [OSError(), subprocess.TimeoutExpired("tool", 5)])
def test_native_delivery_errors_are_nonfatal(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    monkeypatch.setattr("bf.watch_settings.shutil.which", lambda _: "/tool")

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("bf.watch_settings.subprocess.run", fail)
    assert not desktop("title", "body")
    monkeypatch.setattr("bf.watch_settings.shutil.which", lambda _: None)
    assert not desktop("title", "body")


def test_second_watcher_observes_without_running(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    write_watch(brain, b"poll_interval: 1\n")
    observed = []
    monkeypatch.setattr("bf.watch.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("bf.watch.sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr("bf.watch.keyboard", lambda _: nullcontext(-1))
    monkeypatch.setattr(
        "bf.watch._loop",
        lambda _store, dashboard, *_args, **_kwargs: observed.append(
            (dashboard.observe, dashboard.preferences.poll_interval)
        ),
    )
    monkeypatch.setattr(Job, "start", lambda _: pytest.fail("second watcher executed"))
    with collecting(brain, ".watch"):
        watch(brain)
    assert observed == [(True, 1)]


def test_noninteractive_watcher_fails_when_busy_for_supervisor_retry(brain: Store) -> None:
    with collecting(brain, ".watch"), pytest.raises(Error, match="another watcher"):
        watch(brain, json_output=True)


@pytest.mark.parametrize("command", [["watch", "--json", "--interval", "10", "--notify", "off"], ["watch", "--json"]])
def test_cli_uses_integrated_preferences(brain: Store, monkeypatch: pytest.MonkeyPatch, command: list[str]) -> None:
    write_watch(brain, b"interval: 300\npoll_interval: 1\nnotifications: all\nnotification_cooldown: 60\n")
    seen = []
    monkeypatch.setattr("bf.watch.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("bf.watch.sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr("bf.watch.keyboard", lambda _: nullcontext(-1))
    monkeypatch.setattr("bf.watch._loop", lambda _store, dashboard, *_args, **_kwargs: seen.append(dashboard))
    result = CliRunner().invoke(app, [*command, "--brain", str(brain.root)])
    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    expected = WatchSettings(interval=300, poll_interval=1, notifications="all", notification_cooldown=60)
    if "--interval" in command:
        expected = expected.model_copy(update={"interval": 10, "notifications": "off"})
    assert seen[0].preferences == expected
    assert not seen[0].observe


def test_validate_reports_invalid_watch_settings(brain: Store) -> None:
    write_watch(brain, b"interval: 0\n")
    result = CliRunner().invoke(app, ["validate", "--brain", str(brain.root)])
    assert result.exit_code == 1
    assert isinstance(result.exception, Error)
    assert "watch.interval" in str(result.exception)
