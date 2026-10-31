"""Run configured sensors, routines and assistants with direct argv, bounded output and process-group cancellation."""

from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import current_thread, main_thread
from types import FrameType
from typing import Annotated, NoReturn, Protocol, cast
from uuid import uuid4

from pydantic import FailFast, JsonValue, TypeAdapter, ValidationError

from bf import ontology, records
from bf.config import load
from bf.history import ROUTINES, SENSORS, append_log, environment, log_path, remember, state
from bf.markdown import note, parse, validate_okf
from bf.models import (
    _CONTROL,
    MAX_NOTE,
    RECORD_KEYS,
    Assistant,
    Command,
    Config,
    Error,
    Program,
    Record,
    Routine,
    Sensor,
    decode,
    digest,
    explain,
    local,
    suggest,
    timestamp,
)
from bf.storage import BusyError, Store, collecting, relative, writer
from bf.validate import broken_links

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
_EXISTS = "an action for this routine already exists today"
# The whole argument holding an expanded prompt stays below Linux's 128 KiB bound (MAX_ARG_STRLEN, NUL included).
MAX_ARGUMENT = (128 << 10) - 1
# An assistant run's record id: its window end in UTC and the last 8 characters of its job id. Only these records are
# an assistant's to prune.
RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
# A record title shows the reply's first line, shortened.
_TITLE = 120
# A routine's error quotes at most this many characters of its first broken link.
_QUOTE = 200


class Runner(Protocol):
    def __call__(self, argv: list[str], program: Command, store: Store, name: str, stdin: bytes, /) -> bytes: ...


def _now() -> str:
    return local(timestamp(datetime.now(UTC).isoformat()))


def enabled[P: Program | Assistant](programs: Mapping[str, P], kind: str, name: str) -> P:
    """The enabled program called `name`; an unknown or disabled name fails, suggesting only programs that can run."""
    program = programs.get(name)
    if program is None:
        runnable = [known for known, settings in programs.items() if settings.enabled]
        raise Error(f"unknown {kind} {name}; check names in bf.yaml{suggest(name, runnable)}")
    if not program.enabled:
        raise Error(f"{kind} {name} is disabled in bf.yaml; set enabled: true to run it")
    return program


@contextmanager
def starting() -> Generator[None]:
    """Defer cancellation until the caller owns the child and can clean up its process group."""
    if current_thread() is not main_thread():
        yield
        return
    handlers: dict[int, Callable[[int, FrameType | None], object]] = {}
    pending: tuple[int, FrameType | None] | None = None

    def defer(signum: int, frame: FrameType | None) -> None:
        nonlocal pending
        if pending is None:
            pending = (signum, frame)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handler = signal.getsignal(signum)
            if callable(handler):
                handlers[signum] = handler
                signal.signal(signum, defer)
        # Do not block signals with pthread_sigmask: the program would inherit that mask across exec.
        yield
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        if pending is not None:
            signum, frame = pending
            handlers[signum](signum, frame)


class NotStartedError(Error):
    """The program never started, so it cannot have acted: retrying it is safe."""


def executable(store: Store, command: str, env: Mapping[str, str]) -> str:
    """Resolve an owner's argv executable without following brain-owned symlinks."""
    if command.startswith(("sensors/", "routines/", "channels/")):
        relative(command)
        try:
            store.fingerprint(command)
        except FileNotFoundError as error:
            raise NotStartedError(f"{command} does not exist; restore it or correct bf.yaml") from error
        except (Error, OSError) as error:
            raise NotStartedError(str(error) if isinstance(error, Error) else f"cannot read {command}") from error
        return str(store.root / command)
    if "/" in command:
        raise NotStartedError("executable must be a bare command name or a sensors/, routines/ or channels/ path")
    found = shutil.which(command, path=env.get("PATH"))
    if found is None:
        raise NotStartedError(f"{command} is not on PATH")
    return found


def run(
    argv: list[str],
    program: Command,
    store: Store,
    name: str,
    stdin: bytes = b"",
    /,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> bytes:
    """Execute one sensor, routine, assistant harness or channel command from the brain root, feeding it `stdin`;
    returns its stdout.

    Its stderr, and a `log` routine's stdout, go to the program's bounded log in logs/, never to errors.
    """
    # A nested bf call without --brain reads the executing brain, never an inherited selection.
    env = {**environment(), "BF_BRAIN": str(store.root)}
    if cancelled is not None and cancelled():
        raise NotStartedError("program cancelled")
    command = executable(store, argv[0], env)
    started = time.monotonic()
    child: subprocess.Popen[bytes] | None = None
    output, errors = bytearray(), bytearray()
    # A log routine's stdout only feeds its log: keep a rolling tail, as for stderr, instead of failing at max_bytes.
    logged = isinstance(program, Routine) and program.output == "log"
    deadline = started + program.timeout
    exited: float | None = None
    outcome = "failed"
    try:
        with starting():
            if cancelled is not None and cancelled():
                raise NotStartedError("program cancelled")
            try:
                child = subprocess.Popen(  # noqa: S603
                    # Direct argv from the owner's bf.yaml, plus a `bf run` caller's arguments; no shell interprets them.
                    [command, *argv[1:]],
                    cwd=store.root,
                    env=env,
                    stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as error:
                # A log that cannot be written must not hide why the program did not start.
                with suppress(Error, OSError):
                    append_log(store, name, f"{_now()} could not start {command}")
                raise NotStartedError(
                    "could not start the program; check its executable bit and interpreter"
                ) from error
        stdout, stderr = child.stdout, child.stderr
        if stdout is None or stderr is None:
            raise Error("program pipes were not created")
        with selectors.DefaultSelector() as selector:
            for pipe, buffer in ((stdout, output), (stderr, errors)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, buffer)
            if child.stdin is not None:
                # Written as the program reads it, so a large input never blocks its output.
                os.set_blocking(child.stdin.fileno(), False)
                selector.register(child.stdin, selectors.EVENT_WRITE, memoryview(stdin))
            # A run ends with the program's exit and the end of its stdout. Stderr only feeds the log,
            # so a background descendant holding it cannot delay or fail a finished program.
            while stdout in selector.get_map() or child.poll() is None:
                if cancelled is not None and cancelled():
                    raise Error("program cancelled")
                now = time.monotonic()
                if exited is None and child.poll() is not None:
                    exited = now
                if exited is not None and now >= exited + _DRAIN:
                    raise Error(
                        "program exited but a background process kept its stdout open; redirect its descendants' output"
                    )
                if now >= deadline:
                    raise Error(f"program timed out after {program.timeout}s")
                if stdout not in selector.get_map():
                    with suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=0.05)
                for key, events in selector.select(0.05):
                    if events & selectors.EVENT_WRITE:
                        try:
                            written = os.write(key.fd, key.data[:65536])
                        except BrokenPipeError:
                            # The program ended or closed its input without reading all of it.
                            written = len(key.data)
                        if written < len(key.data):
                            selector.modify(key.fileobj, selectors.EVENT_WRITE, key.data[written:])
                        elif child.stdin is not None:
                            selector.unregister(child.stdin)
                            child.stdin.close()
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(chunk)
                    if logged:
                        del output[:-_LOG]
                    elif len(output) > program.max_bytes:
                        raise Error("program output exceeded max_bytes")
                    del errors[:-_LOG]
        # Keep diagnostics written before the exit; a bounded read never waits for descendants.
        with suppress(BlockingIOError):
            for _ in range(_LOG // 65536 + 1):
                if not (chunk := os.read(stderr.fileno(), 65536)):
                    break
                errors.extend(chunk)
        code = child.wait()
        if code < 0:
            # BF's own kills fail above: the program crashed, or another process, such as the out-of-memory killer,
            # ended it.
            try:
                killer = signal.Signals(-code).name
            except ValueError:
                # Real-time signals between SIGRTMIN and SIGRTMAX have no name.
                killer = f"signal {-code}"
            raise Error(f"program was killed by {killer}")
        outcome = f"exited with status {code}"
        if code:
            raise Error(f"program exited with status {code}")
        return bytes(output)
    except Error as error:
        outcome = str(error)
        raise
    finally:
        if child is not None:
            # Descendants can keep pipes open after their leader exits; end the whole session.
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
            shown = bytes(output[-_LOG:]) if logged else b""
            diagnostics = bytes(errors[-_LOG:])
            body = shown + (b"-- stderr --\n" + diagnostics if shown and diagnostics else diagnostics)
            # A log is a diagnostic: failing to write it never changes the run's own outcome.
            with suppress(Error, OSError):
                append_log(store, name, f"{_now()} {outcome} after {time.monotonic() - started:.1f}s", body)


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
    """Remember a failed run, then name its log; provider output never enters the message."""
    with suppress(Error, OSError):
        append_log(store, name, f"{_now()} failed: {message}")
    if not dry_run:
        busy = isinstance(cause, BusyError)
        try:
            # A run that already waited for a busy writer records its failure only if the writer is free now.
            with writer(store, wait=0 if busy else 120):
                failures = int(str(state(store, file).get(name, {}).get("failures", 0))) + 1
                remember(store, name, file, run=timestamp(started.isoformat()), error=message, failures=failures)
        except (Error, OSError) as history_error:
            if not (busy and isinstance(history_error, BusyError)):
                raise Error(f"{name}: {message}; run history could not be saved") from history_error
    raise Error(f"{name}: {message}; see {log_path(name)}") from cause


def _coverage(
    previous: dict[str, object], start: str, end: str, observed: str, sensor: Sensor
) -> tuple[dict[str, str], bool]:
    """The contiguous collected interval after a run, and whether the run is the latest: a fresh success.

    Coverage never extends past the run itself: later items can still appear upstream. A run contiguous with the
    coverage extends it, a backfill that ends before it keeps the resume point, and coverage never claims an
    uncollected gap. When updates resume from the coverage (scheduled window sensors), a later run leaving a gap
    inside the catch-up horizon is not contiguous, so the next update still fills that gap; nothing revisits a
    manual sensor's gap, so its later run counts as contiguous. A window run is the latest only when its end also
    reaches the time it ran: an earlier window, even a first or adjacent one, never claims fresh evidence, and the
    next update resumes from its end. A snapshot replaces its whole catalog, so its end need not reach the run.
    """
    covered = min(end, observed)
    before_start, before_end = str(previous.get("start", "")), str(previous.get("end", ""))
    if before_end:
        # Saved state may predate the coverage bound or follow a clock correction. It cannot prove coverage
        # beyond the last run, which recorded it, nor prevent a current run becoming fresh.
        last = previous.get("run") or previous.get("success") or observed
        before_end = min(timestamp(before_end), timestamp(str(last)), observed)
        before_start = timestamp(before_start) if before_start else ""
        if before_start >= before_end:
            before_start = before_end = ""
    if start >= covered:
        return ({"start": before_start, "end": before_end} if before_end else {}), False
    horizon = timestamp((datetime.fromisoformat(observed) - CATCH_UP).isoformat())
    resumes = sensor.mode == "window" and sensor.refresh > 0
    contiguous = not before_end or (covered >= before_end and (not resumes or start <= max(before_end, horizon)))
    interval = (start, covered) if contiguous else (before_start, before_end)
    if before_start and before_end and start <= before_end and covered >= before_start:
        interval = (min(start, before_start), max(covered, before_end))
    return {"start": interval[0], "end": interval[1]}, contiguous and (sensor.mode == "snapshot" or end >= observed)


def _project(position: int, record: Record, sensor: Sensor, config: Config, observed: str) -> Record:
    """Map one printed record and stamp when it was observed; an error names its zero-based position, never a value."""
    try:
        projected = ontology.project(record, sensor, config)
        stamped = projected.model_copy(update={"attributes": {**projected.attributes, "observed": observed}})
        # Already valid, and `observed` is BF's own canonical instant: the stamp can only exceed the size bound.
        return stamped.readable()
    except (Error, ValueError) as error:
        raise Error(f"record {position}: {error}") from error


def _shrink(store: Store, name: str, incoming: list[Record]) -> str:
    """Why a snapshot's removal needs --allow-removal, or "": a wrong account, a lost folder or a truncated listing
    also looks like a smaller catalog."""
    existing = records.files(store, name)
    kept = {records.path(name, record.id) for record in incoming}
    removed = sum(file not in kept for file in existing)
    if removed and (not incoming or (removed > len(existing) / 2 and removed > SHRINK_FLOOR)):
        return (
            f"snapshot would remove {removed} of {len(existing)} records; kept the existing catalog. "
            f"Check the sensor's scope, then run bf collect {name} --allow-removal to accept the removal"
        )
    return ""


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
    sensor = enabled(config.sensors, "sensor", name)
    if reconcile and sensor.reconcile is None:
        raise Error("reconciliation requires the sensor's reconcile setting")
    with collecting(store, name):
        started = clock()
        observed = timestamp(started.isoformat())
        measured = time.monotonic()
        if reconcile and sensor.reconcile is not None:
            horizon = timestamp((started - timedelta(seconds=sensor.reconcile.lookback)).isoformat())
            if start > horizon or end < observed:
                raise Error("reconciliation must cover its lookback through the current run")
        committed = False
        try:
            # A {{home}} that cannot resolve fails the run in its history, as for routines.
            raw = runner(arguments(store, sensor, start, end), sensor, store, name, b"")
            printed = decode(raw)
            text_limit = sensor.max_text_bytes or (4 << 20)
            truncated_count = 0
            if isinstance(printed, list):
                for position, item in enumerate(printed):
                    # Collection maps fields itself. Checked before validation, which would name invalid keys or the
                    # 1,000-name bound instead of this rule.
                    if isinstance(item, dict) and item.get("fields"):
                        raise Error(f"record {position}: sensors must supply mapped output, not precomputed fields")
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        text = item["text"]
                        if (sensor.truncate_text or sensor.max_text_bytes is not None or len(text) > 4 << 20) and len(
                            encoded_text := text.encode("utf-8")
                        ) > text_limit:
                            cut = encoded_text[:text_limit].decode("utf-8", errors="ignore")
                            attrs = item.get("attributes")
                            updated_attrs = (
                                {**attrs, "truncated": True} if isinstance(attrs, dict) or attrs is None else attrs
                            )
                            printed[position] = {**item, "text": cut, "attributes": updated_attrs}
                            truncated_count += 1
            if truncated_count:
                with suppress(Error, OSError):
                    append_log(
                        store,
                        name,
                        f"{_now()} truncated {truncated_count} oversized record text value(s) to {text_limit} bytes",
                    )
            try:
                # Stop at the first invalid record: a large invalid array must not build one diagnostic per item.
                incoming = TypeAdapter(Annotated[list[Record], FailFast()]).validate_python(printed)
            except ValidationError as error:
                raise Error("collector must print one JSON array of records: " + explain(error, RECORD_KEYS)) from error
            incoming = [
                _project(position, record, sensor, config, observed) for position, record in enumerate(incoming)
            ]
            if len({r.id for r in incoming}) != len(incoming):
                raise Error("collector returned duplicate record ids")
            result: dict[str, object] = {
                "sensor": name,
                "records": len(incoming),
                "requested_start": start,
                "requested_end": end,
                "reconcile": reconcile,
                "output_bytes": len(raw),
            }
            snapshot = sensor.mode == "snapshot"
            if dry_run:
                # What a real run would change, from the records as they are: an interrupted commit that the run
                # would first recover fails the preview instead, as reads do.
                with records.reading(store):
                    counts, _ = records.plan(store, name, incoming, snapshot=snapshot)
                    refused = snapshot and not allow_removal and bool(_shrink(store, name, incoming))
                return {
                    **result,
                    **counts,
                    **({"removal_refused": True} if refused else {}),
                    "elapsed_seconds": round(time.monotonic() - measured, 6),
                    "samples": [r.model_dump(exclude_defaults=True) for r in incoming[:3]],
                }
            with writer(store, wait=120):
                # An interrupted commit may have removed files already: count the catalog it restores.
                records.recover(store)
                if snapshot and not allow_removal and (refusal := _shrink(store, name, incoming)):
                    raise Error(refusal)
                result.update(records.upsert(store, name, incoming, snapshot=snapshot))
                committed = True
                result["elapsed_seconds"] = round(time.monotonic() - measured, 6)
                coverage, latest = _coverage(state(store).get(name, {}), start, end, observed, sensor)
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
            # A failed collection never changes evidence: say so, unless the records were already committed.
            message = (
                "records were committed but local run history could not be saved; retry is safe"
                if committed
                else f"{message}; nothing was written"
            )
            _failed(store, name, SENSORS, started, message, error, dry_run=dry_run)
        counts = ", ".join(f"{result.get(key, 0)} {key}" for key in ("added", "updated", "unchanged", "removed"))
        with suppress(Error, OSError):
            append_log(store, name, f"{_now()} collected {result['records']} records: {counts}")
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
                # Count every failure, but keep only what the reply shows.
                if len(problems) <= LIMIT:
                    problems.append({"file": file, "error": str(error).removeprefix(f"{file}: ")})
                continue
            if projected.fields == stored.fields:
                counts["unchanged"] += 1
                continue
            counts["changed"] += 1
            if dry_run:
                # A preview counts changes without holding the reprojected corpus.
                continue
            changed.append(projected)
            if len(changed) >= REPROJECT:
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


def home_folder() -> str:
    """The folder {{home}} names, resolved only for a program that uses it: a container user without HOME or a passwd
    entry still runs the others."""
    try:
        return str(Path.home())
    except RuntimeError as error:
        raise NotStartedError("cannot find the home directory for {{home}}; set HOME") from error


def expand_lines(store: Store, path: str) -> list[str]:
    """Read a brain-relative line-list file, ignoring blank lines and `#` comments."""
    relative(path)
    try:
        raw = store.read(path, MAX_NOTE)
    except FileNotFoundError as error:
        raise NotStartedError(f"{path} does not exist; restore it or correct bf.yaml") from error
    except (Error, OSError) as error:
        raise NotStartedError(str(error) if isinstance(error, Error) else f"cannot read {path}") from error
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise NotStartedError(f"{path} is not valid UTF-8") from error
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\x00" in stripped or len(stripped) > 16_384:
            raise NotStartedError(f"{path}: line exceeds 16384 characters or contains NUL")
        items.append(stripped)
    return items


def arguments(store: Store, program: Command, start: str, end: str, prompt: str = "") -> list[str]:
    values = {"brain": str(store.root), "start": start, "end": end, "prompt": prompt}
    # One pass: an expanded prompt that mentions a placeholder keeps it as text.
    expanded: list[str] = []
    for index, arg in enumerate(program.command):
        if index > 0 and (lines_match := re.fullmatch(r"\{\{lines:([^{}\\]+)\}\}", arg)):
            expanded.extend(expand_lines(store, lines_match[1]))
        else:
            expanded.append(
                re.sub(
                    r"\{\{(brain|home|start|end|prompt)\}\}",
                    lambda match: home_folder() if match[1] == "home" else values[match[1]],
                    arg,
                )
            )
    if len(expanded) > 128:
        raise NotStartedError("command arguments exceed 128 items after expanding line files")
    return expanded


def _written(store: Store, prefix: str) -> bool:
    """Whether an `actions/{prefix}SUFFIX` folder holds an ACTION.md, which alone makes it an action.

    Folder names, not run history, find it: a retry counts an action whose run history was never saved. An ACTION.md
    link, or a linked or unreadable folder BF cannot look into, also counts; a linked or unreadable `actions/` fails
    by name. A folder holding only the temporary file of a killed write, or an editor's lock, does not stop today's
    action.
    """
    skipped: dict[str, tuple[int, int, int, int]] = {}
    found = store.files("actions", skipped=skipped)
    if "actions" in skipped:
        # The scan skips a folder it cannot read as it skips a link: name the cause the owner must fix.
        if stat.S_ISDIR(store.mode("actions")):
            raise Error("actions: unreadable folder; grant read and search permission")
        raise Error("actions: expected a directory; symlinks and special files are forbidden")
    today = re.compile(re.escape(f"actions/{prefix}") + r"[0-9a-f]{8}(?:/ACTION\.md)?")
    return any(today.fullmatch(name) for name in (*found, *skipped))


def _prune_reports(store: Store, name: str, keep: int, written: dict[str, str]) -> tuple[dict[str, str], int]:
    """Remove the routine's actions older than its newest `keep` that still hold only the ACTION.md BF wrote.

    `written` maps each action this machine wrote to the SHA-256 of its bytes; it returns the entries still worth
    checking and how many folders were removed. An edited action, one holding other files and one written before
    `keep` was set or on another machine stay: they are a person's work, not a disposable report. A folder BF cannot
    read or change stays too, and the routine's log names it.
    """
    reports = {**cast(dict[str, str], state(store, ROUTINES).get(name, {}).get("reports", {})), **written}
    pattern = re.compile(rf"actions/\d{{4}}-\d{{2}}-\d{{2}}_{re.escape(name)}-[0-9a-f]{{8}}/ACTION\.md")
    try:
        found = store.files("actions")
    except (Error, OSError) as error:
        # An incomplete listing could miscount the newest actions: remove none this run.
        with suppress(Error, OSError):
            append_log(store, name, f"{_now()} kept older actions: {error}")
        return reports, 0
    # Folder dates order the actions, but the ones this run wrote are the newest whatever their date: another clone
    # in a time zone ahead, or a clock that ran ahead, can date a folder after today.
    actions = sorted((file for file in found if pattern.fullmatch(file)), key=lambda file: (file in written, file))
    removed = 0
    for path in actions[: max(0, len(actions) - keep)]:
        # Only an action this machine wrote can be a disposable report: never read the others.
        if (expected := reports.pop(path, None)) is None:
            continue
        folder = path.removesuffix("/ACTION.md")
        try:
            # Exactly one entry: an empty inputs/ folder or another file makes it the owner's.
            data = store.read(path, MAX_NOTE) if store.entries(folder) == ["ACTION.md"] else b""
            if data and digest(data) == expected:
                store.delete(path)
                try:
                    store.rmdir(folder)
                    removed += 1
                except Error, OSError:
                    # An entry appeared meanwhile: put the report back rather than leave a folder without it.
                    store.write(path, data)
        except (Error, OSError) as error:
            # The new action is written: a report BF cannot read or remove stays, and never fails the run.
            with suppress(Error, OSError):
                append_log(store, name, f"{_now()} kept {folder}: {error}")
    listed = set(actions)
    return {path: value for path, value in reports.items() if path in listed}, removed


def routine(
    store: Store,
    name: str,
    *,
    start: str,
    end: str,
    dry_run: bool = False,
    args: tuple[str, ...] = (),
    stdin: bytes = b"",
    runner: Runner = run,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Run one routine over [start, end), appending `args` to its command and feeding it `stdin`.

    A `log` routine keeps its output in its log. An `action` routine's Markdown becomes today's action, written only
    after success in a new folder; empty output means there is nothing to review. While today's action exists, a run
    skips, so an action routine keeps at most one action per local day in this brain and never replaces a person's
    edits. A dry run reports the same skip.
    """
    start, end = _window(start, end)
    config = load(store)
    program = enabled(config.routines, "routine", name)
    with collecting(store, name):
        started = clock()
        day = started.astimezone().date().isoformat()
        # A short random suffix keeps actions written the same day by different clones apart.
        folder = f"actions/{day}_{name}-{uuid4().hex[-8:]}"
        path = f"{folder}/ACTION.md"
        written = False
        try:
            raw = runner([*arguments(store, program, start, end), *args], program, store, name, stdin)
            result: dict[str, object] = {"routine": name}
            text = ""
            if program.output == "action":
                try:
                    text = raw.decode("utf-8")
                except UnicodeError as error:
                    raise Error("routine must print UTF-8 Markdown") from error
                if text.strip():
                    # Validate the action's OKF metadata, declared relations and relative links before anything
                    # is written: an action bf validate rejects would block the brain's next checked commit.
                    action = note(path, markdown := parse(path, raw))
                    ontology.note_claims(action, config, strict=True)
                    validate_okf(path, markdown)
                    if broken := broken_links(store, action):
                        # Run history and status keep this error: quote only the start of a link, which can carry
                        # evidence from the output.
                        first = broken[0] if len(broken[0]) <= _QUOTE else broken[0][: _QUOTE - 1] + "…"
                        raise Error(first + (f" and {len(broken) - 1} more" if len(broken) > 1 else ""))
            if dry_run:
                # A preview predicts the skip, but names no action: the real run draws its own folder suffix.
                if text.strip() and _written(store, f"{day}_{name}-"):
                    result["skipped"] = _EXISTS
                return {**result, **({"text": text} if text.strip() else {})}
            with writer(store, wait=120):
                if text.strip() and _written(store, f"{day}_{name}-"):
                    result["skipped"] = _EXISTS
                elif text.strip():
                    store.write(path, raw)
                    written = True
                    result["action"] = path
                    if program.keep is not None:
                        reports, result["removed"] = _prune_reports(store, name, program.keep, {path: digest(raw)})
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
                    **({"reports": reports} if "removed" in result else {}),
                )
            outcome = f"wrote {path}" if "action" in result else str(result.get("skipped", "succeeded"))
            with suppress(Error, OSError):
                append_log(store, name, f"{_now()} {outcome}")
        except (Error, OSError, UnicodeError) as error:
            message = (
                str(error)
                if isinstance(error, Error)
                else "routine files are inaccessible; check its executable, action folder permissions and free space"
            )
            if written:
                message = f"wrote {path} but local run history could not be saved"
            elif program.output == "action":
                message += "; no action was written"
            _failed(store, name, ROUTINES, started, message, error, dry_run=dry_run)
        return result


def parse_reply(raw: bytes, pointer: str | None) -> tuple[str, dict[str, JsonValue]]:
    """The reply text, and the other members of a JSON reply; the error never quotes the output."""
    try:
        output = raw.decode("utf-8")
    except UnicodeError as error:
        raise Error("assistant command must print UTF-8") from error
    if pointer is None:
        return output.strip(), {}
    try:
        document = decode(raw)
    except Error as error:
        raise Error("assistant command must print one JSON object when text is set") from error
    if not isinstance(document, dict):
        raise Error("assistant command must print one JSON object when text is set")
    value = ontology.pointer(cast(JsonValue, document), pointer)
    if not isinstance(value, str):
        raise Error(f"assistant reply has no text string at {pointer}")
    parent, _, key = pointer.rpartition("/")
    holder = ontology.pointer(cast(JsonValue, document), parent) if parent else document
    if isinstance(holder, dict):
        # The reply is the record's text: keep it once.
        holder.pop(key.replace("~1", "/").replace("~0", "~"), None)
    elif isinstance(holder, list):
        # Preserve the other elements' positions without duplicating a potentially large answer.
        holder[int(key)] = None
    return value.strip(), cast(dict[str, JsonValue], document)


def reply_title(name: str, reply: str) -> str:
    """The reply's first line without Markdown heading marks or control characters, else a neutral title."""
    for line in reply.splitlines():
        if line := _CONTROL.sub(" ", line).strip().lstrip("#").strip():
            return line if len(line) <= _TITLE else line[: _TITLE - 1].rstrip() + "…"
    return f"{name} run without a reply"


def hooked(config: Config, hook: str) -> list[str]:
    """The enabled routines a hook runs, in name order."""
    return sorted(name for name, program in config.routines.items() if program.enabled and hook in program.hooks)


def window(store: Store, name: str, now: datetime) -> tuple[str, str]:
    """The window a routine run covers now: since the end of its last reviewed window, else its lookback."""
    config = load(store)
    program, last = config.routines[name], state(store, ROUTINES).get(name, {})
    start = now - timedelta(seconds=program.lookback)
    if last.get("end") and (reviewed := datetime.fromisoformat(str(last["end"]))) < now:
        start = reviewed
    return timestamp(start.isoformat()), timestamp(now.isoformat())


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


def _due_windows(store: Store, programs: Mapping[str, Program], file: str, now: datetime) -> list[tuple[str, str, str]]:
    history = state(store, file)
    return [
        (name, *window(store, name, now))
        for name, program in sorted(programs.items())
        if _due(program, history.get(name, {}), now)
    ]


def due_routines(store: Store, now: datetime) -> list[tuple[str, str, str]]:
    """Due routines; each covers the time since the end of its last reviewed window."""
    return _due_windows(store, load(store).routines, ROUTINES, now)


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
