"""Generate inspectable native scheduler files; activation remains an explicit operating-system action."""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Literal

from bf.config import load
from bf.history import environment
from bf.models import NAME, Error, digest
from bf.storage import Store, writer, xdg_setting
from bf.update import selection

Backend = Literal["auto", "systemd", "launchd", "cron"]


def _literal(value: str) -> str:
    if any(not char.isprintable() for char in value):
        raise Error("schedule paths and environment values must not contain control characters")
    return value


def _unit(value: str, *, expand: bool = False) -> str:
    """systemd quoting is not shell quoting; escape specifiers and environment expansion too."""
    escaped = _literal(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (escaped.replace("$", "$$") if expand else escaped) + '"'


def backend_name(backend: Backend) -> Backend:
    if backend != "auto":
        return backend
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform == "linux" and shutil.which("systemctl"):
        return "systemd"
    return "cron"


def generate(
    store: Store,
    *,
    backend: Backend = "auto",
    every: int = 15,
    name: str = "update",
    sensors: tuple[str, ...] = (),
    routines: tuple[str, ...] = (),
    executable: Path | None = None,
    output: Path | None = None,
) -> dict[str, object]:
    """Preview by default; --output saves new definitions without replacing local edits or enabling jobs."""
    if every not in {1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60}:
        raise Error("schedule interval must divide an hour: 1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30 or 60 minutes")
    if not re.fullmatch(NAME, name):
        raise Error(
            "schedule name must start with a lowercase letter and contain only lowercase letters, digits or hyphens"
        )
    config = load(store)
    selected_sensors, selected_routines = selection(config, sensors, routines)
    selected = [config.sensors[item] for item in selected_sensors] + [
        config.routines[item] for item in selected_routines
    ]
    if not any(program.enabled and program.refresh for program in selected):
        raise Error("selection contains no enabled scheduled programs; configure a nonzero refresh first")
    executable = executable or Path(sys.executable).parent / "bf"
    executable = executable.expanduser().absolute()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise Error("bf executable is unavailable; pass --executable with the absolute installed bf path")
    argv = [str(executable), "update", "--brain", str(store.root)]
    for flag, values in (("--sensor", sensors), ("--routine", routines)):
        for value in dict.fromkeys(values):
            argv.extend((flag, value))
    for value in argv:
        _literal(value)
    backend = backend_name(backend)
    if backend == "systemd" and any(char in str(executable) for char in "\\\"'"):
        raise Error(
            "systemd executable path cannot contain quotes or backslashes; use an installation at a simpler path"
        )
    label = f"bf-{config.name}-{digest(os.fsencode(store.root))[:8]}-{name}"
    env = {"PATH": environment().get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        # Capture the locations every command uses: expanded, and without values the runtime ignores.
        if path := xdg_setting(key):
            env[key] = str(path)
    for value in env.values():
        _literal(value)
    minutes = list(range(0, 60, every))
    warnings = [
        "Generated files do not activate a schedule. Review programs and environment before installation.",
        "Jobs run only while the host and user environment are available; sleep, logout and VM shutdown can stop them.",
        "Use one owner for each selected program; avoid overlapping watch and native schedules.",
        "PATH and XDG locations are captured; credentials and other environment variables are not copied.",
    ]
    files: dict[str, str]
    install: list[list[str]]
    status: list[list[str]]
    remove: list[list[str]]
    # Like other brain-relative options, a relative output directory resolves against the brain root.
    source = Path(os.path.normpath(store.root / (output.expanduser() if output else "settings/schedules")))
    if backend == "systemd":
        service = label + ".service"
        timer = label + ".timer"
        files = {
            service: "# https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html\n"
            f"[Unit]\nDescription=Brain Framework {config.name} ({name})\n\n"
            "[Service]\nType=oneshot\n"
            + "".join(f"Environment={_unit(key + '=' + value)}\n" for key, value in env.items())
            + "ExecStart="
            + " ".join(_unit(value, expand=position > 0) for position, value in enumerate(argv))
            # A oneshot service has no start timeout by default; per-program BF timeouts bound each run.
            + "\nTimeoutStopSec=10s\n",
            timer: "# https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html\n"
            f"[Unit]\nDescription=Check due Brain Framework programs every {every} minutes\n\n"
            f"[Timer]\nOnCalendar=*-*-* *:{','.join(f'{minute:02d}' for minute in minutes)}:00\n"
            "Persistent=true\nRandomizedDelaySec=30s\n\n[Install]\nWantedBy=timers.target\n",
        }
        target = Path.home() / ".config/systemd/user"
        install = [
            ["mkdir", "-p", str(target)],
            ["cp", "-i", str(source / service), str(source / timer), str(target)],
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", timer],
        ]
        status = [
            ["systemctl", "--user", "list-timers", timer],
            ["systemctl", "--user", "show", service, "-p", "Result", "-p", "ExecMainStatus"],
            ["journalctl", "--user", "-u", service, "-n", "30", "--no-pager"],
        ]
        remove = [
            ["systemctl", "--user", "disable", "--now", timer],
            ["systemctl", "--user", "stop", service],
            ["rm", "-i", str(target / timer), str(target / service)],
            ["systemctl", "--user", "daemon-reload"],
        ]
        warnings.append(
            "Verify systemctl --user works. WSL and ChromeOS require a running Linux environment; generation does not verify its lifetime."
        )
    elif backend == "launchd":
        filename = label + ".plist"
        files = {
            filename: plistlib.dumps(
                {
                    "Label": label,
                    "ProgramArguments": argv,
                    "WorkingDirectory": str(store.root),
                    "EnvironmentVariables": env,
                    "StartCalendarInterval": [{"Minute": minute} for minute in minutes],
                    "ProcessType": "Background",
                },
                sort_keys=False,
            ).decode()
        }
        target = Path.home() / "Library/LaunchAgents" / filename
        domain = f"gui/{os.getuid()}"
        install = [
            ["mkdir", "-p", str(target.parent)],
            ["plutil", "-lint", str(source / filename)],
            ["cp", "-i", str(source / filename), str(target)],
            ["launchctl", "bootstrap", domain, str(target)],
        ]
        status = [["launchctl", "print", f"{domain}/{label}"]]
        remove = [["launchctl", "bootout", f"{domain}/{label}"], ["rm", "-i", str(target)]]
        warnings.append(
            "Calendar triggers coalesce after sleep. Power-off misses are not replayed. Per-program timeouts apply; launchd has no total-job timeout here."
        )
    else:
        if any("\\" in value and "%" in value for value in [*argv, *env.values()]):
            raise Error(
                "cron cannot safely represent combined backslashes and percent signs here; use systemd or launchd"
            )
        # cron interprets percent signs before the shell, including those inside shell quotes.
        command = shlex.join(["env", *(key + "=" + value for key, value in env.items()), *argv]).replace("%", "\\%")
        files = {
            label + ".cron": "# https://man7.org/linux/man-pages/man5/crontab.5.html\n"
            f"# Add this line to your existing user crontab; do not replace it with this file.\n"
            f"{','.join(str(minute) for minute in minutes)} * * * * {command}\n"
        }
        install = [["crontab", "-e"]]
        status = [["crontab", "-l"]]
        remove = [["crontab", "-e"]]
        warnings.append(
            "Cron skips missed runs. Add/remove only this job's line with crontab -e; crontab FILE replaces all existing jobs."
        )
    written: list[str] = []
    if output is None:
        warnings.append(
            "Preview only: no file was written; rerun with --output settings/schedules before the install commands."
        )
    else:
        if source.is_symlink():
            raise Error("schedule output directory may not be a symlink")
        # Brain-local definitions use the same confined writer as other brain files.
        if source.is_relative_to(store.root):
            destination = store
            prefix = source.relative_to(store.root).as_posix()
            prefix = "" if prefix == "." else prefix + "/"
        else:
            source.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination, prefix = Store(source), ""
        with writer(destination):
            for filename, content in files.items():
                try:
                    existing = destination.read(prefix + filename)
                except FileNotFoundError:
                    continue
                if existing != content.encode():
                    raise Error(
                        "schedule file already exists with different content; review it or choose another output directory"
                    )
            for filename, content in files.items():
                destination.write(prefix + filename, content.encode())
                written.append(str(source / filename))
    return {
        "backend": backend,
        "name": label,
        "brain": config.name,
        "argv": argv,
        "every_minutes": every,
        "files": files,
        "written": written,
        "install": install,
        "status": status,
        "remove": remove,
        "warnings": warnings,
    }
