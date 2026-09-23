"""Collect every due source of the bases trusted on this machine, then refresh their caches."""

from __future__ import annotations

from datetime import UTC, datetime

from fkf import index
from fkf.collect import Runner, collect, due, run
from fkf.config import load, may_collect
from fkf.models import Error
from fkf.storage import Store


def _failure(error: Error | OSError | UnicodeError) -> str:
    return str(error) if isinstance(error, Error) else "inaccessible files; check permissions and free space"


def update(
    stores: list[Store], *, dry_run: bool = False, now: datetime | None = None, runner: Runner = run
) -> dict[str, object]:
    """One failing source never blocks the others; the report names it and its private log."""
    now = now or datetime.now(UTC)
    bases: list[dict[str, object]] = []
    failed = False
    for store in stores:
        try:
            name = load(store).name
            if not may_collect(store):
                bases.append({"base": name, "skipped": "not trusted to collect on this machine"})
                continue
            windows = due(store, now)
        except (Error, OSError, UnicodeError) as error:
            bases.append({"base": store.root.name, "error": _failure(error)})
            failed = True
            continue
        results: list[dict[str, object]] = []
        for source, start, end in windows:
            result: dict[str, object] = {"source": source, "start": start, "end": end}
            if dry_run:
                results.append({**result, "status": "due"})
                continue
            try:
                result.update(collect(store, source, start=start, end=end, runner=runner, clock=lambda: now))
                result["status"] = "collected"
            except (Error, OSError, UnicodeError) as error:
                result.update(status="failed", error=_failure(error))
                failed = True
            results.append(result)
        report: dict[str, object] = {"base": name, "sources": results}
        if not dry_run:
            try:
                report["index"] = index.refresh(store, wait=120)
                failed |= bool(report["index"]["problems"])
            except (Error, OSError, UnicodeError) as error:
                report["index"] = {"error": _failure(error)}
                failed = True
        bases.append(report)
    return {"ok": not failed, "dry_run": dry_run, "bases": bases}
