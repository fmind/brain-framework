"""Collect every due sensor and run every due routine of the brains trusted here, then refresh their caches."""

from __future__ import annotations

from datetime import UTC, datetime

from bf import index
from bf.collect import Runner, collect, due, due_routines, routine, run
from bf.config import load, may_collect
from bf.models import Error
from bf.storage import Store


def _failure(error: Error | OSError | UnicodeError) -> str:
    return str(error) if isinstance(error, Error) else "inaccessible files; check permissions and free space"


def update(
    stores: list[Store], *, dry_run: bool = False, now: datetime | None = None, runner: Runner = run
) -> dict[str, object]:
    """One failing sensor or routine never blocks the others; the report names it and its private log.

    Routines run after the sensors, so they see this run's evidence.
    """
    now = now or datetime.now(UTC)
    brains: list[dict[str, object]] = []
    failed = False
    for store in stores:
        try:
            name = load(store).name
            if not may_collect(store):
                brains.append({"brain": name, "skipped": "not trusted to collect on this machine"})
                continue
            windows = due(store, now)
            routines = due_routines(store, now)
        except (Error, OSError, UnicodeError) as error:
            brains.append({"brain": store.root.name, "error": _failure(error)})
            failed = True
            continue
        results: list[dict[str, object]] = []
        for sensor, start, end in windows:
            result: dict[str, object] = {"sensor": sensor, "start": start, "end": end}
            if dry_run:
                results.append({**result, "status": "due"})
                continue
            try:
                result.update(collect(store, sensor, start=start, end=end, runner=runner, clock=lambda: now))
                result["status"] = "collected"
            except (Error, OSError, UnicodeError) as error:
                result.update(status="failed", error=_failure(error))
                failed = True
            results.append(result)
        ran: list[dict[str, object]] = []
        for program, start, end in routines:
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
