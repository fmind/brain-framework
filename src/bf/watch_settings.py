"""Brain-owned watch preferences and quiet, content-free desktop notifications."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from pydantic import ValidationError

from bf.config import load
from bf.models import Error, WatchSettings, explain
from bf.runtime import environment
from bf.storage import Store


def settings(store: Store, **overrides: object) -> WatchSettings:
    """CLI values override bf.yaml's watch section, then defaults; reread on restart."""
    # Load the whole configuration first so CLI overrides cannot hide invalid saved values.
    result = load(store).watch
    try:
        return WatchSettings.model_validate(result.model_dump() | {k: v for k, v in overrides.items() if v is not None})
    except ValidationError as error:
        raise Error("invalid watch overrides: " + explain(error)) from error


_DEVNULL = subprocess.DEVNULL
_GDBUS = ("call", "--session", "--dest=org.freedesktop.Notifications", "--object-path=/org/freedesktop/Notifications")
_GDBUS_M = ("--method=org.freedesktop.Notifications.Notify", "Brain Framework", "uint32 0", "")
_APPLE = "display notification (item 2 of argv) with title (item 1 of argv)"


def _argv(title: str, body: str, path: str | None) -> tuple[str | None, list[str]]:
    if sys.platform == "darwin":
        exe = shutil.which("osascript", path=path)
        return exe, [exe or "osascript", "-e", "on run argv", "-e", _APPLE, "-e", "end run", title, body]
    if exe := shutil.which("notify-send", path=path):
        return exe, [exe, "--app-name=Brain Framework", "--", title, body]
    exe = shutil.which("gdbus", path=path)
    return exe, [exe or "gdbus", *_GDBUS, *_GDBUS_M, title, body, "@as []", "@a{sv} {}", "int32 5000"]


def desktop(title: str, body: str) -> bool:
    """Use native local notifications, fixed scripts and literal argv; never a shell."""
    env = environment()
    exe, argv = _argv(title, body, env.get("PATH"))
    if not exe:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 - fixed native tool, no shell or provider content
            argv, check=False, stdin=_DEVNULL, stdout=_DEVNULL, stderr=_DEVNULL, env=env, timeout=5
        )
        return proc.returncode == 0
    except OSError, subprocess.TimeoutExpired:
        return False


@dataclass
class Notifications:
    preferences: WatchSettings
    failure: tuple[str, ...] = ()
    last_sent: float = float("-inf")
    warned: bool = False

    def completed(self, *, failures: tuple[str, ...], worked: bool, now: float) -> str:
        """Notify on failure transitions/recovery or successful work, never idle cycles."""
        if (mode := self.preferences.notifications) == "off":
            return ""
        event = ""
        if failures and failures != self.failure and mode in {"failure", "all"}:
            event = "failed"
        elif self.failure and not failures:
            event = "recovered"
        elif worked and mode in {"success", "all"}:
            event = "completed"
        if not event or now - self.last_sent < self.preferences.notification_cooldown:
            if not event:
                self.failure = failures
            return ""
        self.last_sent, self.failure = now, failures
        if desktop(f"Brain collection {event}", "Open bf watch to inspect local program status.") or self.warned:
            return ""
        self.warned = True
        return "Desktop alerts unavailable; collection continues (set notifications: off)"
