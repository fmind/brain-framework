"""Collect every due sensor, run every due routine and assistant of one brain, then refresh its search cache."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import UTC, datetime
from typing import Literal

from bf import assistants as assistant_runtime
from bf import index
from bf.collect import Runner, collect, due, due_routines, enabled, hooked, routine, run, window
from bf.config import load
from bf.models import Config, Error, suggest
from bf.storage import BusyError, Store, collecting

# Seconds a cycle waits for another update of the same brain before failing visibly.
WAIT = 600
ProgramKind = Literal["sensor", "routine", "assistant"]
KINDS: frozenset[ProgramKind] = frozenset({"sensor", "routine", "assistant"})


def _failure(error: Error | OSError | UnicodeError) -> str:
    return str(error) if isinstance(error, Error) else "inaccessible files; check permissions and free space"


def selection(
    config: Config,
    sensors: tuple[str, ...] = (),
    routines: tuple[str, ...] = (),
    assistants: tuple[str, ...] = (),
    kinds: frozenset[ProgramKind] = KINDS,
) -> tuple[set[str], set[str], set[str]]:
    """Explicit lists select only the programs they name; with no list at all, every program is selected. `kinds`
    keeps only those kinds of program, as watch does to run assistants beside collection."""
    for kind, names, known in (
        ("sensor", sensors, config.sensors),
        ("routine", routines, config.routines),
        ("assistant", assistants, config.assistants),
    ):
        if unknown := sorted(set(names) - known.keys()):
            raise Error(f"unknown {kind} {unknown[0]}; check names in bf.yaml{suggest(unknown[0], known)}")
    if not (sensors or routines or assistants):
        sensors, routines, assistants = tuple(config.sensors), tuple(config.routines), tuple(config.assistants)
    return (
        set(sensors) if "sensor" in kinds else set(),
        set(routines) if "routine" in kinds else set(),
        set(assistants) if "assistant" in kinds else set(),
    )


def scheduled(
    config: Config, sensors: tuple[str, ...] = (), routines: tuple[str, ...] = (), assistants: tuple[str, ...] = ()
) -> list[tuple[int, str]]:
    """The selected enabled programs that refresh on their own, as (refresh seconds, name), fastest first."""
    selected_sensors, selected_routines, selected_assistants = selection(config, sensors, routines, assistants)
    programs = [
        *((name, config.sensors[name]) for name in selected_sensors),
        *((name, config.routines[name]) for name in selected_routines),
    ]
    intervals = [(program.refresh, name) for name, program in programs if program.enabled and program.refresh]
    for name in selected_assistants:
        assistant = config.assistants[name]
        if assistant.enabled:
            intervals.extend((schedule.every, name) for schedule in assistant.schedules.values())
            intervals.extend((item.channel.every, name) for item in assistant.inputs if item.channel.mode == "read")
    return sorted(intervals)


def late(
    config: Config,
    every: int,
    sensors: tuple[str, ...] = (),
    routines: tuple[str, ...] = (),
    assistants: tuple[str, ...] = (),
    *,
    include_assistants: bool = True,
) -> str:
    """Why checking every `every` seconds runs the fastest selected program late, or ""."""
    programs = scheduled(config, sensors, routines, assistants)
    if not include_assistants:
        programs = [(every, name) for every, name in programs if name not in config.assistants]
    if not programs or every < programs[0][0]:
        return ""
    refresh, name = programs[0]
    return (
        f"{name} refreshes every {refresh}s, but checks happen every {every}s: it will run late and status can "
        "report it overdue"
    )


@contextmanager
def _cycle(store: Store, wait: float) -> Generator[None]:
    """Serialize collection cycles of one brain; assistant work runs outside it, so a long agent run never blocks
    collection."""
    with ExitStack() as stack:
        try:
            # Program names cannot start with a dot; this cannot collide with a program's lock.
            stack.enter_context(collecting(store, ".update", wait=wait))
        except BusyError as error:
            raise Error("another update is still active; retry after it finishes") from error
        yield


def update(
    store: Store,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    runner: Runner = run,
    sensors: tuple[str, ...] = (),
    routines: tuple[str, ...] = (),
    assistants: tuple[str, ...] = (),
    kinds: frozenset[ProgramKind] = KINDS,
    wait: float = WAIT,
) -> dict[str, object]:
    """Run one brain's due programs; like collect, a brain that cannot start its cycle fails with an error.

    One failing program never blocks the others; the report names it and its private log. Routines run after the
    sensors, and assistant work after both, so each sees this run's evidence. Collection cycles are serialized per
    brain so watch and timers cannot repeat a stale due list: sensors and routines hold the brain's update lock,
    waiting up to `wait` seconds for another cycle of the same brain, such as a schedule for another selection firing
    at the same minute, then compute their due list inside it. Assistant work, due batch reads, schedules, queued jobs
    and deliveries, runs after its release.
    """
    given = now
    config = load(store)
    # Validate the selection before waiting for the lock or executing anything.
    selected_sensors, selected_routines, selected_assistants = selection(config, sensors, routines, assistants, kinds)
    failed = False
    results: list[dict[str, object]] = []
    ran: list[dict[str, object]] = []
    collecting_programs = not dry_run and bool(selected_sensors or selected_routines)
    with _cycle(store, wait) if collecting_programs else nullcontext():
        # Dated once the cycle lock is held: waiting for another update must not leave due work for the next one.
        now = given or datetime.now(UTC)
        windows = [item for item in due(store, now) if item[0] in selected_sensors]
        routine_windows = [item for item in due_routines(store, now) if item[0] in selected_routines]
        for sensor, start, end, reconcile in windows:
            result: dict[str, object] = {"sensor": sensor, "start": start, "end": end, "reconcile": reconcile}
            if dry_run:
                results.append({**result, "status": "due"})
                continue
            try:
                result.update(
                    collect(store, sensor, start=start, end=end, reconcile=reconcile, runner=runner, clock=lambda: now)
                )
                result["status"] = "collected"
            except BusyError:
                # Another process, such as a manual bf collect, is running it: its run covers this cycle.
                result.update(status="skipped", skipped="another run of this sensor is still active")
            except (Error, OSError, UnicodeError) as error:
                result.update(status="failed", error=_failure(error))
                failed = True
            results.append(result)
        for program, start, end in routine_windows:
            result = {"routine": program, "start": start, "end": end}
            if dry_run:
                ran.append({**result, "status": "due"})
                continue
            try:
                result.update(routine(store, program, start=start, end=end, runner=runner, clock=lambda: now))
                result["status"] = "skipped" if "skipped" in result else "ran"
            except BusyError:
                result.update(status="skipped", skipped="another run of this routine is still active")
            except (Error, OSError, UnicodeError) as error:
                result.update(status="failed", error=_failure(error))
                failed = True
            ran.append(result)
    asked = assistant_runtime.cycle(
        store, selected_assistants, at=given or datetime.now(UTC), dry_run=dry_run, runner=runner
    )
    failed |= any(item["status"] == "failed" for item in asked)
    report: dict[str, object] = {"brain": config.name, "sensors": results, "routines": ran}
    if config.assistants:
        report["assistants"] = asked
    # Enabled programs with refresh: 0 never run here: name them, so an empty cycle is not mistaken for current evidence.
    manual = sorted(
        name
        for name, program in (*config.sensors.items(), *config.routines.items())
        if program.enabled
        and not program.refresh
        and name in selected_sensors | selected_routines | selected_assistants
    )
    if manual:
        report["manual"] = manual
    # An assistant-only cycle that ran nothing changed nothing to index.
    if not dry_run and (kinds & {"sensor", "routine"} or any(item["status"] == "ran" for item in asked)):
        try:
            report["index"] = index.refresh(store, wait=120, recover=True)
            failed |= bool(report["index"]["skipped"])
        except (Error, OSError, UnicodeError) as error:
            report["index"] = {"error": _failure(error)}
            failed = True
    return {"ok": not failed, "dry_run": dry_run, **report}


def run_routines(
    store: Store,
    *,
    names: tuple[str, ...] = (),
    hook: str = "",
    args: tuple[str, ...] = (),
    stdin: bytes = b"",
    dry_run: bool = False,
) -> dict[str, object]:
    """Run named routines, or every enabled routine of a hook, now; one failure never blocks the others.

    Each covers the time since its last reviewed window and receives the same arguments and input. An unknown or
    disabled name fails before any routine runs. A hook that no routine lists runs nothing and succeeds, so a Git
    hook can call it before any routine exists.
    """
    now = datetime.now(UTC)
    config = load(store)
    for name in names:
        if name not in config.routines and name not in config.assistants:
            runnable = [
                known for known, program in (*config.routines.items(), *config.assistants.items()) if program.enabled
            ]
            raise Error(f"unknown routine or assistant {name}; check names in bf.yaml{suggest(name, runnable)}")
        enabled(config.routines, "routine", name)
    selected = list(names) if names else hooked(config, hook)
    results: list[dict[str, object]] = []
    failed = False
    for name in selected:
        start, end = window(store, name, now)
        result: dict[str, object] = {"routine": name}
        try:
            result.update(
                routine(store, name, start=start, end=end, dry_run=dry_run, args=args, stdin=stdin, clock=lambda: now)
            )
            result["status"] = "skipped" if "skipped" in result else "ran"
        except (Error, OSError, UnicodeError) as error:
            result.update(status="failed", error=_failure(error))
            failed = True
        results.append(result)
    return {"ok": not failed, "dry_run": dry_run, **({"hook": hook} if hook else {}), "routines": results}


def run_assistant(store: Store, name: str, *, text: str = "", dry_run: bool = False) -> dict[str, object]:
    """Run one assistant now; its named schedules and subscriptions keep independent progress."""
    return assistant_runtime.manual(store, name, text=text, dry_run=dry_run)
