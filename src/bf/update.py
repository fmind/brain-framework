"""Collect every due sensor and run every due routine of the selected brains, then refresh their caches."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from bf import index
from bf.collect import Runner, collect, due, due_routines, routine, run
from bf.config import load
from bf.models import Config, Error
from bf.storage import BusyError, Store, collecting


def _failure(error: Error | OSError | UnicodeError) -> str:
    return str(error) if isinstance(error, Error) else "inaccessible files; check permissions and free space"


def selection(
    config: Config, sensors: tuple[str, ...] = (), routines: tuple[str, ...] = ()
) -> tuple[set[str], set[str]]:
    """An explicit list selects only those programs; omitted lists select all programs."""
    if set(sensors) - config.sensors.keys() or set(routines) - config.routines.keys():
        raise Error("unknown sensor or routine selection; check names in bf.yaml")
    if sensors or routines:
        return set(sensors), set(routines)
    return set(config.sensors), set(config.routines)


def _update(
    stores: list[Store],
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    runner: Runner = run,
    sensors: tuple[str, ...] = (),
    routines: tuple[str, ...] = (),
) -> dict[str, object]:
    """One failing sensor or routine never blocks the others; the report names it and its private log.

    Routines run after the sensors, so they see this run's evidence.
    """
    now = now or datetime.now(UTC)
    brains: list[dict[str, object]] = []
    failed = False
    for store in stores:
        try:
            config = load(store)
            name = config.name
            selected_sensors, selected_routines = selection(config, sensors, routines)
            windows = [item for item in due(store, now) if item[0] in selected_sensors]
            routine_windows = [item for item in due_routines(store, now) if item[0] in selected_routines]
        except (Error, OSError, UnicodeError) as error:
            brains.append({"brain": store.root.name, "error": _failure(error)})
            failed = True
            continue
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
        report: dict[str, object] = {"brain": name, "sensors": results}
        if ran:
            report["routines"] = ran
        if not dry_run:
            try:
                report["index"] = index.refresh(store, wait=120)
                failed |= bool(report["index"]["problems"])
            except (Error, OSError, UnicodeError) as error:
                report["index"] = {"error": _failure(error)}
                failed = True
        brains.append(report)
    return {"ok": not failed, "dry_run": dry_run, "brains": brains}


def update(
    stores: list[Store],
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    runner: Runner = run,
    sensors: tuple[str, ...] = (),
    routines: tuple[str, ...] = (),
) -> dict[str, object]:
    """Serialize update cycles per brain so watch and timers cannot repeat a stale due list."""
    # Validate all roots before executing anything in the first one.
    if sensors or routines:
        for store in stores:
            selection(load(store), sensors, routines)
    if dry_run:
        return _update(stores, dry_run=True, now=now, runner=runner, sensors=sensors, routines=routines)
    reports: list[object] = []
    ok = True
    for store in stores:
        try:
            # Program names cannot start with a dot; this cannot collide with a sensor's lock.
            with collecting(store, ".update"):
                result = _update([store], now=now, runner=runner, sensors=sensors, routines=routines)
            reports.extend(cast("list[object]", result["brains"]))
            ok &= bool(result["ok"])
        except BusyError:
            reports.append({"brain": store.root.name, "error": "another update is active; retry after it finishes"})
            ok = False
    return {"ok": ok, "dry_run": False, "brains": reports}
