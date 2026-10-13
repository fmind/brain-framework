"""Collect every due sensor, run every due routine and prompt of one brain, then refresh its search cache."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import UTC, datetime
from typing import Literal

from bf import index
from bf.collect import Runner, collect, due, due_prompts, due_routines, enabled, hooked, prompt, routine, run, window
from bf.config import load
from bf.models import Config, Error, suggest
from bf.storage import BusyError, Store, collecting

# Seconds a cycle waits for another update of the same brain before failing visibly.
WAIT = 600
ProgramKind = Literal["sensor", "routine", "prompt"]
KINDS: frozenset[ProgramKind] = frozenset({"sensor", "routine", "prompt"})


def _failure(error: Error | OSError | UnicodeError) -> str:
    return str(error) if isinstance(error, Error) else "inaccessible files; check permissions and free space"


def selection(
    config: Config,
    sensors: tuple[str, ...] = (),
    routines: tuple[str, ...] = (),
    prompts: tuple[str, ...] = (),
    kinds: frozenset[ProgramKind] = KINDS,
) -> tuple[set[str], set[str], set[str]]:
    """An explicit list selects only those programs; omitted lists select all programs. `kinds` keeps only those
    kinds of program, as watch does to run prompts beside collection."""
    for kind, names, known in (
        ("sensor", sensors, config.sensors),
        ("routine", routines, config.routines),
        ("prompt", prompts, config.prompts),
    ):
        if unknown := sorted(set(names) - known.keys()):
            raise Error(f"unknown {kind} {unknown[0]}; check names in bf.yaml{suggest(unknown[0], known)}")
    if not (sensors or routines or prompts):
        sensors, routines, prompts = tuple(config.sensors), tuple(config.routines), tuple(config.prompts)
    return (
        set(sensors) if "sensor" in kinds else set(),
        set(routines) if "routine" in kinds else set(),
        set(prompts) if "prompt" in kinds else set(),
    )


def scheduled(
    config: Config, sensors: tuple[str, ...] = (), routines: tuple[str, ...] = (), prompts: tuple[str, ...] = ()
) -> list[tuple[int, str]]:
    """The selected enabled programs that refresh on their own, as (refresh seconds, name), fastest first."""
    selected_sensors, selected_routines, selected_prompts = selection(config, sensors, routines, prompts)
    programs = [
        *((name, config.sensors[name]) for name in selected_sensors),
        *((name, config.routines[name]) for name in selected_routines),
        *((name, config.prompts[name]) for name in selected_prompts),
    ]
    return sorted((program.refresh, name) for name, program in programs if program.enabled and program.refresh)


def late(
    config: Config,
    every: int,
    sensors: tuple[str, ...] = (),
    routines: tuple[str, ...] = (),
    prompts: tuple[str, ...] = (),
) -> str:
    """Why checking every `every` seconds runs the fastest selected program late, or ""."""
    programs = scheduled(config, sensors, routines, prompts)
    if not programs or every < programs[0][0]:
        return ""
    refresh, name = programs[0]
    return (
        f"{name} refreshes every {refresh}s, but checks happen every {every}s: it will run late and status can "
        "report it overdue"
    )


@contextmanager
def _cycle(store: Store, wait: float) -> Iterator[None]:
    """Serialize collection cycles of one brain; prompts run outside it, so a long agent run never blocks collection."""
    with ExitStack() as stack:
        try:
            # Program names cannot start with a dot; this cannot collide with a program's lock.
            stack.enter_context(collecting(store, ".update", wait=wait))
        except BusyError as error:
            raise Error("another update is still active; retry after it finishes") from error
        yield


def _update(
    store: Store,
    *,
    dry_run: bool,
    now: datetime | None,
    runner: Runner,
    sensors: tuple[str, ...],
    routines: tuple[str, ...],
    prompts: tuple[str, ...] = (),
    kinds: frozenset[ProgramKind] = KINDS,
    wait: float = 0,
) -> dict[str, object]:
    """One failing program never blocks the others; the report names it and its private log.

    Routines run after the sensors, and prompts after both, so each sees this run's evidence. Sensors and routines
    hold the brain's update lock, computing their due list inside it; prompts run after its release, each skipping
    while a scheduled run of it is still active.
    """
    given = now
    now = now or datetime.now(UTC)
    config = load(store)
    selected_sensors, selected_routines, selected_prompts = selection(config, sensors, routines, prompts, kinds)
    failed = False
    results: list[dict[str, object]] = []
    ran: list[dict[str, object]] = []
    collecting_programs = not dry_run and bool(selected_sensors or selected_routines)
    with _cycle(store, wait) if collecting_programs else nullcontext():
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
            except (Error, OSError, UnicodeError) as error:
                result.update(status="failed", error=_failure(error))
                failed = True
            ran.append(result)
    asked: list[dict[str, object]] = []
    # Sensors and earlier prompts can run for minutes: each prompt is due, and dated, by the time it starts.
    clock = (lambda: given) if given else (lambda: datetime.now(UTC))
    for program, start, end in [item for item in due_prompts(store, clock()) if item[0] in selected_prompts]:
        result = {"prompt": program, "start": start, "end": end}
        if dry_run:
            asked.append({**result, "status": "due"})
            continue
        try:
            result.update(prompt(store, program, start=start, end=end, scheduled=True, runner=runner, clock=clock))
            result.pop("text", None)
            result["status"] = "skipped" if "skipped" in result else "ran"
        except BusyError:
            # A scheduled run still answering keeps its window: the next cycle covers this one too.
            result.update(status="skipped", skipped="a scheduled run of this prompt is still active")
        except (Error, OSError, UnicodeError) as error:
            result.update(status="failed", error=_failure(error))
            failed = True
        asked.append(result)
    report: dict[str, object] = {"brain": config.name, "sensors": results, "routines": ran}
    if config.prompts:
        report["prompts"] = asked
    # Enabled programs with refresh: 0 never run here: name them, so an empty cycle is not mistaken for current evidence.
    manual = sorted(
        name
        for name, program in (*config.sensors.items(), *config.routines.items(), *config.prompts.items())
        if program.enabled and not program.refresh and name in selected_sensors | selected_routines | selected_prompts
    )
    if manual:
        report["manual"] = manual
    # A prompt-only cycle that ran nothing changed nothing to index.
    if not dry_run and (kinds & {"sensor", "routine"} or any(item["status"] == "ran" for item in asked)):
        try:
            report["index"] = index.refresh(store, wait=120, recover=True)
            failed |= bool(report["index"]["skipped"])
        except (Error, OSError, UnicodeError) as error:
            report["index"] = {"error": _failure(error)}
            failed = True
    return {"ok": not failed, "dry_run": dry_run, **report}


def update(
    store: Store,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    runner: Runner = run,
    sensors: tuple[str, ...] = (),
    routines: tuple[str, ...] = (),
    prompts: tuple[str, ...] = (),
    kinds: frozenset[ProgramKind] = KINDS,
    wait: float = WAIT,
) -> dict[str, object]:
    """Run one brain's due programs; like collect, a brain that cannot start its cycle fails with an error.

    Collection cycles are serialized per brain so watch and timers cannot repeat a stale due list. A cycle waits up
    to `wait` seconds for another cycle of the same brain, such as a schedule for another selection firing at the
    same minute, then computes its due list inside the lock.
    """
    # Validate the selection before waiting for the lock or executing anything.
    selection(load(store), sensors, routines, prompts, kinds)
    return _update(
        store,
        dry_run=dry_run,
        now=now,
        runner=runner,
        sensors=sensors,
        routines=routines,
        prompts=prompts,
        kinds=kinds,
        wait=wait,
    )


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


def run_prompt(store: Store, name: str, *, text: str = "", dry_run: bool = False) -> dict[str, object]:
    """Run one prompt now with `text` as its {{input}}, such as an owner's message, and reply with its answer.

    It never waits for a scheduled run of the same prompt and leaves its schedule unchanged.
    """
    now = datetime.now(UTC)
    enabled(load(store).prompts, "prompt", name)
    start, end = window(store, name, now)
    result: dict[str, object] = {"prompt": name}
    try:
        result.update(prompt(store, name, start=start, end=end, text=text, dry_run=dry_run, clock=lambda: now))
        result["status"] = "ran"
    except (Error, OSError, UnicodeError) as error:
        result.update(status="failed", error=_failure(error))
    return {"ok": result["status"] == "ran", "dry_run": dry_run, "prompts": [result]}
