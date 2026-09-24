"""Coverage reports distinguish maintained sources from retained historical evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from bf.cli import app
from bf.health import source_health
from bf.models import encode
from bf.storage import Store, state_store


def test_collection_coverage_does_not_claim_archives_are_fresh(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 3\nname: fixture\nsensors:\n"
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
    assert report["current"]["window"] == {"since": "2026-09-22T12:00:00Z", "until": "2026-09-23T12:00:00Z"}
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
    assert CliRunner().invoke(app, ["validate", "--brain", "on"]).exit_code == 0


def test_status_keeps_indexed_totals_separate_from_last_run_counts(brain: Store) -> None:
    brain.write("bf.yaml", b"version: 3\nname: fixture\nsensors:\n  current:\n    command: [echo]\n")
    brain.write("memories/current/undated.jsonl", b'{"id":"a","title":"First"}\n{"id":"b","title":"Second"}\n')
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
        b"version: 3\nname: fixture\nsensors:\n  agenda:\n    command: [echo]\n    mode: snapshot\n    enabled: false\n",
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
