"""Generate inspectable native scheduler files; activation remains an explicit operating-system action."""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bf.config import load
from bf.history import environment
from bf.models import NAME, Error, digest
from bf.storage import Store, expand, writer, xdg, xdg_setting
from bf.update import late, scheduled

Backend = Literal["auto", "systemd", "launchd", "cron"]
# The files hold this machine's paths: a preview suggests, and its commands copy from, a folder outside the brain.
DEFAULT_OUTPUT = Path("~/.config/bf-schedules")


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


@dataclass(frozen=True)
class _Job:
    """What every backend schedules: one `bf update` of one brain at fixed minutes of each hour."""

    label: str
    description: str
    argv: list[str]
    env: dict[str, str]
    every: int
    source: Path

    @property
    def minutes(self) -> list[int]:
        return list(range(0, 60, self.every))


@dataclass(frozen=True)
class _Plan:
    """A backend's files and the commands that install, inspect and remove them, with its own caveat."""

    files: dict[str, str]
    install: list[list[str]]
    status: list[list[str]]
    remove: list[list[str]]
    warning: str


def _systemd(job: _Job, _store: Store) -> _Plan:
    service, timer = job.label + ".service", job.label + ".timer"
    files = {
        service: "# https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html\n"
        f"[Unit]\nDescription={job.description}\n\n"
        "[Service]\nType=oneshot\n"
        + "".join(f"Environment={_unit(key + '=' + value)}\n" for key, value in job.env.items())
        + "ExecStart="
        + " ".join(_unit(value, expand=position > 0) for position, value in enumerate(job.argv))
        # A oneshot service has no start timeout by default; per-program BF timeouts bound each run. A stop
        # leaves time to roll back an interrupted record commit, as watch does.
        + "\nTimeoutStopSec=60s\n",
        timer: "# https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html\n"
        f"[Unit]\nDescription=Check due Brain Framework programs every {job.every} minutes\n\n"
        f"[Timer]\nOnCalendar=*-*-* *:{','.join(f'{minute:02d}' for minute in job.minutes)}:00\n"
        "Persistent=true\nRandomizedDelaySec=30s\n\n[Install]\nWantedBy=timers.target\n",
    }
    # The user manager reads units below XDG_CONFIG_HOME when the session sets it.
    target = xdg("XDG_CONFIG_HOME", ".config") / "systemd/user"
    return _Plan(
        files,
        install=[
            ["mkdir", "-p", str(target)],
            ["cp", "-i", str(job.source / service), str(job.source / timer), str(target)],
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", timer],
        ],
        status=[
            ["systemctl", "--user", "list-timers", timer],
            ["systemctl", "--user", "show", service, "-p", "Result", "-p", "ExecMainStatus"],
            ["journalctl", "--user", "-u", service, "-n", "30", "--no-pager"],
        ],
        remove=[
            ["systemctl", "--user", "disable", "--now", timer],
            ["systemctl", "--user", "stop", service],
            ["rm", "-i", str(target / timer), str(target / service)],
            ["systemctl", "--user", "daemon-reload"],
        ],
        warning="Verify systemctl --user works. WSL and ChromeOS require a running Linux environment; generation "
        "does not verify its lifetime.",
    )


def _launchd(job: _Job, store: Store) -> _Plan:
    filename = job.label + ".plist"
    plist = {
        "Label": job.label,
        "ProgramArguments": job.argv,
        "WorkingDirectory": str(store.root),
        "EnvironmentVariables": job.env,
        "StartCalendarInterval": [{"Minute": minute} for minute in job.minutes],
        "ProcessType": "Background",
        # Seconds between the stop request and SIGKILL: time to roll back an interrupted record commit.
        "ExitTimeOut": 60,
    }
    target = Path.home() / "Library/LaunchAgents" / filename
    domain = f"gui/{os.getuid()}"
    return _Plan(
        {filename: plistlib.dumps(plist, sort_keys=False).decode()},
        install=[
            ["mkdir", "-p", str(target.parent)],
            ["plutil", "-lint", str(job.source / filename)],
            ["cp", "-i", str(job.source / filename), str(target)],
            ["launchctl", "bootstrap", domain, str(target)],
        ],
        status=[["launchctl", "print", f"{domain}/{job.label}"]],
        remove=[["launchctl", "bootout", f"{domain}/{job.label}"], ["rm", "-i", str(target)]],
        warning="Calendar triggers coalesce after sleep. Power-off misses are not replayed. Per-program timeouts "
        "apply; launchd has no total-job timeout here.",
    )


def _cron(job: _Job, _store: Store) -> _Plan:
    if any("\\" in value and "%" in value for value in [*job.argv, *job.env.values()]):
        raise Error("cron cannot safely represent combined backslashes and percent signs here; use systemd or launchd")
    # cron interprets percent signs before the shell, including those inside shell quotes.
    command = shlex.join(["env", *(key + "=" + value for key, value in job.env.items()), *job.argv])
    line = f"{','.join(str(minute) for minute in job.minutes)} * * * * {command.replace('%', '\\%')}\n"
    return _Plan(
        {
            job.label + ".cron": "# https://man7.org/linux/man-pages/man5/crontab.5.html\n"
            "# Add this line to your existing user crontab; do not replace it with this file.\n" + line
        },
        install=[["crontab", "-e"]],
        status=[["crontab", "-l"]],
        remove=[["crontab", "-e"]],
        warning="Cron skips missed runs. Add/remove only this job's line with crontab -e; crontab FILE replaces all "
        "existing jobs.",
    )


def _save(store: Store, source: Path, files: dict[str, str]) -> list[str]:
    """Write new definitions, never replacing a local edit; the paths written."""
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
    # File names embed the brain: its own lock serializes generation, wherever the files go. An outside folder
    # is no brain, and may even hold the state directory where locks live.
    with writer(store, wait=30):
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
    return [str(source / filename) for filename in files]


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
    if not scheduled(config, sensors, routines):
        raise Error("selection contains no enabled scheduled programs; configure a nonzero refresh first")
    executable = executable or Path(sys.executable).parent / "bf"
    executable = expand(executable).absolute()
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
    warnings = [
        "Generated files do not activate a schedule. Review programs and environment before installation.",
        "Jobs run only while the host and user environment are available; sleep, logout and VM shutdown can stop them.",
        "Use one owner for each selected program; avoid overlapping watch and native schedules.",
        "PATH and XDG locations are captured; credentials and other environment variables are not copied.",
    ]
    # A slower check can be deliberate: warn, since a program is due only once its refresh has passed.
    if reason := late(config, every * 60, sensors, routines):
        warnings.append(f"{reason}. Choose an --every shorter than the shortest refresh.")
    # Like other brain-relative options, a relative output directory resolves against the brain root.
    source = Path(os.path.normpath(store.root / expand(output or DEFAULT_OUTPUT)))
    job = _Job(label, f"Brain Framework {config.name} ({name})", argv, env, every, source)
    plan = {"systemd": _systemd, "launchd": _launchd, "cron": _cron}[backend](job, store)
    warnings.append(plan.warning)
    written: list[str] = []
    if output is None:
        warnings.append(
            f"Preview only: no file was written; rerun with --output {DEFAULT_OUTPUT} before the install commands."
        )
    else:
        written = _save(store, source, plan.files)
    files, install, status, remove = plan.files, plan.install, plan.status, plan.remove
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
