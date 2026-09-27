"""Frequent deltas and periodic reconciliation share one source and advance only on success."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from pydantic import ValidationError

from bf import collect as collection
from bf.collect import collect, due, state
from bf.health import report
from bf.models import Error, Sensor
from bf.storage import Store
from bf.update import update

NOW = datetime(2026, 9, 8, tzinfo=UTC)
CONFIG = b"""version: 6
name: fixture
sensors:
  mail:
    command: [echo, "{{start}}", "{{end}}"]
    refresh: 3600
    reconcile: {refresh: 86400, lookback: 604800}
"""


def test_daily_reconciliation_preserves_hourly_resume_and_reports_cost(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    first = cast(Any, update([brain], dry_run=True, now=NOW))["brains"][0]["sensors"][0]
    assert first["reconcile"] is True
    assert first["start"] == "2026-09-01T00:00:00.000000Z"
    assert state(brain) == {}
    windows = []

    def fake(argv, *_):
        windows.append(argv[1:])
        return b'[{"id":"one","title":"Evidence"}]'

    assert update([brain], now=NOW, runner=fake)["ok"]
    assert state(brain)["mail"]["reconciled"] == NOW.isoformat()
    assert due(brain, NOW + timedelta(minutes=30)) == []
    assert update([brain], now=NOW + timedelta(hours=1), runner=fake)["ok"]
    assert windows[-1] == ["2026-09-07T23:55:00.000000Z", "2026-09-08T01:00:00.000000Z"]
    assert state(brain)["mail"]["reconciled"] == NOW.isoformat()
    assert due(brain, NOW + timedelta(days=1)) == [
        ("mail", "2026-09-02T00:00:00.000000Z", "2026-09-09T00:00:00.000000Z", True)
    ]
    result = cast(Any, report([brain], now=NOW + timedelta(hours=1)))["brains"][0]["sources"]["mail"]
    assert result["last_run"]["unchanged"] == 1
    assert result["last_run"]["output_bytes"] == 33
    assert result["last_run"]["elapsed_seconds"] >= 0
    assert result["last_run"]["requested_start"] == windows[-1][0]
    assert result["last_run"]["reconcile"] is False


def test_failed_commit_does_not_advance_reconciliation(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write("bf.yaml", CONFIG)
    original = collection.records.upsert

    def fail(*_, **__):
        raise Error("synthetic write failure")

    monkeypatch.setattr(collection.records, "upsert", fail)
    assert not update([brain], now=NOW, runner=lambda *_: b"[]")["ok"]
    assert not state(brain)["mail"].get("reconciled")
    assert due(brain, NOW)[0][-1] is True
    monkeypatch.setattr(collection.records, "upsert", original)
    assert update([brain], now=NOW, runner=lambda *_: b"[]")["ok"]
    assert state(brain)["mail"]["reconciled"] == NOW.isoformat()


def test_manual_failure_retries_before_the_previous_success_expires(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    assert update([brain], now=NOW, runner=lambda *_: b"[]")["ok"]
    later = NOW + timedelta(minutes=5)

    def fail(*_):
        raise Error("synthetic provider failure")

    with pytest.raises(Error, match="synthetic provider failure"):
        collect(
            brain,
            "mail",
            start=(later - timedelta(hours=1)).isoformat(),
            end=later.isoformat(),
            clock=lambda: later,
            runner=fail,
        )
    assert due(brain, later)[0][0] == "mail"
    assert update([brain], now=later, runner=lambda *_: b"[]")["ok"]
    assert due(brain, later) == []


def test_reconciliation_keeps_longer_catchup_and_manual_backfills_do_not_acknowledge_it(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    collect(
        brain,
        "mail",
        start="2026-08-01T00:00:00Z",
        end="2026-08-02T00:00:00Z",
        runner=lambda *_: b"[]",
        clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
    )
    assert not state(brain)["mail"].get("reconciled")
    assert due(brain, NOW)[0] == ("mail", "2026-08-09T00:00:00.000000Z", "2026-09-08T00:00:00.000000Z", True)
    with pytest.raises(Error, match="cover its lookback"):
        collect(brain, "mail", start="2026-09-07T00:00:00Z", end=NOW.isoformat(), reconcile=True, clock=lambda: NOW)


@pytest.mark.parametrize(
    "settings",
    [
        {"mode": "snapshot", "reconcile": {"refresh": 86400, "lookback": 604800}},
        {"reconcile": {"refresh": 0, "lookback": 604800}},
        {"reconcile": {"refresh": 86400, "lookback": 0}},
        {"reconcile": {"refresh": "daily", "lookback": 604800}},
    ],
)
def test_invalid_reconciliation_is_rejected(settings: dict) -> None:
    with pytest.raises(ValidationError):
        Sensor.model_validate({"command": ["echo"], **settings})
