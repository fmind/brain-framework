"""Explicit refresh of due sources; the owner supplies the external scheduler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fkf.collect import Runner, collect, run
from fkf.config import load
from fkf.index import build, document
from fkf.models import Collection, Error
from fkf.storage import Store


def update(
    store: Store, *, dry_run: bool = False, now: datetime | None = None, runner: Runner = run
) -> dict[str, object]:
    """Use durable successful captures as checkpoints; failed windows remain due."""
    now = now or datetime.now(UTC)
    config = load(store)
    latest: dict[str, Collection] = {}
    for path in store.files("records"):
        if not path.endswith(".json"):
            continue
        capture = document(store, path)
        if not capture.automatic:
            continue
        previous = latest.get(capture.source)
        if previous is None or (capture.captured, capture.end) > (previous.captured, previous.end):
            latest[capture.source] = capture
    results: list[dict[str, object]] = []
    for name, source in sorted(config.sources.items()):
        if not source.enabled or not source.refresh:
            continue
        previous = latest.get(name)
        if previous and now < datetime.fromisoformat(previous.captured) + timedelta(seconds=source.refresh):
            results.append({"source": name, "status": "not-due"})
            continue
        # Snapshots always request a complete catalog. Windowed sources resume with deliberate
        # overlap for late arrivals; adapters with modified-time limitations must document them.
        start = now - timedelta(seconds=source.lookback)
        if previous and previous.end and source.mode == "window":
            start = min(datetime.fromisoformat(previous.end), now) - timedelta(seconds=source.overlap)
            if start >= now:
                start = now - timedelta(seconds=source.lookback)
        window = {"start": start.isoformat(), "end": now.isoformat()}
        result: dict[str, object] = {"source": name, **window}
        if dry_run:
            result["status"] = "due"
        else:
            try:
                result.update(
                    collect(
                        store,
                        name,
                        start=window["start"],
                        end=window["end"],
                        runner=runner,
                        clock=lambda: now,
                        automatic=True,
                    )
                )
                result["status"] = "collected"
            except Error:
                # The per-source result identifies recovery scope without forwarding provider data.
                result.update(status="failed", error="collection failed; inspect the source with fkf collect")
        results.append(result)
    report: dict[str, object] = {
        "base": {"id": config.id, "name": config.name},
        "dry_run": dry_run,
        "ok": all(result["status"] != "failed" for result in results),
        "sources": results,
    }
    if not dry_run:
        report["build"] = build(store, if_stale=True)
    return report
