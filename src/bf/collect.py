"""Record collection (`emit: records`), declarative field mapping, retention, reprojection and due scheduling."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast

from pydantic import FailFast, TypeAdapter, ValidationError

from bf import jobs, ontology, records
from bf import map as mapper
from bf.config import load
from bf.models import RECORD_KEYS, Config, Error, Job, Record, decode, explain, local, suggest, timestamp
from bf.runtime import Queue, append_log
from bf.storage import Store, collecting, writer

CATCH_UP = timedelta(days=30)
SCHEDULE_BACKOFF = 60
SHRINK_FLOOR = 10
MAX_COUNT = 2**53 - 1
REPROJECT = 1000
LIMIT = 200


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _now() -> str:
    return local(timestamp(_utc_now().isoformat()))


def _window(start: str, end: str) -> tuple[str, str]:
    try:
        s, e = timestamp(start), timestamp(end)
    except ValueError as error:
        raise Error("start and end require timezone-aware timestamps") from error
    if s >= e:
        raise Error("start must be earlier than end")
    return s, e


def _parse_collector_output(raw: bytes) -> tuple[object, dict[str, int], str | None]:
    """Parse JSON array, `{records, incomplete?, cursor?}`, single JSON object, or JSON Lines."""
    stripped = raw.strip()
    if not stripped:
        return [], {}, None
    try:
        printed = decode(raw)
    except Error:
        raw_lines = [line for line in raw.splitlines() if line.strip()]
        if not stripped.startswith(b"[") and len(raw_lines) >= 2:
            items: list[object] = []
            for index, line in enumerate(raw_lines, 1):
                try:
                    obj = decode(line)
                except Error as line_error:
                    raise Error(f"line {index}: {line_error}") from line_error
                if not isinstance(obj, dict):
                    raise Error(f"line {index}: collector JSONL line must be a JSON object") from None
                items.append(obj)
            return items, {}, None
        raise
    if isinstance(printed, dict) and ({"records", "incomplete", "cursor"} & printed.keys()):
        return _envelope(printed)
    return ([printed] if isinstance(printed, dict) else printed), {}, None


def _envelope(printed: dict[str, object]) -> tuple[object, dict[str, int], str | None]:
    if not (printed.keys() <= {"records", "incomplete", "cursor"}) or "records" not in printed:
        raise Error("collector object may hold only records, incomplete and cursor")
    incomplete: dict[str, int] = {}
    if "incomplete" in printed:
        raw_inc = printed["incomplete"]
        if not isinstance(raw_inc, dict) or raw_inc.keys() != {"missed", "of"}:
            raise Error("incomplete must be an object holding only missed and of")
        for key in ("of", "missed"):
            if type(raw_inc[key]) is not int or not 1 <= raw_inc[key] <= MAX_COUNT:
                raise Error(f"incomplete.{key} must be an integer from 1 to {MAX_COUNT}")
        if raw_inc["missed"] > raw_inc["of"]:
            raise Error("incomplete.missed must not exceed incomplete.of")
        incomplete = {"missed": raw_inc["missed"], "of": raw_inc["of"]}
    if not isinstance(printed["records"], list):
        raise Error("records must be one JSON array of records")
    cursor = printed.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise Error("cursor must be a string")
    return printed["records"], incomplete, cursor


def _prepare(position: int, record: Record, config: Config, observed: str) -> Record:
    try:
        ontology.validate(record, config)
        stamped = record.model_copy(update={"attributes": {**record.attributes, "observed": observed}})
        return cast("Callable[[], Record]", stamped.readable)()
    except (Error, ValueError) as error:
        raise Error(f"record {position}: {error}") from error


def _shrink(store: Store, src: str, job_name: str, retention: records.Retention) -> str:
    exempt = set(retention.removals)
    existing = [file for file in records.files(store, src) if file not in exempt]
    kept = {records.path(src, record.id) for record in retention.kept}
    removed = sum(file not in kept for file in existing)
    if removed and (not retention.kept or (removed > len(existing) / 2 and removed > SHRINK_FLOOR)):
        return (
            f"snapshot would remove {removed} of {len(existing)} records; kept the existing catalog. "
            f"Check the job's scope, then run bf collect {job_name} --allow-removal to accept the removal"
        )
    return ""


def _retention(retention: records.Retention) -> dict[str, int]:
    return {k: v for k, v in (("forgotten", retention.forgotten), ("expired", retention.expired)) if v}


def _coverage(
    previous: Mapping[str, object], start: str, end: str, observed: str, job: Job
) -> tuple[dict[str, str], bool]:
    """Compute updated contiguous window coverage `[window_start, window_end]` and whether this run is latest."""
    mode = job.mode or "window"
    covered = min(end, observed)
    before_start, before_end = str(previous.get("window_start", "")), str(previous.get("window_end", ""))
    if before_end:
        last = previous.get("last_run") or previous.get("last_success") or observed
        before_end = min(timestamp(before_end), timestamp(str(last)), observed)
        before_start = timestamp(before_start) if before_start else ""
        if before_start >= before_end:
            before_start = before_end = ""
    if start >= covered:
        return ({"window_start": before_start, "window_end": before_end} if before_end else {}), False
    horizon = timestamp((datetime.fromisoformat(observed) - CATCH_UP).isoformat())
    resumes = mode == "window" and any(t.every is not None or t.at is not None for t in job.triggers)
    contiguous = not before_end or (covered >= before_end and (not resumes or start <= max(before_end, horizon)))
    interval = (start, covered) if contiguous else (before_start, before_end)
    if before_start and before_end and start <= before_end and covered >= before_start:
        interval = (min(start, before_start), max(covered, before_end))
    latest = contiguous and (mode in {"snapshot", "cursor"} or end >= observed)
    return {"window_start": interval[0], "window_end": interval[1]}, latest


def _truncate_text(store: Store, name: str, job: Job, printed: object) -> None:
    if not isinstance(printed, list):
        return
    truncated = 0
    for pos, item in enumerate(printed):
        if isinstance(item, dict) and "fields" in item:
            raise Error(f"record {pos}: collectors must output @type, relations and attributes, not fields")
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            try:
                encoded = item["text"].encode("utf-8")
            except UnicodeEncodeError:
                continue
            if job.max_text_bytes is not None and len(encoded) > job.max_text_bytes:
                cut = encoded[: job.max_text_bytes].decode("utf-8", errors="ignore")
                attrs = item.get("attributes")
                new_attrs = {**(attrs or {}), "partial": True} if attrs is None or isinstance(attrs, dict) else attrs
                printed[pos] = {**item, "text": cut, "attributes": new_attrs}
                truncated += 1
            elif job.max_text_bytes is None and len(encoded) > 4 << 20:
                raise Error(f"record {pos}: text exceeds 4 MiB; set max_text_bytes on this job to cut longer text")
    if truncated and job.max_text_bytes is not None:
        with suppress(Error, OSError):
            append_log(
                store,
                name,
                f"{_now()} truncated {truncated} oversized record text value(s) to {job.max_text_bytes} bytes",
            )


def _default_window(job: Job, prev: Mapping[str, object], started: datetime, start: str, end: str) -> tuple[str, str]:
    resolved_end = timestamp(started.isoformat()) if end in {"", "now"} else end
    if start:
        return _window(start, resolved_end)
    anchor = started - timedelta(seconds=job.lookback)
    if (job.mode or "window") == "window" and prev.get("window_end"):
        floor = started - CATCH_UP
        resumed = max(datetime.fromisoformat(str(prev["window_end"])), floor) - timedelta(seconds=job.overlap)
        if resumed < started:
            anchor = max(resumed, floor)
    return _window(timestamp(anchor.isoformat()), resolved_end)


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
    else:
        if (declared := config.jobs.get(name)) is None:
            raise Error(f"unknown job {name}; check jobs in bf.yaml{suggest(name, list(config.jobs))}")
        job, dry_run = declared, True
        runner = cast("jobs.Runner", lambda *_: fixture)
    if job.emit != "records":
        raise Error(f"job {name} does not emit records (emit: {job.emit})")
    if reconcile and job.reconcile_spec is None:
        raise Error("reconciliation requires the job's reconcile setting")

    src, mode = job.source or name, job.mode or "window"
    previous = Queue(store).state_of(name) if fixture is None else {}
    started = clock()
    start, end = _default_window(job, previous, started, start, end)
    observed, measured = timestamp(started.isoformat()), time.monotonic()

    with collecting(store, name):
        committed = False
        try:
            if reconcile and job.reconcile_spec is not None:
                lb = job.reconcile_spec.lookback or job.lookback
                if start > timestamp((started - timedelta(seconds=lb)).isoformat()) or end < observed:
                    raise Error("reconciliation must cover its lookback through the current run")
            prev_cursor = str(previous.get("cursor") or "")
            argv = (
                []
                if fixture is not None
                else jobs.expand_argv(store, job.run, name, start=start, end=end, cursor=prev_cursor)
            )
            raw = runner(argv, job, store, name, b"")
            if not raw.strip():
                printed, incomplete, cursor_out = [], {}, None
            elif job.map is not None:
                printed, cursor_out = mapper.extract(raw, job.map)
                incomplete = {}
            else:
                printed, incomplete, cursor_out = _parse_collector_output(raw)
            _truncate_text(store, name, job, printed)
            try:
                incoming = TypeAdapter(Annotated[list[Record], FailFast()]).validate_python(printed)
            except ValidationError as error:
                raise Error("collector must print one JSON array of records: " + explain(error, RECORD_KEYS)) from error
            incoming = [_prepare(pos, rec, config, observed) for pos, rec in enumerate(incoming)]
            if len({r.id for r in incoming}) != len(incoming):
                raise Error("collector returned duplicate record ids")
            cutoff = timestamp((started - timedelta(seconds=job.expire_after)).isoformat()) if job.expire_after else ""
            next_cursor = cursor_out if cursor_out is not None else prev_cursor
            result: dict[str, object] = {
                "job": name,
                "source": src,
                "records": len(incoming),
                "requested_start": start,
                "requested_end": end,
                "reconcile": reconcile,
                "output_bytes": len(raw),
                **({"incomplete": incomplete} if incomplete else {}),
                **({"cursor": next_cursor} if cursor_out is not None else {}),
            }
            snapshot, expire_stored = mode == "snapshot" and not incomplete, not incomplete
            if dry_run:
                with records.reading(store):
                    kept = records.retention(store, src, incoming, cutoff=cutoff, expire_stored=expire_stored)
                    counts, _ = records.plan(store, src, kept.kept, snapshot=snapshot)
                    if snapshot:
                        counts["removed"] -= len(kept.removals)
                    refused = snapshot and not allow_removal and bool(_shrink(store, src, name, kept))
                return {
                    **result,
                    "records": len(kept.kept),
                    **counts,
                    **_retention(kept),
                    **({"removal_refused": True} if refused else {}),
                    "elapsed_seconds": round(time.monotonic() - measured, 6),
                    "samples": [r.model_dump(exclude_defaults=True) for r in kept.kept[:3]],
                }
            # Critical invariant: NEVER open/lock `.bf/runtime.sqlite3` inside `with writer(store):`.
            with writer(store, wait=120):
                records.recover(store)
                if clock is _utc_now or live:
                    commit_obs = timestamp(max(started, _utc_now()).isoformat())
                    if commit_obs != observed:
                        incoming = [
                            r.model_copy(update={"attributes": {**r.attributes, "observed": commit_obs}})
                            for r in incoming
                        ]
                kept = records.retention(store, src, incoming, cutoff=cutoff, expire_stored=expire_stored)
                if snapshot and not allow_removal and (refusal := _shrink(store, src, name, kept)):
                    raise Error(refusal)
                result.update(records.upsert(store, src, kept.kept, snapshot=snapshot, removals=kept.removals))
                if job.keep is not None:
                    pruned = records.prune(store, src, job.keep, re.compile(r".*"))
                    result["expired"] = kept.expired + pruned
                else:
                    result["expired"] = kept.expired
                result.update(records=len(kept.kept), forgotten=kept.forgotten)
                if not result["expired"]:
                    result.pop("expired")
                if not result["forgotten"]:
                    result.pop("forgotten")
                committed = True
                result["elapsed_seconds"] = round(time.monotonic() - measured, 6)

            coverage, latest = _coverage(previous, start, end, observed, job)
            streak = int(cast("int", previous.get("incomplete_runs", 0)))
            runs = int(cast("int", previous.get("runs", 0))) + 1
            metrics = {k: v for k, v in result.items() if k not in {"job", "source", "incomplete", "cursor"}}
            queue = Queue(store)
            state_patch: dict[str, object] = {
                "last_run": observed,
                **coverage,
                "last_error": "",
                "error_count": 0,
                "runs": runs,
                "forgotten": 0,
                "expired": 0,
                **({"last_success": observed} if latest else {}),
                **({"last_reconcile": observed} if reconcile and latest else {}),
                **({"cursor": next_cursor} if cursor_out is not None else {}),
                **metrics,
                **(
                    {**incomplete, "incomplete_runs": streak + 1}
                    if incomplete
                    else {"missed": 0, "of": 0, "incomplete_runs": 0}
                    if streak
                    else {}
                ),
            }
            queue.remember(name, **state_patch)
            for ev_kind, key in (
                ("record.created", "added"),
                ("record.updated", "updated"),
                ("record.removed", "removed"),
            ):
                if count := cast("int", result.get(key, 0)):
                    queue.record_event(ev_kind, src, {"source": src, "job": name, "count": count})
            queue.record_event(
                "job.completed", name, {"job": name, "emit": "records", "source": src, "records": result["records"]}
            )
        except (Error, OSError, UnicodeError) as error:
            base = (
                str(error)
                if isinstance(error, Error)
                else "collector files are inaccessible; check executable, record permissions and free space"
            )
            msg = (
                "records were committed but local run history could not be saved; retry is safe"
                if committed
                else f"{base}; nothing was written"
            )
            jobs.record_failure(store, name, error, msg, dry_run=dry_run, at=observed)
        counts_msg = ", ".join(f"{result.get(k, 0)} {k}" for k in ("added", "updated", "unchanged", "removed"))
        missing = f"; missed {incomplete['missed']} of {incomplete['of']}" if incomplete else ""
        with suppress(Error, OSError):
            append_log(store, name, f"{_now()} collected {result['records']} records: {counts_msg}{missing}")
        return result


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
    counts = {"records": 0, "changed": 0, "unchanged": 0, "failed": 0}
    problems: list[dict[str, str]] = []
    with ExitStack() as stack:
        stack.enter_context(collecting(store, job_name))
        if dry_run:
            stack.enter_context(records.reading(store))
        else:
            stack.enter_context(writer(store, wait=120))
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
                    msg = explain(error, RECORD_KEYS) if isinstance(error, ValidationError) else str(error)
                    problems.append({"file": file, "error": msg.removeprefix(f"{file}: ")})
                continue
            if normalized == stored:
                counts["unchanged"] += 1
                continue
            counts["changed"] += 1
            if dry_run:
                continue
            changed.append(normalized)
            if len(changed) >= REPROJECT:
                records.upsert(store, src, changed, snapshot=False)
                changed = []
        if changed and not dry_run:
            records.upsert(store, src, changed, snapshot=False)
    return {
        "job": job_name,
        "source": src,
        "dry_run": dry_run,
        **counts,
        **({"problems": problems[:LIMIT]} if problems else {}),
        **({"problems_truncated": True} if len(problems) > LIMIT else {}),
    }


def next_due(job: Job, last: Mapping[str, object], now: datetime, *, default_tz: str | None = None) -> datetime | None:
    """UTC instant when an enabled scheduled job is next due; None when manual or disabled."""
    if not job.enabled:
        return None
    timed = [t for t in job.triggers if t.every is not None or t.at is not None]
    if not timed:
        return None
    if last.get("last_error") or last.get("error"):
        cap = timedelta(seconds=min(t.every or 86_400 for t in timed))
        errs = max(1, int(str(last.get("error_count", last.get("failures", 1)))))
        delay = min(cap, timedelta(seconds=SCHEDULE_BACKOFF * 2 ** min(errs - 1, 30)))
        anchor = str(last.get("last_run") or last.get("run") or "")
        if not anchor:
            return now
        since = datetime.fromisoformat(anchor)
        return now if since > now else since + delay
    last_success = str(last.get("last_success") or last.get("success") or "")
    return min(t.schedule_due(now, last_success, timezone=default_tz) for t in timed)


def _is_due(job: Job, last: Mapping[str, object], now: datetime, tz: str | None) -> bool:
    return (due_at := next_due(job, last, now, default_tz=tz)) is not None and due_at <= now


def due(store: Store, now: datetime) -> list[tuple[str, str, str, bool]]:
    """Due `emit: records` jobs with their `[start, end)` window and reconcile flag."""
    config, states = load(store), Queue(store).all_states()
    windows: list[tuple[str, str, str, bool]] = []
    for name, job in sorted(config.jobs.items()):
        if job.emit != "records":
            continue
        last = states.get(name, {})
        if not _is_due(job, last, now, config.defaults.timezone):
            continue
        start = now - timedelta(seconds=job.lookback)
        if last.get("window_end") and (job.mode or "window") == "window":
            floor = now - CATCH_UP
            resumed = max(datetime.fromisoformat(str(last["window_end"])), floor) - timedelta(seconds=job.overlap)
            if resumed < now:
                start = max(resumed, floor)
        rec = job.reconcile_spec
        reconciled = str(last.get("last_reconcile") or "")
        revisit = rec is not None and (
            not reconciled
            or not timedelta(0) <= now - datetime.fromisoformat(reconciled) < timedelta(seconds=rec.every)
        )
        if revisit and rec is not None:
            start = min(start, now - timedelta(seconds=rec.lookback or job.lookback))
        windows.append((name, timestamp(start.isoformat()), timestamp(now.isoformat()), revisit))
    return windows


def due_jobs(store: Store, now: datetime) -> list[tuple[str, str, str]]:
    """Due scheduled jobs that do not emit records (`emit != "records"`)."""
    config, states = load(store), Queue(store).all_states()
    items: list[tuple[str, str, str]] = []
    for name, job in sorted(config.jobs.items()):
        if job.emit == "records":
            continue
        last = states.get(name, {})
        if not _is_due(job, last, now, config.defaults.timezone):
            continue
        start = now - timedelta(seconds=job.lookback)
        if last.get("window_end") and (reviewed := datetime.fromisoformat(str(last["window_end"]))) < now:
            start = reviewed
        items.append((name, timestamp(start.isoformat()), timestamp(now.isoformat())))
    return items
