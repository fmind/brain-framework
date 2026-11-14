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
from bf.runtime import _ZERO_STATUS, Queue, environment, log_path
from bf.storage import Store

_C1 = ("records", "added", "updated", "unchanged", "removed", "forgotten")
_COUNTERS = (*_C1, "expired", "requested_start", "requested_end", "reconcile", "elapsed_seconds", "output_bytes")
_RETAINED, DEGRADED = frozenset({"forgotten", "expired"}), 3
_Map = Mapping[str, object]
_Strs = Mapping[str, str]
_Entry = dict[str, object]
_States = tuple[dict[str, _Entry], dict[str, int | str | None], set[str], dict[str, str], str]
_Errs = tuple[set[str], _Strs]
_Ctx = tuple[str, datetime, str | None]


def _freshness(job: Job | None, success: str, now: datetime, *, default_tz: str | None = None) -> str:
    """Compute `fresh`, `overdue`, `never`, `manual`, or `unknown` from triggers and `last_success`."""
    if job is None or not job.enabled:
        return "unknown"
    if not (timed := job.timed):
        return "manual"
    if not success:
        return "never"
    if (last_dt := datetime.fromisoformat(success)) > now:
        return "overdue"
    for trig in timed:
        grace = trig.every if trig.every is not None else 86_400
        if now > trig.schedule_due(last_dt, success, timezone=default_tz) + timedelta(seconds=grace):
            return "overdue"
    return "fresh"


def degradation(entry: Mapping[str, object]) -> dict[str, object] | None:
    """Incomplete run counts from the last committed collection; None after a complete run."""
    runs, missed, of = (int(cast("int", entry.get(k, 0))) for k in ("incomplete_runs", "missed", "of"))
    return {"runs": runs, "missed": missed, "of": of} if runs >= 1 and 1 <= missed <= of else None


def lasting(health: Mapping[str, object]) -> bool:
    """Whether a source's runs reported missing parts at least DEGRADED times in a row."""
    degraded = health.get("degraded")
    return isinstance(degraded, Mapping) and int(cast("int", degraded["runs"])) >= DEGRADED


def is_late(job: Job, health: Mapping[str, object], now: datetime, *, default_tz: str | None = None) -> bool:
    """Whether a scheduled job is `overdue`, or `never` succeeded with an already-due slot."""
    if health["freshness"] != "never":
        return health["freshness"] == "overdue"
    return any(t.schedule_due(now, timezone=default_tz) <= now for t in job.timed)


def _health_entry(job: Job | None, entry: _Map, changed: str, now: datetime, tz: str | None) -> _Entry:
    success = str(entry.get("last_success", ""))
    state = "historical" if job is None else "active" if job.enabled else "disabled"
    item: _Entry = {"state": state, "freshness": _freshness(job, success, now, default_tz=tz)}
    if success and (job is None or job.emit == "records"):
        item["last_collected"] = success
    mode = (job.mode or "window") if job is not None else "window"
    if job is not None and job.emit == "records" and mode in {"window", "cursor", "snapshot"}:
        item["mode"] = mode
    win_ok = job is None or (job.emit == "records" and mode == "window")
    if win_ok and entry.get("window_start") and entry.get("window_end"):
        item["window"] = {"since": entry["window_start"], "until": entry["window_end"]}
    if entry.get("last_error") and (job is None or job.enabled):
        item["failed"] = True
    sa = job.silent_after if job and job.enabled and job.timed and success and changed else None
    if sa is not None and now - datetime.fromisoformat(changed) > timedelta(seconds=sa):
        item["silent"] = True
    if degraded := degradation(entry):
        item["degraded"] = degraded
    return item


def _load_states(store: Store) -> _States:
    """Read `.bf/runtime.sqlite3` strictly read-only without ever creating the database."""
    if not (queue := Queue(store)).exists():
        return {}, dict(_ZERO_STATUS), set(), {}, ""
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
        return {}, dict(_ZERO_STATUS), set(), {}, msg


def _changed(counts: object) -> dict[str, str]:
    if not isinstance(counts, Mapping):
        return {}
    return {k: str(v.get("changed", "")) for k, v in counts.items() if isinstance(v, Mapping)}


def source_health(
    store: Store, names: Iterable[str] = (), *, now: datetime | None = None, config: Config | None = None
) -> dict[str, dict[str, object]]:
    """Describe active, disabled and historical record sources without running commands or creating runtime DB."""
    now, config, states = now or datetime.now(UTC), config or load(store), _load_states(store)[0]
    chg, rec_sources, out = _changed(names), config.record_sources(), {}
    for src in sorted({*rec_sources, *names}):
        jname, job = rec_sources.get(src, ("", None))
        out[src] = _health_entry(job, states.get(jname, {}), chg.get(src, ""), now, config.defaults.timezone)
    return out


def _attach_run(target: _Entry, entry: _Map, log_name: str, active: bool, err: str = "") -> None:
    if counters := {k: entry[k] for k in _COUNTERS if k in entry and (entry[k] or k not in _RETAINED)}:
        target["last_run"] = counters
    if active and err:
        fails = max(1, int(cast("int", entry.get("error_count", 1))))
        target.update(failed=True, error=err, failures=fails, log=log_path(log_name))


def _job_entry(name: str, job: Job, entry: _Map, err_info: tuple[bool, str], ctx: _Ctx) -> _Entry:
    in_failing, d_err = err_info
    changed, now, tz = ctx
    out: _Entry = {"enabled": job.enabled, "emit": job.emit, "priority": job.effective_priority}
    if job.emit == "records":
        out["source"] = job.source or name
    out |= _health_entry(job, entry, changed, now, tz)
    if success := str(entry.get("last_success", "")):
        out["last_success"] = success
    if due_at := collect.next_due(job, entry, now, default_tz=tz):
        out["next_due"] = timestamp(due_at.isoformat())
    if reconciled := str(entry.get("last_reconcile", "")):
        out["last_reconcile"] = reconciled
    out |= {k: entry[k] for k in ("runs", "action", "record") if entry.get(k)}
    q_msg = "unresolved work in runtime queue; inspect bf queue" if in_failing else ""
    _attach_run(out, entry, name, job.enabled, str(entry.get("last_error") or d_err or q_msg))
    return out


def _attention_list(config: Config, jobs: Mapping[str, _Map], errs: _Errs, now: datetime) -> list[_Entry]:
    failing, d_errs = errs
    tz, seen, alerts = config.defaults.timezone, set(), []
    for name, job in sorted(config.jobs.items()):
        if not job.enabled:
            continue
        item = jobs[name]
        failed = bool(item.get("failed") or name in failing or name in d_errs)
        silent, degraded = bool(item.get("silent")), lasting(item)
        if failed or is_late(job, item, now, default_tz=tz) or silent or degraded:
            seen.add(name)
            flags = {k: True for k, on in (("failed", failed), ("silent", silent), ("degraded", degraded)) if on}
            alerts.append({"kind": "job", "name": name, "freshness": str(item["freshness"]), **flags})
    return alerts + [
        {"kind": "job", "name": n, "freshness": "unknown", "failed": True}
        for n in sorted((failing | d_errs.keys()) - seen)
        if n not in config.jobs
    ]


def _all_jobs(config: Config, states: Mapping[str, _Map], errs: _Errs, chg: _Strs, now: datetime) -> dict[str, _Entry]:
    failing, d_errs = errs
    tz = config.defaults.timezone
    return {
        n: _job_entry(n, j, states.get(n, {}), (n in failing, d_errs.get(n, "")), (chg.get(j.source or n, ""), now, tz))
        for n, j in sorted(config.jobs.items())
    }


def attention(
    store: Store, now: datetime | None = None, counts: Mapping[str, Mapping[str, object]] | None = None
) -> list[dict[str, object]]:
    """Enabled jobs that failed, are late, fell silent or stay degraded, plus failing queue jobs."""
    now, config = now or datetime.now(UTC), load(store)
    states, _, failing, d_errs, _ = _load_states(store)
    errs = (failing, d_errs)
    return _attention_list(config, _all_jobs(config, states, errs, _changed(counts), now), errs, now)


def _pinned(store: Store) -> str:
    try:
        lock = tomllib.loads(store.read("uv.lock").decode())
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
        states, q_summary, failing, d_errs, q_err = _load_states(store)
    except sqlite3.DatabaseError as error:
        raise Error(CACHE_ERROR) from error
    changed, tz, errs = _changed(counts), config.defaults.timezone, (failing, d_errs)
    jobs = _all_jobs(config, states, errs, changed, now)
    rec_sources, sources = config.record_sources(), {}
    for src in sorted({*rec_sources, *counts}):
        jname, job = rec_sources.get(src, ("", None))
        run = states.get(jname, {}) if jname else {}
        base_entry = _health_entry(job, run, changed.get(src, ""), now, tz)
        entry: dict[str, object] = {**base_entry, **counts.get(src, {"records": 0, "bytes": 0})}
        if run.get("last_reconcile"):
            entry["reconciled"] = run["last_reconcile"]
        _attach_run(entry, run, jname, job is None or job.enabled, str(run.get("last_error") or ""))
        sources[src] = {k: v for k, v in entry.items() if v != ""}
    problems = cast("list[dict[str, object]]", summary["problems"])
    if q_err:
        problems.append({"error": q_err})
    alerts = _attention_list(config, jobs, errs, now)
    cache_ok = summary["cache"] in {"ready", "busy"}
    healthy = cache_ok and not (alerts or problems or q_summary["failed"] or q_summary["uncertain"])
    cov: dict[str, dict[str, int]] = {}
    for k in ("active", "disabled", "historical"):
        recs = [int(cast("int", counts.get(n, {}).get("records", 0))) for n, v in sources.items() if v["state"] == k]
        cov[k] = {"sources": len(recs), "records": sum(recs)}
    rt = {"installed": __version__, **({"pinned": p} if (p := _pinned(store)) else {})}
    head = {"healthy": healthy, "brain": config.name, "path": str(store.root), "registered": is_registered(store)}
    mid = {"runtime": rt, **summary, "attention": alerts, "jobs": jobs, "sources": sources}
    return head | mid | {"coverage": cov, "queue": q_summary, "usage": usage.summary(store)}


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
    pfx = ("sensors/", "routines/", "channels/")
    bins = [*job.requires.bins, *(c[0] for c in cmds if c and not c[0].startswith(pfx))]
    missing_bins = list(dict.fromkeys(b for b in bins if shutil.which(b, path=host_path) is None))
    missing_env = list(dict.fromkeys(v for v in job.requires.env if not os.environ.get(v)))
    errors = [e for c in cmds if c and c[0].startswith(pfx) and (e := _probe(store, c[0], exe=True))]
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
    extras = {k: v for k, v in (("missing_bins", missing_bins), ("missing_env", missing_env), ("error", err)) if v}
    return {"enabled": True, "ok": not (missing_bins or missing_env or err), **extras}


def doctor(store: Store) -> dict[str, object]:
    """Verify configured jobs have required binaries, non-empty env vars (presence only), scripts and prompts."""
    config, host_path, q_err = load(store), environment().get("PATH", ""), _load_states(store)[4]
    jobs_out = {n: _check_job_env(store, config, j, host_path) for n, j in sorted(config.jobs.items())}
    out = {"healthy": not q_err and all(bool(e["ok"]) for e in jobs_out.values()), "brain": config.name}
    return out | {"path": str(store.root), "jobs": jobs_out, "problems": [{"error": q_err}] if q_err else []}
