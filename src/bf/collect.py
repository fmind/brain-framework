"""Record collection (`emit: records`), declarative field mapping, retention, reprojection and due scheduling."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast

from pydantic import FailFast, TypeAdapter, ValidationError

from bf import jobs, models, ontology, records, runtime
from bf import map as mapper
from bf.config import load
from bf.models import Config, Error, InputError, Job, Record
from bf.runtime import Queue, append_log, scheduled_anchor
from bf.storage import Store, collecting, ping_wakeup, writer

CATCH_UP, SCHEDULE_BACKOFF, SHRINK_FLOOR, MAX_COUNT, REPROJECT, LIMIT = timedelta(days=30), 60, 10, 2**53 - 1, 1000, 200
_ts = models.timestamp


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _window(start: str, end: str) -> tuple[str, str]:
    try:
        s, e = _ts(start), _ts(end)
    except ValueError as error:
        raise Error("start and end require timezone-aware timestamps") from error
    if s >= e:
        raise Error("start must be earlier than end")
    return s, e


def _envelope(printed: dict[str, object]) -> tuple[object, dict[str, int], str | None]:
    if not (printed.keys() <= {"records", "incomplete", "cursor"}) or "records" not in printed:
        raise Error("collector object may hold only records, incomplete and cursor")
    inc: dict[str, int] = {}
    if "incomplete" in printed:
        raw_inc = printed["incomplete"]
        if not isinstance(raw_inc, dict) or raw_inc.keys() != {"missed", "of"}:
            raise Error("incomplete must be an object holding only missed and of")
        for key in ("of", "missed"):
            if type(raw_inc[key]) is not int or not 1 <= raw_inc[key] <= MAX_COUNT:
                raise Error(f"incomplete.{key} must be an integer from 1 to {MAX_COUNT}")
        if raw_inc["missed"] > raw_inc["of"]:
            raise Error("incomplete.missed must not exceed incomplete.of")
        inc = {"missed": raw_inc["missed"], "of": raw_inc["of"]}
    if not isinstance(printed["records"], list):
        raise Error("records must be one JSON array of records")
    if (cursor := printed.get("cursor")) is not None and not isinstance(cursor, str):
        raise Error("cursor must be a string")
    return printed["records"], inc, cursor


def _coverage(prev: Mapping[str, object], start: str, end: str, observed: str, job: Job) -> tuple[dict[str, str], bool]:
    """Compute updated contiguous window coverage `[window_start, window_end]` and whether this run is latest."""
    mode, covered = job.mode or "window", min(end, observed)
    b_start, b_end = str(prev.get("window_start", "")), str(prev.get("window_end", ""))
    if b_end:
        last = prev.get("last_run") or prev.get("last_success") or observed
        b_end = min(_ts(b_end), _ts(str(last)), observed)
        b_start = "" if (s := _ts(b_start) if b_start else "") >= b_end else s
        if not b_start and b_start >= b_end:
            b_end = ""
    if start >= covered:
        return ({"window_start": b_start, "window_end": b_end} if b_end else {}), False
    horizon = _ts((datetime.fromisoformat(observed) - CATCH_UP).isoformat())
    resumes = mode == "window" and bool(job.timed)
    contiguous = not b_end or (covered >= b_end and (not resumes or start <= max(b_end, horizon)))
    lo, hi = (start, covered) if contiguous else (b_start, b_end)
    if b_start and b_end and start <= b_end and covered >= b_start:
        lo, hi = min(start, b_start), max(covered, b_end)
    return {"window_start": lo, "window_end": hi}, contiguous and (mode in {"snapshot", "cursor"} or end >= observed)


def _parse_incoming(
    store: Store, log_name: str, config: Config, job: Job | None, raw: bytes, observed: str
) -> tuple[list[Record], dict[str, int], str | None]:
    max_tb, inc = job.max_text_bytes if job else None, cast("dict[str, int]", {})
    if not raw.strip():
        printed: object = []
        cursor_out: str | None = None
    elif job is not None and job.map is not None:
        printed, cursor_out = mapper.extract(raw, job.map)
    else:
        printed, cursor_out = mapper.load_json(raw), None
        if isinstance(printed, dict):
            printed, inc, cursor_out = (
                _envelope(printed) if ({"records", "incomplete", "cursor"} & printed.keys()) else ([printed], {}, None)
            )
    if isinstance(printed, list):
        truncated = 0
        for pos, item in enumerate(printed):
            if isinstance(item, dict) and "fields" in item:
                raise Error(f"record {pos}: collectors must output @type, relations and attributes, not fields")
            if not isinstance(item, dict) or not isinstance(text := item.get("text"), str):
                continue
            try:
                encoded = text.encode("utf-8")
            except UnicodeEncodeError:
                continue
            if max_tb is not None and len(encoded) > max_tb:
                attrs = item.get("attributes")
                new_a = {**(attrs or {}), "partial": True} if attrs is None or isinstance(attrs, dict) else attrs
                printed[pos] = {**item, "text": encoded[:max_tb].decode("utf-8", errors="ignore"), "attributes": new_a}
                truncated += 1
            elif max_tb is None and len(encoded) > 4 << 20:
                raise Error(f"record {pos}: text exceeds 4 MiB; set max_text_bytes on this job to cut longer text")
        if truncated and max_tb is not None:
            with suppress(Error, OSError):
                msg = f"{models.local(runtime.now())} truncated {truncated} oversized record text value(s) to {max_tb} bytes"
                append_log(store, log_name, msg)
    try:
        incoming = TypeAdapter(Annotated[list[Record], FailFast()]).validate_python(printed)
    except ValidationError as error:
        err_msg = "collector must print one JSON array of records: " + models.explain(error, models.RECORD_KEYS)
        raise Error(err_msg) from error
    validated: list[Record] = []
    for pos, rec in enumerate(incoming):
        try:
            ontology.validate(rec, config)
            stamped = rec.model_copy(update={"attributes": {**rec.attributes, "observed": observed}})
            validated.append(cast("Callable[[], Record]", stamped.readable)())
        except (Error, ValueError) as error:
            raise Error(f"record {pos}: {error}") from error
    if len({r.id for r in validated}) != len(validated):
        raise Error("collector returned duplicate record ids")
    return validated, inc, cursor_out


_Batch = tuple[Store, str, str, list[Record]]
_BatchOpts = tuple[tuple[bool, bool, bool], tuple[str, int | None, bool, float], Callable[[], str] | None]


def _apply_batch(batch: _Batch, opts: _BatchOpts, base: dict[str, object]) -> dict[str, object]:
    store, src, job_name, incoming = batch
    (expire_stored, snapshot, allow_removal), (cutoff, keep, dry_run, measured), stamp_fn = opts
    # Critical invariant: NEVER open/lock `.bf/runtime.sqlite3` inside `with writer(store):`.
    with records.reading(store) if dry_run else writer(store, wait=120):
        if not dry_run:
            records.recover(store)
            if stamp_fn and (stamp := stamp_fn()):
                incoming = [r.model_copy(update={"attributes": {**r.attributes, "observed": stamp}}) for r in incoming]
        kept = records.retention(store, src, incoming, cutoff=cutoff, expire_stored=expire_stored)
        kept_paths, rem_set = {records.path(src, r.id) for r in kept.kept}, set(kept.removals)
        existing = [f for f in records.files(store, src) if f not in rem_set] if snapshot and not allow_removal else []
        rem = sum(f not in kept_paths for f in existing)
        if (too_small := rem and (not kept.kept or (rem > len(existing) / 2 and rem > SHRINK_FLOOR))) and not dry_run:
            raise Error(
                f"snapshot would remove {rem} of {len(existing)} records; kept the existing catalog. "
                f"Check the job's scope, then run bf collect {job_name} --allow-removal to accept the removal"
            )
        extra: dict[str, object] = {}
        if dry_run:
            counts, _ = records.plan(store, src, kept.kept, snapshot=snapshot)
            if snapshot:
                counts["removed"] -= len(kept.removals)
            if too_small:
                extra["removal_refused"] = True
            exp = kept.expired
            extra["samples"] = [r.model_dump(exclude_defaults=True) for r in kept.kept[:3]]
        else:
            counts = records.upsert(store, src, kept.kept, snapshot=snapshot, removals=kept.removals)
            pruned = records.prune(store, src, keep, re.compile(r".*")) if keep is not None else 0
            exp = kept.expired + pruned
        extra |= {k: v for k, v in (("forgotten", kept.forgotten), ("expired", exp)) if v}
        elapsed = round(time.monotonic() - measured, 6)
        return {**base, **counts, "records": len(kept.kept), **extra, "elapsed_seconds": elapsed}


def _resume_start(job: Job, prev: Mapping[str, object], anchor_now: datetime) -> datetime:
    start = anchor_now - timedelta(seconds=job.lookback)
    if (job.mode or "window") == "window" and prev.get("window_end"):
        floor = anchor_now - CATCH_UP
        resumed = max(datetime.fromisoformat(str(prev["window_end"])), floor) - timedelta(seconds=job.overlap)
        if resumed < anchor_now:
            return max(resumed, floor)
    return start


def collect(
    store: Store,
    name: str,
    *,
    start: str = "",
    end: str = "",
    dry_run: bool = False,
    reconcile: bool = False,
    allow_removal: bool = False,
    runner: jobs.Runner = jobs.run,
    clock: Callable[[], datetime] = _utc_now,
    live: bool = False,
    fixture: bytes | None = None,
) -> dict[str, object]:
    """Run one `emit: records` job over `[start, end)` and upsert its records; locks never mix `writer` and SQLite."""
    config = load(store)
    if fixture is None:
        job = jobs.enabled(config.jobs, name)
    elif (job := config.jobs.get(name)) is None:
        raise Error(f"unknown job {name}; check jobs in bf.yaml{models.suggest(name, list(config.jobs))}")
    else:
        dry_run, runner = True, cast("jobs.Runner", lambda *_: fixture)
    if job.emit != "records":
        raise Error(f"job {name} does not emit records (emit: {job.emit})")
    if reconcile and job.reconcile_spec is None:
        raise Error("reconciliation requires the job's reconcile setting")
    src, mode = job.source or name, job.mode or "window"
    previous = Queue(store).state_of(name) if fixture is None else {}
    started, measured = clock(), time.monotonic()
    res_end = _ts(started.isoformat()) if end in {"", "now"} else end
    start, end = _window(start or _ts(_resume_start(job, previous, started).isoformat()), res_end)
    observed = _ts(started.isoformat())

    with collecting(store, name):
        committed = False
        try:
            if reconcile and (rs := job.reconcile_spec) is not None:
                earliest = _ts((started - timedelta(seconds=rs.lookback or job.lookback)).isoformat())
                if start > earliest or end < observed:
                    raise Error("reconciliation must cover its lookback through the current run")
            prev_cursor, exp = str(previous.get("cursor") or ""), jobs.expand_argv
            argv = [] if fixture is not None else exp(store, job.run, name, start=start, end=end, cursor=prev_cursor)
            raw = runner(argv, job, store, name, b"")
            incoming, inc, cursor_out = _parse_incoming(store, name, config, job, raw, observed)
            cutoff = _ts((started - timedelta(seconds=job.expire_after)).isoformat()) if job.expire_after else ""
            next_cursor = cursor_out if cursor_out is not None else prev_cursor
            result: dict[str, object] = {"job": name, "source": src, "records": len(incoming), "reconcile": reconcile}
            result |= {"requested_start": start, "requested_end": end, "output_bytes": len(raw)}
            if inc:
                result["incomplete"] = inc
            if cursor_out is not None:
                result["cursor"] = next_cursor

            def _stamp() -> str:
                s = _ts(max(started, _utc_now()).isoformat()) if (clock is _utc_now or live) else ""
                return "" if s == observed else s

            flags = (not inc, mode == "snapshot" and not inc, allow_removal)
            opts = (cutoff, job.keep, dry_run, measured)
            result = _apply_batch((store, src, name, incoming), (flags, opts, _stamp), result)
            if dry_run:
                return result
            committed = True
            cov, latest = _coverage(previous, start, end, observed, job)
            runs = int(cast("int", previous.get("runs", 0))) + 1
            metrics = {k: v for k, v in result.items() if k not in {"job", "source", "incomplete", "cursor"}}
            patch: dict[str, object] = {"last_run": observed, **cov, "last_error": "", "error_count": 0}
            patch |= {"runs": runs, "forgotten": 0, "expired": 0, **metrics}
            streak = int(cast("int", previous.get("incomplete_runs", 0)))
            if inc or streak:
                patch |= {**inc, "incomplete_runs": streak + 1} if inc else {"missed": 0, "of": 0, "incomplete_runs": 0}
            if latest:
                patch["last_success"] = observed
                if reconcile:
                    patch["last_reconcile"] = observed
            if cursor_out is not None:
                patch["cursor"] = next_cursor
            queue = Queue(store)
            queue.remember(name, **patch)
            queue.resolve_incident(name)
            ping_wakeup(store)
        except (Error, OSError, UnicodeError) as error:
            io_err = "collector files are inaccessible; check executable, record permissions and free space"
            done_err = "records were committed but local run history could not be saved; retry is safe"
            base_err = str(error) if isinstance(error, Error) else io_err
            msg = done_err if committed else f"{base_err}; nothing was written"
            jobs.record_failure(store, name, error, msg, dry_run=dry_run, at=observed)
        counts_msg = ", ".join(f"{result.get(k, 0)} {k}" for k in ("added", "updated", "unchanged", "removed"))
        missing = f"; missed {inc['missed']} of {inc['of']}" if inc else ""
        with suppress(Error, OSError):
            append_log(
                store,
                name,
                f"{models.local(runtime.now())} collected {result['records']} records: {counts_msg}{missing}",
            )
        return result


def ingest(
    store: Store,
    source: str,
    raw: bytes,
    *,
    snapshot: bool = False,
    allow_removal: bool = False,
    dry_run: bool = False,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, object]:
    """Validate and upsert externally supplied records without opening `.bf/runtime.sqlite3`."""
    if not re.fullmatch(models.NAME, source) or source == models.RESERVED:
        raise InputError(
            "record source must start with a lowercase letter and use only lowercase letters, digits and hyphens",
            "source",
        )
    config, started, measured = load(store), clock(), time.monotonic()
    job_name, job = config.record_sources().get(source, (source, None))
    observed = _ts(started.isoformat())
    incoming, inc, cursor_out = _parse_incoming(store, job_name, config, job, raw, observed)
    cutoff = _ts((started - timedelta(seconds=job.expire_after)).isoformat()) if job and job.expire_after else ""
    base: dict[str, object] = {"source": source, "records": len(incoming), "output_bytes": len(raw)}
    if inc:
        base["incomplete"] = inc
    if cursor_out is not None:
        base["cursor"] = cursor_out
    flags, opts = (not inc, snapshot and not inc, allow_removal), (cutoff, job.keep if job else None, dry_run, measured)
    out = _apply_batch((store, source, job_name, incoming), (flags, opts, None), base)
    if not dry_run:
        ping_wakeup(store)
    return out


def reproject(store: Store, name: str, *, dry_run: bool = False) -> dict[str, object]:
    """Re-validate and normalize stored records for job or source `name`; never deletes."""
    config = load(store)
    sources = config.record_sources()
    if name in config.jobs and config.jobs[name].emit == "records":
        job_name, src = name, config.jobs[name].source or name
    elif name in sources:
        job_name, src = sources[name][0], name
    else:
        raise Error(f"record job or source {name} is not declared in bf.yaml")
    counts, problems = {"records": 0, "changed": 0, "unchanged": 0, "failed": 0}, []
    with collecting(store, job_name), records.reading(store) if dry_run else writer(store, wait=120):
        if not dry_run:
            records.recover(store)
        changed: list[Record] = []
        for file in records.files(store, src):
            counts["records"] += 1
            try:
                stored = records.load(store, file)
                normalized = Record.model_validate(stored.model_dump(by_alias=True))
                ontology.validate(normalized, config)
            except (Error, ValidationError) as error:
                counts["failed"] += 1
                if len(problems) <= LIMIT:
                    msg = (
                        models.explain(error, models.RECORD_KEYS) if isinstance(error, ValidationError) else str(error)
                    )
                    problems.append({"file": file, "error": msg.removeprefix(f"{file}: ")})
                continue
            if normalized == stored:
                counts["unchanged"] += 1
                continue
            counts["changed"] += 1
            if not dry_run:
                changed.append(normalized)
                if len(changed) >= REPROJECT:
                    records.upsert(store, src, changed, snapshot=False)
                    changed = []
        if changed and not dry_run:
            records.upsert(store, src, changed, snapshot=False)
    out: dict[str, object] = {"job": job_name, "source": src, "dry_run": dry_run, **counts}
    if problems:
        out["problems"] = problems[:LIMIT]
    if len(problems) > LIMIT:
        out["problems_truncated"] = True
    return out


def next_due(job: Job, last: Mapping[str, object], now: datetime, *, default_tz: str | None = None) -> datetime | None:
    """UTC instant when an enabled scheduled job is next due; None when manual or disabled."""
    if not job.enabled or not (timed := job.timed):
        return None
    if last.get("last_error") or last.get("error"):
        cap = timedelta(seconds=min(t.every or 86_400 for t in timed))
        errs = max(1, int(str(last.get("error_count", last.get("failures", 1)))))
        delay = min(cap, timedelta(seconds=SCHEDULE_BACKOFF * 2 ** min(errs - 1, 30)))
        if not (anchor := str(last.get("last_run") or last.get("run") or "")):
            return now
        return now if (since := datetime.fromisoformat(anchor)) > now else since + delay
    last_success = scheduled_anchor(last)
    return min(t.schedule_due(now, last_success, timezone=default_tz) for t in timed)


def _due_iter(store: Store, now: datetime, emit_records: bool) -> list[tuple[Job, str, Mapping[str, object]]]:
    config, states = load(store), Queue(store).all_states()
    tz = config.defaults.timezone
    return [
        (job, name, last)
        for name, job in sorted(config.jobs.items())
        if (job.emit == "records") == emit_records
        and (d := next_due(job, last := states.get(name, {}), now, default_tz=tz)) is not None
        and d <= now
    ]


def due(store: Store, now: datetime) -> list[tuple[str, str, str, bool]]:
    """Due `emit: records` jobs with their `[start, end)` window and reconcile flag."""
    windows: list[tuple[str, str, str, bool]] = []
    for job, name, last in _due_iter(store, now, True):
        start, rec = _resume_start(job, last, now), job.reconcile_spec
        reconciled = str(last.get("last_reconcile") or "")
        span = timedelta(seconds=rec.every if rec else 0)
        in_window = bool(reconciled) and timedelta(0) <= now - datetime.fromisoformat(reconciled) < span
        if revisit := rec is not None and not in_window:
            start = min(start, now - timedelta(seconds=rec.lookback or job.lookback))
        windows.append((name, _ts(start.isoformat()), _ts(now.isoformat()), revisit))
    return windows


def due_jobs(store: Store, now: datetime) -> list[tuple[str, str, str]]:
    """Due scheduled jobs that do not emit records (`emit != "records"`)."""
    items: list[tuple[str, str, str]] = []
    for job, name, last in _due_iter(store, now, False):
        start = now - timedelta(seconds=job.lookback)
        if last.get("window_end") and (reviewed := datetime.fromisoformat(str(last["window_end"]))) < now:
            start = reviewed
        items.append((name, _ts(start.isoformat()), _ts(now.isoformat())))
    return items
