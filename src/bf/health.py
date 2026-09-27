"""Collection coverage shared by status and retrieval; freshness never implies complete history."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import cast

from bf import index, usage
from bf.collect import ROUTINES, log_path, state
from bf.config import load
from bf.models import Error, Program
from bf.storage import Store


def _freshness(program: Program, success: str, now: datetime) -> str:
    """Scheduled programs are fresh within twice their refresh, based on local run history."""
    if not program.refresh:
        return "manual"
    if not success:
        return "never"
    return "stale" if datetime.fromisoformat(success) < now - timedelta(seconds=2 * program.refresh) else "fresh"


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


def routine_health(store: Store, *, now: datetime | None = None) -> dict[str, dict[str, object]]:
    """Each configured routine's last run, error, latest action and freshness."""
    now = now or datetime.now(UTC)
    config, history = load(store), state(store, ROUTINES)
    result: dict[str, dict[str, object]] = {}
    for name, settings in sorted(config.routines.items()):
        entry = history.get(name, {})
        success = str(entry.get("success", ""))
        item: dict[str, object] = {"enabled": settings.enabled, "freshness": "unknown"}
        if settings.enabled:
            item["freshness"] = _freshness(settings, success, now)
        for key in ("run", "success", "error", "action"):
            if entry.get(key):
                item[key] = entry[key]
        if entry.get("error"):
            item["log"] = str(log_path(store, name))
        result[name] = item
    return result


def attention(store: Store, now: datetime | None = None) -> list[dict[str, object]]:
    """Scheduled sensors and routines this machine runs that failed or have not succeeded recently."""
    now = now or datetime.now(UTC)
    config = load(store)
    result: list[dict[str, object]] = []
    for name, health in source_health(store, now=now).items():
        settings = config.sensors.get(name)
        if (
            settings
            and settings.enabled
            and settings.refresh
            and (health.get("failed") or health["freshness"] in {"never", "stale"})
        ):
            result.append(
                {"sensor": name, "freshness": health["freshness"], **({"failed": True} if health.get("failed") else {})}
            )
    for name, health in routine_health(store, now=now).items():
        settings = config.routines[name]
        if settings.enabled and settings.refresh and (health.get("error") or health["freshness"] in {"never", "stale"}):
            result.append(
                {"routine": name, "freshness": health["freshness"], **({"failed": True} if health.get("error") else {})}
            )
    return result


def report(stores: list[Store], now: datetime | None = None) -> dict[str, object]:
    """Per brain: cache, notes, records, source and routine freshness, errors, logs and usage."""
    now = now or datetime.now(UTC)
    brains, healthy = [], True
    for store in stores:
        try:
            config, history, summary = load(store), state(store), index.status(store)
        except (Error, OSError, UnicodeError, sqlite3.DatabaseError) as error:
            if len(stores) == 1:
                if isinstance(error, sqlite3.DatabaseError):
                    raise Error("the search cache is unavailable; run bf build") from error
                raise
            # Status diagnoses brains: one that cannot load is reported, and the others still are.
            message = str(error) if isinstance(error, Error) else "inaccessible brain or cache; check its path"
            brains.append({"brain": store.root.name, "path": str(store.root), "error": message})
            healthy = False
            continue
        counts = cast("dict[str, dict[str, object]]", summary.pop("sources"))
        coverage = source_health(store, counts, now=now)
        sources: dict[str, dict[str, object]] = {}
        for name in sorted({*config.sensors, *counts}):
            settings = config.sensors.get(name)
            run = history.get(name, {})
            counters = {
                key: run[key]
                for key in (
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
                if key in run
            }
            entry: dict[str, object] = {
                **{key: value for key, value in run.items() if key not in counters},
                **counts.get(name, {"records": 0}),
                **coverage[name],
            }
            if counters:
                entry["last_run"] = counters
            if settings is None:
                entry["configured"] = False
            else:
                entry["enabled"] = settings.enabled
                if settings.enabled and settings.refresh:
                    entry["stale"] = coverage[name]["freshness"] in {"never", "stale"}
                    healthy &= not entry["stale"]
                if entry.get("error"):
                    entry["log"] = str(log_path(store, name))
                    # Like routines, only a scheduled sensor this machine runs fails the check.
                    healthy &= not (settings.enabled and settings.refresh)
            sources[name] = {key: value for key, value in entry.items() if value != ""}
        routines = routine_health(store, now=now)
        for name, entry in routines.items():
            settings = config.routines[name]
            if settings.enabled and settings.refresh:
                entry["stale"] = entry["freshness"] in {"never", "stale"}
                healthy &= not entry["stale"] and not entry.get("error")
        healthy &= not summary["problems"] and summary["index"] == "ready"
        brains.append(
            {
                "brain": config.name,
                "path": str(store.root),
                **summary,
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
    return {"healthy": healthy, "brains": brains}
