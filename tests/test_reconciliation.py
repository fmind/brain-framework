"""Frequent deltas and periodic reconciliation share one source and advance only on success."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from pydantic import ValidationError

from bf import collect as collection
from bf.collect import collect, due
from bf.health import report
from bf.history import state
from bf.models import Error, Sensor, encode
from bf.storage import Store, state_store
from bf.update import update
from bf.watch import snapshot

NOW = datetime(2026, 9, 8, tzinfo=UTC)
AT = "2026-09-08T00:00:00.000000Z"
CONFIG = b"""version: 7
name: fixture
sensors:
  mail:
    command: [echo, "{{start}}", "{{end}}"]
    refresh: 3600
    reconcile: {refresh: 86400, lookback: 604800}
"""


def test_daily_reconciliation_preserves_hourly_resume_and_reports_cost(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    first = cast(Any, update(brain, dry_run=True, now=NOW))["sensors"][0]
    assert first["reconcile"] is True
    assert first["start"] == "2026-09-01T00:00:00.000000Z"
    assert state(brain) == {}
    windows = []

    def fake(argv, *_):
        windows.append(argv[1:])
        return b'[{"id":"one","title":"Evidence"}]'

    assert update(brain, now=NOW, runner=fake)["ok"]
    assert state(brain)["mail"]["reconciled"] == AT
    assert due(brain, NOW + timedelta(minutes=30)) == []
    assert update(brain, now=NOW + timedelta(hours=1), runner=fake)["ok"]
    assert windows[-1] == ["2026-09-07T23:55:00.000000Z", "2026-09-08T01:00:00.000000Z"]
    assert state(brain)["mail"]["reconciled"] == AT
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
    assert not update(brain, now=NOW, runner=lambda *_: b"[]")["ok"]
    assert not state(brain)["mail"].get("reconciled")
    # The retry after the failure backoff still reconciles.
    retry = NOW + timedelta(minutes=1)
    assert due(brain, retry)[0][-1] is True
    monkeypatch.setattr(collection.records, "upsert", original)
    assert update(brain, now=retry, runner=lambda *_: b"[]")["ok"]
    assert state(brain)["mail"]["reconciled"] == "2026-09-08T00:01:00.000000Z"


@pytest.mark.parametrize("extreme", [False, True])
def test_future_success_cannot_freeze_schedules_after_a_clock_correction(brain: Store, extreme: bool) -> None:
    now = datetime.now(UTC)
    future = "9999-12-31T23:59:59Z" if extreme else (now + timedelta(days=1)).isoformat()
    brain.write("bf.yaml", CONFIG + b"routines:\n  review:\n    command: [review]\n    refresh: 3600\n")
    local = state_store(brain.root)
    local.write("sensors.json", encode({"mail": {"success": future, "reconciled": future}}))
    local.write("routines.json", encode({"review": {"success": future}}))
    assert not report([brain], now=now)["healthy"]
    assert {row.name: row.status for row in snapshot(brain)} == {"mail": "due", "review": "due"}
    plan = cast(Any, update(brain, dry_run=True, now=now))
    assert plan["sensors"][0]["reconcile"] is True
    assert plan["routines"][0]["routine"] == "review"
    assert update(brain, now=now, runner=lambda argv, *_: b"" if argv[0] == "review" else b"[]")["ok"]
    assert report([brain], now=now)["healthy"]
    assert due(brain, now) == []


def test_ancient_resume_timestamp_respects_the_catchup_bound(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    state_store(brain.root).write("sensors.json", encode({"mail": {"end": "0001-01-01T00:00:00Z"}}))
    assert due(brain, NOW)[0][1] == "2026-08-09T00:00:00.000000Z"


def test_manual_failure_retries_before_the_previous_success_expires(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    assert update(brain, now=NOW, runner=lambda *_: b"[]")["ok"]
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
    # The failure backs off for a minute, far less than the hourly refresh of the earlier success.
    retry = later + timedelta(minutes=1)
    assert due(brain, retry - timedelta(seconds=1)) == []
    assert due(brain, retry)[0][0] == "mail"
    assert update(brain, now=retry, runner=lambda *_: b"[]")["ok"]
    assert due(brain, retry) == []


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
