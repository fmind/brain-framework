"""Collect every due sensor and run every due routine of one brain, then refresh its search cache."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime

from bf import index
from bf.collect import Runner, collect, due, due_routines, enabled, hooked, routine, run, window
from bf.config import load
from bf.models import Config, Error, suggest
from bf.storage import BusyError, Store, collecting

# Seconds a cycle waits for another update of the same brain before failing visibly.
WAIT = 600


def _failure(error: Error | OSError | UnicodeError) -> str:
    return str(error) if isinstance(error, Error) else "inaccessible files; check permissions and free space"


def selection(
    config: Config, sensors: tuple[str, ...] = (), routines: tuple[str, ...] = ()
) -> tuple[set[str], set[str]]:
    """An explicit list selects only those programs; omitted lists select all programs."""
    for kind, names, known in (("sensor", sensors, config.sensors), ("routine", routines, config.routines)):
        if unknown := sorted(set(names) - known.keys()):
            raise Error(f"unknown {kind} {unknown[0]}; check names in bf.yaml{suggest(unknown[0], known)}")
    if sensors or routines:
        return set(sensors), set(routines)
    return set(config.sensors), set(config.routines)


def _update(
    store: Store,
    *,
    dry_run: bool,
    now: datetime | None,
    runner: Runner,
    sensors: tuple[str, ...],
    routines: tuple[str, ...],
) -> dict[str, object]:
    """One failing sensor or routine never blocks the others; the report names it and its private log.

    Routines run after the sensors, so they see this run's evidence.
    """
    now = now or datetime.now(UTC)
    config = load(store)
    selected_sensors, selected_routines = selection(config, sensors, routines)
    windows = [item for item in due(store, now) if item[0] in selected_sensors]
    routine_windows = [item for item in due_routines(store, now) if item[0] in selected_routines]
    failed = False
    results: list[dict[str, object]] = []
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
    ran: list[dict[str, object]] = []
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
    report: dict[str, object] = {"brain": config.name, "sensors": results, "routines": ran}
    # Enabled programs with refresh: 0 never run here: name them, so an empty cycle is not mistaken for current evidence.
    manual = sorted(
        name
        for name, program in (*config.sensors.items(), *config.routines.items())
        if program.enabled and not program.refresh and name in selected_sensors | selected_routines
    )
    if manual:
        report["manual"] = manual
    if not dry_run:
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
    wait: float = WAIT,
) -> dict[str, object]:
    """Run one brain's due programs; like collect, a brain that cannot start its cycle fails with an error.

    Cycles are serialized per brain so watch and timers cannot repeat a stale due list. A cycle waits up to
    `wait` seconds for another cycle of the same brain, such as a schedule for another selection firing at
    the same minute, then computes its due list inside the lock.
    """
    if dry_run:
        return _update(store, dry_run=True, now=now, runner=runner, sensors=sensors, routines=routines)
    # Validate the selection before waiting for the lock or executing anything.
    selection(load(store), sensors, routines)
    with ExitStack() as cycle:
        try:
            # Program names cannot start with a dot; this cannot collide with a sensor's lock.
            cycle.enter_context(collecting(store, ".update", wait=wait))
        except BusyError as error:
            raise Error("another update is still active; retry after it finishes") from error
        return _update(store, dry_run=False, now=now, runner=runner, sensors=sensors, routines=routines)


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
