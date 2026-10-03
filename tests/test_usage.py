"""Usage rotation keeps the windows `bf status` summarizes within a hard size bound and marks a window it cuts."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bf import usage
from bf.cli import app
from bf.models import encode, timestamp
from bf.storage import Store, state_store


def lines(count: int, age: timedelta, now: datetime, results: int = 1) -> bytes:
    """`count` searches that happened `age` ago."""
    return encode({"at": timestamp((now - age).isoformat()), "op": "search", "results": results}) * count


def log(brain: Store) -> Path:
    return state_store(brain.root).root / usage.USAGE


def test_rotation_drops_events_older_than_the_largest_window_and_keeps_the_rest(brain: Store) -> None:
    now = datetime.now(UTC)
    # Old searches fill most of the limit; malformed, naive and unrepresentable lines never stop rotation.
    old = lines(13_000, timedelta(days=45), now)
    noise = b'not json\n{"at":"2026-09-01T00:00:00","op":"read","results":1}\n{"since":"x"}\n'
    extreme = b'{"at":"0001-01-01T00:00:00+05:00","op":"read","results":1}\n'
    recent = lines(3_000, timedelta(days=20), now) + lines(1_000, timedelta(days=2), now, results=0)
    log(brain).write_bytes(old + noise + extreme + recent)
    usage.note(brain, "read", 1)
    kept = log(brain).read_bytes()
    assert kept.startswith(recent)
    assert json.loads(kept[len(recent) :])["op"] == "read"
    # Every event of both windows is counted, so neither names a `since`.
    assert usage.summary(brain) == {
        "7d": {"search": 1_000, "empty": 1_000, "read": 1},
        "30d": {"search": 4_000, "empty": 1_000, "read": 1},
    }


@pytest.mark.parametrize(("days", "cut"), [(10, {"30d"}), (1, {"7d", "30d"})])
def test_a_window_the_size_limit_cuts_names_its_oldest_counted_event(brain: Store, days: int, cut: set[str]) -> None:
    now = datetime.now(UTC)
    # Heavy use: the window's own events exceed the limit, which stays a hard bound.
    log(brain).write_bytes(lines(17_000, timedelta(days=days), now) + lines(100, timedelta(hours=1), now))
    usage.note(brain, "search", 1)
    data = log(brain).read_bytes()
    assert len(data) <= usage.LIMIT
    mark, *events = data.splitlines()
    since = timestamp((now - timedelta(days=days)).isoformat())
    assert json.loads(mark) == {"since": since}
    summary = usage.summary(brain)
    assert summary["30d"]["search"] == len(events)
    assert {window for window, counts in summary.items() if counts.get("since") == since} == cut
    # Once the cut leaves a window, it counts that window's events completely again.
    later = usage.summary(brain, now=now + timedelta(days=31 - days))
    assert not any("since" in counts for counts in later.values())
    # A mark only counts as rotation's first line.
    log(brain).write_bytes(lines(1, timedelta(hours=1), now) + mark + b"\n")
    assert "since" not in usage.summary(brain)["7d"]


def test_status_states_since_in_local_time(brain: Store) -> None:
    now = datetime.now(UTC)
    log(brain).write_bytes(lines(17_000, timedelta(days=10), now))
    usage.note(brain, "search", 1)
    result = CliRunner().invoke(app, ["status", "--brain", str(brain.root)])
    assert result.exit_code == 0, result.output
    counts = json.loads(result.stdout)["brains"][0]["usage"]["30d"]
    # Like every reply instant: the local offset, to the second; the suite runs in UTC.
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", counts["since"])
