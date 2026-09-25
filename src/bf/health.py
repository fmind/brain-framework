"""Collection coverage shared by status and retrieval; freshness never implies complete history."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import cast

from bf import index, usage
from bf.collect import ROUTINES, log_path, state
from bf.config import load, may_collect
from bf.models import Error, Program
from bf.storage import Store


def _freshness(program: Program, success: str, now: datetime, trusted: bool) -> str:
    """Scheduled programs are fresh within twice their refresh; freshness is unknown where they may not run."""
    if not program.refresh:
        return "manual"
    if not (trusted or success):
        return "unknown"
    if not success:
        return "never"
    return "stale" if datetime.fromisoformat(success) < now - timedelta(seconds=2 * program.refresh) else "fresh"


def source_health(
    store: Store, names: Iterable[str] = (), *, now: datetime | None = None, trusted: bool | None = None
) -> dict[str, dict[str, object]]:
    """Describe active, disabled and historical sources without running them or exposing logs.

    `trusted` defaults to this machine's collection trust, so every reply reports the same freshness.
    """
    now = now or datetime.now(UTC)
    if trusted is None:
        try:
            trusted = may_collect(store)
        except Error, OSError, UnicodeError:
            # An unreadable registry grants nothing: freshness stays unknown and the read still answers.
            trusted = False
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
        # Historical sources have no reviewed declaration left: their text is external.
        item["trust"] = settings.trust if settings is not None else "external"
        if settings is not None:
            item["mode"] = settings.mode
        if (settings is None or settings.mode == "window") and entry.get("start") and entry.get("end"):
            item["window"] = {"since": entry["start"], "until": entry["end"]}
        if entry.get("error"):
            item["failed"] = True
        if settings is not None and settings.enabled:
            item["freshness"] = _freshness(settings, success, now, trusted)
        result[name] = item
    return result


def routine_health(store: Store, *, now: datetime | None = None, trusted: bool = False) -> dict[str, dict[str, object]]:
    """Each configured routine's last run, error, latest action and freshness."""
    now = now or datetime.now(UTC)
    config, history = load(store), state(store, ROUTINES)
    result: dict[str, dict[str, object]] = {}
    for name, settings in sorted(config.routines.items()):
        entry = history.get(name, {})
        success = str(entry.get("success", ""))
        item: dict[str, object] = {"enabled": settings.enabled, "freshness": "unknown"}
        if settings.enabled:
            item["freshness"] = _freshness(settings, success, now, trusted)
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
    trusted = may_collect(store)
    if not trusted:
        return []
    config = load(store)
    result: list[dict[str, object]] = []
    for name, health in source_health(store, now=now, trusted=True).items():
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
    for name, health in routine_health(store, now=now, trusted=True).items():
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
        config, history, summary, trusted = load(store), state(store), index.status(store), may_collect(store)
        counts = cast("dict[str, dict[str, object]]", summary.pop("sources"))
        coverage = source_health(store, counts, now=now, trusted=trusted)
        sources: dict[str, dict[str, object]] = {}
        for name in sorted({*config.sensors, *counts}):
            settings = config.sensors.get(name)
            run = history.get(name, {})
            counters = {key: run[key] for key in ("records", "added", "updated", "unchanged", "removed") if key in run}
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
                if settings.enabled and settings.refresh and trusted:
                    entry["stale"] = coverage[name]["freshness"] in {"never", "stale"}
                    healthy &= not entry["stale"]
                if entry.get("error"):
                    entry["log"] = str(log_path(store, name))
                    # Like routines, only a scheduled sensor this machine runs fails the check.
                    healthy &= not (settings.enabled and settings.refresh and trusted)
            sources[name] = {key: value for key, value in entry.items() if value != ""}
        routines = routine_health(store, now=now, trusted=trusted)
        for name, entry in routines.items():
            settings = config.routines[name]
            if settings.enabled and settings.refresh and trusted:
                entry["stale"] = entry["freshness"] in {"never", "stale"}
                healthy &= not entry["stale"] and not entry.get("error")
        healthy &= not summary["problems"] and summary["index"] == "ready"
        brains.append(
            {
                "brain": config.name,
                "path": str(store.root),
                "collect": trusted,
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
