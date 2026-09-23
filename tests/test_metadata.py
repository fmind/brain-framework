"""Time, provenance and filter contracts at the public model boundary."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from fkf.models import Query, Record, moment, timestamp
from fkf.storage import Store


def test_local_dates_use_the_offset_at_that_midnight(monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as context:
        context.setenv("TZ", "Europe/Paris")
        time.tzset()
        try:
            september = datetime(2026, 9, 23, 12, tzinfo=UTC)
            transition = datetime(2026, 3, 29, 12, tzinfo=UTC)
            assert moment("2026-01-01", september) == "2025-12-31T23:00:00.000000Z"
            assert moment("today", transition) == "2026-03-28T23:00:00.000000Z"
            assert moment("yesterday", transition) == "2026-03-27T23:00:00.000000Z"
            assert moment("1d", transition) == "2026-03-28T12:00:00.000000Z"
        finally:
            context.undo()
            time.tzset()


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
    assert Query(current=True).current
    assert Query(changed_since="2026-09-22T00:00:00Z").changed_since == "2026-09-22T00:00:00.000000Z"
    with pytest.raises(ValidationError):
        Record(id="document", title="Decision", attributes={"updated": "2026-09-23"})


def test_note_dates_follow_local_days_without_rebuilding_the_cache(
    base: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fkf import index
    from fkf.retrieve import search

    for day in ("2026-09-22", "2026-09-23", "2026-09-24", "2026-03-29"):
        base.write(f"projects/{day}.md", f"---\nupdated: {day}\n---\n# Localday {day}\n".encode())
    index.refresh(base)
    cache_inode = (base.root / index.CACHE).stat().st_ino
    for zone, day, end, expected in [
        ("America/New_York", "2026-09-23", "2026-09-24", "2026-09-23T04:00:00.000000Z"),
        ("Europe/Paris", "2026-09-23", "2026-09-24", "2026-09-22T22:00:00.000000Z"),
        ("Europe/Paris", "2026-03-29", "2026-03-30", "2026-03-28T23:00:00.000000Z"),
    ]:
        with monkeypatch.context() as context:
            context.setenv("TZ", zone)
            time.tzset()
            try:
                for options in ({"since": moment(day)}, {"changed_since": moment(day)}):
                    reply = search(
                        [base],
                        Query.model_validate({"text": "localday", "until": moment(end), **options}),
                        counted=False,
                    )
                    assert isinstance(reply["items"], list)
                    assert [item["ref"] for item in reply["items"]] == [f"projects/{day}.md"]
                    assert reply["items"][0]["time"] == expected
            finally:
                context.undo()
                time.tzset()
    assert (base.root / index.CACHE).stat().st_ino == cache_inode
