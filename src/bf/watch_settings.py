"""Brain-owned watch preferences and quiet, content-free desktop notifications."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, ValidationError

from bf.config import yaml_object
from bf.history import environment
from bf.models import Error, Model, explain
from bf.storage import Store


class Settings(Model):
    """Optional watch preferences; CLI values override the file, then defaults apply. Restart to reload."""

    interval: int = Field(default=60, ge=5, le=86400, description="Seconds between checks for due programs.")
    poll_interval: float = Field(
        default=2, ge=0.2, le=60, description="Seconds between local history reads and JSON snapshots."
    )
    notifications: Literal["off", "failure", "success", "all"] = Field(
        default="failure",
        description="Desktop alert policy. Recovery alerts accompany enabled modes.",
    )
    notification_cooldown: int = Field(
        default=300, ge=0, le=86400, description="Minimum seconds between notification attempts."
    )


def settings(store: Store, **overrides: object) -> Settings:
    """CLI values override settings/watch.yaml, then defaults; reread on restart."""
    name = "settings/watch.yaml"
    try:
        values = yaml_object(store.read(name, 65536), name)
    except FileNotFoundError:
        values = {}
    try:
        # Validate the file even when a CLI override would hide an invalid value.
        result = Settings.model_validate(values)
        return Settings.model_validate(result.model_dump() | {k: v for k, v in overrides.items() if v is not None})
    except ValidationError as error:
        # The brain's owner writes this file, like bf.yaml: name each invalid key so it can be fixed.
        raise Error(f"invalid {name}: " + explain(error)) from error


def desktop(title: str, body: str) -> bool:
    """Use native local notifications, fixed scripts and literal argv; never a shell."""
    if sys.platform == "darwin":
        executable = shutil.which("osascript")
        argv = [
            executable or "osascript",
            "-e",
            "on run argv",
            "-e",
            "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e",
            "end run",
            title,
            body,
        ]
    elif executable := shutil.which("notify-send"):
        argv = [executable, "--app-name=Brain Framework", "--", title, body]
    else:
        executable = shutil.which("gdbus")
        argv = [
            executable or "gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.Notifications",
            "--object-path",
            "/org/freedesktop/Notifications",
            "--method",
            "org.freedesktop.Notifications.Notify",
            "Brain Framework",
            "uint32 0",
            "",
            title,
            body,
            "@as []",
            "@a{sv} {}",
            "int32 5000",
        ]
    if not executable:
        return False
    try:
        return (
            subprocess.run(  # noqa: S603 - fixed native tool, no shell or provider content
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment(),
                timeout=5,
                check=False,
            ).returncode
            == 0
        )
    except OSError, subprocess.TimeoutExpired:
        return False


@dataclass
class Notifications:
    preferences: Settings
    failure: tuple[str, ...] = ()
    last_sent: float = float("-inf")
    warned: bool = False

    def completed(self, *, failures: tuple[str, ...], worked: bool, now: float) -> str:
        """Notify on failure transitions/recovery or successful work, never idle cycles."""
        mode = self.preferences.notifications
        if mode == "off":
            return ""
        recovery = bool(self.failure) and not failures
        changed_failure = bool(failures) and failures != self.failure
        event = (
            "failed"
            if changed_failure and mode in {"failure", "all"}
            else "recovered"
            if recovery
            else "completed"
            if not failures and worked and mode in {"success", "all"}
            else ""
        )
        if not event:
            self.failure = failures
            return ""
        if now - self.last_sent < self.preferences.notification_cooldown:
            return ""
        # Remember attempts too: an unavailable desktop must not cause repeated subprocesses or warnings.
        self.last_sent = now
        self.failure = failures
        if desktop(f"Brain collection {event}", "Open bf watch to inspect local program status."):
            return ""
        if not self.warned:
            self.warned = True
            # One screen line (80 columns): the dashboard truncates longer messages.
            return "Desktop alerts unavailable; collection continues (set notifications: off)"
        return ""
