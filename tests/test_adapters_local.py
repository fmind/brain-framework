"""Catalog adapters for local Git history and Google Calendar behave against fakes and a temporary repository."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from conftest import Provider
from fkf.index import build
from fkf.models import Collection, Query, encode
from fkf.retrieve import find
from fkf.storage import Store

PAGE_ONE = {
    "kind": "calendar#events",
    "timeZone": "Europe/Luxembourg",
    "nextPageToken": "second",
    "items": [
        {
            "id": "evt1",
            "summary": "Retention review",
            "description": "Agree the retention policy.",
            "start": {"dateTime": "2026-09-01T10:00:00+02:00", "timeZone": "Europe/Luxembourg"},
            "end": {"dateTime": "2026-09-01T11:00:00+02:00"},
            "status": "confirmed",
            "htmlLink": "https://calendar.google.com/event?eid=evt1",
            "updated": "2026-08-31T08:00:00.000Z",
            "location": "Room 4",
            "source": {"url": "https://example.invalid/project", "title": "Project"},
            "attachments": [{"title": "Agenda", "fileUrl": "https://example.invalid/agenda"}],
        },
        {"id": "evt2", "status": "cancelled", "start": {"dateTime": "2026-09-01T12:00:00Z"}},
    ],
}
PAGE_TWO = {
    "kind": "calendar#events",
    "timeZone": "Europe/Luxembourg",
    "items": [{"id": "evt3", "summary": "Offsite", "start": {"date": "2026-09-02"}}],
}


def test_calendar_pages_project_facts_and_all_day_boundaries(provider: Provider) -> None:
    provider.install(
        "gws",
        [
            {"match": ["events", "list", '"pageToken": "second"'], "stdout": PAGE_TWO},
            {"match": ["events", "list"], "stdout": PAGE_ONE},
        ],
    )
    records = provider.records("google-calendar.py", "team", "2026-09-01T00:00:00Z", "2026-09-03T00:00:00Z")
    assert [r.id for r in records] == ["evt1", "evt2", "evt3"]
    review = records[0]
    assert review.title == "Retention review"
    assert review.time == "2026-09-01T08:00:00.000000Z"
    for fact in ("Status: confirmed", "Location: Room 4", "Agree the retention policy.", "Attachment: Agenda"):
        assert fact in review.text
    assert review.links == ["https://example.invalid/agenda", "https://example.invalid/project"]
    assert review.aliases == ["calendar:team/evt1"]
    assert records[1].title == "Cancelled calendar event"
    assert records[2].time == "2026-09-01T22:00:00.000000Z"
    assert records[2].attributes["all_day"] is True
    calls = provider.calls("gws")
    assert len(calls) == 2
    assert '"calendarId": "team"' in calls[0][-1]


def test_calendar_fails_closed_on_provider_error_repeated_token_and_bad_shape(provider: Provider) -> None:
    for responses in [
        [{"match": ["events"], "stdout": "", "stderr": "token expired for user@example.invalid", "code": 1}],
        [{"match": ["events"], "stdout": {**PAGE_ONE, "nextPageToken": "loop"}, "repeat": True}],
        [{"match": ["events"], "stdout": {"kind": "other"}}],
    ]:
        provider.install("gws", responses)
        result = provider.run("google-calendar.py", "primary", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
        assert result.returncode == 1
        assert result.stdout == ""
        assert "user@example.invalid" not in result.stderr
        assert "Calendar collection failed" in result.stderr


def test_git_history_projects_commits_of_nested_checkouts(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "code"
    repository = root / "owner" / "project"
    repository.mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-09-01T10:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-09-01T10:00:00+00:00",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
    }

    def run(*args: str) -> None:
        # Real git is the provider this adapter wraps; the repository is temporary and offline.
        subprocess.run(["git", "-C", str(repository), *args], env=env, check=True, capture_output=True)  # noqa: S603,S607

    run("init", "-q", "-b", "main")
    run("config", "user.email", "owner@example.invalid")
    run("config", "user.name", "Owner")
    (repository / "note.md").write_text("decision\n")
    run("add", "note.md")
    run("commit", "-q", "-m", "feat: keep durable evidence\n\nBecause providers forget.")
    (root / "loose").symlink_to(repository, target_is_directory=True)
    records = provider.records("git-history.py", str(root), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    assert len(records) == 1
    commit = records[0]
    assert commit.title == "owner/project: feat: keep durable evidence"
    assert commit.time == "2026-09-01T10:00:00.000000Z"
    assert "Because providers forget." in commit.text
    assert commit.links == ["person:email/owner@example.invalid", "repo:local/owner/project"]
    assert commit.aliases == [f"commit:local/{commit.id}"]
    assert provider.records("git-history.py", str(root), "2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z") == []
    assert provider.run("git-history.py", str(tmp_path / "absent"), "2026-09-01T00:00:00Z", "x").returncode == 1
    assert json.loads(provider.run("git-history.py", str(root), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z").stdout)


def test_people_and_repository_identities_join_across_providers(
    provider: Provider, tmp_path: Path, base: Store
) -> None:
    root = tmp_path / "code"
    (root / "project/.git").mkdir(parents=True)
    provider.install(
        "git",
        [
            {"match": ["remote"], "stdout": "git@github.com:Owner/Project.git\n"},
            {
                "match": ["log"],
                "stdout": "abc\u00002026-09-01T10:00:00Z\u0000Owner@Example.invalid\u0000Keep evidence\u0000",
            },
        ],
    )
    commits = provider.records("git-history.py", str(root), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    assert "repo:github.com/owner/project" in commits[0].links
    provider.install(
        "gws",
        [
            {
                "match": ["events"],
                "stdout": {
                    "kind": "calendar#events",
                    "items": [
                        {
                            "id": "event",
                            "summary": "Review",
                            "organizer": {"email": "Owner@Example.invalid"},
                            "attendees": [{"email": "colleague@example.invalid"}],
                        }
                    ],
                },
            }
        ],
    )
    events = provider.records("google-calendar.py", "primary", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    for source, records in [("git", commits), ("calendar", events)]:
        base.write(
            f"records/{source}/one.json",
            encode(Collection(source=source, captured="2026-09-02T00:00:00Z", records=records).model_dump()),
        )
    build(base)
    result = json.loads(encode(find(base, Query(text="person:email/owner@example.invalid"))))
    assert {item["source"] for item in result["items"]} == {"git", "calendar"}
