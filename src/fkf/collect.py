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

from pydantic import TypeAdapter, ValidationError

from fkf import records
from fkf.config import load, may_collect
from fkf.models import Error, Record, Source, decode, encode, explain, timestamp
from fkf.storage import Store, relative, state_store, writer

_LOG = 256 << 10
_STARTUP = (
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "BASH_ENV",
    "ENV",
)


class Runner(Protocol):
    def __call__(self, argv: list[str], source: Source, store: Store, log: Path, /) -> bytes: ...


def run(argv: list[str], source: Source, store: Store, log: Path) -> bytes:
    """Execute one collector from the base root; stderr goes to a bounded private log, never to errors."""
    executable = argv[0]
    if executable.startswith("sources/"):
        relative(executable)
        store.read(executable, 1 << 20)
        executable = str(store.root / executable)
    elif "/" in executable:
        raise Error("executable must be a bare command name or a sources/ path")
    elif (found := shutil.which(executable)) is None:
        raise Error(f"{executable} is not on PATH")
    else:
        executable = found
    env = {key: value for key, value in os.environ.items() if key not in _STARTUP}
    try:
        child = subprocess.Popen(  # noqa: S603  # nosemgrep: dangerous-subprocess-use-audit
            # Direct argv from the owner's fkf.yaml; no shell interprets it.
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
    deadline = time.monotonic() + source.timeout
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, buffer in ((child.stdout, output), (child.stderr, errors)):
                if pipe is None:
                    raise Error("collector pipes were not created")
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, buffer)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise Error(f"collector timed out after {source.timeout}s; nothing was written")
                if not selector.get_map():
                    with suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=0.05)
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(chunk)
                    if len(output) > source.max_bytes:
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
        log.write_bytes(bytes(errors[-_LOG:]))
        log.chmod(0o600)


def state(store: Store) -> dict[str, dict[str, object]]:
    """Per-source run history, kept on this machine outside the base."""
    try:
        value = decode(state_store(store.root).read("sources.json"))
    except FileNotFoundError:
        return {}
    return value if isinstance(value, dict) else {}  # type: ignore[return-value]


def _remember(store: Store, name: str, **values: object) -> None:
    current = state(store)
    current[name] = {**current.get(name, {}), **values}
    state_store(store.root).write("sources.json", encode(current))


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
    """Run one source over [start, end) and upsert its records; failures write nothing."""
    try:
        start, end = timestamp(start), timestamp(end)
    except ValueError as error:
        raise Error("collection start/end require timezone-aware timestamps") from error
    if start >= end:
        raise Error("collection start must be earlier than end")
    source = load(store).sources.get(name)
    if source is None or not source.enabled:
        raise Error(f"source {name} is unknown or disabled")
    if not may_collect(store):
        raise Error("this base may not run collectors here; trust it with fkf register --collect")
    values = {"base": str(store.root), "home": str(Path.home()), "start": start, "end": end}
    argv = [re.sub(r"\{\{(base|home|start|end)\}\}", lambda match: values[match[1]], arg) for arg in source.command]
    log = log_path(store, name)
    started = clock()
    try:
        raw = runner(argv, source, store, log)
        try:
            incoming = TypeAdapter(list[Record]).validate_python(decode(raw))
        except ValidationError as error:
            raise Error("collector must print one JSON array of records: " + explain(error)) from error
        if len({r.id for r in incoming}) != len(incoming):
            raise Error("collector returned duplicate record ids")
        result: dict[str, object] = {"source": name, "records": len(incoming)}
        if dry_run:
            return {**result, "samples": [r.model_dump(exclude_defaults=True) for r in incoming[:3]]}
        with writer(store, wait=120):
            result.update(records.upsert(store, name, incoming, snapshot=source.mode == "snapshot"))
    except Error as error:
        if not dry_run:
            _remember(store, name, run=started.isoformat(), error=str(error))
        raise Error(f"{name}: {error}; see {log}") from error
    previous = str(state(store).get(name, {}).get("end", ""))
    _remember(store, name, run=started.isoformat(), success=started.isoformat(), end=max(previous, end), error="")
    return result


def due(store: Store, now: datetime) -> list[tuple[str, str, str]]:
    """Enabled sources whose refresh interval elapsed, with the window each should collect."""
    config, history = load(store), state(store)
    windows = []
    for name, source in sorted(config.sources.items()):
        if not source.enabled or not source.refresh:
            continue
        last = history.get(name, {})
        if last.get("success") and now < datetime.fromisoformat(str(last["success"])) + timedelta(
            seconds=source.refresh
        ):
            continue
        start = now - timedelta(seconds=source.lookback)
        if last.get("end") and source.mode == "window":
            # Resume after the last window with overlap for late arrivals (upserts make it harmless),
            # catching up at most 30 days after a long pause.
            resumed = datetime.fromisoformat(str(last["end"])) - timedelta(seconds=source.overlap)
            if resumed < now:
                start = max(resumed, now - timedelta(days=30))
        windows.append((name, timestamp(start.isoformat()), timestamp(now.isoformat())))
    return windows
