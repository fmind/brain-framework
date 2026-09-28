"""Private run history of sensors and routines, and the environment programs inherit.

Status, retrieval and dashboards read this history without importing the process runner.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import Field, ValidationError, field_validator

from bf.models import NAME, Error, Model, decode, encode, timestamp
from bf.storage import Store, state_store

SENSORS = "sensors.json"
ROUTINES = "routines.json"

# Known variables that make a shell, loader or interpreter run code before the program itself.
_STARTUP = (
    "BASH_ENV",
    "BASHOPTS",
    "ENV",
    "GCONV_PATH",
    "JAVA_TOOL_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "_JAVA_OPTIONS",
    "NODE_OPTIONS",
    "NODE_PATH",
    "RUBYOPT",
    "RUBYLIB",
    "PERL5OPT",
    "PERL5LIB",
    "PERLLIB",
    "PS4",
    "SHELLOPTS",
)
_PREFIXES = ("LD_", "DYLD_", "BASH_FUNC_", "PYTHON", "LUA_INIT")


class _Run(Model):
    run: str = ""
    success: str = ""
    start: str = ""
    end: str = ""
    error: str = ""
    records: int = Field(default=0, ge=0)
    added: int = Field(default=0, ge=0)
    updated: int = Field(default=0, ge=0)
    unchanged: int = Field(default=0, ge=0)
    removed: int = Field(default=0, ge=0)
    requested_start: str = ""
    requested_end: str = ""
    reconciled: str = ""
    reconcile: bool = False
    elapsed_seconds: float = Field(default=0.0, ge=0)
    output_bytes: int = Field(default=0, ge=0)
    action: str = ""
    failures: int = Field(default=0, ge=0)

    @field_validator("run", "success", "start", "end", "requested_start", "requested_end", "reconciled")
    @classmethod
    def instant(cls, value: str) -> str:
        return timestamp(value) if value else value


def environment() -> dict[str, str]:
    """The caller's environment without loader injection or relative PATH entries."""
    env = {key: value for key, value in os.environ.items() if key not in _STARTUP and not key.startswith(_PREFIXES)}
    if "PATH" in env:
        # Programs run from the brain root, where a relative entry would find commands inside the brain.
        env["PATH"] = os.pathsep.join(entry for entry in env["PATH"].split(os.pathsep) if Path(entry).is_absolute())
    return env


def state(store: Store, file: str = SENSORS) -> dict[str, dict[str, object]]:
    """Per-sensor or per-routine run history, kept on this machine outside the brain."""
    try:
        value = decode(state_store(store.root).read(file))
    except FileNotFoundError, Error:
        return {}
    if not isinstance(value, dict):
        return {}
    valid = {}
    for name, entry in value.items():
        if not isinstance(name, str) or not re.fullmatch(NAME, name):
            continue
        try:
            valid[name] = _Run.model_validate(entry).model_dump(exclude_unset=True)
        except ValidationError:
            # Disposable run history must not stop evidence recovery or other sensors.
            continue
    return valid


def remember(store: Store, name: str, file: str = SENSORS, /, **values: object) -> None:
    """Update local history while the caller holds the brain writer lock."""
    current = state(store, file)
    current[name] = {**current.get(name, {}), **values}
    state_store(store.root).write(file, encode(current))


def log_path(store: Store, name: str) -> Path:
    return state_store(store.root).root / f"{name}.log"
