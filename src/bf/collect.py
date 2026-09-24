"""Run configured collectors with direct argv, bounded output and process-group cancellation."""

from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import Field, TypeAdapter, ValidationError, field_validator

from bf import records
from bf.config import load, may_collect
from bf.models import NAME, Error, Model, Record, Sensor, decode, encode, explain, timestamp
from bf.storage import Store, collecting, relative, state_store, writer

_LOG = 256 << 10
_STARTUP = (
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "BASH_ENV",
    "ENV",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "NODE_OPTIONS",
    "NODE_PATH",
    "RUBYOPT",
    "RUBYLIB",
    "PERL5OPT",
    "PERL5LIB",
    "PERLLIB",
)


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

    @field_validator("run", "success", "start", "end")
    @classmethod
    def instant(cls, value: str) -> str:
        if value:
            timestamp(value)
        return value


class Runner(Protocol):
    def __call__(self, argv: list[str], sensor: Sensor, store: Store, log: Path, /) -> bytes: ...


def run(argv: list[str], sensor: Sensor, store: Store, log: Path) -> bytes:
    """Execute one collector from the brain root; stderr goes to a bounded private log, never to errors."""
    executable = argv[0]
    if executable.startswith("sensors/"):
        relative(executable)
        store.read(executable, 1 << 20)
        executable = str(store.root / executable)
    elif "/" in executable:
        raise Error("executable must be a bare command name or a sensors/ path")
    elif (found := shutil.which(executable)) is None:
        raise Error(f"{executable} is not on PATH")
    else:
        executable = found
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _STARTUP and not key.startswith(("LD_", "DYLD_", "BASH_FUNC_"))
    }
    try:
        child = subprocess.Popen(  # noqa: S603  # nosemgrep: dangerous-subprocess-use-audit
            # Direct argv from the owner's bf.yaml; no shell interprets it.
            [executable, *argv[1:]],
            cwd=store.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise Error("could not start the collector; check its executable bit and interpreter") from error
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + sensor.timeout
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, buffer in ((child.stdout, output), (child.stderr, errors)):
                if pipe is None:
                    raise Error("collector pipes were not created")
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, buffer)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise Error(f"collector timed out after {sensor.timeout}s; nothing was written")
                if not selector.get_map():
                    with suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=0.05)
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(chunk)
                    if len(output) > sensor.max_bytes:
                        raise Error("collector output exceeded max_bytes; nothing was written")
                    del errors[:-_LOG]
        code = child.wait()
        if code:
            raise Error(f"collector exited with status {code}; nothing was written")
        return bytes(output)
    finally:
        # Descendants can keep pipes open after their leader exits; end the whole session.
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        for pipe in (child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()
        # Use the same confined atomic writer as other private state.
        Store(log.parent).write(log.name, bytes(errors[-_LOG:]))


def state(store: Store) -> dict[str, dict[str, object]]:
    """Per-sensor run history, kept on this machine outside the brain."""
    try:
        value = decode(state_store(store.root).read("sensors.json"))
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


def _remember(store: Store, name: str, **values: object) -> None:
    """Update local history while the caller holds the brain writer lock."""
    current = state(store)
    current[name] = {**current.get(name, {}), **values}
    state_store(store.root).write("sensors.json", encode(current))


def log_path(store: Store, name: str) -> Path:
    return state_store(store.root).root / f"{name}.log"


def collect(
    store: Store,
    name: str,
    *,
    start: str,
    end: str,
    dry_run: bool = False,
    runner: Runner = run,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Run one sensor over [start, end) and upsert its records; failures write nothing."""
    try:
        start, end = timestamp(start), timestamp(end)
    except ValueError as error:
        raise Error("collection start/end require timezone-aware timestamps") from error
    if start >= end:
        raise Error("collection start must be earlier than end")
    sensor = load(store).sensors.get(name)
    if sensor is None or not sensor.enabled:
        raise Error(f"sensor {name} is unknown or disabled")
    if not may_collect(store):
        raise Error("this brain may not run collectors here; trust it with bf register --collect")
    values = {"brain": str(store.root), "home": str(Path.home()), "start": start, "end": end}
    argv = [re.sub(r"\{\{(brain|home|start|end)\}\}", lambda match: values[match[1]], arg) for arg in sensor.command]
    log = log_path(store, name)
    with collecting(store, name):
        started = clock()
        committed = False
        try:
            raw = runner(argv, sensor, store, log)
            try:
                incoming = TypeAdapter(list[Record]).validate_python(decode(raw))
            except ValidationError as error:
                raise Error("collector must print one JSON array of records: " + explain(error)) from error
            if len({r.id for r in incoming}) != len(incoming):
                raise Error("collector returned duplicate record ids")
            observed = timestamp(started.isoformat())
            incoming = [
                record.model_copy(update={"attributes": {**record.attributes, "observed": observed}})
                for record in incoming
            ]
            result: dict[str, object] = {"sensor": name, "records": len(incoming)}
            if dry_run:
                return {**result, "samples": [r.model_dump(exclude_defaults=True) for r in incoming[:3]]}
            with writer(store, wait=120):
                result.update(records.upsert(store, name, incoming, snapshot=sensor.mode == "snapshot"))
                committed = True
                previous = state(store).get(name, {})
                # Coverage is a contiguous interval, not an assertion that omitted windows were collected.
                previous_start, previous_end = str(previous.get("start", "")), str(previous.get("end", ""))
                coverage_start, coverage_end = start, end
                if previous_start and previous_end and start <= previous_end and end >= previous_start:
                    coverage_start, coverage_end = min(start, previous_start), max(end, previous_end)
                _remember(
                    store,
                    name,
                    run=started.isoformat(),
                    success=started.isoformat(),
                    start=coverage_start,
                    end=coverage_end,
                    error="",
                    **{key: value for key, value in result.items() if key != "sensor"},
                )
        except (Error, OSError, UnicodeError) as error:
            message = (
                str(error)
                if isinstance(error, Error)
                else ("collector files are inaccessible; check its executable, record permissions and free space")
            )
            if committed:
                message = "records were committed but local run history could not be saved; retry is safe"
            if not dry_run:
                try:
                    with writer(store, wait=120):
                        _remember(store, name, run=started.isoformat(), error=message)
                except (Error, OSError) as history_error:
                    raise Error(f"{name}: {message}; run history could not be saved") from history_error
            raise Error(f"{name}: {message}; see {log}") from error
        return result


def due(store: Store, now: datetime) -> list[tuple[str, str, str]]:
    """Enabled sensors whose refresh interval elapsed, with the window each should collect."""
    config, history = load(store), state(store)
    windows = []
    for name, sensor in sorted(config.sensors.items()):
        if not sensor.enabled or not sensor.refresh:
            continue
        last = history.get(name, {})
        if last.get("success") and now < datetime.fromisoformat(str(last["success"])) + timedelta(
            seconds=sensor.refresh
        ):
            continue
        start = now - timedelta(seconds=sensor.lookback)
        if last.get("end") and sensor.mode == "window":
            # Resume after the last window with overlap for late arrivals (upserts make it harmless),
            # catching up at most 30 days after a long pause.
            resumed = datetime.fromisoformat(str(last["end"])) - timedelta(seconds=sensor.overlap)
            if resumed < now:
                start = max(resumed, now - timedelta(days=30))
        windows.append((name, timestamp(start.isoformat()), timestamp(now.isoformat())))
    return windows
