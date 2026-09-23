"""Collect every due source of the bases trusted on this machine, then refresh their caches."""

from __future__ import annotations

from datetime import UTC, datetime

from fkf import index
from fkf.collect import Runner, collect, due, run
from fkf.config import load, may_collect
from fkf.models import Error
from fkf.storage import Store


def update(
    stores: list[Store], *, dry_run: bool = False, now: datetime | None = None, runner: Runner = run
) -> dict[str, object]:
    """One failing source never blocks the others; the report names it and its private log."""
    now = now or datetime.now(UTC)
    bases: list[dict[str, object]] = []
    failed = False
    for store in stores:
        name = load(store).name
        if not may_collect(store):
            bases.append({"base": name, "skipped": "not trusted to collect on this machine"})
            continue
        results: list[dict[str, object]] = []
        for source, start, end in due(store, now):
            result: dict[str, object] = {"source": source, "start": start, "end": end}
            if dry_run:
                results.append({**result, "status": "due"})
                continue
            try:
                result.update(collect(store, source, start=start, end=end, runner=runner, clock=lambda: now))
                result["status"] = "collected"
            except Error as error:
                result.update(status="failed", error=str(error))
                failed = True
            results.append(result)
        report: dict[str, object] = {"base": name, "sources": results}
        if not dry_run:
            report["index"] = index.refresh(store, wait=120)
        bases.append(report)
    return {"ok": not failed, "dry_run": dry_run, "bases": bases}
