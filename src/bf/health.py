"""Collection coverage shared by status and retrieval; freshness never implies complete history."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import cast

from bf import index, usage
from bf.config import Selection, brain_name, load, related
from bf.history import ROUTINES, log_path, state
from bf.models import CACHE, Config, Error, Program
from bf.storage import Store

# Counters of the last successful collection, reported apart from indexed totals.
_COUNTERS = (
    "records",
    "added",
    "updated",
    "unchanged",
    "removed",
    "requested_start",
    "requested_end",
    "reconcile",
    "elapsed_seconds",
    "output_bytes",
)


# Freshness of a scheduled program that has not succeeded within twice its refresh, or ever.
_LATE = frozenset({"overdue", "never"})


def _freshness(program: Program, success: str, now: datetime) -> str:
    """Scheduled programs are fresh within twice their refresh, based on local run history."""
    if not program.refresh:
        return "manual"
    if not success:
        return "never"
    elapsed = now - datetime.fromisoformat(success)
    return "fresh" if timedelta(0) <= elapsed <= timedelta(seconds=2 * program.refresh) else "overdue"


def source_health(
    store: Store, names: Iterable[str] = (), *, now: datetime | None = None
) -> dict[str, dict[str, object]]:
    """Describe active, disabled and historical sources without running them or exposing logs."""
    now = now or datetime.now(UTC)
    config, history = load(store), state(store)
    result: dict[str, dict[str, object]] = {}
    for name in sorted({*config.sensors, *names}):
        settings = config.sensors.get(name)
        entry = history.get(name, {})
        success = str(entry.get("success", ""))
        item: dict[str, object] = {
            "state": "historical" if settings is None else "active" if settings.enabled else "disabled",
            "freshness": "unknown",
        }
        if success:
            # Run history already holds canonical UTC instants.
            item["last_collected"] = success
        if settings is not None:
            item["mode"] = settings.mode
        if (settings is None or settings.mode == "window") and entry.get("start") and entry.get("end"):
            item["window"] = {"since": entry["start"], "until": entry["end"]}
        if entry.get("error"):
            item["failed"] = True
        if settings is not None and settings.enabled:
            item["freshness"] = _freshness(settings, success, now)
        result[name] = item
    return result


def _failure(name: str, entry: dict[str, object]) -> dict[str, object]:
    """The last attempt's error, consecutive failures and log; empty after a success."""
    if not entry.get("error"):
        return {}
    return {
        "failed": True,
        "error": entry["error"],
        "failures": entry.get("failures", 1),
        "log": log_path(name),
    }


def routine_health(store: Store, *, now: datetime | None = None) -> dict[str, dict[str, object]]:
    """Each configured routine's state, freshness, last success, latest action and failure."""
    now = now or datetime.now(UTC)
    config, history = load(store), state(store, ROUTINES)
    result: dict[str, dict[str, object]] = {}
    for name, settings in sorted(config.routines.items()):
        entry = history.get(name, {})
        success = str(entry.get("success", ""))
        item: dict[str, object] = {
            "state": "active" if settings.enabled else "disabled",
            "freshness": _freshness(settings, success, now) if settings.enabled else "unknown",
        }
        if success:
            item["last_success"] = success
        if entry.get("action"):
            item["action"] = entry["action"]
        result[name] = {**item, **_failure(name, entry)}
    return result


def _attention(
    config: Config, sources: dict[str, dict[str, object]], routines: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    """Scheduled programs this machine runs that failed or have not succeeded recently: only those are late."""
    result: list[dict[str, object]] = []
    for kind, healths, programs in (("sensor", sources, config.sensors), ("routine", routines, config.routines)):
        for name, health in healths.items():
            settings, failed = programs.get(name), health.get("failed")
            if settings and settings.enabled and settings.refresh and (failed or health["freshness"] in _LATE):
                result.append({kind: name, "freshness": health["freshness"], **({"failed": True} if failed else {})})
    return result


def attention(store: Store, now: datetime | None = None) -> list[dict[str, object]]:
    """Scheduled sensors and routines this machine runs that failed or have not succeeded recently."""
    now = now or datetime.now(UTC)
    return _attention(load(store), source_health(store, now=now), routine_health(store, now=now))


def report(stores: list[Store], now: datetime | None = None) -> dict[str, object]:
    """Per brain: cache, notes, records, source and routine freshness, errors, logs and usage."""
    now = now or datetime.now(UTC)
    unavailable = stores.unavailable if isinstance(stores, Selection) else {}
    brains: list[dict[str, object]] = [{"brain": name, "error": error} for name, error in unavailable.items()]
    healthy = not brains
    for store in stores:
        try:
            config, history, summary = load(store), state(store), index.status(store)
        except (Error, OSError, UnicodeError, sqlite3.DatabaseError) as error:
            if len(stores) == 1:
                if isinstance(error, sqlite3.DatabaseError):
                    raise Error(CACHE) from error
                raise
            # Status diagnoses brains: one that cannot load is reported, and the others still are.
            message = str(error) if isinstance(error, Error) else "inaccessible brain or cache; check its path"
            brains.append({"brain": brain_name(store), "path": str(store.root), "error": message})
            healthy = False
            continue
        counts = cast("dict[str, dict[str, object]]", summary.pop("sources"))
        coverage = source_health(store, counts, now=now)
        sources: dict[str, dict[str, object]] = {}
        for name in sorted({*config.sensors, *counts}):
            run = history.get(name, {})
            # One shape shared with retrieval coverage, plus indexed totals and local run diagnostics.
            entry: dict[str, object] = {**coverage[name], **counts.get(name, {"records": 0, "bytes": 0})}
            if counters := {key: run[key] for key in _COUNTERS if key in run}:
                entry["last_run"] = counters
            if run.get("reconciled"):
                entry["reconciled"] = run["reconciled"]
            entry.update(_failure(name, run))
            sources[name] = {key: value for key, value in entry.items() if value != ""}
        routines = routine_health(store, now=now)
        # The reasons a brain is unhealthy, beside its problems: the home page's list of late or failed programs.
        alerts = _attention(config, coverage, routines)
        # A busy cache still serves its last generation, marked stale in replies, while a live writer finishes.
        healthy &= not alerts and not summary["problems"] and summary["cache"] in {"ready", "busy"}
        brains.append(
            {
                "brain": config.name,
                "path": str(store.root),
                **summary,
                "attention": alerts,
                "sources": sources,
                **({"routines": routines} if routines else {}),
                "coverage": {
                    kind: {
                        "sources": sum(value["state"] == kind for value in coverage.values()),
                        "records": sum(
                            int(cast("int", counts.get(name, {}).get("records", 0)))
                            for name, value in coverage.items()
                            if value["state"] == kind
                        ),
                    }
                    for kind in ("active", "disabled", "historical")
                },
                "usage": usage.summary(store),
            }
        )
    # A reference that retrieval cannot include makes every search and read incomplete, like a skipped file.
    for problem in related(stores)[1]:
        if problem["brain"] in unavailable:
            continue
        healthy = False
        owners = [entry for entry in brains if entry["brain"] == problem["brain"] and "problems" in entry]
        for entry in owners:
            cast("list[dict[str, object]]", entry["problems"]).append(
                {key: value for key, value in problem.items() if key != "brain"}
            )
        if not owners:
            # An ambiguous name may belong to no reported brain: report it like an unavailable one.
            brains.append({"brain": problem["brain"], "error": problem["error"]})
    return {"healthy": healthy, "brains": brains}
