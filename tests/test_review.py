"""Automatic reminders use edits and deadlines without asserting that anyone verified a note."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import cast

import pytest

from bf import pages
from bf.markdown import note
from bf.models import Error
from bf.storage import Store

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def write(brain: Store, path: str, metadata: str, modified: str) -> None:
    brain.write(path, f"---\n{metadata}\n---\n# Note\n".encode())
    instant = datetime.fromisoformat(modified).timestamp()
    os.utime(brain.root / path, (instant, instant))


def entry(brain: Store, path: str) -> dict[str, object]:
    return next(
        item
        for item in cast("list[dict[str, object]]", pages.folder([brain], path.split("/")[0], NOW)["items"])
        if item["ref"] == path
    )


def test_edits_automatically_refresh_reminders_without_changing_authored_dates(brain: Store) -> None:
    path = "projects/clock.md"
    metadata = "type: project\nupdated: 2026-01-01"
    write(brain, path, metadata, "2026-09-01T12:00:00Z")
    before = entry(brain, path)
    assert before["review_due"] == "2026-09-15"
    assert before["review_reasons"] == ["due"]
    assert before["review_source"] == "modified"
    original = brain.read(path)
    instant = NOW.timestamp()
    os.utime(brain.root / path, (instant, instant))
    after = entry(brain, path)
    assert after["modified"] == "2026-09-27T12:00:00.000000Z"
    assert after["review_due"] == "2026-10-11"
    assert "review" not in after
    assert after["date"] == before["date"] == "2026-01-01"
    assert brain.read(path) == original


def test_okf_stale_after_overrides_recent_edits(brain: Store) -> None:
    path = "projects/deadline.md"
    write(brain, path, "type: project\nstale_after: 2026-09-27T00:00:00Z", NOW.isoformat())
    found = entry(brain, path)
    assert found["review_due"] == "2026-09-27"
    assert found["review_source"] == "stale_after"
    assert found["review_reasons"] == ["due"]
    # An old edit is not due while the note is not yet stale.
    write(brain, path, "type: project\nstale_after: 2026-10-01T00:00:00+02:00", "2026-01-01T00:00:00Z")
    assert "review" not in entry(brain, path)


@pytest.mark.parametrize("path", ["concepts/opt-in.md", "actions/2026-09-27_work/ACTION.md"])
def test_other_note_kinds_only_get_requested_reminders(brain: Store, path: str) -> None:
    write(brain, path, "type: concept", "2026-01-01T00:00:00Z")
    assert "review_due" not in entry(brain, path)
    write(brain, path, "type: concept\nstale_after: 2026-09-26T12:00:00Z", "2026-09-23T12:00:00Z")
    assert entry(brain, path)["review_due"] == "2026-09-26"
    assert entry(brain, path)["review_reasons"] == ["due"]
    write(brain, path, "type: concept\nstatus: deprecated\nstale_after: 2026-09-26T12:00:00Z", "2026-01-01T00:00:00Z")
    assert "review_due" not in entry(brain, path)


def test_future_filesystem_clock_requests_attention_instead_of_deferring_forever(brain: Store) -> None:
    path = "projects/future-clock.md"
    write(brain, path, "type: project", "2030-01-01T00:00:00Z")
    found = entry(brain, path)
    assert found["review"] is True
    assert found["review_reasons"] == ["future_modified"]
    assert found["modified"] == "2030-01-01T00:00:00.000000Z"


def test_newer_evidence_keeps_event_time_and_can_arrive_before_deadline(brain: Store) -> None:
    path = "projects/offline.md"
    original = brain.read(path).replace(b"---\n", b"---\nstale_after: 2036-01-01T00:00:00Z\n", 1)
    brain.write(path, original)
    instant = datetime(2026, 8, 31, 9, tzinfo=UTC).timestamp()
    os.utime(brain.root / path, (instant, instant))
    found = entry(brain, path)
    assert found["new_links"] == 1
    assert found["review_reasons"] == ["newer_evidence"]
    instant = datetime(2026, 9, 27, 9, tzinfo=UTC).timestamp()
    os.utime(brain.root / path, (instant, instant))
    assert "new_links" not in entry(brain, path)
    assert brain.read(path) == original


@pytest.mark.parametrize(
    "metadata",
    [
        "stale_after: 2026-02-30T00:00:00Z",
        "stale_after: tomorrow",
        # An instant needs its timezone, and a date alone is not an instant.
        "stale_after: 2026-10-01T00:00:00",
        "stale_after: 2026-10-01",
        "stale_after: 3",
    ],
)
def test_review_configuration_rejects_invalid_inputs(metadata: str) -> None:
    with pytest.raises(Error, match="invalid frontmatter"):
        note("projects/x.md", f"---\ntype: project\n{metadata}\n---\n# X\n".encode())
