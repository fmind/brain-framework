"""Time, provenance and filter contracts at the public model boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from textwrap import dedent

import pytest
from pydantic import ValidationError

from bf.models import Error, Query, Record, timestamp
from bf.pages import period, scope
from bf.storage import Store


def python_in_timezone(zone: str, program: str, *arguments: str) -> None:
    # Choose TZ at process startup; Intel macOS Python can omit time.tzset().
    result = subprocess.run(  # noqa: S603 - synthetic timezone behavior in the tested interpreter
        [sys.executable, "-c", dedent(program), *arguments],
        env={**os.environ, "TZ": zone},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_local_dates_use_the_offset_at_that_midnight() -> None:
    python_in_timezone(
        "Europe/Paris",
        """
        from datetime import UTC, datetime
        from bf.models import moment

        september = datetime(2026, 9, 23, 12, tzinfo=UTC)
        transition = datetime(2026, 3, 29, 12, tzinfo=UTC)
        assert moment("2026-01-01", september) == "2025-12-31T23:00:00.000000Z"
        assert moment("today", transition) == "2026-03-28T23:00:00.000000Z"
        assert moment("yesterday", transition) == "2026-03-27T23:00:00.000000Z"
        assert moment("1d", transition) == "2026-03-28T12:00:00.000000Z"
        """,
    )


def test_timestamp_overflow_is_a_validation_failure() -> None:
    with pytest.raises(ValueError, match="timestamp"):
        timestamp("0001-01-01T00:00:00+14:00")
    with pytest.raises(ValidationError):
        Record(id="event", title="Boundary", time="9999-12-31T23:59:59-14:00")


def test_revision_times_and_source_filters_are_strict() -> None:
    record = Record(
        id="document",
        title="Decision",
        attributes={"updated": "2026-09-23T14:00:00+02:00", "observed": "2026-09-23T12:05:00Z"},
    )
    assert record.updated == "2026-09-23T12:00:00.000000Z"
    assert record.observed == "2026-09-23T12:05:00.000000Z"
    # Adapters resolve periods into canonical bounds; queries accept only canonical instants.
    assert scope("7d")["since"] < scope("today")["until"]
    assert Query(text="x", **scope("2026-09")).until == scope("2026-10")["since"]
    with pytest.raises(ValidationError):
        Query(text="x", since="7d")
    with pytest.raises(Error, match="scope accepts"):
        scope("soon")
    with pytest.raises(Error, match="invalid period"):
        period("2026-13-01")
    with pytest.raises(ValidationError):
        Record(id="document", title="Decision", attributes={"updated": "2026-09-23"})


def test_note_dates_follow_local_days_without_rebuilding_the_cache(brain: Store) -> None:
    from bf import index

    for day in ("2026-09-22", "2026-09-23", "2026-09-24", "2026-03-29"):
        brain.write(f"projects/{day}.md", f"---\nupdated: {day}\n---\n# Localday {day}\n".encode())
    index.refresh(brain)
    cache_inode = (brain.root / index.CACHE).stat().st_ino
    for zone, day, expected in [
        ("America/New_York", "2026-09-23", "2026-09-23T04:00:00.000000Z"),
        ("Europe/Paris", "2026-09-23", "2026-09-22T22:00:00.000000Z"),
        ("Europe/Paris", "2026-03-29", "2026-03-28T23:00:00.000000Z"),
    ]:
        python_in_timezone(
            zone,
            """
            import sys
            from pathlib import Path
            from bf.models import Query
            from bf.pages import scope
            from bf.retrieve import read, search
            from bf.storage import Store

            root, day, expected = sys.argv[1:]
            store = Store(Path(root))
            reply = search([store], Query(text="localday", **scope(day)), counted=False)
            assert [item["ref"] for item in reply["items"]] == [f"projects/{day}.md"]
            assert reply["items"][0]["time"] == expected
            page = read([store], day, counted=False)
            assert [item["ref"] for item in page["items"]] == [f"projects/{day}.md"]
            assert page["since"] == expected
            """,
            str(brain.root),
            day,
            expected,
        )
    assert (brain.root / index.CACHE).stat().st_ino == cache_inode
