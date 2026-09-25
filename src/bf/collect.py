"""Run configured sensors and routines with direct argv, bounded output and process-group cancellation."""

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
from typing import NoReturn, Protocol

from pydantic import Field, TypeAdapter, ValidationError, field_validator

from bf import ontology, records
from bf.config import load, may_collect
from bf.markdown import note
from bf.models import NAME, Error, Model, Program, Record, decode, encode, explain, timestamp
from bf.storage import Store, collecting, relative, state_store, writer

SENSORS = "sensors.json"
ROUTINES = "routines.json"

_LOG = 256 << 10
# Variables that make a shell, loader or interpreter run code before the program itself.
_STARTUP = (
    "BASH_ENV",
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
    action: str = ""

    @field_validator("run", "success", "start", "end")
    @classmethod
    def instant(cls, value: str) -> str:
        if value:
            timestamp(value)
        return value


class Runner(Protocol):
    def __call__(self, argv: list[str], sensor: Program, store: Store, log: Path, /) -> bytes: ...


def environment() -> dict[str, str]:
    """The caller's environment without loader injection or relative PATH entries."""
    env = {key: value for key, value in os.environ.items() if key not in _STARTUP and not key.startswith(_PREFIXES)}
    if "PATH" in env:
        # Programs run from the brain root, where a relative entry would find commands inside the brain.
        env["PATH"] = os.pathsep.join(entry for entry in env["PATH"].split(os.pathsep) if Path(entry).is_absolute())
    return env


def run(argv: list[str], sensor: Program, store: Store, log: Path) -> bytes:
    """Execute one sensor or routine from the brain root; stderr goes to a bounded private log, never to errors."""
    env = environment()
    executable = argv[0]
    if executable.startswith(("sensors/", "routines/")):
        relative(executable)
        # A regular file below the brain, never a link; any size.
        store.fingerprint(executable)
        executable = str(store.root / executable)
    elif "/" in executable:
        raise Error("executable must be a bare command name or a sensors/ or routines/ path")
    elif (found := shutil.which(executable, path=env.get("PATH"))) is None:
        raise Error(f"{executable} is not on PATH")
    else:
        executable = found
    try:
        child = subprocess.Popen(  # noqa: S603
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
        raise Error("could not start the program; check its executable bit and interpreter") from error
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + sensor.timeout
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, buffer in ((child.stdout, output), (child.stderr, errors)):
                if pipe is None:
                    raise Error("program pipes were not created")
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, buffer)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise Error(f"program timed out after {sensor.timeout}s; nothing was written")
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
                        raise Error("program output exceeded max_bytes; nothing was written")
                    del errors[:-_LOG]
        code = child.wait()
        if code:
            raise Error(f"program exited with status {code}; nothing was written")
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


def _remember(store: Store, name: str, file: str = SENSORS, /, **values: object) -> None:
    """Update local history while the caller holds the brain writer lock."""
    current = state(store, file)
    current[name] = {**current.get(name, {}), **values}
    state_store(store.root).write(file, encode(current))


def log_path(store: Store, name: str) -> Path:
    return state_store(store.root).root / f"{name}.log"


def _window(start: str, end: str) -> tuple[str, str]:
    try:
        start, end = timestamp(start), timestamp(end)
    except ValueError as error:
        raise Error("start and end require timezone-aware timestamps") from error
    if start >= end:
        raise Error("start must be earlier than end")
    return start, end


def _trusted(store: Store, programs: str) -> None:
    if not may_collect(store):
        raise Error(f"this brain may not run {programs} here; trust it with bf register --collect")


def _failed(
    store: Store, name: str, file: str, started: datetime, message: str, cause: BaseException, *, dry_run: bool
) -> NoReturn:
    """Remember a failed run, then name its private log; provider output never enters the message."""
    if not dry_run:
        try:
            with writer(store, wait=120):
                _remember(store, name, file, run=started.isoformat(), error=message)
        except (Error, OSError) as history_error:
            raise Error(f"{name}: {message}; run history could not be saved") from history_error
    raise Error(f"{name}: {message}; see {log_path(store, name)}") from cause


def _coverage(previous: dict[str, object], start: str, end: str, observed: str) -> tuple[dict[str, str], bool]:
    """The contiguous collected interval after a run, and whether the run is the latest window.

    Coverage never extends past the run itself: later items can still appear upstream. A backfill that
    ends before the recorded coverage neither moves the resume point nor counts as a fresh success, and
    coverage never claims an uncollected gap.
    """
    covered = min(end, observed)
    before_start, before_end = str(previous.get("start", "")), str(previous.get("end", ""))
    if before_end:
        # Saved state may predate the coverage bound or follow a clock correction. It cannot
        # prove coverage beyond its successful run, nor prevent a current run becoming fresh.
        before_end = min(timestamp(before_end), timestamp(str(previous.get("success") or observed)), observed)
        before_start = timestamp(before_start) if before_start else ""
        if before_start >= before_end:
            before_start = before_end = ""
    if start >= covered:
        return ({"start": before_start, "end": before_end} if before_end else {}), False
    latest = not before_end or covered >= before_end
    interval = (start, covered) if latest else (before_start, before_end)
    if before_start and before_end and start <= before_end and covered >= before_start:
        interval = (min(start, before_start), max(covered, before_end))
    return {"start": interval[0], "end": interval[1]}, latest


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
    start, end = _window(start, end)
    config = load(store)
    sensor = config.sensors.get(name)
    if sensor is None or not sensor.enabled:
        raise Error(f"sensor {name} is unknown or disabled")
    _trusted(store, "collectors")
    argv = _argv(store, sensor, start, end)
    with collecting(store, name):
        started = clock()
        committed = False
        try:
            raw = runner(argv, sensor, store, log_path(store, name))
            try:
                incoming = TypeAdapter(list[Record]).validate_python(decode(raw))
            except ValidationError as error:
                # Record fields and item positions only: a key the provider printed may be private text.
                known = {*Record.model_fields, "[key]"}
                raise Error("collector must print one JSON array of records: " + explain(error, known)) from error
            incoming = [ontology.project(record, sensor, config) for record in incoming]
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
                if sensor.mode == "snapshot" and not incoming and records.partitions(store, name):
                    # A wrong account or an unmounted folder also looks empty: never erase a catalog silently.
                    raise Error(
                        f"snapshot returned no records; kept the existing catalog. Check the sensor's scope, "
                        f"or delete memories/{name}/ to clear it deliberately"
                    )
                result.update(records.upsert(store, name, incoming, snapshot=sensor.mode == "snapshot"))
                committed = True
                coverage, latest = _coverage(state(store).get(name, {}), start, end, observed)
                _remember(
                    store,
                    name,
                    SENSORS,
                    run=started.isoformat(),
                    **coverage,
                    error="",
                    **({"success": started.isoformat()} if latest else {}),
                    **{key: value for key, value in result.items() if key != "sensor"},
                )
        except (Error, OSError, UnicodeError) as error:
            message = (
                str(error)
                if isinstance(error, Error)
                else "collector files are inaccessible; check its executable, record permissions and free space"
            )
            if committed:
                message = "records were committed but local run history could not be saved; retry is safe"
            _failed(store, name, SENSORS, started, message, error, dry_run=dry_run)
        return result


def _argv(store: Store, program: Program, start: str, end: str) -> list[str]:
    values = {"brain": str(store.root), "home": str(Path.home()), "start": start, "end": end}
    return [re.sub(r"\{\{(brain|home|start|end)\}\}", lambda match: values[match[1]], arg) for arg in program.command]


def routine(
    store: Store,
    name: str,
    *,
    start: str,
    end: str,
    dry_run: bool = False,
    runner: Runner = run,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Run one routine over [start, end); its Markdown becomes today's action, written only after success.

    Empty output means there is nothing to review. An existing action folder is never overwritten,
    so a routine writes at most one action per local day and never replaces a person's edits.
    """
    start, end = _window(start, end)
    program = load(store).routines.get(name)
    if program is None or not program.enabled:
        raise Error(f"routine {name} is unknown or disabled")
    _trusted(store, "routines")
    with collecting(store, name):
        started = clock()
        folder = f"actions/{started.astimezone().date().isoformat()}_{name}"
        path = f"{folder}/ACTION.md"
        try:
            raw = runner(_argv(store, program, start, end), program, store, log_path(store, name))
            try:
                text = raw.decode("utf-8")
            except UnicodeError as error:
                raise Error("routine must print UTF-8 Markdown") from error
            result: dict[str, object] = {"routine": name}
            if text.strip():
                # Validate as an authored note, including its declared links, before anything is written.
                ontology.note_claims(note(path, raw), load(store))
                result["action"] = path
            if dry_run:
                return {**result, **({"text": text} if text.strip() else {})}
            with writer(store, wait=120):
                if text.strip() and store.files(folder):
                    del result["action"]
                    result["skipped"] = "an action for this routine already exists today"
                elif text.strip():
                    store.write(path, raw)
                # A skipped review keeps its window open, so the next written action covers it.
                reviewed = {} if "skipped" in result else {"start": start, "end": end}
                _remember(
                    store,
                    name,
                    ROUTINES,
                    run=started.isoformat(),
                    success=started.isoformat(),
                    **reviewed,
                    error="",
                    **({"action": path} if "action" in result else {}),
                )
        except (Error, OSError, UnicodeError) as error:
            message = (
                str(error)
                if isinstance(error, Error)
                else "routine files are inaccessible; check its executable, action folder permissions and free space"
            )
            _failed(store, name, ROUTINES, started, message, error, dry_run=dry_run)
        return result


def _due(program: Program, last: dict[str, object], now: datetime) -> bool:
    """Enabled, scheduled, and its refresh interval has elapsed since the last success."""
    success = str(last.get("success", ""))
    elapsed = not success or now >= datetime.fromisoformat(success) + timedelta(seconds=program.refresh)
    return program.enabled and bool(program.refresh) and elapsed


def due_routines(store: Store, now: datetime) -> list[tuple[str, str, str]]:
    """Due routines; each covers the time since the end of its last reviewed window."""
    config, history = load(store), state(store, ROUTINES)
    windows = []
    for name, program in sorted(config.routines.items()):
        last = history.get(name, {})
        if not _due(program, last, now):
            continue
        start = now - timedelta(seconds=program.lookback)
        if last.get("end") and (reviewed := datetime.fromisoformat(str(last["end"]))) < now:
            start = reviewed
        windows.append((name, timestamp(start.isoformat()), timestamp(now.isoformat())))
    return windows


def due(store: Store, now: datetime) -> list[tuple[str, str, str]]:
    """Due sensors, with the window each should collect."""
    config, history = load(store), state(store)
    windows = []
    for name, sensor in sorted(config.sensors.items()):
        last = history.get(name, {})
        if not _due(sensor, last, now):
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
