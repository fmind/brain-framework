"""Collection coverage shared by status and retrieval; freshness never implies complete history."""

from __future__ import annotations

import sqlite3
import tomllib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

from bf import __version__, index, usage
from bf.config import Selection, brain_name, load, related
from bf.history import ASSISTANTS, ROUTINES, log_path, state
from bf.models import CACHE, Assistant, Config, Error, Program, Sensor
from bf.runtime import Queue
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


def _silent(sensor: Sensor | None, success: str, changed: str, now: datetime) -> bool:
    """Whether a scheduled source that still succeeds has gained no new or changed record within `silent_after`.

    A source with no record yet is never silent: its zero count already shows it.
    """
    if sensor is None or not sensor.enabled or not sensor.refresh or sensor.silent_after is None:
        return False
    if not success or not changed:
        return False
    return now - datetime.fromisoformat(changed) > timedelta(seconds=sensor.silent_after)


def source_health(
    store: Store, names: Iterable[str] = (), *, now: datetime | None = None
) -> dict[str, dict[str, object]]:
    """Describe active, disabled and historical sources without running them or exposing logs.

    `names` may be the indexed counts per source, whose `changed` then reports a source silent past its
    `silent_after`.
    """
    now = now or datetime.now(UTC)
    changed = (
        {name: str(count.get("changed", "")) for name, count in names.items() if isinstance(count, Mapping)}
        if isinstance(names, Mapping)
        else {}
    )
    config, history, asked = load(store), state(store), state(store, ASSISTANTS)
    assistant_states = assistant_health(store, now=now)
    result: dict[str, dict[str, object]] = {}
    for name in sorted({*config.sensors, *config.assistants, *names}):
        if name in config.assistants:
            # A prompt's records are its runs: the prompt is their source.
            prompt = config.assistants[name]
            entry = asked.get(name, {})
            success = str(entry.get("success", ""))
            item: dict[str, object] = {"state": "active" if prompt.enabled else "disabled", "freshness": "unknown"}
            if success:
                item["last_collected"] = success
            if entry.get("error"):
                item["failed"] = True
            if prompt.enabled:
                item["freshness"] = assistant_states[name]["freshness"]
            result[name] = item
            continue
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
        if _silent(settings, success, changed.get(name, ""), now):
            item["silent"] = True
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


def assistant_health(store: Store, *, now: datetime | None = None) -> dict[str, dict[str, object]]:
    """Each configured prompt's state, freshness, last scheduled success, runs, latest record and failure."""
    now = now or datetime.now(UTC)
    config, history = load(store), state(store, ASSISTANTS)
    result: dict[str, dict[str, object]] = {}
    if not config.assistants:
        return result
    queue = Queue(store)
    report = queue.report(limit=10000)
    for name, settings in sorted(config.assistants.items()):
        entry = history.get(name, {})
        schedules: dict[str, dict[str, object]] = {}
        for schedule, spec in settings.schedules.items():
            progress = queue.progress(f"schedule:{name}:schedule:{schedule}")
            success = str(progress.get("success", ""))
            freshness = (
                "never"
                if not success
                else "fresh"
                if timedelta(0) <= now - datetime.fromisoformat(success) <= timedelta(seconds=2 * spec.every)
                else "overdue"
            )
            due = datetime.fromisoformat(success) + timedelta(seconds=spec.every) if success else now
            schedules[schedule] = {
                "freshness": freshness,
                "every": spec.every,
                "next_due": due.isoformat(),
                **({"last_success": success} if success else {}),
            }
        freshness = next(
            (kind for kind in ("overdue", "never") if any(item["freshness"] == kind for item in schedules.values())),
            "fresh" if schedules else "manual",
        )
        failures = [
            job
            for job in cast(list[dict[str, object]], report["jobs"])
            if job["assistant"] == name and job["state"] in {"failed", "uncertain"}
        ]
        item: dict[str, object] = {
            "state": "active" if settings.enabled else "disabled",
            "freshness": freshness if settings.enabled else "unknown",
            "runs": entry.get("runs", 0),
            "schedules": schedules,
            "inputs": [binding.channel.model_dump() for binding in settings.inputs],
        }
        if entry.get("success"):
            item["last_success"] = entry["success"]
        if entry.get("record"):
            item["record"] = entry["record"]
        if failures:
            item.update(failed=True, error=failures[0]["error"], log=log_path(name))
        result[name] = item
    return result


def _attention(
    config: Config,
    sources: dict[str, dict[str, object]],
    routines: dict[str, dict[str, object]],
    assistants: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Scheduled programs this machine runs that failed, have not succeeded recently or, for a source, fell silent."""
    result: list[dict[str, object]] = []
    for kind, healths, programs in (
        ("sensor", {name: health for name, health in sources.items() if name not in config.assistants}, config.sensors),
        ("routine", routines, config.routines),
        ("assistant", assistants or {}, config.assistants),
    ):
        for name, health in healths.items():
            settings, failed = programs.get(name), health.get("failed")
            silent = health.get("silent")
            if (
                settings
                and settings.enabled
                and (
                    bool(settings.schedules or settings.inputs or settings.triggers)
                    if isinstance(settings, Assistant)
                    else settings.refresh
                )
                and (failed or silent or health["freshness"] in _LATE)
            ):
                result.append(
                    {
                        kind: name,
                        "freshness": health["freshness"],
                        **({"failed": True} if failed else {}),
                        **({"silent": True} if silent else {}),
                    }
                )
    return result


def attention(store: Store, now: datetime | None = None) -> list[dict[str, object]]:
    """Scheduled sensors, routines and assistants this machine runs that failed, are late or fell silent."""
    now = now or datetime.now(UTC)
    try:
        with index.database(store) as (connection, _):
            counts = index.sources(connection)
    except Error, OSError, sqlite3.DatabaseError:
        # Silence needs the cache; failures and lateness come from run history alone.
        counts = {}
    return _attention(
        load(store),
        source_health(store, counts, now=now),
        routine_health(store, now=now),
        assistant_health(store, now=now),
    )


def _pinned(store: Store) -> str:
    """The brain-framework version a brain's uv.lock pins, read as data and never run; empty without one."""
    try:
        lock = tomllib.loads(store.read("uv.lock").decode("utf-8"))
    except Error, OSError, UnicodeError, tomllib.TOMLDecodeError:
        return ""
    packages = lock.get("package")
    for package in packages if isinstance(packages, list) else ():
        if isinstance(package, dict) and package.get("name") == "brain-framework":
            version = package.get("version")
            return version if isinstance(version, str) else ""
    return ""


def report(stores: list[Store], now: datetime | None = None) -> dict[str, object]:
    """Per brain: cache, notes, records, source, routine and assistant freshness, errors, logs and usage."""
    now = now or datetime.now(UTC)
    unavailable = stores.unavailable if isinstance(stores, Selection) else {}
    brains: list[dict[str, object]] = [{"brain": name, "error": error} for name, error in unavailable.items()]
    healthy = not brains
    for store in stores:
        try:
            config, history, summary = load(store), state(store), index.status(store)
            counts = cast("dict[str, dict[str, object]]", summary.pop("sources"))
            coverage = source_health(store, counts, now=now)
            routines = routine_health(store, now=now)
            assistants = assistant_health(store, now=now)
            queue = Queue(store)
            messaging = queue.report() if config.assistants or config.channels or queue.exists() else None
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
        sources: dict[str, dict[str, object]] = {}
        for name in sorted({*config.sensors, *config.assistants, *counts}):
            # A prompt's run counters belong to its prompts entry.
            run = {} if name in config.assistants else history.get(name, {})
            # One shape shared with retrieval coverage, plus indexed totals and local run diagnostics.
            entry: dict[str, object] = {**coverage[name], **counts.get(name, {"records": 0, "bytes": 0})}
            if counters := {key: run[key] for key in _COUNTERS if key in run}:
                entry["last_run"] = counters
            if run.get("reconciled"):
                entry["reconciled"] = run["reconciled"]
            entry.update(_failure(name, run))
            sources[name] = {key: value for key, value in entry.items() if value != ""}
        # The reasons a brain is unhealthy, beside its problems: the home page's list of late or failed programs.
        alerts = _attention(config, coverage, routines, assistants)
        # A busy cache still serves its last generation, marked stale in replies, while a live writer finishes.
        if messaging:
            job_counts = cast(dict[str, int], messaging["counts"])
            unhealthy = bool(
                job_counts.get("failed", 0) or job_counts.get("uncertain", 0) or messaging["delivery_failures"]
            ) or bool(cast(dict[str, int], messaging["delivery_counts"]).get("uncertain", 0))
            active_transports = {
                f"health:channel:{item.channel.name}"
                for settings in config.assistants.values()
                if settings.enabled
                for item in settings.inputs
                if item.channel.mode == "stream"
            }
            active_transports |= {
                f"health:read:{name}:{item.channel.name}"
                for name, settings in config.assistants.items()
                if settings.enabled
                for item in settings.inputs
                if item.channel.mode == "read"
            }
            active_transports |= {
                "health:reactive",
                "health:proactive",
                *(f"health:delivery:{name}" for name in config.channels),
            }
            unhealthy |= any(
                bool(cast(dict[str, object], value).get("error"))
                for key, value in cast(dict[str, object], messaging["transports"]).items()
                if key in active_transports
            )
            healthy &= not unhealthy
        healthy &= not alerts and not summary["problems"] and summary["cache"] in {"ready", "busy"}
        brains.append(
            {
                "brain": config.name,
                "path": str(store.root),
                # Agents compare the pinned major version with the installed one before trusting either.
                "runtime": {"installed": __version__, **({"pinned": pinned} if (pinned := _pinned(store)) else {})},
                **summary,
                "attention": alerts,
                "sources": sources,
                **({"routines": routines} if routines else {}),
                **({"assistants": assistants} if assistants else {}),
                **({"messaging": messaging} if messaging else {}),
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
