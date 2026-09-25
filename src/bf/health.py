"""Collection coverage shared by status and retrieval; freshness never implies complete history."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import cast

from bf import index, usage
from bf.collect import log_path, state
from bf.config import load, may_collect
from bf.storage import Store


def source_health(
    store: Store, names: Iterable[str] = (), *, now: datetime | None = None, trusted: bool = False
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
            if not settings.refresh:
                item["freshness"] = "manual"
            elif trusted or success:
                item["freshness"] = (
                    "never"
                    if not success
                    else "stale"
                    if datetime.fromisoformat(success) < now - timedelta(seconds=2 * settings.refresh)
                    else "fresh"
                )
        result[name] = item
    return result


def report(stores: list[Store], now: datetime | None = None) -> dict[str, object]:
    """Per brain: cache, notes, records, source freshness, errors, logs, notes due for review and usage."""
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
                    healthy &= not settings.enabled
            sources[name] = {key: value for key, value in entry.items() if value != ""}
        healthy &= not summary["problems"] and summary["index"] == "ready"
        brains.append(
            {
                "brain": config.name,
                "path": str(store.root),
                "collect": trusted,
                **summary,
                "sources": sources,
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
