"""Coverage reports distinguish maintained sources from retained historical evidence."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from bf import assistants, index, records
from bf.cli import app
from bf.collect import collect, routine
from bf.config import ABSENT, register
from bf.health import report, source_health
from bf.history import log_path
from bf.messages import Message
from bf.models import Error, Query, encode, timestamp
from bf.retrieve import read, search
from bf.runtime import DATABASE, Queue
from bf.storage import Store, runtime_store, state_store
from bf.update import update


def test_status_describes_each_program_once_in_canonical_utc(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nsensors:\n"
        b"  mail:\n    command: [echo]\n    refresh: 3600\n    reconcile: {refresh: 86400, lookback: 604800}\n"
        b"  folders:\n    command: [echo]\n    mode: snapshot\n    refresh: 86400\n"
        b"routines:\n  review:\n    command: [review]\n    refresh: 86400\n",
    )
    now = datetime(2026, 9, 23, 13, tzinfo=UTC)
    at = "2026-09-23T13:00:00.000000Z"
    week = "2026-09-16T13:00:00.000000Z"
    one = b'[{"id":"one","title":"One"}]'
    collect(brain, "mail", start=week, end=at, reconcile=True, runner=lambda *_: one, clock=lambda: now)
    with pytest.raises(Error, match="invalid JSON"):
        collect(brain, "folders", start=week, end=at, runner=lambda *_: b"broken", clock=lambda: now)
    routine(brain, "review", start=week, end=at, runner=lambda *_: b"", clock=lambda: now)
    status = cast("dict", report([brain], now=now))["brains"][0]
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
    assert status["routines"]["review"] == {"state": "active", "freshness": "fresh", "last_success": at}
    # Before 17 an unhealthy brain listed no reason: attention names the programs, as the home page does.
    assert status["attention"] == [{"sensor": "folders", "freshness": "never", "failed": True}]
    assert status["problems"] == []
    assert not cast("dict", report([brain], now=now))["healthy"]
    # Every reply instant uses one canonical UTC form, so clients can compare them as strings.
    instants = re.findall(r'"(\d{4}-\d\d-\d\dT[^"]*)"', json.dumps(status))
    assert instants
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", instant) for instant in instants)


def test_history_offsets_read_back_as_canonical_utc_in_status_and_home(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nsensors:\n  mail:\n    command: [echo]\n    refresh: 3600\n"
        b"routines:\n  review:\n    command: [review]\n    refresh: 60\n",
    )
    local = "2026-09-23T15:00:00+02:00"
    state_store(brain.root).write(
        "sensors.json", encode({"mail": {"success": local, "start": local, "end": local, "run": local}})
    )
    state_store(brain.root).write(
        "routines.json", encode({"review": {"success": local, "run": local, "error": "failed"}})
    )
    now = datetime(2026, 9, 23, 14, tzinfo=UTC)
    # Status and the home page's attention list read the same private history.
    replies = [report([brain], now=now), read([brain])]
    instants = re.findall(r'"(\d{4}-\d\d-\d\dT[^"]*)"', json.dumps(replies))
    assert "2026-09-23T13:00:00.000000Z" in instants
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", instant) for instant in instants)


@pytest.mark.parametrize("results", ["1e999", "-1", "true", '"0"', "0.5"])
def test_invalid_usage_counts_do_not_break_health_or_hide_valid_events(brain: Store, results: str) -> None:
    now = datetime.now(UTC).isoformat()
    state_store(brain.root).write(
        "usage.jsonl",
        (
            f'{{"at":"{now}","op":"search","results":{results}}}\n'
            '{"at":"9999-12-31T23:59:59Z","op":"search","results":0}\n'
            f'{{"at":"{now}","op":"search","results":0}}\n'
            f'{{"at":"{now}","op":"read","results":1}}\n'
        ).encode(),
    )
    result = CliRunner().invoke(app, ["status", "--check", "--brain", str(brain.root)])
    assert result.exit_code == 0, result.exception
    usage = json.loads(result.stdout)["brains"][0]["usage"]
    assert usage == {period: {"search": 1, "empty": 1, "read": 1} for period in ("7d", "30d")}


def test_collection_coverage_does_not_claim_archives_are_fresh(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nsensors:\n"
        b"  current:\n    command: [echo]\n    refresh: 3600\n"
        b"  missing:\n    command: [echo]\n    refresh: 3600\n"
        b"  paused:\n    command: [echo]\n    enabled: false\n"
        b"  manual:\n    command: [echo]\n",
    )
    state_store(brain.root).write(
        "sensors.json",
        encode(
            {
                "current": {
                    "success": "2026-09-23T12:00:00Z",
                    "start": "2026-09-22T12:00:00Z",
                    "end": "2026-09-23T12:00:00Z",
                }
            }
        ),
    )
    report = source_health(brain, ["archive"], now=datetime(2026, 9, 23, 13, tzinfo=UTC))
    assert report["current"]["freshness"] == "fresh"
    assert report["current"]["window"] == {
        "since": "2026-09-22T12:00:00.000000Z",
        "until": "2026-09-23T12:00:00.000000Z",
    }
    assert report["missing"]["freshness"] == "never"
    assert report["paused"]["state"] == "disabled"
    assert report["archive"] == {"state": "historical", "freshness": "unknown"}
    assert report["manual"]["freshness"] == "manual"
    assert source_health(brain, now=datetime(2026, 9, 23, 15, tzinfo=UTC))["current"]["freshness"] == "overdue"


def test_status_keeps_indexed_totals_separate_from_last_run_counts(brain: Store) -> None:
    brain.write("bf.yaml", b"version: 8\nname: fixture\nsensors:\n  current:\n    command: [echo]\n")
    brain.write(
        "memories/current/ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb.json",
        b'{"id":"a","title":"First"}\n',
    )
    brain.write(
        "memories/current/3e23e8160039594a33894f6564e1b1348bbd7a0088d42c4acb73eeaed59c009d.json",
        b'{"id":"b","title":"Second"}\n',
    )
    state_store(brain.root).write("sensors.json", encode({"current": {"records": 1, "unchanged": 1}}))
    result = CliRunner().invoke(app, ["status", "--brain", str(brain.root)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["brains"][0]
    assert report["sources"]["current"]["records"] == 2
    assert report["sources"]["current"]["last_run"] == {"records": 1, "unchanged": 1}
    assert report["coverage"]["active"] == {"sources": 1, "records": 2}


def test_snapshot_health_does_not_claim_a_historical_window_and_disabled_failures_are_inactive(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nsensors:\n  agenda:\n    command: [echo]\n    mode: snapshot\n    enabled: false\n",
    )
    state_store(brain.root).write(
        "sensors.json",
        encode({"agenda": {"start": "2026-09-22T00:00:00Z", "end": "2026-09-23T00:00:00Z", "error": "old failure"}}),
    )
    health = source_health(brain)["agenda"]
    assert health["mode"] == "snapshot"
    assert "window" not in health
    result = CliRunner().invoke(app, ["status", "--check", "--brain", str(brain.root)])
    assert result.exit_code == 0, result.output


def test_reads_and_searches_report_the_same_freshness_as_status(brain: Store) -> None:
    brain.write(
        "bf.yaml", b"version: 8\nname: fixture\nsensors:\n  meetings:\n    command: [echo]\n    refresh: 3600\n"
    )
    record = cast("dict[str, object]", read([brain], "meetings:decision-1")["collection"])
    found = cast("list[dict[str, object]]", search([brain], Query(text="offline retrieval"))["sources"])
    status = source_health(brain)["meetings"]
    # The scheduled sensor never succeeded on this machine: every reply says so.
    assert record["freshness"] == found[0]["freshness"] == status["freshness"] == "never"
    register(brain)
    record = cast("dict[str, object]", read([brain], "meetings:decision-1")["collection"])
    assert record["freshness"] == "never"


@pytest.mark.usefixtures("brain")
def test_status_reports_a_broken_brain_and_still_reports_the_others(tmp_path: Path) -> None:
    # The broken brain keeps its registered name, not its directory's.
    (tmp_path / "other-dir").mkdir()
    other = Store(tmp_path / "other-dir")
    other.write("bf.yaml", b"version: 8\nname: other\n")
    register(other)
    other.write("bf.yaml", b"version: 8\nname: [broken\n")
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    brains = json.loads(result.stdout)["brains"]
    assert [entry["brain"] for entry in brains] == ["fixture", "other"]
    assert brains[1]["error"] == "bf.yaml: invalid YAML at line 3, column 1"
    assert CliRunner().invoke(app, ["status", "--check"]).exit_code == 1
    # Word, known-identity and unknown-identity searches report it once, by the same name.
    for query in ("offline", "repo:example/project", "repo:example/absent"):
        found = CliRunner().invoke(app, ["search", query])
        assert found.exit_code == 0, found.output
        assert json.loads(found.stdout)["problems"] == [{"brain": "other", "error": brains[1]["error"]}]
    # A brain chosen alone still fails with the named file.
    alone = CliRunner().invoke(app, ["status", "--brain", str(other.root)])
    assert alone.exit_code == 1
    assert "bf.yaml: invalid YAML at line 3" in str(alone.exception)


def test_status_reports_a_broken_brain_beside_an_unavailable_one(tmp_path: Path) -> None:
    # Of two registered brains, one moved away and the other's bf.yaml broke: neither hides the other.
    for name in ("moved", "broken"):
        (tmp_path / name).mkdir()
        Store(tmp_path / name).write("bf.yaml", f"version: 8\nname: {name}\n".encode())
        register(Store(tmp_path / name))
    broken = Store(tmp_path / "broken")
    broken.write("bf.yaml", b"version: 8\nname: [broken\n")
    (tmp_path / "moved").rename(tmp_path / "elsewhere")
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["brains"] == [
        {"brain": "moved", "error": ABSENT},
        {"brain": "broken", "path": str(broken.root), "error": "bf.yaml: invalid YAML at line 3, column 1"},
    ]
    assert CliRunner().invoke(app, ["status", "--check"]).exit_code == 1


def test_status_warns_without_failing_when_a_scanned_tree_nears_the_scan_limit(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = ["status", "--check", "--brain", str(brain.root)]
    assert "warnings" not in json.loads(CliRunner().invoke(app, command).stdout)["brains"][0]
    # Pretend the two meeting records fill over 80% of what one source may hold.
    monkeypatch.setattr(index, "CROWDED", 1)
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 0, result.output
    entry = json.loads(result.stdout)["brains"][0]
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
    entry = json.loads(result.stdout)["brains"][0]
    # Status never applies the journal: it describes the last generation and names the recovery.
    assert entry["pending_transaction"] is True
    assert entry["problems"][0]["error"].endswith("run bf update or bf build to recover it")
    assert (entry["cache"], entry["notes"]) == (("stale", 2) if cached else ("missing", 0))
    assert brain.read("memories/.pending/manifest.json")
    assert update(brain)["ok"]
    recovered = json.loads(CliRunner().invoke(app, command).stdout)["brains"][0]
    assert "pending_transaction" not in recovered
    assert (recovered["cache"], recovered["problems"]) == ("ready", [])


def test_status_reports_the_bytes_each_source_holds(brain: Store) -> None:
    sizes = [(brain.root / records.path("meetings", key)).stat().st_size for key in ("decision-1", "lunch")]
    entry = cast("dict", report([brain]))["brains"][0]
    assert entry["sources"]["meetings"]["bytes"] == sum(sizes)


def test_a_source_that_succeeds_without_new_evidence_falls_silent(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nsensors:\n"
        b"  papers:\n    command: [echo]\n    refresh: 86400\n    silent_after: 345600\n"
        b"  quiet:\n    command: [echo]\n    refresh: 86400\n",
    )
    first = datetime(2026, 9, 20, 13, tzinfo=UTC)
    one = b'[{"id":"one","title":"One","time":"2026-09-10T00:00:00Z"}]'
    for sensor in ("papers", "quiet"):
        collect(
            brain,
            sensor,
            start="2026-09-19T13:00:00Z",
            end="2026-09-20T13:00:00Z",
            runner=lambda *_: one,
            clock=lambda: first,
        )
    # Daily runs keep succeeding with the same record: fresh, but nothing new for five days.
    later = datetime(2026, 9, 25, 13, tzinfo=UTC)
    for sensor in ("papers", "quiet"):
        collect(
            brain,
            sensor,
            start="2026-09-20T13:00:00Z",
            end="2026-09-25T13:00:00Z",
            runner=lambda *_: one,
            clock=lambda: later,
        )
    index.refresh(brain)
    full = cast("dict", report([brain], now=later))
    status = full["brains"][0]
    papers = status["sources"]["papers"]
    # The event time is old, the change is when this brain first saw the record.
    assert (papers["freshness"], papers["silent"], papers["changed"]) == ("fresh", True, "2026-09-20T13:00:00.000000Z")
    assert "silent" not in status["sources"]["quiet"]
    assert status["attention"] == [{"sensor": "papers", "freshness": "fresh", "silent": True}]
    assert full["healthy"] is False
    # A changed record ends the silence.
    two = b'[{"id":"one","title":"One, revised","time":"2026-09-10T00:00:00Z"}]'
    collect(
        brain,
        "papers",
        start="2026-09-20T13:00:00Z",
        end="2026-09-25T13:00:00Z",
        runner=lambda *_: two,
        clock=lambda: later,
    )
    index.refresh(brain)
    assert cast("dict", report([brain], now=later))["brains"][0]["attention"] == []


def test_status_names_the_installed_and_pinned_runtime(brain: Store) -> None:
    status = cast("dict", report([brain]))["brains"][0]
    assert set(status["runtime"]) == {"installed"}
    brain.write(
        "uv.lock",
        b'version = 1\n\n[[package]]\nname = "brain-framework"\nversion = "17.2.0"\n\n'
        b'[[package]]\nname = "other"\nversion = "1.0"\n',
    )
    assert cast("dict", report([brain]))["brains"][0]["runtime"]["pinned"] == "17.2.0"
    # A lock that does not parse pins nothing: status never fails on it, even nested past the recursion limit or
    # holding an integer past Python's digit limit.
    for lock in (b"[[package\n", b"a = " + b"[" * 100_000 + b"]" * 100_000, b"a = " + b"1" * 5000):
        brain.write("uv.lock", lock)
        assert "pinned" not in cast("dict", report([brain]))["brains"][0]["runtime"]


ASSISTED = b"""version: 8
name: fixture
sensors: {}
channels:
  chat:
    send: {command: [send-chat]}
    stream: {command: [receive-chat]}
assistants:
  personal:
    prompt: prompts/personal.md
    command: [harness]
    inputs:
      - channel: {name: chat, mode: stream}
    outputs:
      - reply: {}
  notes:
    prompt: prompts/personal.md
    command: [harness]
"""


def assisted(brain: Store) -> Store:
    brain.write("bf.yaml", ASSISTED)
    brain.write("prompts/personal.md", b"Answer {{input}}.\n")
    return brain


def incoming(key: str = "message-1") -> Message:
    return Message(
        id=key, sender="owner", conversation="thread-1", time=timestamp(datetime.now(UTC).isoformat()), text="Why?"
    )


def test_retrieval_and_status_never_create_or_write_lock_the_queue(brain: Store) -> None:
    assisted(brain)
    database = runtime_store(brain).root / DATABASE
    search([brain], Query(text="offline retrieval"))
    read([brain], "meetings:decision-1")
    read([brain])
    assert report([brain])["healthy"]
    # Reading reports nothing yet: only execution creates and binds the queue.
    assert not database.exists()
    assistants.accept(brain, "chat", [incoming()], {"personal"})
    # An execution transaction holds the queue's write lock: readers neither wait for it nor take it.
    writer = sqlite3.connect(database, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        status = cast("dict", report([brain]))["brains"][0]
        assert read([brain], "meetings:decision-1")["ref"] == "meetings:decision-1"
        assert time.monotonic() - started < 5
    finally:
        writer.close()
    assert status["messaging"]["counts"] == {"pending": 1}


def test_a_queue_execution_refuses_still_leaves_reads_and_status_working(brain: Store) -> None:
    assisted(brain)
    assistants.accept(brain, "chat", [incoming()], {"personal"})
    # Renaming the brain leaves its queue bound to the former name.
    brain.write("bf.yaml", ASSISTED.replace(b"name: fixture", b"name: renamed"))
    assert read([brain], "meetings:decision-1")["ref"] == "meetings:decision-1"
    status = cast("dict", report([brain]))
    entry = status["brains"][0]
    assert not status["healthy"]
    # Status names the reason instead of failing, and claims no freshness it cannot read.
    refused = (
        "assistant queue holds unresolved work admitted under this brain's previous path or name; restore them, "
        "resolve the work with bf queue, then move or rename the brain again"
    )
    assert [problem["error"] for problem in entry["problems"]] == [refused]
    assert entry["assistants"]["personal"]["freshness"] == "unknown"
    assert "messaging" not in entry
    # Drained under its former name, the queue follows the renamed brain: status reads it without a problem.
    brain.write("bf.yaml", ASSISTED)
    for job in cast("list[dict]", Queue(brain).report()["jobs"]):
        Queue(brain).retry(str(job["id"]), cancel=True)
    brain.write("bf.yaml", ASSISTED.replace(b"name: fixture", b"name: renamed"))
    entry = cast("dict", report([brain]))["brains"][0]
    assert entry["problems"] == []
    assert "messaging" in entry


def test_status_names_unresolved_work_and_recovers_once_it_is_resolved(brain: Store) -> None:
    assisted(brain)
    queue = Queue(brain)

    def failing(*_: object) -> bytes:
        raise Error("harness timed out")

    # A manual run that failed, and a message whose answer could not be sent.
    assert not assistants.manual(brain, "notes", runner=failing)["ok"]
    assistants.accept(brain, "chat", [incoming()], {"personal"})
    key = queue.next(True, {"personal"})
    assert key

    def sent(argv: list[str], *_: object) -> bytes:
        if argv[0] == "send-chat":
            raise Error("send failed")
        return b"Because."

    assistants.execute(brain, key, runner=sent)
    assistants.deliver(brain, runner=sent)
    # A dispatcher's lane checkpoint repeats what the durable job already says.
    queue.set_progress("health:reactive", {"error": "harness timed out", "at": "2026-10-17T00:00:00.000000Z"})
    status = cast("dict", report([brain]))
    attention = status["brains"][0]["attention"]
    assert not status["healthy"]
    assert attention == [
        {"assistant": "notes", "freshness": "manual", "failed": True},
        {"channel": "chat", "freshness": "unknown", "failed": True},
    ]
    for job in cast("list[dict]", queue.report()["jobs"]):
        if job["state"] in {"failed", "uncertain"}:
            queue.retry(str(job["id"]), cancel=True)
    for delivery in cast("list[dict]", queue.report()["deliveries"]):
        queue.retry(str(delivery["id"]), cancel=True)
    # Resolved work no longer fails the check, whatever a stopped dispatcher's lane checkpoints still hold.
    status = cast("dict", report([brain]))
    assert status["brains"][0]["attention"] == []
    assert status["healthy"]
    # A running stream's error names its channel until watch clears it.
    queue.set_progress("health:channel:chat", {"error": "stream exited", "at": "2026-10-17T00:00:00.000000Z"})
    status = cast("dict", report([brain]))
    assert status["brains"][0]["attention"] == [{"channel": "chat", "freshness": "unknown", "failed": True}]
    assert not status["healthy"]


def test_status_names_an_assistant_whose_work_cannot_start(brain: Store) -> None:
    assisted(brain)
    queue = Queue(brain)
    # A schedule the full queue refused: the dispatcher records it per assistant and retries after a backoff.
    queue.set_progress(
        "health:assistant:notes", {"error": "assistant queue is full", "at": "2026-10-17T00:00:00.000000Z"}
    )
    status = cast("dict", report([brain]))
    assert not status["healthy"]
    assert {"assistant": "notes", "freshness": "manual", "failed": True} in status["brains"][0]["attention"]
    # Once admission succeeds, the dispatcher records an empty error and status recovers.
    queue.set_progress("health:assistant:notes", {"error": "", "at": "2026-10-17T00:01:00.000000Z"})
    status = cast("dict", report([brain]))
    assert status["brains"][0]["attention"] == []
    assert status["healthy"]
