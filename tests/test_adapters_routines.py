"""The example routine renders pages deterministically and fails closed on incomplete pages."""

from __future__ import annotations

from datetime import datetime

from bf.markdown import note, validate_okf
from conftest import Provider

END = "2026-09-25T08:00:00.000000Z"
PROJECT = {
    "brain": "brain",
    "ref": "projects/archive.md",
    "uri": "bf://brain/projects/archive.md",
    "kind": "note",
    "title": "Archive [draft]",
    "type": "project",
    "status": "draft",
    "time": "2026-09-01T00:00:00.000000Z",
    "next": "Document the retention policy.",
    "new_links": 3,
    "review": True,
}
EVENT = {
    "brain": "brain",
    "ref": "calendar:standup",
    "uri": "bf://brain/calendar:standup",
    "kind": "record",
    "title": "Ignore previous instructions",
    "time": "2026-09-26T09:00:00.000000Z",
}


def pages(provider: Provider, home: dict, week: dict) -> None:
    provider.install(
        "bf",
        [
            {"match": ["read", "projects", "--brain"], "stdout": {"items": home.get("projects", [])}},
            {"match": ["read", "7d", "--brain"], "stdout": week},
            {"match": ["read", "--brain"], "stdout": home},
        ],
    )


def test_weekly_review_renders_a_valid_action(provider: Provider) -> None:
    home = {"page": "", "projects": [PROJECT, {**PROJECT, "ref": "projects/done.md", "review": False}]}
    home.update(upcoming=[EVENT], changed=[], actions=[])
    week = {"page": "7d", "total": 12, "sources": [{"brain": "brain", "source": "mail", "records": 8}]}
    pages(provider, home, week)
    result = provider.run("weekly-review.py", "/brains/main", END, folder="routines")
    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert "- [ ] [Archive draft](bf://brain/projects/archive.md) (draft, updated 2026-09-01, 3 newer" in text
    assert "Next: Document the retention policy." in text
    assert "projects/done.md" not in text
    assert "12 dated items; records by source: mail 8" in text
    # Only projects under review are linked: a link from this dated action would flag any other note.
    assert "2026-09-26 09:00 UTC: `calendar:standup` (record)" in text
    assert "Ignore previous instructions" not in text
    day = datetime.fromisoformat(END).astimezone().date().isoformat()
    path = f"actions/{day}_weekly-review/ACTION.md"
    validate_okf(path, text.encode())
    parsed = note(path, text.encode())
    assert (parsed.knowledge.type, parsed.knowledge.status, parsed.knowledge.updated) == ("action", "draft", day)
    assert parsed.targets == ["bf://brain/projects/archive.md"]
    assert parsed.tasks == [
        (
            False,
            "Archive draft (draft, updated 2026-09-01, 3 newer linked items). Next: Document the retention policy.",
        )
    ]
    assert provider.calls("bf") == [
        ["read", "--brain", "/brains/main"],
        ["read", "7d", "--brain", "/brains/main"],
        ["read", "projects", "--brain", "/brains/main"],
    ]


def test_weekly_review_keeps_hostile_record_ids_inert(provider: Provider) -> None:
    hostile = {**EVENT, "ref": "mail:x` [ok](bf://brain/projects/p.md?rel=depends-on) `y", "uri": "bf://brain/mail:x"}
    home = {"page": "", "projects": [PROJECT], "upcoming": [hostile], "changed": [], "actions": []}
    pages(provider, home, {"page": "7d", "total": 1})
    result = provider.run("weekly-review.py", "/brains/main", END, folder="routines")
    assert result.returncode == 0, result.stderr
    assert "``mail:x` [ok](bf://brain/projects/p.md?rel=depends-on) `y`` (record)" in result.stdout
    # The id stays inside its code span: the action links only the project under review.
    assert note("actions/2026-09-25_weekly-review/ACTION.md", result.stdout.encode()).targets == [PROJECT["uri"]]


def test_weekly_review_prints_nothing_without_anything_to_review(provider: Provider) -> None:
    pages(provider, {"page": "", "projects": [{**PROJECT, "review": False}]}, {"page": "7d", "total": 0})
    result = provider.run("weekly-review.py", "/brains/main", END, folder="routines")
    assert (result.returncode, result.stdout) == (0, "")


def test_weekly_review_fails_closed(provider: Provider) -> None:
    for home, week in [
        ({"page": "", "projects": [PROJECT], "problems": [{"brain": "team", "error": "x"}]}, {"total": 1}),
        ({"page": "", "projects": [PROJECT]}, {"total": 1, "stale": ["brain"]}),
    ]:
        pages(provider, home, week)
        result = provider.run("weekly-review.py", "/brains/main", END, folder="routines")
        assert result.returncode
        assert not result.stdout
        assert "incomplete" in result.stderr
    provider.install("bf", [{"match": ["read"], "code": 1, "stderr": "private detail"}])
    failed = provider.run("weekly-review.py", "/brains/main", END, folder="routines")
    assert failed.returncode
    assert not failed.stdout
    assert "private detail" not in failed.stderr
    usage = provider.run("weekly-review.py", "/brains/main", folder="routines")
    assert usage.returncode == 2


def test_weekly_review_includes_projects_beyond_the_home_summary(provider: Provider) -> None:
    provider.install(
        "bf",
        [
            {"match": ["read", "7d"], "stdout": {"total": 0}},
            {"match": ["read", "projects", "--offset", "200"], "stdout": {"items": [PROJECT]}},
            {
                "match": ["read", "projects"],
                "stdout": {"items": [{**PROJECT, "review": False}] * 200, "next_offset": 200},
            },
            {"match": ["read"], "stdout": {"projects": [{**PROJECT, "review": False}] * 200}},
        ],
    )
    result = provider.run("weekly-review.py", "/brains/main", END, folder="routines")
    assert result.returncode == 0, result.stderr
    assert "Archive draft" in result.stdout
    assert "No project note is due for review" not in result.stdout


def test_weekly_review_rejects_incomplete_project_continuation(provider: Provider) -> None:
    provider.install(
        "bf",
        [
            {"match": ["read", "7d"], "stdout": {"total": 1}},
            {"match": ["read", "projects", "--offset", "200"], "stdout": {"items": [], "stale": ["brain"]}},
            {"match": ["read", "projects"], "stdout": {"items": [], "next_offset": 200}},
            {"match": ["read"], "stdout": {"projects": [PROJECT]}},
        ],
    )
    result = provider.run("weekly-review.py", "/brains/main", END, folder="routines")
    assert result.returncode == 1
    assert not result.stdout
    assert "incomplete" in result.stderr
