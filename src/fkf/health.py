"""Collection coverage shared by status and retrieval; freshness never implies complete history."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from fkf.collect import state
from fkf.config import load, may_collect
from fkf.storage import Store


def source_health(
    store: Store, names: Iterable[str] = (), *, now: datetime | None = None
) -> dict[str, dict[str, object]]:
    """Describe active, disabled and historical sources without running them or exposing logs."""
    now = now or datetime.now(UTC)
    config, history, trusted = load(store), state(store), may_collect(store)
    result: dict[str, dict[str, object]] = {}
    for name in sorted({*config.sources, *names}):
        settings = config.sources.get(name)
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
            elif trusted:
                item["freshness"] = (
                    "never"
                    if not success
                    else "stale"
                    if datetime.fromisoformat(success) < now - timedelta(seconds=2 * settings.refresh)
                    else "fresh"
                )
        result[name] = item
    return result
