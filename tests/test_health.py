"""Coverage reports distinguish maintained sources from retained historical evidence."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from bf import index
from bf.cli import app
from bf.collect import collect, routine
from bf.config import register
from bf.health import report, source_health
from bf.history import log_path
from bf.models import Error, Query, encode
from bf.retrieve import read, search
from bf.storage import Store, state_store


def test_status_describes_each_program_once_in_canonical_utc(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 6\nname: fixture\nsensors:\n"
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
        "last_run",
        "reconciled",
        "stale",
    }
    assert (mail["state"], mail["freshness"], mail["stale"], mail["records"]) == ("active", "fresh", False, 1)
    assert (mail["last_collected"], mail["reconciled"], mail["window"]) == (at, at, {"since": week, "until": at})
    assert mail["last_run"]["added"] == 1
    assert status["sources"]["folders"] == {
        "state": "active",
        "mode": "snapshot",
        "freshness": "never",
        "records": 0,
        "failed": True,
        "error": "invalid JSON document",
        "failures": 1,
        "log": str(log_path(brain, "folders")),
        "stale": True,
    }
    assert status["routines"]["review"] == {"state": "active", "freshness": "fresh", "last_success": at, "stale": False}
    # Every reply instant uses one canonical UTC form, so clients can compare them as strings.
    instants = re.findall(r'"(\d{4}-\d\d-\d\dT[^"]*)"', json.dumps(status))
    assert instants
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", instant) for instant in instants)


def test_history_offsets_read_back_as_canonical_utc_in_status_and_home(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 6\nname: fixture\nsensors:\n  mail:\n    command: [echo]\n    refresh: 3600\n"
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
        b"version: 6\nname: fixture\nsensors:\n"
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
    assert source_health(brain, now=datetime(2026, 9, 23, 15, tzinfo=UTC))["current"]["freshness"] == "stale"


def test_init_quotes_yaml_names_and_keeps_private_evidence_out_of_git(tmp_path) -> None:
    result = CliRunner().invoke(app, ["init", str(tmp_path / "brain"), "--name", "on"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["brain"] == "on"
    assert "memories/" in (tmp_path / "brain" / ".gitignore").read_text()
    assert CliRunner().invoke(app, ["validate", "--brain", str(tmp_path / "brain")]).exit_code == 0


def test_status_keeps_indexed_totals_separate_from_last_run_counts(brain: Store) -> None:
    brain.write("bf.yaml", b"version: 6\nname: fixture\nsensors:\n  current:\n    command: [echo]\n")
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
        b"version: 6\nname: fixture\nsensors:\n  agenda:\n    command: [echo]\n    mode: snapshot\n    enabled: false\n",
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
        "bf.yaml", b"version: 6\nname: fixture\nsensors:\n  meetings:\n    command: [echo]\n    refresh: 3600\n"
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
    other.write("bf.yaml", b"version: 6\nname: other\n")
    register(other)
    other.write("bf.yaml", b"version: 6\nname: [broken\n")
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
