"""Job health, source coverage, runtime status summary and preflight doctor checks; strictly read-only."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import tomllib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

from bf import __version__, collect, index, usage
from bf.config import is_registered, load
from bf.models import CACHE_ERROR, MAX_PROMPT, Config, Error, Job, decode, timestamp
from bf.runtime import Queue, environment, log_path
from bf.storage import Store

_COUNTERS = (
    "records",
    "added",
    "updated",
    "unchanged",
    "removed",
    "forgotten",
    "expired",
    "requested_start",
    "requested_end",
    "reconcile",
    "elapsed_seconds",
    "output_bytes",
)
_RETAINED = frozenset({"forgotten", "expired"})
DEGRADED = 3
_EMPTY_Q: dict[str, int | str | None] = {
    "pending": 0,
    "failed": 0,
    "uncertain": 0,
    "wakes": 0,
    "active_runs": 0,
    "oldest_pending": None,
}


def _freshness(job: Job | None, success: str, now: datetime, *, default_tz: str | None = None) -> str:
    """Compute `fresh`, `overdue`, `never`, `manual`, or `unknown` from triggers and `last_success`."""
    if job is None or not job.enabled:
        return "unknown"
    timed = [t for t in job.triggers if t.every is not None or t.at is not None]
    if not timed:
        return "manual"
    if not success:
        return "never"
    last_dt = datetime.fromisoformat(success)
    if last_dt > now:
        return "overdue"
    for trig in timed:
        grace = trig.every if trig.every is not None else 86_400
        next_at = trig.schedule_due(last_dt, success, timezone=default_tz)
        if now > next_at + timedelta(seconds=grace):
            return "overdue"
    return "fresh"


def _silent(job: Job | None, success: str, changed: str, now: datetime) -> bool:
    if job is None or not job.enabled or job.silent_after is None:
        return False
    if not any(t.every is not None or t.at is not None for t in job.triggers) or not success or not changed:
        return False
    return now - datetime.fromisoformat(changed) > timedelta(seconds=job.silent_after)


def degradation(entry: Mapping[str, object]) -> dict[str, object] | None:
    """Incomplete run counts from the last committed collection; None after a complete run."""
    runs, missed, of = (int(cast("int", entry.get(k, 0))) for k in ("incomplete_runs", "missed", "of"))
    if runs < 1 or not 1 <= missed <= of:
        return None
    return {"runs": runs, "missed": missed, "of": of}


def lasting(health: Mapping[str, object]) -> bool:
    """Whether a source's runs reported missing parts at least DEGRADED times in a row."""
    degraded = health.get("degraded")
    return isinstance(degraded, Mapping) and int(cast("int", degraded["runs"])) >= DEGRADED


def is_late(job: Job, health: Mapping[str, object], now: datetime, *, default_tz: str | None = None) -> bool:
    """Whether a scheduled job is `overdue`, or `never` succeeded with an already-due slot."""
    if health["freshness"] == "overdue":
        return True
    if health["freshness"] != "never":
        return False
    timed = [t for t in job.triggers if t.every is not None or t.at is not None]
    return any(t.schedule_due(now, timezone=default_tz) <= now for t in timed)


def _health_entry(
    job: Job | None, entry: Mapping[str, object], changed: str, now: datetime, tz: str | None
) -> dict[str, object]:
    success = str(entry.get("last_success", ""))
    item: dict[str, object] = {
        "state": "historical" if job is None else "active" if job.enabled else "disabled",
        "freshness": _freshness(job, success, now, default_tz=tz),
    }
    if success and (job is None or job.emit == "records"):
        item["last_collected"] = success
    mode = (job.mode or "window") if job is not None else "window"
    if job is not None and job.emit == "records" and mode in {"window", "cursor", "snapshot"}:
        item["mode"] = mode
    win = (
        (job is None or (job.emit == "records" and mode == "window"))
        and entry.get("window_start")
        and entry.get("window_end")
    )
    if win:
        item["window"] = {"since": entry["window_start"], "until": entry["window_end"]}
    if entry.get("last_error") and (job is None or job.enabled):
        item["failed"] = True
    if _silent(job, success, changed, now):
        item["silent"] = True
    if degraded := degradation(entry):
        item["degraded"] = degraded
    return item


def _load_states(
    store: Store,
) -> tuple[dict[str, dict[str, object]], dict[str, int | str | None], set[str], dict[str, str], str]:
    """Read `.bf/runtime.sqlite3` strictly read-only without ever creating the database."""
    queue = Queue(store)
    if not queue.exists():
        return {}, dict(_EMPTY_Q), set(), {}, ""
    try:
        states, q_summary = queue.all_states(), queue.status()
        failing_jobs, failing_transports = queue.failing()
        daemon_errors: dict[str, str] = {}
        with queue.read() as db:
            for key, raw in db.execute("SELECT key,value FROM checkpoints WHERE key LIKE 'health:daemon:%'"):
                if isinstance(val := decode(raw), dict) and val.get("error"):
                    daemon_errors[str(key).removeprefix("health:daemon:")] = str(val["error"])
        return states, q_summary, set(failing_jobs) | set(failing_transports), daemon_errors, ""
    except (Error, OSError, sqlite3.DatabaseError) as error:
        msg = str(error) if isinstance(error, Error) else "runtime database is inaccessible; check its state"
        return {}, dict(_EMPTY_Q), set(), {}, msg


def source_health(
    store: Store, names: Iterable[str] = (), *, now: datetime | None = None, config: Config | None = None
) -> dict[str, dict[str, object]]:
    """Describe active, disabled and historical record sources without running commands or creating runtime DB."""
    now, config, states = now or datetime.now(UTC), config or load(store), _load_states(store)[0]
    changed = (
        {k: str(v.get("changed", "")) for k, v in names.items() if isinstance(v, Mapping)}
        if isinstance(names, Mapping)
        else {}
    )
    rec_sources = config.record_sources()
    return {
        src: _health_entry(
            rec_sources[src][1] if src in rec_sources else None,
            states.get(rec_sources[src][0], {}) if src in rec_sources else {},
            changed.get(src, ""),
            now,
            config.defaults.timezone,
        )
        for src in sorted({*rec_sources, *names})
    }


def _attach_run(
    target: dict[str, object], entry: Mapping[str, object], log_name: str, active: bool, err: str = ""
) -> None:
    if counters := {k: entry[k] for k in _COUNTERS if k in entry and (entry[k] or k not in _RETAINED)}:
        target["last_run"] = counters
    if active and err:
        target.update(
            failed=True,
            error=err,
            failures=max(1, int(cast("int", entry.get("error_count", 1)))),
            log=log_path(log_name),
        )


def _job_entry(
    name: str,
    job: Job,
    entry: Mapping[str, object],
    failing: set[str],
    daemon_err: str,
    changed: str,
    now: datetime,
    tz: str | None,
) -> dict[str, object]:
    out: dict[str, object] = {
        "enabled": job.enabled,
        "emit": job.emit,
        "priority": job.effective_priority,
        **({"source": job.source or name} if job.emit == "records" else {}),
        **_health_entry(job, entry, changed, now, tz),
    }
    if success := str(entry.get("last_success", "")):
        out["last_success"] = success
    if due_at := collect.next_due(job, entry, now, default_tz=tz):
        out["next_due"] = timestamp(due_at.isoformat())
    if reconciled := str(entry.get("last_reconcile", "")):
        out["last_reconcile"] = reconciled
    out.update({k: entry[k] for k in ("runs", "action", "record") if entry.get(k)})
    err = str(
        entry.get("last_error")
        or daemon_err
        or ("unresolved work in runtime queue; inspect bf queue" if name in failing else "")
    )
    _attach_run(out, entry, name, job.enabled, err)
    return out


def _attention_list(
    config: Config,
    jobs: Mapping[str, Mapping[str, object]],
    failing: set[str],
    daemon_errors: Mapping[str, str],
    now: datetime,
) -> list[dict[str, object]]:
    tz, seen, alerts = config.defaults.timezone, set(), []
    for name, job in sorted(config.jobs.items()):
        if not job.enabled:
            continue
        item = jobs[name]
        failed = bool(item.get("failed") or name in failing or name in daemon_errors)
        silent, degraded = bool(item.get("silent")), lasting(item)
        if failed or is_late(job, item, now, default_tz=tz) or silent or degraded:
            seen.add(name)
            flags = {k: True for k, on in (("failed", failed), ("silent", silent), ("degraded", degraded)) if on}
            alerts.append({"kind": "job", "name": name, "freshness": str(item["freshness"]), **flags})
    alerts.extend(
        {"kind": "job", "name": n, "freshness": "unknown", "failed": True}
        for n in sorted((failing | daemon_errors.keys()) - seen)
        if n not in config.jobs
    )
    return alerts


def _all_jobs(
    config: Config,
    states: Mapping[str, Mapping[str, object]],
    failing: set[str],
    daemon_errors: Mapping[str, str],
    changed: Mapping[str, str],
    now: datetime,
) -> dict[str, dict[str, object]]:
    tz = config.defaults.timezone
    return {
        n: _job_entry(
            n, j, states.get(n, {}), failing, daemon_errors.get(n, ""), changed.get(j.source or n, ""), now, tz
        )
        for n, j in sorted(config.jobs.items())
    }


def attention(
    store: Store, now: datetime | None = None, counts: Mapping[str, Mapping[str, object]] | None = None
) -> list[dict[str, object]]:
    """Enabled jobs that failed, are late, fell silent or stay degraded, plus failing queue jobs."""
    now, config = now or datetime.now(UTC), load(store)
    changed = {k: str(v.get("changed", "")) for k, v in (counts or {}).items()}
    states, _, failing, daemon_errors, _ = _load_states(store)
    return _attention_list(
        config, _all_jobs(config, states, failing, daemon_errors, changed, now), failing, daemon_errors, now
    )


def _pinned(store: Store) -> str:
    try:
        lock = tomllib.loads(store.read("uv.lock").decode("utf-8"))
    except Error, OSError, ValueError, RecursionError:
        return ""
    for pkg in lock.get("package", ()) if isinstance(lock.get("package"), list) else ():
        if isinstance(pkg, dict) and pkg.get("name") == "brain-framework" and isinstance(v := pkg.get("version"), str):
            return v
    return ""


def report(store: Store, now: datetime | None = None) -> dict[str, object]:
    """One brain's cache, notes, jobs, record sources, queue summary, registration and usage; offline and read-only."""
    now = now or datetime.now(UTC)
    try:
        config, summary = load(store), index.status(store)
        counts = cast("dict[str, dict[str, object]]", summary.pop("sources"))
        states, q_summary, failing, daemon_errors, q_err = _load_states(store)
    except sqlite3.DatabaseError as error:
        raise Error(CACHE_ERROR) from error
    changed, tz = {k: str(v.get("changed", "")) for k, v in counts.items()}, config.defaults.timezone
    jobs = _all_jobs(config, states, failing, daemon_errors, changed, now)
    rec_sources = config.record_sources()
    sources: dict[str, dict[str, object]] = {}
    for src in sorted({*rec_sources, *counts}):
        job_name, job = rec_sources.get(src, ("", None))
        run = states.get(job_name, {}) if job_name else {}
        entry: dict[str, object] = {
            **_health_entry(job, run, changed.get(src, ""), now, tz),
            **counts.get(src, {"records": 0, "bytes": 0}),
        }
        if run.get("last_reconcile"):
            entry["reconciled"] = run["last_reconcile"]
        _attach_run(entry, run, job_name, job is None or job.enabled, str(run.get("last_error") or ""))
        sources[src] = {k: v for k, v in entry.items() if v != ""}
    problems = cast("list[dict[str, object]]", summary["problems"])
    if q_err:
        problems.append({"error": q_err})
    alerts = _attention_list(config, jobs, failing, daemon_errors, now)
    healthy = not (alerts or problems or q_summary["failed"] or q_summary["uncertain"]) and summary["cache"] in {
        "ready",
        "busy",
    }
    return {
        "healthy": healthy,
        "brain": config.name,
        "path": str(store.root),
        "registered": is_registered(store),
        "runtime": {"installed": __version__, **({"pinned": p} if (p := _pinned(store)) else {})},
        **summary,
        "attention": alerts,
        "jobs": jobs,
        "sources": sources,
        "coverage": {
            kind: {
                "sources": sum(v["state"] == kind for v in sources.values()),
                "records": sum(
                    int(cast("int", counts.get(n, {}).get("records", 0)))
                    for n, v in sources.items()
                    if v["state"] == kind
                ),
            }
            for kind in ("active", "disabled", "historical")
        },
        "queue": q_summary,
        "usage": usage.summary(store),
    }


def _probe(store: Store, path: str, *, exe: bool = False, directory: bool = False) -> str:
    try:
        m = store.mode(path)
    except Error, OSError:
        return f"{path} does not exist"
    if directory:
        return "" if stat.S_ISDIR(m) else f"cwd {path} is not a directory"
    if not stat.S_ISREG(m):
        return f"{path} is not {'an executable ' if exe else 'a '}regular file"
    return f"{path} is not an executable regular file" if exe and not (m & stat.S_IXUSR) else ""


def _check_job_env(store: Store, config: Config, job: Job, host_path: str) -> dict[str, object]:
    if not job.enabled:
        return {"enabled": False, "ok": True}
    cmds = (job.run, job.harness or (config.defaults.harness if job.prompt else []), job.send, job.gate)
    bins = [
        *job.requires.bins,
        *(c[0] for c in cmds if c and not c[0].startswith(("sensors/", "routines/", "channels/"))),
    ]
    missing_bins = list(dict.fromkeys(b for b in bins if shutil.which(b, path=host_path) is None))
    missing_env = list(dict.fromkeys(v for v in job.requires.env if not os.environ.get(v)))
    errors = [
        e
        for c in cmds
        if c and c[0].startswith(("sensors/", "routines/", "channels/")) and (e := _probe(store, c[0], exe=True))
    ]
    if job.prompt:
        if e := _probe(store, job.prompt):
            errors.append(e)
        elif store.lstat(job.prompt).st_size > MAX_PROMPT:
            errors.append(f"{job.prompt} exceeds {MAX_PROMPT} bytes")
        if not (job.harness or config.defaults.harness):
            errors.append("missing harness (set job.harness or defaults.harness)")
    if job.cwd and (e := _probe(store, job.cwd, directory=True)):
        errors.append(e)
    err = "; ".join(errors)
    return {
        "enabled": True,
        "ok": not (missing_bins or missing_env or err),
        **({"missing_bins": missing_bins} if missing_bins else {}),
        **({"missing_env": missing_env} if missing_env else {}),
        **({"error": err} if err else {}),
    }


def doctor(store: Store) -> dict[str, object]:
    """Verify configured jobs have required binaries, non-empty env vars (presence only), scripts and prompts."""
    config, host_path = load(store), environment().get("PATH", "")
    q_err = _load_states(store)[4]
    jobs_out = {n: _check_job_env(store, config, j, host_path) for n, j in sorted(config.jobs.items())}
    return {
        "healthy": not q_err and all(bool(entry["ok"]) for entry in jobs_out.values()),
        "brain": config.name,
        "path": str(store.root),
        "jobs": jobs_out,
        "problems": [{"error": q_err}] if q_err else [],
    }
