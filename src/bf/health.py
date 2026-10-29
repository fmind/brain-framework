"""Collection coverage shared by status and retrieval; freshness never implies complete history."""

from __future__ import annotations

import os
import sqlite3
import tomllib
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import cast

from bf import __version__, index, usage
from bf.config import Selection, brain_name, load, related
from bf.history import ASSISTANTS, ROUTINES, log_path, state
from bf.models import CACHE, Assistant, Config, Error, Program, Sensor, decode, timestamp
from bf.runtime import DATABASE, Queue, check_schema
from bf.storage import Store

# Counters of the last committed collection, reported apart from indexed totals.
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


def _freshness(program: Program | None, success: str, now: datetime) -> str:
    """Scheduled programs are fresh within twice their refresh, based on local run history.

    A disabled or undeclared program, such as a historical source, is unknown.
    """
    if program is None or not program.enabled:
        return "unknown"
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
    store: Store, names: Iterable[str] = (), *, now: datetime | None = None, config: Config | None = None
) -> dict[str, dict[str, object]]:
    """Describe active, disabled and historical sources without running them or exposing logs.

    `names` may be the indexed counts per source, whose `changed` then reports a source silent past its
    `silent_after`. A caller passes the `config` it loaded, so one reply describes one bf.yaml even mid-save.
    """
    now = now or datetime.now(UTC)
    changed = (
        {name: str(count.get("changed", "")) for name, count in names.items() if isinstance(count, Mapping)}
        if isinstance(names, Mapping)
        else {}
    )
    config = config or load(store)
    history, asked = state(store), state(store, ASSISTANTS)
    assistant_states = assistant_health(store, now=now, config=config)
    result: dict[str, dict[str, object]] = {}
    for name in sorted({*config.sensors, *config.assistants, *names}):
        if name in config.assistants:
            # An assistant's records are its runs: the assistant is their source.
            assistant, health = config.assistants[name], assistant_states[name]
            success = str(asked.get(name, {}).get("success", ""))
            item: dict[str, object] = {"state": "active" if assistant.enabled else "disabled", "freshness": "unknown"}
            if success:
                item["last_collected"] = success
            # Its failed or uncertain work, unresolved until retried or cancelled, even once disabled. An unreadable
            # queue, which names no log, leaves the freshness unknown instead.
            if health.get("failed") and "log" in health:
                item["failed"] = True
            if assistant.enabled:
                item["freshness"] = health["freshness"]
            result[name] = item
            continue
        settings = config.sensors.get(name)
        entry = history.get(name, {})
        success = str(entry.get("success", ""))
        item: dict[str, object] = {
            "state": "historical" if settings is None else "active" if settings.enabled else "disabled",
            "freshness": _freshness(settings, success, now),
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


def routine_health(
    store: Store, *, now: datetime | None = None, config: Config | None = None
) -> dict[str, dict[str, object]]:
    """Each configured routine's state, freshness, last success, latest action and failure."""
    now = now or datetime.now(UTC)
    config, history = config or load(store), state(store, ROUTINES)
    result: dict[str, dict[str, object]] = {}
    for name, settings in sorted(config.routines.items()):
        entry = history.get(name, {})
        success = str(entry.get("success", ""))
        item: dict[str, object] = {
            "state": "active" if settings.enabled else "disabled",
            "freshness": _freshness(settings, success, now),
        }
        if success:
            item["last_success"] = success
        if entry.get("action"):
            item["action"] = entry["action"]
        result[name] = {**item, **_failure(name, entry)}
    return result


class _Reader(Queue):
    """The durable queue as status and retrieval see it: read only when it exists, never created, bound or locked for
    writing, so reading never waits behind or blocks assistant work."""

    def exists(self) -> bool:
        # Execution creates the file, then commits its schema: until then the queue holds nothing.
        try:
            return self.store.lstat(DATABASE).st_size > 0
        except FileNotFoundError:
            return False

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        info = self.store.lstat(DATABASE)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Error("assistant queue must be a private regular file owned by you")
        for suffix in ("-wal", "-shm", "-journal"):
            with suppress(FileNotFoundError):
                self.store.lstat(DATABASE + suffix)
        try:
            uri = (self.store.root / DATABASE).as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA trusted_schema=OFF")
                # One snapshot for every query of this read.
                connection.execute("BEGIN")
                if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise Error("unsupported assistant queue version; preserve it and use its matching BF release")
                check_schema(connection)
                # The binding execution writes and checks: a queue it refuses reports nothing either. A drained
                # queue follows a moved or renamed brain at its next execution, so reading it is safe now.
                bound = connection.execute("SELECT value FROM checkpoints WHERE key='binding'").fetchone()
                if (
                    bound is not None
                    and decode(bound[0]) != {"path": str(self.brain.root), "name": load(self.brain).name}
                    and Queue.unresolved(connection)
                ):
                    raise Error(
                        "assistant queue holds unresolved work admitted under this brain's previous path or name; "
                        "restore them, resolve the work with bf queue, then move or rename the brain again"
                    )
                yield connection
            finally:
                connection.close()
        except sqlite3.DatabaseError as error:
            # A read-only reader cannot roll back the journal a killed writer left; the next writer does.
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_READONLY_ROLLBACK:
                raise Error("assistant queue holds an interrupted write; run bf queue to recover it") from error
            raise Error(
                "assistant queue is unavailable; preserve its files and check permissions, disk space or corruption"
            ) from error

    def failing(self) -> tuple[list[str], list[str]]:
        """Assistants with failed or uncertain jobs, and channels with unresolved deliveries that failed or are
        uncertain: durable work only an explicit retry or cancel resolves."""
        with self.connect() as db:
            jobs = db.execute(
                "SELECT DISTINCT assistant FROM jobs WHERE state IN ('failed','uncertain') ORDER BY assistant"
            ).fetchall()
            deliveries = db.execute(
                "SELECT DISTINCT channel FROM deliveries WHERE state NOT IN ('delivered','cancelled') "
                "AND (state='uncertain' OR error!='') ORDER BY channel"
            ).fetchall()
        return [row[0] for row in jobs], [row[0] for row in deliveries]


def assistant_health(
    store: Store, *, now: datetime | None = None, config: Config | None = None
) -> dict[str, dict[str, object]]:
    """Each assistant `config` declares (by default, bf.yaml's): its state, freshness, last scheduled success, runs,
    latest record and failure.

    A queue that cannot be read leaves every freshness unknown and names the reason as each assistant's error.
    """
    now = now or datetime.now(UTC)
    config, history = config or load(store), state(store, ASSISTANTS)
    result: dict[str, dict[str, object]] = {}
    if not config.assistants:
        return result
    checkpoints: dict[str, object] = {}
    failures: dict[str, str] = {}
    unavailable = ""
    try:
        if (queue := _Reader(store)).exists():
            with queue.connect() as db:
                checkpoints = {
                    row[0]: decode(row[1])
                    for row in db.execute("SELECT key,value FROM checkpoints WHERE key LIKE 'schedule:%'")
                }
                # Each assistant's latest unresolved failure.
                failures = {
                    row[0]: row[1]
                    for row in db.execute(
                        "SELECT assistant,error,max(updated) FROM jobs WHERE state IN ('failed','uncertain') "
                        "GROUP BY assistant"
                    )
                }
    except (Error, OSError) as error:
        unavailable = str(error) if isinstance(error, Error) else "assistant queue is inaccessible; check its state"
    for name, settings in sorted(config.assistants.items()):
        entry = history.get(name, {})
        schedules: dict[str, dict[str, object]] = {}
        for schedule, spec in settings.schedules.items():
            if unavailable:
                schedules[schedule] = {"freshness": "unknown", "every": spec.every}
                continue
            progress = checkpoints.get(f"schedule:{name}:schedule:{schedule}")
            success = str(progress.get("success", "")) if isinstance(progress, dict) else ""
            due = spec.next_due(now, success)
            freshness = (
                "never"
                if not success
                else "fresh"
                if (
                    now <= due + timedelta(seconds=spec.every)
                    if spec.at is not None or spec.days is not None
                    else timedelta(0) <= now - datetime.fromisoformat(success) <= timedelta(seconds=2 * spec.every)
                )
                else "overdue"
            )
            schedules[schedule] = {
                "freshness": freshness,
                "every": spec.every,
                # The canonical instant replies present with the local offset, like every other time.
                "next_due": timestamp(due.isoformat()),
                **({"last_success": success} if success else {}),
            }
        freshness = next(
            (kind for kind in ("overdue", "never") if any(item["freshness"] == kind for item in schedules.values())),
            "fresh" if schedules else "manual",
        )
        item: dict[str, object] = {
            "state": "active" if settings.enabled else "disabled",
            "freshness": "unknown" if unavailable or not settings.enabled else freshness,
            "runs": entry.get("runs", 0),
            "schedules": schedules,
            "inputs": [binding.channel.model_dump() for binding in settings.inputs],
        }
        if entry.get("success"):
            item["last_success"] = entry["success"]
        if entry.get("record"):
            item["record"] = entry["record"]
        if unavailable:
            item.update(failed=True, error=unavailable)
        elif name in failures:
            item.update(failed=True, error=failures[name], log=log_path(name))
        result[name] = item
    return result


def _attention(
    config: Config,
    sources: dict[str, dict[str, object]],
    routines: dict[str, dict[str, object]],
    assistants: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    """Scheduled programs this machine runs that failed, have not succeeded recently or, for a source, fell silent, and
    assistants with failed work however it was asked."""
    result: list[dict[str, object]] = []
    for kind, healths, programs in (
        ("sensor", {name: health for name, health in sources.items() if name not in config.assistants}, config.sensors),
        ("routine", routines, config.routines),
        ("assistant", assistants, config.assistants),
    ):
        for name, health in healths.items():
            settings, failed = programs.get(name), health.get("failed")
            silent = health.get("silent")
            if (
                settings
                and settings.enabled
                and (
                    # An assistant's failed work stays queued until retried or cancelled, however it was asked.
                    (failed or bool(settings.schedules or settings.inputs or settings.triggers))
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


def attention(
    store: Store, now: datetime | None = None, counts: Mapping[str, Mapping[str, object]] | None = None
) -> list[dict[str, object]]:
    """Scheduled sensors, routines and assistants this machine runs that failed, are late or fell silent, and
    assistants with failed work.

    `counts` are the indexed counts per source, as `index.sources` gives them, from a caller holding the cache open:
    opening it again would wait behind a publish that waits for that caller.
    """
    now = now or datetime.now(UTC)
    if counts is None:
        try:
            with index.database(store) as (connection, _):
                counts = index.sources(connection)
        except Error, OSError, sqlite3.DatabaseError:
            # Silence needs the cache; failures and lateness come from run history alone.
            counts = {}
    config = load(store)
    return _attention(
        config,
        source_health(store, counts, now=now, config=config),
        routine_health(store, now=now, config=config),
        assistant_health(store, now=now, config=config),
    )


def _messaging(store: Store, config: Config) -> tuple[dict[str, object] | None, list[str], list[str]]:
    """The queue's report, read only when it exists, then the assistants and channels it shows failing.

    Failed and uncertain work counts until it is retried or cancelled. Stream and read errors count while the
    dispatcher that records them runs: watch clears them whenever its dispatcher starts or stops.
    """
    queue = _Reader(store)
    if not queue.exists():
        return None, [], []
    messaging = queue.report()
    assistants, channels = queue.failing()
    transports = cast(dict[str, object], messaging["transports"])
    # Work that cannot start counts too: a schedule or trigger the queue refused, a trigger waiting for its prompt,
    # and a dispatcher pass that fails for every assistant.
    blocked = {
        str(item.get("assistant"))
        for item in cast(list[dict[str, object]], messaging.get("triggers", []))
        if item.get("state") == "pending" and item.get("error")
    }
    for name, settings in config.assistants.items():
        if settings.enabled and (
            name in blocked
            or any(
                isinstance(value := transports.get(key), dict) and value.get("error")
                for key in (f"health:assistant:{name}", "health:dispatcher")
            )
        ):
            assistants = sorted({*assistants, name})
    for name, settings in config.assistants.items():
        for item in settings.inputs if settings.enabled else ():
            channel = item.channel.name
            key = f"health:channel:{channel}" if item.channel.mode == "stream" else f"health:read:{name}:{channel}"
            value = transports.get(key)
            if isinstance(value, dict) and value.get("error") and channel not in channels:
                channels.append(channel)
    return messaging, assistants, sorted(channels)


def _pinned(store: Store) -> str:
    """The brain-framework version a brain's uv.lock pins, read as data and never run; empty without one."""
    try:
        lock = tomllib.loads(store.read("uv.lock").decode("utf-8"))
    except Error, OSError, ValueError, RecursionError:
        # A shared brain supplies its lock: deep nesting or an overlong integer pins nothing, like any parse error.
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
            coverage = source_health(store, counts, now=now, config=config)
            routines = routine_health(store, now=now, config=config)
            assistants = assistant_health(store, now=now, config=config)
        except (Error, OSError, UnicodeError, sqlite3.DatabaseError) as error:
            # Status diagnoses brains: among registered ones, even beside only unavailable ones, one that cannot load
            # is reported, and the others still are. A brain selected alone fails with its own error.
            if len(stores) == 1 and not unavailable:
                if isinstance(error, sqlite3.DatabaseError):
                    raise Error(CACHE) from error
                raise
            message = str(error) if isinstance(error, Error) else "inaccessible brain or cache; check its path"
            brains.append({"brain": brain_name(store), "path": str(store.root), "error": message})
            healthy = False
            continue
        sources: dict[str, dict[str, object]] = {}
        for name in sorted({*config.sensors, *config.assistants, *counts}):
            # An assistant's run counters belong to its assistants entry.
            run = {} if name in config.assistants else history.get(name, {})
            # One shape shared with retrieval coverage, plus indexed totals and local run diagnostics.
            entry: dict[str, object] = {**coverage[name], **counts.get(name, {"records": 0, "bytes": 0})}
            if counters := {key: run[key] for key in _COUNTERS if key in run}:
                entry["last_run"] = counters
            if run.get("reconciled"):
                entry["reconciled"] = run["reconciled"]
            entry.update(_failure(name, run))
            sources[name] = {key: value for key, value in entry.items() if value != ""}
        problems = cast("list[dict[str, object]]", summary["problems"])
        try:
            messaging, jobs, channels = _messaging(store, config)
        except (Error, OSError) as error:
            # Unread work is unknown work: the reply is incomplete, not healthy.
            messaging, jobs, channels = None, [], []
            problems.append(
                {
                    "error": str(error)
                    if isinstance(error, Error)
                    else "assistant queue is inaccessible; check its state"
                }
            )
        # The reasons a brain is unhealthy, beside its problems: the home page's late or failed programs, then
        # unresolved assistant work and failing channels.
        alerts = _attention(config, coverage, routines, assistants)
        named = {entry.get("assistant") for entry in alerts}
        alerts += [
            {"assistant": name, "freshness": str(assistants.get(name, {}).get("freshness", "unknown")), "failed": True}
            for name in jobs
            if name not in named
        ]
        alerts += [{"channel": name, "freshness": "unknown", "failed": True} for name in channels]
        # A busy cache still serves its last generation, marked stale in replies, while a live writer finishes.
        healthy &= not alerts and not problems and summary["cache"] in {"ready", "busy"}
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
