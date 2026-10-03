"""Automatic reminders use edits and deadlines without asserting that anyone verified a note."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from bf import pages
from bf.markdown import note
from bf.models import Error, Record
from bf.retrieve import read
from bf.storage import Store
from conftest import records_file

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def write(brain: Store, path: str, metadata: str, modified: str) -> None:
    brain.write(path, f"---\n{metadata}\n---\n# Note\n".encode())
    instant = datetime.fromisoformat(modified).timestamp()
    os.utime(brain.root / path, (instant, instant))


def entry(brain: Store, path: str, now: datetime = NOW) -> dict[str, object]:
    return next(
        item
        for item in cast("list[dict[str, object]]", pages.folder([brain], path.split("/")[0], now)["items"])
        if item["ref"] == path
    )


def edit(brain: Store, path: str, instant: datetime) -> None:
    os.utime(brain.root / path, (instant.timestamp(), instant.timestamp()))


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


def test_newer_linked_evidence_can_arrive_before_the_deadline(brain: Store) -> None:
    path = "projects/offline.md"
    original = brain.read(path).replace(b"---\n", b"---\nstale_after: 2036-01-01T00:00:00Z\n", 1)
    brain.write(path, original)
    edit(brain, path, datetime(2026, 8, 31, 9, tzinfo=UTC))
    found = entry(brain, path)
    # The meeting links to the project and the project cites it: one linked item.
    assert (found["new_links"], found["newer"]) == (1, ["meetings:decision-1"])
    assert found["review_reasons"] == ["newer_evidence"]
    edit(brain, path, datetime(2026, 9, 27, 9, tzinfo=UTC))
    assert not {"new_links", "newer"} & set(entry(brain, path))
    assert brain.read(path) == original


def test_future_evidence_counts_once_it_happens_and_an_edit_clears_it(brain: Store) -> None:
    path = "projects/offline.md"
    edit(brain, path, datetime(2026, 9, 20, tzinfo=UTC))
    linked = ["repo:example/project"]
    records_file(
        brain,
        "calendar",
        [
            # Invited before the edit and held after it: the meeting happened since.
            Record(
                id="held",
                title="Held",
                time="2026-09-26T09:00:00Z",
                attributes={"updated": "2026-09-10T09:00:00Z"},
                links=linked,
            ),
            Record(id="coming", title="Coming", time="2026-10-20T09:00:00Z", links=linked),
            # Rescheduled since the edit: its upstream change is newer, while the meeting itself is still to come.
            Record(
                id="moved",
                title="Moved",
                time="2026-10-22T09:00:00Z",
                attributes={"updated": "2026-09-25T09:00:00Z"},
                links=linked,
            ),
        ],
    )
    found = entry(brain, path)
    assert (found["new_links"], found["newer"]) == (2, ["calendar:held", "calendar:moved"])
    later = datetime(2026, 10, 21, tzinfo=UTC)
    assert entry(brain, path, later)["newer"] == ["calendar:coming", "calendar:held", "calendar:moved"]
    # An edit clears the flag before the coming meetings happen: they belong to the home page's upcoming items.
    edit(brain, path, NOW)
    assert "review" not in entry(brain, path)
    assert entry(brain, path, later)["newer"] == ["calendar:coming"]


def test_upstream_changes_to_linked_records_need_review(brain: Store) -> None:
    path = "projects/offline.md"
    edit(brain, path, datetime(2026, 9, 20, tzinfo=UTC))
    linked = ["repo:example/project"]
    records_file(
        brain,
        "jira",
        [
            # An old issue closed since the edit keeps its event time; its upstream change is newer.
            Record(
                id="WEB-7",
                title="Closed",
                time="2026-09-01T09:00:00Z",
                attributes={"updated": "2026-09-24T09:00:00Z"},
                links=linked,
            ),
            # First collected since the edit, unchanged upstream since before it: collecting is not a change.
            Record(
                id="WEB-8",
                title="Backfilled",
                time="2026-09-02T09:00:00Z",
                attributes={"updated": "2026-09-03T09:00:00Z", "observed": "2026-09-25T09:00:00Z"},
                links=linked,
            ),
            # A provider clock ahead of this one would otherwise hold the flag through every edit.
            Record(
                id="WEB-9",
                title="Skewed",
                time="2026-09-03T09:00:00Z",
                attributes={"updated": "2027-01-01T00:00:00Z"},
                links=linked,
            ),
        ],
    )
    found = entry(brain, path)
    assert (found["review_reasons"], found["new_links"], found["newer"]) == (["newer_evidence"], 1, ["jira:WEB-7"])
    edit(brain, path, NOW)
    assert "review" not in entry(brain, path)


def test_a_note_citing_changed_evidence_names_it(brain: Store) -> None:
    path = "concepts/choice.md"
    brain.write(path, b"---\ntype: concept\nstale_after: 2036-01-01T00:00:00Z\n---\n# Choice\n\n[Brief](brief:site).\n")
    edit(brain, path, datetime(2026, 9, 20, tzinfo=UTC))
    records_file(brain, "brief", [Record(id="site", title="Brief", time="2026-09-01T09:00:00Z")])
    assert "review" not in entry(brain, path)
    records_file(
        brain,
        "brief",
        [Record(id="site", title="Brief", time="2026-09-01T09:00:00Z", attributes={"updated": "2026-09-25T09:00:00Z"})],
    )
    found = entry(brain, path)
    assert (found["review_reasons"], found["new_links"], found["newer"]) == (["newer_evidence"], 1, ["brief:site"])


def test_a_note_cites_records_by_address_alias_and_okf_sources(brain: Store) -> None:
    path = "concepts/cites.md"
    brain.write(
        path,
        b"---\ntype: concept\nstale_after: 2036-01-01T00:00:00Z\nsources:\n  - resource: docs:spec\n---\n# Cites\n\n"
        b"[Mail](bf://fixture/mail:qualified) and [deck](deck:launch).\n",
    )
    edit(brain, path, datetime(2026, 9, 20, tzinfo=UTC))
    changed = "2026-09-25T09:00:00Z"
    records_file(
        brain,
        "mail",
        [Record(id="qualified", title="Mail", time="2026-09-01T09:00:00Z", attributes={"updated": changed})],
    )
    records_file(brain, "decks", [Record(id="d", title="Deck", time="2026-09-24T09:00:00Z", aliases=["deck:launch"])])
    # Undated, like a document without an event time: only its upstream change is newer.
    records_file(brain, "docs", [Record(id="spec", title="Spec", attributes={"updated": changed})])
    found = entry(brain, path)
    assert (found["new_links"], found["newer"]) == (3, ["docs:spec", "mail:qualified", "decks:d"])


def test_a_note_never_cites_through_a_name_several_records_claim(brain: Store) -> None:
    path = "projects/launch.md"
    brain.write(
        path, b"---\ntype: project\n---\n# Launch\n\n[Deck](deck:launch), [mail](mail:m) and [brief](brief:site).\n"
    )
    edit(brain, path, datetime(2026, 9, 20, tzinfo=UTC))
    records_file(brain, "drive", [Record(id="a", title="Deck", time="2026-09-24T09:00:00Z", aliases=["deck:launch"])])
    records_file(brain, "slack", [Record(id="b", title="Deck", time="2026-09-25T09:00:00Z", aliases=["deck:launch"])])
    # A record's ref still names it when another record repeats that ref as an alias, which then names nothing.
    records_file(brain, "mail", [Record(id="m", title="Mail", time="2026-09-24T09:00:00Z")])
    records_file(brain, "chat", [Record(id="c", title="Chat", time="2026-09-25T09:00:00Z", aliases=["mail:m"])])
    records_file(brain, "brief", [Record(id="site", title="Brief", time="2026-09-26T09:00:00Z")])
    found = entry(brain, path)
    # Like reads, the review names only the records a link identifies.
    assert (found["new_links"], found["newer"], found["review_reasons"]) == (
        2,
        ["brief:site", "mail:m"],
        ["newer_evidence"],
    )
    with pytest.raises(Error, match="ambiguous identity"):
        read([brain], "deck:launch")
    assert read([brain], "mail:m")["ref"] == "mail:m"


def test_links_through_a_name_another_item_claims_are_not_newer(brain: Store) -> None:
    now = datetime.now(UTC)
    path = "projects/p.md"
    brain.write(path, b"---\ntype: project\naliases: [repo:shared, tickets:T-1]\n---\n# P\n")
    edit(brain, path, now - timedelta(days=3))
    day = (now - timedelta(days=1)).isoformat()
    records_file(brain, "repos", [Record(id="shared", title="Repo", aliases=["repo:shared"])])
    records_file(brain, "tickets", [Record(id="T-1", title="Ticket")])
    records_file(
        brain,
        "mail",
        [
            Record(id="m", title="Shared", time=day, links=["repo:shared", "tickets:T-1"]),
            Record(id="n", title="Owned", time=day, links=["bf://fixture/projects/p.md"]),
        ],
    )
    whole = read([brain], path)
    # Backlinks leave out both shared names, and so do the review signals beside them.
    groups = cast("list[dict[str, list[dict[str, object]]]]", whole["backlinks"])
    assert [item["ref"] for group in groups for item in group["items"]] == ["mail:n"]
    problems = cast("list[dict[str, object]]", whole["problems"])
    assert {"error": "2 aliases are also claimed elsewhere and were not expanded"} in problems
    assert (whole["new_links"], whole["newer"], whole["review_reasons"]) == (1, ["mail:n"], ["newer_evidence"])
    assert entry(brain, path, now)["newer"] == ["mail:n"]


def test_newer_names_the_five_newest_linked_items_and_counts_all(brain: Store) -> None:
    path = "projects/offline.md"
    edit(brain, path, datetime(2026, 9, 20, tzinfo=UTC))
    records_file(
        brain,
        "mail",
        [
            Record(id=f"m{day}", title="Mail", time=f"2026-09-{day}T09:00:00Z", links=["repo:example/project"])
            for day in range(21, 27)
        ],
    )
    # A link to one of the project's sections links to the project.
    brain.write(
        "concepts/later.md", b"---\nupdated: 2026-09-27\n---\n# Later\n\n[Why](../projects/offline.md#decision)\n"
    )
    found = entry(brain, path)
    assert found["new_links"] == 7
    assert found["newer"] == ["concepts/later.md", "mail:m26", "mail:m25", "mail:m24", "mail:m23"]


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
