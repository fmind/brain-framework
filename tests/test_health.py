"""Coverage, job health, queue diagnostics and preflight doctor checks."""

from __future__ import annotations

import json
import re
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from bf import health, index, jobs, records, usage
from bf.cli import app
from bf.collect import collect
from bf.config import ABSENT, load, register
from bf.health import attention, doctor, source_health
from bf.models import WEEKDAYS, Error, Query, timestamp
from bf.retrieve import read, search
from bf.runtime import Message, Outgoing, Queue, log_path
from bf.storage import QUEUE, Store


def test_status_describes_each_program_once_in_canonical_utc(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n"
        b"  mail:\n    run: [echo]\n    emit: records\n    mode: window\n    on: {every: 3600}\n"
        b"    reconcile: {every: 86400, lookback: 604800}\n"
        b"  folders:\n    run: [echo]\n    emit: records\n    mode: snapshot\n    on: {every: 86400}\n"
        b"  review:\n    run: [review]\n    emit: log\n    on: {every: 86400}\n",
    )
    now = datetime(2026, 9, 23, 13, tzinfo=UTC)
    at = "2026-09-23T13:00:00.000000Z"
    week = "2026-09-16T13:00:00.000000Z"
    one = b'[{"id":"one","title":"One"}]'
    collect(brain, "mail", start=week, end=at, reconcile=True, runner=lambda *_: one, clock=lambda: now)
    with pytest.raises(Error, match="invalid JSON"):
        collect(brain, "folders", start=week, end=at, runner=lambda *_: b"broken", clock=lambda: now)
    jobs.run_job(brain, "review", since=week, until=at, runner=lambda *_: b"")
    status = cast("dict", health.report(brain, now=now))
    mail = status["sources"]["mail"]
    # The coverage shape shared with search and read, plus indexed totals and local run details.
    assert set(mail) == {
        "state",
        "mode",
        "freshness",
        "last_collected",
        "window",
        "records",
        "changed",
        "bytes",
        "last_run",
        "reconciled",
    }
    assert (mail["state"], mail["freshness"], mail["records"]) == ("active", "fresh", 1)
    assert (mail["last_collected"], mail["reconciled"], mail["window"]) == (at, at, {"since": week, "until": at})
    assert mail["last_run"]["added"] == 1
    assert status["sources"]["folders"] == {
        "state": "active",
        "mode": "snapshot",
        "freshness": "never",
        "records": 0,
        "bytes": 0,
        "failed": True,
        "error": "invalid JSON document; nothing was written",
        "failures": 1,
        "log": log_path("folders"),
    }
    assert status["jobs"]["review"] == {
        "enabled": True,
        "emit": "log",
        "priority": "normal",
        "state": "active",
        "freshness": "fresh",
        "last_success": at,
        "next_due": "2026-09-24T13:00:00.000000Z",
        "runs": 1,
    }
    assert status["attention"] == [{"kind": "job", "name": "folders", "freshness": "never", "failed": True}]
    assert status["problems"] == []
    assert not status["healthy"]
    # Every reply instant uses one canonical UTC form, so clients can compare them as strings.
    instants = re.findall(r'"(\d{4}-\d\d-\d\dT[^"]*)"', json.dumps(status))
    assert instants
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", instant) for instant in instants)


def test_history_offsets_read_back_as_canonical_utc_in_status_and_home(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n"
        b"  mail:\n    run: [echo]\n    emit: records\n    mode: window\n    on: {every: 3600}\n"
        b"  review:\n    run: [review]\n    emit: log\n    on: {every: 60}\n",
    )
    local = "2026-09-23T15:00:00+02:00"
    queue = Queue(brain)
    queue.remember("mail", last_success=local, window_start=local, window_end=local, last_run=local)
    queue.remember("review", last_success=local, last_run=local, last_error="failed", error_count=1)
    now = datetime(2026, 9, 23, 14, tzinfo=UTC)
    # Status and the home page's attention list read the same runtime job state.
    replies = [health.report(brain, now=now), read(brain)]
    instants = re.findall(r'"(\d{4}-\d\d-\d\dT[^"]*)"', json.dumps(replies))
    assert "2026-09-23T13:00:00.000000Z" in instants
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", instant) for instant in instants)


def test_invalid_usage_counts_do_not_break_health_or_hide_valid_events(brain: Store) -> None:
    usage.note(brain, "search", 0, kind="none")
    usage.note(brain, "read", 1, kind="note")
    with closing(sqlite3.connect(brain.root / usage.USAGE)) as connection, connection:
        connection.execute("INSERT INTO usage_days(day,search,empty,read) VALUES(?,?,?,?)", ("bad-date", 9, 9, 9))
        connection.execute("INSERT INTO usage_days(day,search,empty,read) VALUES(?,?,?,?)", ("9999-12-31", 9, 9, 9))
        connection.execute(
            "INSERT INTO usage_days(day,search,empty,read) VALUES(?,?,?,?)",
            ((datetime.now(UTC).date() - timedelta(days=1)).isoformat(), -5, 10, -2),
        )
    result = CliRunner().invoke(app, ["status", "--check", "--brain", str(brain.root)])
    assert result.exit_code == 0, result.exception
    expected = {
        "search": 1,
        "empty": 1,
        "read": 1,
        "kinds": {"read": {"note": 1}, "search": {"none": 1}},
        "empty_kinds": {"none": 1},
    }
    assert json.loads(result.stdout)["usage"] == dict.fromkeys(("7d", "30d"), expected)
    brain.write(usage.USAGE, b"not sqlite")
    corrupt = CliRunner().invoke(app, ["status", "--check", "--brain", str(brain.root)])
    assert corrupt.exit_code == 0, corrupt.exception
    assert json.loads(corrupt.stdout)["usage"] == {
        period: {"search": 0, "empty": 0, "read": 0} for period in ("7d", "30d")
    }


def test_collection_coverage_does_not_claim_archives_are_fresh(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n"
        b"  current:\n    run: [echo]\n    emit: records\n    on: {every: 3600}\n"
        b"  missing:\n    run: [echo]\n    emit: records\n    on: {every: 3600}\n"
        b"  paused:\n    run: [echo]\n    emit: records\n    enabled: false\n"
        b"  manual:\n    run: [echo]\n    emit: records\n"
        b"  cursor:\n    run: [echo]\n    emit: records\n    mode: cursor\n    on: {every: 3600}\n",
    )
    queue = Queue(brain)
    queue.remember(
        "current",
        last_success="2026-09-23T12:00:00Z",
        window_start="2026-09-22T12:00:00Z",
        window_end="2026-09-23T12:00:00Z",
    )
    queue.remember("cursor", last_success="2026-09-23T12:00:00Z", cursor="tok-1")
    report = source_health(brain, ["archive"], now=datetime(2026, 9, 23, 13, tzinfo=UTC))
    assert report["current"]["freshness"] == "fresh"
    assert report["current"]["window"] == {
        "since": "2026-09-22T12:00:00.000000Z",
        "until": "2026-09-23T12:00:00.000000Z",
    }
    assert report["cursor"]["mode"] == "cursor"
    assert report["cursor"]["freshness"] == "fresh"
    assert "window" not in report["cursor"]
    assert report["missing"]["freshness"] == "never"
    assert report["paused"]["state"] == "disabled"
    assert report["archive"] == {"state": "historical", "freshness": "unknown"}
    assert report["manual"]["freshness"] == "manual"
    assert source_health(brain, now=datetime(2026, 9, 23, 15, tzinfo=UTC))["current"]["freshness"] == "overdue"


def test_status_keeps_indexed_totals_separate_from_last_run_counts(brain: Store) -> None:
    brain.write("bf.yaml", b"version: 11\nname: fixture\njobs:\n  current:\n    run: [echo]\n    emit: records\n")
    brain.write(
        "memories/current/ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb.json",
        b'{"id":"a","title":"First"}\n',
    )
    brain.write(
        "memories/current/3e23e8160039594a33894f6564e1b1348bbd7a0088d42c4acb73eeaed59c009d.json",
        b'{"id":"b","title":"Second"}\n',
    )
    Queue(brain).remember("current", records=1, unchanged=1)
    result = CliRunner().invoke(app, ["status", "--brain", str(brain.root)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["sources"]["current"]["records"] == 2
    assert report["sources"]["current"]["last_run"] == {"records": 1, "unchanged": 1}
    assert report["coverage"]["active"] == {"sources": 1, "records": 2}


def test_snapshot_health_does_not_claim_a_historical_window_and_disabled_failures_are_inactive(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n  agenda:\n    run: [echo]\n    emit: records\n    mode: snapshot\n    enabled: false\n",
    )
    Queue(brain).remember(
        "agenda",
        window_start="2026-09-22T00:00:00Z",
        window_end="2026-09-23T00:00:00Z",
        last_error="old failure",
        error_count=1,
    )
    entry = source_health(brain)["agenda"]
    assert entry["mode"] == "snapshot"
    assert "window" not in entry
    result = CliRunner().invoke(app, ["status", "--check", "--brain", str(brain.root)])
    assert result.exit_code == 0, result.output


def test_reads_and_searches_report_the_same_freshness_as_status(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n  meetings:\n    run: [echo]\n    emit: records\n    on: {every: 3600}\n",
    )
    record = cast("dict[str, object]", read(brain, "meetings:decision-1")["collection"])
    found = cast("list[dict[str, object]]", search(brain, Query(text="offline retrieval"))["sources"])
    status = source_health(brain)["meetings"]
    # The scheduled job never succeeded on this machine: every reply says so.
    assert record["freshness"] == found[0]["freshness"] == status["freshness"] == "never"
    register(brain)
    record = cast("dict[str, object]", read(brain, "meetings:decision-1")["collection"])
    assert record["freshness"] == "never"


def test_status_fails_on_a_broken_brain(tmp_path: Path) -> None:
    (tmp_path / "other-dir").mkdir()
    other = Store(tmp_path / "other-dir")
    other.write("bf.yaml", b"version: 11\nname: other\n")
    register(other, default=True)
    other.write("bf.yaml", b"version: 11\nname: [broken\n")
    for command in (["status"], ["status", "--check"], ["status", "--brain", str(other.root)]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 1
        assert "bf.yaml: invalid YAML at line 3" in str(result.exception)


def test_status_fails_when_the_default_brain_is_absent(tmp_path: Path) -> None:
    (tmp_path / "moved").mkdir()
    moved = Store(tmp_path / "moved")
    moved.write("bf.yaml", b"version: 11\nname: moved\n")
    register(moved, default=True)
    (tmp_path / "moved").rename(tmp_path / "elsewhere")
    result = CliRunner().invoke(app, ["status", "--check"])
    assert result.exit_code == 1
    assert ABSENT in str(result.exception)


def test_status_warns_without_failing_when_a_scanned_tree_nears_the_scan_limit(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = ["status", "--check", "--brain", str(brain.root)]
    assert "warnings" not in json.loads(CliRunner().invoke(app, command).stdout)
    # Pretend the two meeting records fill over 80% of what one source may hold.
    monkeypatch.setattr(index, "CROWDED", 1)
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 0, result.output
    entry = json.loads(result.stdout)
    assert entry["warnings"] == [
        {"warning": "directory nears the scan limit", "directory": "memories/meetings", "entries": 2, "limit": 100_000}
    ]
    assert json.loads(result.stdout)["healthy"]


@pytest.mark.parametrize("cached", [True, False])
def test_status_reports_an_interrupted_transaction_instead_of_failing(brain: Store, *, cached: bool) -> None:
    if cached:
        index.refresh(brain)
    decision = records.path("meetings", "decision-1")
    brain.write("memories/.pending/0.before", brain.read(decision))
    brain.write(
        "memories/.pending/manifest.json",
        json.dumps({"source": "meetings", "changes": [{"name": decision.rsplit("/", 1)[1], "existed": True}]}).encode(),
    )
    command = ["status", "--check", "--brain", str(brain.root)]
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 1
    entry = json.loads(result.stdout)
    # Status never applies the journal: it describes the last generation and names the recovery.
    assert entry["pending_transaction"] is True
    assert entry["problems"][0]["error"].endswith("run bf update or bf build to recover it")
    assert (entry["cache"], entry["notes"]) == (("stale", 2) if cached else ("missing", 0))
    assert brain.read("memories/.pending/manifest.json")
    assert CliRunner().invoke(app, ["build", "--brain", str(brain.root)]).exit_code == 0
    recovered = json.loads(CliRunner().invoke(app, command).stdout)
    assert "pending_transaction" not in recovered
    assert (recovered["cache"], recovered["problems"]) == ("ready", [])


def test_status_reports_the_bytes_each_source_holds(brain: Store) -> None:
    sizes = [(brain.root / records.path("meetings", key)).stat().st_size for key in ("decision-1", "lunch")]
    entry = cast("dict", health.report(brain))
    assert entry["sources"]["meetings"]["bytes"] == sum(sizes)


def test_a_source_that_succeeds_without_new_evidence_falls_silent(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n"
        b"  papers:\n    run: [echo]\n    emit: records\n    on: {every: 86400}\n    silent_after: 345600\n"
        b"  quiet:\n    run: [echo]\n    emit: records\n    on: {every: 86400}\n",
    )
    first = datetime(2026, 9, 20, 13, tzinfo=UTC)
    one = b'[{"id":"one","title":"One","attributes":{"time":"2026-09-10T00:00:00Z"}}]'
    for source in ("papers", "quiet"):
        collect(
            brain,
            source,
            start="2026-09-19T13:00:00Z",
            end="2026-09-20T13:00:00Z",
            runner=lambda *_: one,
            clock=lambda: first,
        )
    # Daily runs keep succeeding with the same record: fresh, but nothing new for five days.
    later = datetime(2026, 9, 25, 13, tzinfo=UTC)
    for source in ("papers", "quiet"):
        collect(
            brain,
            source,
            start="2026-09-20T13:00:00Z",
            end="2026-09-25T13:00:00Z",
            runner=lambda *_: one,
            clock=lambda: later,
        )
    index.refresh(brain)
    full = cast("dict", health.report(brain, now=later))
    papers = full["sources"]["papers"]
    # The event time is old, the change is when this brain first saw the record.
    assert (papers["freshness"], papers["silent"], papers["changed"]) == ("fresh", True, "2026-09-20T13:00:00.000000Z")
    assert "silent" not in full["sources"]["quiet"]
    assert full["attention"] == [{"kind": "job", "name": "papers", "freshness": "fresh", "silent": True}]
    assert full["healthy"] is False
    # A changed record ends the silence.
    two = b'[{"id":"one","title":"One, revised","attributes":{"time":"2026-09-10T00:00:00Z"}}]'
    collect(
        brain,
        "papers",
        start="2026-09-20T13:00:00Z",
        end="2026-09-25T13:00:00Z",
        runner=lambda *_: two,
        clock=lambda: later,
    )
    index.refresh(brain)
    assert cast("dict", health.report(brain, now=later))["attention"] == []


def test_status_names_the_installed_and_pinned_runtime(brain: Store) -> None:
    status = cast("dict", health.report(brain))
    assert set(status["runtime"]) == {"installed"}
    brain.write(
        "uv.lock",
        b'version = 1\n\n[[package]]\nname = "brain-framework"\nversion = "17.2.0"\n\n'
        b'[[package]]\nname = "other"\nversion = "1.0"\n',
    )
    assert cast("dict", health.report(brain))["runtime"]["pinned"] == "17.2.0"
    # A lock that does not parse pins nothing: status never fails on it, even nested past the recursion limit or
    # holding an integer past Python's digit limit.
    for lock in (b"[[package\n", b"a = " + b"[" * 100_000 + b"]" * 100_000, b"a = " + b"1" * 5000):
        brain.write("uv.lock", lock)
        assert "pinned" not in cast("dict", health.report(brain))["runtime"]


DISPATCH = b"""version: 11
name: fixture
defaults:
  harness: [harness]
jobs:
  chat-send:
    send: [send-chat]
    idempotent: true
  chat-in:
    send: [receive-chat]
    on: {daemon: true}
    emit: messages
  personal:
    prompt: prompts/personal.md
    emit: message
    on: {event: {kind: [message.received]}}
  notes:
    prompt: prompts/personal.md
    emit: message
"""


def dispatched(brain: Store) -> Store:
    brain.write("bf.yaml", DISPATCH)
    brain.write("prompts/personal.md", b"Answer {{context}}.\n")
    return brain


def incoming(key: str = "message-1") -> Message:
    return Message(
        id=key, sender="owner", conversation="thread-1", time=timestamp(datetime.now(UTC).isoformat()), text="Why?"
    )


def test_retrieval_and_status_never_create_or_write_lock_the_queue(brain: Store) -> None:
    dispatched(brain)
    database = brain.root / QUEUE
    search(brain, Query(text="offline retrieval"))
    read(brain, "meetings:decision-1")
    read(brain)
    assert health.report(brain)["healthy"]
    # Reading reports nothing yet: only execution creates and binds the queue.
    assert not database.exists()
    queue = Queue(brain)
    queue.admit("chat-in", [incoming()])
    _key, _ = queue.enqueue_delivery(
        "chat-send",
        Outgoing(
            id="out-1",
            conversation="thread-1",
            text="hi",
        ),
        ["send-chat"],
        idempotent=True,
        job="personal",
    )
    # An execution transaction holds the queue's write lock: readers neither wait for it nor take it.
    writer = sqlite3.connect(database, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        status = cast("dict", health.report(brain))
        assert read(brain, "meetings:decision-1")["ref"] == "meetings:decision-1"
        assert time.monotonic() - started < 5
    finally:
        writer.close()
    assert status["queue"]["pending"] == 1


def test_a_queue_execution_refusal_still_leaves_reads_and_status_working(brain: Store) -> None:
    dispatched(brain)
    index.refresh(brain)
    queue = Queue(brain)
    key, _ = queue.enqueue_delivery(
        "chat-send",
        Outgoing(
            id="out-1",
            conversation="thread-1",
            text="hi",
        ),
        ["send-chat"],
        idempotent=True,
        job="personal",
    )
    # Renaming the brain leaves its queue bound to the former name.
    brain.write("bf.yaml", DISPATCH.replace(b"name: fixture", b"name: renamed"))
    assert read(brain, "meetings:decision-1")["ref"] == "meetings:decision-1"
    status = cast("dict", health.report(brain))
    assert not status["healthy"]
    refused = "runtime queue holds unresolved work under a previous path or name; resolve via bf queue first"
    assert [problem["error"] for problem in status["problems"]] == [refused]
    assert status["jobs"]["personal"]["freshness"] == "manual"
    # Drained under its former name, the queue follows the renamed brain cleanly.
    brain.write("bf.yaml", DISPATCH)
    queue.retry(key, cancel=True)
    brain.write("bf.yaml", DISPATCH.replace(b"name: fixture", b"name: renamed"))
    recovered = cast("dict", health.report(brain))
    assert recovered["problems"] == []
    assert recovered["healthy"]


def test_status_names_unresolved_work_and_recovers_once_it_is_resolved(brain: Store) -> None:
    dispatched(brain)
    index.refresh(brain)
    queue = Queue(brain)

    def failing(*_: object) -> bytes:
        raise Error("harness timed out")

    # A manual job run that failed, and a reply delivery whose transport failed.
    with pytest.raises(Error, match="harness timed out"):
        jobs.run_job(brain, "notes", runner=failing)

    def send_failing(argv: list[str], *_: object) -> bytes:
        if argv[0] == "send-chat":
            raise Error("send failed")
        return b"Because."

    post = jobs.post_command(brain, "Because.", to="chat-send", thread="thread-1", deliver=True, runner=send_failing)
    delivery_id = str(post["id"])
    queue.set_progress("health:daemon:chat-in", {"error": "stream exited", "at": "2026-10-17T00:00:00.000000Z"})
    status = cast("dict", health.report(brain))
    assert not status["healthy"]
    assert {"kind": "job", "name": "notes", "freshness": "manual", "failed": True} in status["attention"]
    assert {"kind": "job", "name": "chat-in", "freshness": "manual", "failed": True} in status["attention"]
    assert {"kind": "job", "name": "chat-send", "freshness": "manual", "failed": True} in status["attention"]
    # Clear the failed run state, cancel the pending delivery and clear the daemon error.
    queue.remember("notes", last_error="", error_count=0)
    queue.retry(delivery_id, cancel=True)
    queue.set_progress("health:daemon:chat-in", {"error": "", "at": "2026-10-17T00:01:00.000000Z"})
    recovered = cast("dict", health.report(brain))
    assert recovered["attention"] == []
    assert recovered["healthy"]


def test_a_wall_clock_schedule_is_overdue_once_a_slot_after_its_last_success_passes_unrun(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n"
        b"  daily:\n    run: [daily]\n    emit: log\n    on: {at: '09:00', timezone: UTC}\n"
        b"  weekly:\n    run: [weekly]\n    emit: log\n    on: {at: '09:00', timezone: UTC, days: [mon]}\n",
    )
    queue, now = Queue(brain), datetime(2026, 9, 27, 9, 30, tzinfo=UTC)  # a Sunday

    def freshness(name: str, success: datetime) -> object:
        queue.remember(name, last_success=timestamp(success.isoformat()))
        return cast("dict", health.report(brain, now=now)["jobs"])[name]["freshness"]

    # Three missed daily slots, measured from the first slot after the last success rather than the next one.
    assert freshness("daily", datetime(2026, 9, 24, 9, 0, 5, tzinfo=UTC)) == "overdue"
    assert {"kind": "job", "name": "daily", "freshness": "overdue"} in attention(brain, now=now)
    # A success just after today's slot, or yesterday's while today's runs, stays fresh.
    assert freshness("daily", datetime(2026, 9, 27, 9, 0, 5, tzinfo=UTC)) == "fresh"
    assert freshness("daily", datetime(2026, 9, 26, 9, 0, 5, tzinfo=UTC)) == "fresh"
    # A weekly job that ran last Monday is fresh on Sunday, and overdue after five missed weeks.
    assert freshness("weekly", datetime(2026, 9, 21, 9, 0, 5, tzinfo=UTC)) == "fresh"
    assert freshness("weekly", datetime(2026, 8, 24, 9, 0, 5, tzinfo=UTC)) == "overdue"


def test_a_schedule_without_a_success_needs_attention_only_once_due(brain: Store) -> None:
    morning = b"  morning:\n    run: [morning]\n    emit: log\n    on: {at: '07:30', timezone: UTC, days: [mon, tue, wed, thu, fri]}\n"
    brain.write("bf.yaml", b"version: 11\nname: fixture\njobs:\n" + morning)
    sunday, monday = datetime(2026, 9, 27, 19, 5, tzinfo=UTC), datetime(2026, 9, 28, 7, 31, tzinfo=UTC)
    # Declared on Sunday evening: it has not run, which its freshness says, but its first slot is Monday morning.
    status = cast("dict", health.report(brain, now=sunday))
    job_entry = status["jobs"]["morning"]
    assert job_entry["freshness"] == "never"
    assert job_entry["next_due"] == timestamp(monday.replace(minute=30).isoformat())
    assert (status["attention"], status["healthy"]) == ([], True)
    assert attention(brain, now=sunday) == []
    # Monday's slot passed without a run: the scheduler would start it now, so it is late.
    late = [{"kind": "job", "name": "morning", "freshness": "never"}]
    status = cast("dict", health.report(brain, now=monday))
    assert (status["attention"], status["healthy"]) == (late, False)
    assert attention(brain, now=monday) == late
    # An interval schedule without a success is due at once beside a waiting wall-clock job.
    hourly = b"  hourly:\n    run: [hourly]\n    emit: log\n    on: {every: 3600}\n"
    brain.write("bf.yaml", b"version: 11\nname: fixture\njobs:\n" + morning + hourly)
    assert attention(brain, now=sunday) == [{"kind": "job", "name": "hourly", "freshness": "never"}]
    Queue(brain).remember("hourly", last_success=timestamp(sunday.isoformat()))
    assert attention(brain, now=sunday) == []


def test_status_check_passes_before_a_new_schedule_first_falls_due(brain: Store) -> None:
    # A weekday three days away is never today, even if the test crosses midnight.
    later = WEEKDAYS[(datetime.now(UTC).weekday() + 3) % 7]
    waiting = f"  weekly:\n    run: [weekly]\n    emit: log\n    on: {{at: '00:00', timezone: UTC, days: [{later}]}}\n".encode()
    command = ["status", "--check", "--brain", str(brain.root)]
    brain.write("bf.yaml", b"version: 11\nname: fixture\njobs:\n" + waiting)
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["jobs"]["weekly"]["freshness"] == "never"
    hourly = b"  hourly:\n    run: [hourly]\n    emit: log\n    on: {every: 3600}\n"
    brain.write("bf.yaml", b"version: 11\nname: fixture\njobs:\n" + waiting + hourly)
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 1
    assert json.loads(result.stdout)["attention"] == [{"kind": "job", "name": "hourly", "freshness": "never"}]


def test_coverage_marks_a_record_job_with_failed_latest_run_as_failed(brain: Store) -> None:
    brain.write("bf.yaml", b"version: 11\nname: fixture\njobs:\n  notes:\n    run: [echo]\n    emit: records\n")
    now = datetime(2026, 9, 23, 13, tzinfo=UTC)
    start, end = "2026-09-22T13:00:00Z", "2026-09-23T13:00:00Z"
    collect(
        brain, "notes", start=start, end=end, runner=lambda *_: b'[{"id":"n1","title":"Checked"}]', clock=lambda: now
    )
    with pytest.raises(Error, match="collector timed out"):
        collect(
            brain,
            "notes",
            start=start,
            end=end,
            runner=lambda *_: (_ for _ in ()).throw(Error("collector timed out")),
            clock=lambda: now,
        )

    def coverage(reply: dict[str, object]) -> dict[str, object]:
        return next(entry for entry in cast("list[dict]", reply["sources"]) if entry["source"] == "notes")

    # Search coverage and status say what the latest run did, even with a stored record present.
    assert coverage(search(brain, Query(text="Checked", prefix="memories/notes")))["failed"] is True
    assert coverage(search(brain, Query(text="offline retrieval")))["failed"] is True
    assert cast("dict", health.report(brain))["sources"]["notes"]["failed"] is True
    Queue(brain).remember("notes", last_error="", error_count=0)
    assert "failed" not in coverage(search(brain, Query(text="Checked", prefix="memories/notes")))
    assert "failed" not in source_health(brain)["notes"]


def test_status_says_an_interrupted_queue_write_recovers_with_bf_queue(brain: Store) -> None:
    dispatched(brain)
    index.refresh(brain)
    queue = Queue(brain)
    queue.set_progress("created", {})
    database = brain.root / QUEUE
    # A writer killed after spilling pages to disk leaves a rollback journal that read-only inspection surfaces.
    killed = (
        "import os, signal, sqlite3, sys\n"
        "db = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "db.execute('PRAGMA cache_size=5')\n"
        "db.execute('BEGIN IMMEDIATE')\n"
        "for i in range(2000):\n"
        "    db.execute('INSERT INTO checkpoints VALUES (?,?)', (f'spill:{i}', 'x' * 500))\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    process = subprocess.run([sys.executable, "-I", "-c", killed, str(database)], timeout=60, check=False)  # noqa: S603
    assert process.returncode == -signal.SIGKILL
    assert database.with_name(database.name + "-journal").exists()
    interrupted = "runtime database holds an interrupted write; run bf queue to recover it"
    entry = cast("dict", health.report(brain))
    assert {"error": interrupted} in entry["problems"]
    assert not entry["healthy"]
    doc = doctor(brain)
    assert doc["problems"] == [{"error": interrupted}]
    assert not doc["healthy"]
    # Any queue write rolls the interrupted journal back.
    queue.compact()
    assert not database.with_name(database.name + "-journal").exists()
    assert queue.progress("spill:0") == {}
    entry = cast("dict", health.report(brain))
    assert entry["problems"] == []


def test_status_and_coverage_describe_one_bf_yaml_while_it_is_saved(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatched(brain)
    brain.write("bf.yaml", DISPATCH.replace(b"  notes:", b"  renamed:"))
    renamed = load(brain)
    brain.write("bf.yaml", DISPATCH)
    original = load(brain)
    # An editor's atomic save lands right after the first load: every later one reads the renamed job.
    loads = iter([original])
    monkeypatch.setattr("bf.health.load", lambda _store: next(loads, renamed))
    entry = cast("dict", health.report(brain))
    assert set(entry["jobs"]) == {"chat-in", "chat-send", "notes", "personal"}
    loads = iter([original])
    assert attention(brain) == []
    brain.write("bf.yaml", b"version: 11\nname: fixture\njobs:\n  mail:\n    run: [echo]\n    emit: records\n")
    src_a = load(brain)
    brain.write("bf.yaml", b"version: 11\nname: fixture\njobs:\n  renamed:\n    run: [echo]\n    emit: records\n")
    src_b = load(brain)
    loads = iter([src_a])
    monkeypatch.setattr("bf.health.load", lambda _store: next(loads, src_b))
    assert set(source_health(brain)) == {"mail"}


def test_disabled_programs_report_no_failure_while_their_queued_work_stays_listed(brain: Store) -> None:
    configured = (
        b"version: 11\nname: fixture\ndefaults:\n  harness: [harness]\njobs:\n"
        b"  mail:\n    run: [mail]\n    emit: records\n    on: {every: 3600}\n"
        b"  review:\n    run: [review]\n    emit: log\n    on: {every: 3600}\n"
        b"  notes:\n    prompt: prompts/personal.md\n    emit: message\n"
    )
    brain.write("bf.yaml", configured)
    brain.write("prompts/personal.md", b"Answer {{context}}.\n")

    def failing(*_: object) -> bytes:
        raise Error("provider timed out")

    def cancelled(*_: object) -> bytes:
        raise jobs.CancelledError("interrupted")

    start, end = "2026-10-30T00:00:00Z", "2026-10-31T00:00:00Z"
    with pytest.raises(Error, match="provider timed out"):
        collect(brain, "mail", start=start, end=end, runner=failing)
    with pytest.raises(Error, match="provider timed out"):
        jobs.run_job(brain, "review", runner=failing)
    with pytest.raises(jobs.CancelledError, match="interrupted"):
        jobs.run_job(brain, "notes", runner=cancelled)
    status = cast("dict", health.report(brain))
    assert not status["healthy"]
    assert {"failed", "error", "log"} <= status["sources"]["mail"].keys()
    assert {"failed", "error", "log"} <= status["jobs"]["review"].keys()
    assert {"failed", "error", "log"} <= status["jobs"]["notes"].keys()
    # Disabled jobs hide their failure diagnostics and leave attention empty, while preserving queue state.
    disabled = configured.replace(b"    on: {every: 3600}\n", b"    on: {every: 3600}\n    enabled: false\n").replace(
        b"    emit: message\n",
        b"    emit: message\n    enabled: false\n",
    )
    brain.write("bf.yaml", disabled)
    status = cast("dict", health.report(brain))
    failure = {"failed", "error", "failures", "log"}
    assert not failure & status["sources"]["mail"].keys()
    assert not failure & status["jobs"]["mail"].keys()
    assert not failure & status["jobs"]["review"].keys()
    assert not failure & status["jobs"]["notes"].keys()
    assert (status["sources"]["mail"]["state"], status["jobs"]["notes"]["state"]) == ("disabled", "disabled")
    assert status["attention"] == []
    assert status["queue"]["uncertain"] == 1
    coverage = {item["source"]: item for item in cast("list[dict]", search(brain, Query(text="anything"))["sources"])}
    assert not failure & coverage["mail"].keys()
    # Enabled again, unresolved failures surface immediately.
    brain.write("bf.yaml", configured)
    assert not cast("dict", health.report(brain))["healthy"]


def test_a_source_that_keeps_missing_parts_needs_attention_after_three_runs(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n"
        b"  feeds:\n    run: [echo]\n    emit: records\n    on: {every: 3600}\n"
        b"  manual:\n    run: [echo]\n    emit: records\n",
    )
    now = datetime.now(UTC)
    partial = b'{"records":[{"id":"one","title":"One"}],"incomplete":{"missed":1,"of":3}}'

    def run(source: str, output: bytes) -> None:
        start, end = timestamp((now - timedelta(hours=1)).isoformat()), timestamp(now.isoformat())
        collect(brain, source, start=start, end=end, runner=lambda *_: output, clock=lambda: now)

    for runs in (1, 2):
        run("feeds", partial)
        entry = cast("dict", health.report(brain, now=now))
        # Shown from the first incomplete run beside freshness the run still earned.
        assert entry["sources"]["feeds"]["degraded"] == {"runs": runs, "missed": 1, "of": 3}
        assert entry["sources"]["feeds"]["freshness"] == "fresh"
        assert entry["attention"] == []
    assert cast("dict", health.report(brain, now=now))["healthy"] is True
    run("feeds", partial)
    run("manual", partial)
    full = cast("dict", health.report(brain, now=now))
    # A manual source shows its gap but, like its failures, needs no attention.
    assert full["sources"]["manual"]["degraded"] == {"runs": 1, "missed": 1, "of": 3}
    assert full["attention"] == [{"kind": "job", "name": "feeds", "freshness": "fresh", "degraded": True}]
    assert full["healthy"] is False
    assert CliRunner().invoke(app, ["status", "--check", "--brain", str(brain.root)]).exit_code == 1
    # Home page, search coverage and memories pages share attention and source state.
    assert {"kind": "job", "name": "feeds", "freshness": "fresh", "degraded": True} in cast("dict", read(brain))[
        "attention"
    ]
    found = cast("dict", search(brain, Query(text="one")))
    assert {source["source"]: source.get("degraded") for source in found["sources"]}["feeds"]["runs"] == 3
    memories = cast("dict", read(brain, "memories"))
    assert {source["source"]: source.get("degraded") for source in memories["sources"]}["manual"]["runs"] == 1
    assert cast("dict", read(brain, "feeds:one"))["collection"]["degraded"]["missed"] == 1
    # The next complete run clears it.
    run("feeds", b'[{"id":"one","title":"One"}]')
    entry = cast("dict", health.report(brain, now=now))
    assert "degraded" not in entry["sources"]["feeds"]
    assert entry["attention"] == []


def test_hand_edited_incomplete_counts_never_report_a_gap(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n  feeds:\n    run: [echo]\n    emit: records\n    on: {every: 3600}\n",
    )
    queue = Queue(brain)
    for counts in (
        {"incomplete_runs": 3},
        {"incomplete_runs": 3, "missed": 2, "of": 1},
        {"incomplete_runs": 0, "missed": 1, "of": 2},
    ):
        queue.remember("feeds", **counts)
        assert "degraded" not in source_health(brain)["feeds"]
        assert attention(brain) == [{"kind": "job", "name": "feeds", "freshness": "never"}]


def test_home_and_review_attention_include_failed_jobs(brain: Store) -> None:
    dispatched(brain)
    with pytest.raises(Error, match="boom"):
        jobs.run_job(brain, "notes", runner=lambda *_: (_ for _ in ()).throw(Error("boom")))
    Queue(brain).set_progress("health:daemon:chat-in", {"error": "transport down", "at": "2026-10-17T00:00:00.000000Z"})
    expected = cast("dict", health.report(brain))["attention"]
    assert {"kind": "job", "name": "notes", "freshness": "manual", "failed": True} in expected
    assert {"kind": "job", "name": "chat-in", "freshness": "manual", "failed": True} in expected
    assert read(brain)["attention"] == expected
    assert read(brain, "review")["attention"] == expected


def test_doctor_checks_bins_env_scripts_prompts_and_runtime_without_leaking_secrets(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_marker = "synthetic-doc-env-marker-12345"
    monkeypatch.setenv("BF_DOC_PRESENT", env_marker)
    monkeypatch.delenv("BF_DOC_MISSING", raising=False)
    brain.write("sensors/nonexec.sh", b"#!/bin/sh\necho ok\n")
    brain.write("sensors/good.sh", b"#!/bin/sh\necho ok\n")
    (brain.root / "sensors/good.sh").chmod(0o700)
    brain.write("prompts/ok.md", b"Hi.\n")
    brain.write("not-a-dir", b"file\n")
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n"
        b"  healthy-job:\n    run: [sensors/good.sh]\n    emit: log\n    requires: {env: [BF_DOC_PRESENT]}\n"
        b"  disabled-broken:\n    enabled: false\n    run: [bf-nonexistent-binary]\n    emit: log\n"
        b"    requires: {bins: [bf-missing-bin], env: [BF_DOC_MISSING]}\n"
        b"  broken-env-bin:\n    run: [bf-nonexistent-cmd]\n    emit: log\n"
        b"    requires: {bins: [bf-missing-bin], env: [BF_DOC_MISSING]}\n"
        b"  broken-script:\n    run: [sensors/nonexec.sh]\n    emit: log\n"
        b"  broken-prompt:\n    prompt: prompts/missing.md\n    cwd: not-a-dir\n    emit: log\n",
    )
    report = cast("dict", doctor(brain))
    assert not report["healthy"]
    # Present secret values must never appear anywhere in bf doctor output.
    assert env_marker not in json.dumps(report)
    assert report["jobs"]["healthy-job"] == {"enabled": True, "ok": True}
    assert report["jobs"]["disabled-broken"] == {"enabled": False, "ok": True}
    assert report["jobs"]["broken-env-bin"]["missing_bins"] == ["bf-missing-bin", "bf-nonexistent-cmd"]
    assert report["jobs"]["broken-env-bin"]["missing_env"] == ["BF_DOC_MISSING"]
    assert "not an executable regular file" in str(report["jobs"]["broken-script"]["error"])
    assert "missing.md" in str(report["jobs"]["broken-prompt"]["error"])
    assert "missing harness" in str(report["jobs"]["broken-prompt"]["error"])
    assert "not-a-dir" in str(report["jobs"]["broken-prompt"]["error"])

    # Corrupt runtime SQLite surfaces in doctor problems while offline doctor CLI exits non-zero.
    brain.write("bf.yaml", b"version: 11\nname: fixture\njobs:\n  healthy-job:\n    run: [sensors/good.sh]\n")
    brain.write(QUEUE, b"corrupt")
    cli = CliRunner().invoke(app, ["doctor", "--brain", str(brain.root)])
    assert cli.exit_code == 1
    parsed = json.loads(cli.stdout)
    assert not parsed["healthy"]
    assert parsed["problems"]
    assert env_marker not in cli.stdout
