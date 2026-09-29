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
from contextlib import ExitStack, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, Protocol
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from bf import ontology, records
from bf.config import load
from bf.history import ROUTINES, SENSORS, environment, log_path, remember, state
from bf.markdown import note, validate_okf
from bf.models import Config, Error, Program, Record, Sensor, decode, explain, timestamp
from bf.storage import Store, collecting, relative, writer

_LOG = 256 << 10
# Seconds a finished program's background descendants may keep its stdout open.
_DRAIN = 1.0
# Window sensors catch up at most this far after a pause.
CATCH_UP = timedelta(days=30)
# A failed program's first retry waits this long, doubling per consecutive failure up to its refresh.
BACKOFF = timedelta(minutes=1)
# A snapshot may remove half of its catalog, or up to this many records, without --allow-removal.
SHRINK_FLOOR = 10
# Reprojected records committed per transaction; a reply lists at most LIMIT records that failed.
REPROJECT = 1000
LIMIT = 200


class Runner(Protocol):
    def __call__(self, argv: list[str], sensor: Program, store: Store, log: Path, /) -> bytes: ...


def run(argv: list[str], sensor: Program, store: Store, log: Path) -> bytes:
    """Execute one sensor or routine from the brain root; stderr goes to a bounded private log, never to errors."""
    # A nested bf call without --brain reads the executing brain, never an inherited selection.
    env = {**environment(), "BF_BRAIN": str(store.root)}
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
    stdout, stderr = child.stdout, child.stderr
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + sensor.timeout
    exited: float | None = None
    try:
        if stdout is None or stderr is None:
            raise Error("program pipes were not created")
        with selectors.DefaultSelector() as selector:
            for pipe, buffer in ((stdout, output), (stderr, errors)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, buffer)
            # A run ends with the program's exit and the end of its stdout. Stderr only feeds the log,
            # so a background descendant holding it cannot delay or fail a finished program.
            while stdout in selector.get_map() or child.poll() is None:
                now = time.monotonic()
                if exited is None and child.poll() is not None:
                    exited = now
                if exited is not None and now >= exited + _DRAIN:
                    raise Error(
                        "program exited but a background process kept its stdout open; "
                        "redirect its descendants' output; nothing was written"
                    )
                if now >= deadline:
                    raise Error(f"program timed out after {sensor.timeout}s; nothing was written")
                if stdout not in selector.get_map():
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
        # Keep diagnostics written before the exit; a bounded read never waits for descendants.
        with suppress(BlockingIOError):
            for _ in range(_LOG // 65536 + 1):
                if not (chunk := os.read(stderr.fileno(), 65536)):
                    break
                errors.extend(chunk)
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


def _window(start: str, end: str) -> tuple[str, str]:
    try:
        start, end = timestamp(start), timestamp(end)
    except ValueError as error:
        raise Error("start and end require timezone-aware timestamps") from error
    if start >= end:
        raise Error("start must be earlier than end")
    return start, end


def _failed(
    store: Store, name: str, file: str, started: datetime, message: str, cause: BaseException, *, dry_run: bool
) -> NoReturn:
    """Remember a failed run, then name its private log; provider output never enters the message."""
    if not dry_run:
        try:
            with writer(store, wait=120):
                failures = int(str(state(store, file).get(name, {}).get("failures", 0))) + 1
                remember(store, name, file, run=timestamp(started.isoformat()), error=message, failures=failures)
        except (Error, OSError) as history_error:
            raise Error(f"{name}: {message}; run history could not be saved") from history_error
    raise Error(f"{name}: {message}; see {log_path(store, name)}") from cause


def _coverage(
    previous: dict[str, object], start: str, end: str, observed: str, *, resumes: bool
) -> tuple[dict[str, str], bool]:
    """The contiguous collected interval after a run, and whether the run is the latest window.

    Coverage never extends past the run itself: later items can still appear upstream. A backfill that
    ends before the recorded coverage neither moves the resume point nor counts as a fresh success, and
    coverage never claims an uncollected gap. When updates resume from the coverage (scheduled window
    sensors), a later run leaving a gap inside the catch-up horizon is not the latest either, so the
    next update still fills that gap. Nothing revisits a manual sensor's gap, so its later run is the latest.
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
    horizon = timestamp((datetime.fromisoformat(observed) - CATCH_UP).isoformat())
    latest = not before_end or (covered >= before_end and (not resumes or start <= max(before_end, horizon)))
    interval = (start, covered) if latest else (before_start, before_end)
    if before_start and before_end and start <= before_end and covered >= before_start:
        interval = (min(start, before_start), max(covered, before_end))
    return {"start": interval[0], "end": interval[1]}, latest


def _project(position: int, record: Record, sensor: Sensor, config: Config) -> Record:
    """Map one printed record; an error names its zero-based position in the output, never a value."""
    try:
        return ontology.project(record, sensor, config)
    except Error as error:
        raise Error(f"record {position}: {error}") from error


def _check_shrink(store: Store, name: str, incoming: list[Record]) -> None:
    """A wrong account, a lost folder or a truncated listing also looks like a smaller catalog."""
    existing = records.files(store, name)
    kept = {records.path(name, record.id) for record in incoming}
    removed = sum(file not in kept for file in existing)
    if removed and (not incoming or (removed > len(existing) / 2 and removed > SHRINK_FLOOR)):
        raise Error(
            f"snapshot would remove {removed} of {len(existing)} records; kept the existing catalog. "
            f"Check the sensor's scope, then run bf collect {name} --allow-removal to accept the removal"
        )


def collect(
    store: Store,
    name: str,
    *,
    start: str,
    end: str,
    dry_run: bool = False,
    reconcile: bool = False,
    allow_removal: bool = False,
    runner: Runner = run,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Run one sensor over [start, end) and upsert its records; failures write nothing.

    A snapshot that would empty its catalog, or remove more than half of it and more than SHRINK_FLOOR
    records, fails unless `allow_removal` accepts it for this run.
    """
    start, end = _window(start, end)
    config = load(store)
    sensor = config.sensors.get(name)
    if sensor is None or not sensor.enabled:
        raise Error(f"sensor {name} is unknown or disabled")
    if reconcile and sensor.reconcile is None:
        raise Error("reconciliation requires the sensor's reconcile setting")
    argv = _argv(store, sensor, start, end)
    with collecting(store, name):
        started = clock()
        measured = time.monotonic()
        if reconcile and sensor.reconcile is not None:
            horizon = timestamp((started - timedelta(seconds=sensor.reconcile.lookback)).isoformat())
            if start > horizon or end < timestamp(started.isoformat()):
                raise Error("reconciliation must cover its lookback through the current run")
        committed = False
        try:
            raw = runner(argv, sensor, store, log_path(store, name))
            try:
                incoming = TypeAdapter(list[Record]).validate_python(decode(raw))
            except ValidationError as error:
                # Record fields and item positions only: a key the provider printed may be private text.
                known = {*Record.model_fields, "[key]"}
                raise Error("collector must print one JSON array of records: " + explain(error, known)) from error
            incoming = [_project(position, record, sensor, config) for position, record in enumerate(incoming)]
            if len({r.id for r in incoming}) != len(incoming):
                raise Error("collector returned duplicate record ids")
            observed = timestamp(started.isoformat())
            incoming = [
                record.model_copy(update={"attributes": {**record.attributes, "observed": observed}})
                for record in incoming
            ]
            result: dict[str, object] = {
                "sensor": name,
                "records": len(incoming),
                "requested_start": start,
                "requested_end": end,
                "reconcile": reconcile,
                "output_bytes": len(raw),
            }
            if dry_run:
                return {
                    **result,
                    "elapsed_seconds": round(time.monotonic() - measured, 6),
                    "samples": [r.model_dump(exclude_defaults=True) for r in incoming[:3]],
                }
            with writer(store, wait=120):
                # An interrupted commit may have removed files already: count the catalog it restores.
                records.recover(store)
                if sensor.mode == "snapshot" and not allow_removal:
                    _check_shrink(store, name, incoming)
                result.update(records.upsert(store, name, incoming, snapshot=sensor.mode == "snapshot"))
                committed = True
                result["elapsed_seconds"] = round(time.monotonic() - measured, 6)
                previous = state(store).get(name, {})
                resumes = sensor.mode == "window" and sensor.refresh > 0
                coverage, latest = _coverage(previous, start, end, observed, resumes=resumes)
                remember(
                    store,
                    name,
                    SENSORS,
                    run=observed,
                    **coverage,
                    error="",
                    failures=0,
                    **({"success": observed} if latest else {}),
                    **({"reconciled": observed} if reconcile and latest else {}),
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


def reproject(store: Store, name: str, *, dry_run: bool = False) -> dict[str, object]:
    """Re-apply a sensor's current field mappings to its stored records; never runs the sensor or removes a record.

    Changed records are committed like a window collection, in transactions of up to REPROJECT records that keep
    memory bounded: an interrupted run leaves each record either reprojected or as it was, and a rerun completes it.
    A record the current mappings reject keeps its stored fields and is counted as failed.
    """
    config = load(store)
    sensor = config.sensors.get(name)
    if sensor is None:
        raise Error(f"sensor {name} is not declared in bf.yaml; reprojection applies its field mappings")
    counts = {"records": 0, "changed": 0, "unchanged": 0, "failed": 0}
    problems: list[dict[str, str]] = []
    with ExitStack() as stack:
        # The sensor's own lock keeps a collection from committing an older projection in between.
        stack.enter_context(collecting(store, name))
        if dry_run:
            stack.enter_context(records.reading(store))
        else:
            stack.enter_context(writer(store, wait=120))
            records.recover(store)
        changed: list[Record] = []
        for file in records.files(store, name):
            counts["records"] += 1
            try:
                stored = records.load(store, file)
                projected = ontology.reproject(stored, sensor, config)
            except Error as error:
                counts["failed"] += 1
                problems.append({"file": file, "error": str(error).removeprefix(f"{file}: ")})
                continue
            if projected.fields == stored.fields:
                counts["unchanged"] += 1
                continue
            counts["changed"] += 1
            changed.append(projected)
            if not dry_run and len(changed) >= REPROJECT:
                records.upsert(store, name, changed, snapshot=False)
                changed = []
        if changed and not dry_run:
            records.upsert(store, name, changed, snapshot=False)
    return {
        "sensor": name,
        "dry_run": dry_run,
        **counts,
        **({"problems": problems[:LIMIT]} if problems else {}),
        **({"problems_truncated": True} if len(problems) > LIMIT else {}),
    }


def _argv(store: Store, program: Program, start: str, end: str) -> list[str]:
    values = {"brain": str(store.root), "home": str(Path.home()), "start": start, "end": end}
    return [re.sub(r"\{\{(brain|home|start|end)\}\}", lambda match: values[match[1]], arg) for arg in program.command]


def _written(store: Store, prefix: str) -> bool:
    """Whether an `actions/{prefix}UUID` folder holds anything, such as an ACTION.md or an editor's `.#ACTION.md` lock.

    Folder names, not run history, find it: a retry counts an action whose run history was never saved. A linked
    folder or a link or unaddressable name inside also counts, so nothing is written beside it; a linked `actions/`
    fails by name.
    """
    skipped: dict[str, tuple[int, int, int, int]] = {}
    found = store.files("actions", skipped=skipped)
    if "actions" in skipped:
        raise Error("actions: expected a directory; symlinks and special files are forbidden")
    today = re.compile(re.escape(f"actions/{prefix}") + r"[0-9a-f]{32}(?:/|$)")
    return any(today.match(name) for name in (*found, *skipped))


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
    so a routine writes at most one action per local day in this brain and never replaces a person's edits.
    """
    start, end = _window(start, end)
    program = load(store).routines.get(name)
    if program is None or not program.enabled:
        raise Error(f"routine {name} is unknown or disabled")
    with collecting(store, name):
        started = clock()
        day = started.astimezone().date().isoformat()
        folder = f"actions/{day}_{name}-{uuid4().hex}"
        path = f"{folder}/ACTION.md"
        try:
            raw = runner(_argv(store, program, start, end), program, store, log_path(store, name))
            try:
                text = raw.decode("utf-8")
            except UnicodeError as error:
                raise Error("routine must print UTF-8 Markdown") from error
            result: dict[str, object] = {"routine": name}
            if text.strip():
                # Validate the action's OKF metadata and declared links before anything is written.
                ontology.note_claims(note(path, raw), load(store))
                validate_okf(path, raw)
                result["action"] = path
            if dry_run:
                return {**result, **({"text": text} if text.strip() else {})}
            with writer(store, wait=120):
                if text.strip() and _written(store, f"{day}_{name}-"):
                    del result["action"]
                    result["skipped"] = "an action for this routine already exists today"
                elif text.strip():
                    store.write(path, raw)
                # A skipped review keeps its window open, so the next written action covers it.
                reviewed = {} if "skipped" in result else {"start": start, "end": end}
                at = timestamp(started.isoformat())
                remember(
                    store,
                    name,
                    ROUTINES,
                    run=at,
                    success=at,
                    **reviewed,
                    error="",
                    failures=0,
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


def next_due(program: Program, last: dict[str, object], now: datetime) -> datetime | None:
    """When an enabled scheduled program is next due; None when it is manual or disabled.

    It is due one refresh after its last success. After consecutive failures, it retries after
    BACKOFF, doubling per failure but never waiting longer than its refresh.
    """
    if not program.enabled or not program.refresh:
        return None
    refresh = timedelta(seconds=program.refresh)
    anchor, delay = str(last.get("success", "")), refresh
    if last.get("error"):
        failures = max(1, int(str(last.get("failures", 1))))
        anchor, delay = str(last.get("run", "")), min(refresh, BACKOFF * 2 ** min(failures - 1, 30))
    if not anchor:
        return now
    since = datetime.fromisoformat(anchor)
    # Clock corrections must not suppress work until a future timestamp catches up.
    return now if since > now else since + delay


def _due(program: Program, last: dict[str, object], now: datetime) -> bool:
    return (due_at := next_due(program, last, now)) is not None and due_at <= now


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


def due(store: Store, now: datetime) -> list[tuple[str, str, str, bool]]:
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
            # catching up at most CATCH_UP after a long pause.
            floor = now - CATCH_UP
            resumed = max(datetime.fromisoformat(str(last["end"])), floor) - timedelta(seconds=sensor.overlap)
            if resumed < now:
                start = max(resumed, floor)
        reconciliation = sensor.reconcile
        reconciled = str(last.get("reconciled", ""))
        revisit = reconciliation is not None and (
            not reconciled
            or not timedelta(0) <= now - datetime.fromisoformat(reconciled) < timedelta(seconds=reconciliation.refresh)
        )
        if revisit and reconciliation is not None:
            start = min(start, now - timedelta(seconds=reconciliation.lookback))
        windows.append((name, timestamp(start.isoformat()), timestamp(now.isoformat()), revisit))
    return windows
