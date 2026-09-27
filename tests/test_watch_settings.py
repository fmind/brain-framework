"""Watch preferences, notification transitions and native delivery failures."""

from __future__ import annotations

import subprocess
from contextlib import nullcontext

import pytest

from bf.models import Error
from bf.storage import Store, collecting
from bf.watch import Job, watch
from bf.watch_settings import Notifications, Settings, desktop, settings


def test_settings_defaults_precedence_and_file_validation(brain: Store) -> None:
    assert settings(brain) == Settings()
    brain.write(
        "settings/watch.yaml", b"interval: 300\npoll_interval: 1\nnotifications: all\nnotification_cooldown: 60\n"
    )
    result = settings(brain, interval=10, notifications="off")
    assert (result.interval, result.poll_interval, result.notifications, result.notification_cooldown) == (
        10,
        1,
        "off",
        60,
    )
    brain.write("settings/watch.yaml", b"interval: 0\n")
    with pytest.raises(Error, match=r"settings/watch\.yaml"):
        settings(brain, interval=60)


@pytest.mark.parametrize(
    "data",
    [
        b"notifications: false",
        b"notifications: email",
        b"interval: '60'",
        b"poll_interval: .nan",
        b"notification_cooldown: -1",
        b"secret-value: bad",
        b"interval: 60\ninterval: 30",
        b"- bad",
    ],
)
def test_invalid_settings_fail_closed(brain: Store, data: bytes) -> None:
    brain.write("settings/watch.yaml", data)
    with pytest.raises(Error, match=r"watch\.yaml") as caught:
        settings(brain)
    assert "secret-value" not in str(caught.value)


def test_settings_reject_redirected_files(brain: Store, tmp_path) -> None:
    target = tmp_path / "outside.yaml"
    target.write_text("interval: 5\n")
    (brain.root / "settings").mkdir()
    (brain.root / "settings/watch.yaml").symlink_to(target)
    with pytest.raises(Error, match="symlink"):
        settings(brain)


def test_notifications_deduplicate_failures_defer_recovery_and_ignore_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []
    monkeypatch.setattr("bf.watch_settings.desktop", lambda *args: sent.append(args) or True)
    notices = Notifications(Settings())
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
def test_notification_modes(monkeypatch: pytest.MonkeyPatch, mode: str, expected: list[str]) -> None:
    sent = []
    monkeypatch.setattr("bf.watch_settings.desktop", lambda title, _: sent.append(title.split()[-1]) or True)
    notices = Notifications(Settings(notifications=mode, notification_cooldown=0))
    for now, (failures, worked) in enumerate(
        [((), False), ((), True), (("failed",), False), (("failed",), False), ((), True)]
    ):
        notices.completed(failures=failures, worked=worked, now=now)
    assert sent == expected


def test_unavailable_desktop_warns_once_and_does_not_stop_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bf.watch_settings.desktop", lambda *_: False)
    notices = Notifications(Settings(notification_cooldown=0))
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
    observed = []
    monkeypatch.setattr("bf.watch.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("bf.watch.sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr("bf.watch.keyboard", lambda _: nullcontext(-1))
    monkeypatch.setattr(
        "bf.watch._loop", lambda _store, dashboard, *_args, **_kwargs: observed.append(dashboard.observe)
    )
    monkeypatch.setattr(Job, "start", lambda _: pytest.fail("second watcher executed"))
    with collecting(brain, ".watch"):
        watch(brain)
    assert observed == [True]


def test_noninteractive_watcher_fails_when_busy_for_supervisor_retry(brain: Store) -> None:
    with collecting(brain, ".watch"), pytest.raises(Error, match="another watcher"):
        watch(brain, json_output=True)
