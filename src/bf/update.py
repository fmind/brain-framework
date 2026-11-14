"""Run due jobs (records, scheduled, events, wakes, deliveries) of one brain, then refresh its search cache."""

from __future__ import annotations

from collections.abc import Callable, Collection, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Event
from typing import cast

from pydantic import JsonValue

from bf import collect, index
from bf.config import load
from bf.jobs import Runner, deliver_due, dispatch_events, run, run_job
from bf.models import Config, Error, reason, suggest
from bf.runtime import Queue, sync_item_events
from bf.storage import BusyError, Store, collecting

WAIT, WORKERS = 600, 4
_INACCESSIBLE = "inaccessible files; check permissions and free space"


def selection(config: Config, names: tuple[str, ...] = ()) -> set[str]:
    """Explicit job names select only those jobs; an empty tuple selects every configured job."""
    if names and (unknown := sorted(set(names) - config.jobs.keys())):
        raise Error(f"unknown job {unknown[0]}; check jobs in bf.yaml{suggest(unknown[0], config.jobs)}")
    return set(names) if names else set(config.jobs)


def scheduled(config: Config, names: tuple[str, ...] = ()) -> list[tuple[int, str]]:
    """Selected enabled non-daemon jobs with interval triggers, as (every_seconds, name), fastest first."""
    sel = ((n, config.jobs[n]) for n in selection(config, names))
    return sorted((t.every, n) for n, j in sel if j.enabled and not j.is_daemon for t in j.triggers if t.every)


def late(config: Config, every: int, names: tuple[str, ...] = ()) -> str:
    """Why checking every `every` seconds runs the fastest selected job late, or ""."""
    if not (items := scheduled(config, names)) or every < items[0][0]:
        return ""
    r, name = items[0]
    return f"{name} refreshes every {r}s, but checks happen every {every}s: it will run late and status can report it overdue"


@contextmanager
def _cycle(store: Store, wait: float) -> Generator[None]:
    """Serialize record collection cycles per brain; non-record jobs, wakes and deliveries run after it."""
    try:
        with collecting(store, ".update", wait=wait):
            yield
    except BusyError as error:
        raise Error("another update is still active; retry after it finishes") from error


def _exec(
    base: dict[str, object], dry_run: bool, status: str, fn: Callable[[], dict[str, object]]
) -> tuple[dict[str, object], bool]:
    if dry_run:
        return {**base, "status": "due"}, False
    try:
        out = fn()
        return {**base, **out, "status": "gated" if out.get("status") == "gated" else status}, False
    except BusyError:
        return {**base, "status": "skipped", "skipped": "another run of this job is still active"}, False
    except (Error, OSError, UnicodeError) as error:
        return {**base, "status": "failed", "error": reason(error, _INACCESSIBLE)}, True


def _refresh_index(store: Store, reply: dict[str, object], *, sync_events: bool = False) -> bool:
    try:
        idx = index.refresh(store, wait=120, recover=True)
        reply["index"] = idx
        if sync_events:
            sync_item_events(store)
        return bool(idx["skipped"])
    except (Error, OSError, UnicodeError) as error:
        reply["index"] = {"error": reason(error, _INACCESSIBLE)}
        return True


def run_wakes(
    store: Store,
    config: Config,
    selected: Collection[str],
    *,
    dry_run: bool = False,
    runner: Runner = run,
    stopped: Event | None = None,
) -> tuple[list[dict[str, object]], bool]:
    """Claim and run due pending wakes for enabled non-daemon jobs in `selected`."""
    if not (queue := Queue(store)).exists():
        return [], False
    results, failed = [], False
    for w in queue.due_wakes():
        if stopped is not None and stopped.is_set():
            break
        name, wid, job = str(w["job"]), str(w["id"]), config.jobs.get(str(w["job"]))
        if name not in selected or job is None or not job.enabled or job.is_daemon:
            continue
        if not dry_run and not queue.claim_wake(wid):
            continue
        rt, depth = str(w.get("reason") or ""), int(cast("int", w.get("wake_depth", 0)))
        base: dict[str, object] = {"job": name, "emit": job.emit, "kind": "wake", "wake_id": wid}
        fn = partial(run_job, store, name, kind="wake", reason_text=rt, wake_depth=depth, runner=runner)
        res, err = _exec(base, dry_run, "ran", fn)
        if not dry_run:
            msg = "job busy during wake" if res["status"] == "skipped" else str(res.get("error") or "")
            queue.finish_wake(wid, state="failed" if err or res["status"] == "skipped" else "done", error=msg)
        results.append(res)
        failed |= err
    return results, failed


def run_deliveries(
    store: Store, targets: Collection[str] = (), *, runner: Runner = run, stopped: Event | None = None
) -> tuple[int, bool]:
    """Attempt up to currently-pending due outbound deliveries cleanly without infinite retry loops."""
    if not (queue := Queue(store)).exists():
        return 0, False
    pending, attempted, failed = max(0, int(cast("int", queue.status().get("pending", 0)))), 0, False
    for target in sorted(targets) if targets else [""]:
        for _ in range(pending):
            if stopped is not None and stopped.is_set():
                return attempted, failed
            try:
                if not deliver_due(store, job=target, runner=runner):
                    break
                attempted += 1
            except BusyError:
                break
            except Error, OSError, UnicodeError:
                failed = True
                break
    return attempted, failed


def update(
    store: Store,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    runner: Runner = run,
    jobs: tuple[str, ...] = (),
    wait: float = WAIT,
) -> dict[str, object]:
    """Run due record collections, scheduled jobs, event dispatches, wakes and deliveries, then refresh the index."""
    given, config = now, load(store)
    selected, failed, results = selection(config, jobs), False, []
    cycles = not dry_run and any(config.jobs[n].emit == "records" for n in selected)
    with _cycle(store, wait) if cycles else nullcontext():
        now = given or datetime.now(UTC)
        rec_win = [item for item in collect.due(store, now) if item[0] in selected]

        def _collect(item: tuple[str, str, str, bool], run_fn: Runner = runner) -> tuple[dict[str, object], bool]:
            n, s, e, rec = item
            base: dict[str, object] = {"job": n, "emit": "records", "start": s, "end": e, "reconcile": rec}
            p = partial(collect.collect, store, n, start=s, end=e, reconcile=rec)
            return _exec(base, dry_run, "collected", partial(p, runner=run_fn, clock=lambda: now, live=given is None))

        if len(rec_win) > 1 and not dry_run:
            stop = Event()
            par = partial(run, cancelled=stop.is_set) if runner is run else runner
            with ThreadPoolExecutor(max_workers=min(WORKERS, len(rec_win))) as pool:
                try:
                    outcomes = list(pool.map(lambda w: _collect(w, par), rec_win))
                except BaseException:
                    stop.set()
                    raise
        else:
            outcomes = [_collect(w) for w in rec_win]
        results.extend(r for r, _ in outcomes)
        failed |= any(f for _, f in outcomes)
    sched_now = given or max(datetime.now(UTC), now + timedelta(microseconds=1))
    other = [w for w in collect.due_jobs(store, sched_now) if w[0] in selected and not config.jobs[w[0]].is_daemon]
    after_records = len(results)
    for n, s, e in sorted(other, key=lambda w: (config.jobs[w[0]].is_low_priority, w[0])):
        base: dict[str, object] = {"job": n, "emit": config.jobs[n].emit, "kind": "schedule", "start": s, "end": e}
        fn = partial(run_job, store, n, since=s, until=e, kind="schedule", runner=runner)
        res, err = _exec(base, dry_run, "ran", fn)
        results.append(res)
        failed |= err
    reply: dict[str, object] = {"brain": config.name, "jobs": results}
    if not dry_run:
        failed |= _refresh_index(store, reply, sync_events=True)
        if Queue(store).exists():

            def _on_event(n: str, evs: list[JsonValue]) -> dict[str, object]:
                nonlocal failed
                base: dict[str, object] = {"job": n, "emit": config.jobs[n].emit, "kind": "event"}
                fn = partial(run_job, store, n, kind="event", events=evs, runner=runner, check_gate=False)
                out, err = _exec(base, False, "ran", fn)
                results.append(out)
                failed |= err
                return out

            non_daemon = [n for n in sorted(selected) if not config.jobs[n].is_daemon]
            dispatch_events(store, non_daemon, runner=runner, submit=_on_event)
    wake_res, wake_failed = run_wakes(store, config, selected, dry_run=dry_run, runner=runner)
    results.extend(wake_res)
    failed |= wake_failed
    if not dry_run and any(
        r.get("status") == "ran" and ((j := config.jobs[str(r["job"])]).emit == "action" or bool(j.prompt))
        for r in results[after_records:]
    ):
        failed |= _refresh_index(store, reply)
    delivered, del_failed = (0, False) if dry_run else run_deliveries(store, selected if jobs else (), runner=runner)
    failed |= del_failed
    if manual := [
        n
        for n in sorted(selected)
        if (j := config.jobs[n]).enabled
        and (j.run or j.prompt or not j.send)
        and not any(t.every or t.at or t.daemon or t.event for t in j.triggers)
    ]:
        reply["manual"] = manual
    if delivered:
        reply["deliveries"] = delivered
    return {"ok": not failed, "dry_run": dry_run, **reply}
