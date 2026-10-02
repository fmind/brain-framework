"""Catalog adapters for local Git history and Google Calendar behave against fakes and a temporary repository."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from bf.models import Query
from bf.retrieve import search
from bf.storage import Store
from conftest import ROOT, Provider, records_file

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
# git-history checks the Git version once before reading any repository.
GIT_VERSION = {"match": ["version"], "stdout": "git version 2.47.0\n", "repeat": True}
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


def test_calendar_agenda_and_window_keep_cancellations_titled_as_cancelled(provider: Provider) -> None:
    offsite = {
        "id": "offsite",
        "status": "cancelled",
        "summary": "Team offsite",
        "start": {"dateTime": "2026-09-29T09:00:00Z"},
    }
    # A cancelled instance of a recurring event may carry only its original start.
    instance = {
        "id": "weekly_20260930",
        "status": "cancelled",
        "recurringEventId": "weekly",
        "originalStartTime": {"dateTime": "2026-09-30T09:00:00Z"},
    }
    page = {"kind": "calendar#events", "items": [offsite, instance]}
    provider.install("gws", [{"match": ["events", "list"], "stdout": page, "repeat": True}])
    window = ("primary", "2026-09-28T00:00:00Z", "2026-10-01T00:00:00Z")
    # The agenda snapshot keeps a cancellation in place, so it never shrinks toward the removal guard.
    agenda = provider.records("google-calendar.py", *window, "--agenda-days", "7")
    records = provider.records("google-calendar.py", *window)
    assert all(json.loads(call[-1])["showDeleted"] is True for call in provider.calls("gws"))
    assert "originalStartTime" in json.loads(provider.calls("gws")[-1][-1])["fields"]
    expected = [
        ("Cancelled: Team offsite", "2026-09-29T09:00:00.000000Z"),
        ("Cancelled calendar event", "2026-09-30T09:00:00.000000Z"),
    ]
    assert [(record.title, record.time) for record in agenda] == expected
    assert [(record.title, record.time) for record in records] == expected
    assert [record.aliases for record in agenda] == [["agenda:primary/offsite"], ["agenda:primary/weekly_20260930"]]


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
        subprocess.run(["git", "-C", str(repository), *args], env=env, check=True, capture_output=True, timeout=30)  # noqa: S603,S607

    run("init", "-q", "-b", "main")
    run("config", "user.email", "owner@fmind.dev")
    run("config", "user.name", "Owner")
    (repository / "note.md").write_text("decision\n")
    run("add", "note.md")
    run("commit", "-q", "-m", "feat: keep durable evidence\n\nBecause providers forget.")
    (root / "loose").symlink_to(repository, target_is_directory=True)
    records = provider.records("git-history.py", str(root), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    assert len(records) == 1
    commit = records[0]
    assert commit.attributes["author_refs"] == ["person:email/owner@fmind.dev"]
    assert "repo:local/owner/project" in cast(list[str], commit.attributes["repository_refs"])
    assert commit.title == "owner/project: feat: keep durable evidence"
    assert commit.time == "2026-09-01T10:00:00.000000Z"
    assert "Because providers forget." in commit.text
    assert commit.links == ["person:email/owner@fmind.dev", "repo:local/owner/project"]
    assert commit.aliases == [f"commit:local/{commit.id}"]
    assert provider.records("git-history.py", str(root), "2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z") == []
    assert provider.run("git-history.py", str(tmp_path / "absent"), "2026-09-01T00:00:00Z", "x").returncode == 1
    assert json.loads(provider.run("git-history.py", str(root), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z").stdout)
    # Automated history stays out: bot and test authors, hidden repositories and skipped loops.
    for email in ("dependabot[bot]@users.noreply.github.com", "test@example.invalid"):
        run("-c", f"user.email={email}", "commit", "-q", "--allow-empty", "-m", "chore: automated")
    hidden = root / ".codex" / "memories"
    hidden.mkdir(parents=True)
    subprocess.run(["git", "-C", str(hidden), "init", "-q"], env=env, check=True, capture_output=True, timeout=30)  # noqa: S603,S607
    window = (str(root), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    assert len(provider.records("git-history.py", *window)) == 1
    # Before 17 a linked worktree collected its repository's history again under its own folder name.
    run("worktree", "add", "-q", "-b", "feature", str(root / "owner" / "project-feature"))
    assert [record.id.partition("@")[0] for record in provider.records("git-history.py", *window)] == ["owner/project"]
    assert provider.records("git-history.py", *window, "--skip", "owner/project") == []
    assert provider.run("git-history.py", *window, "--bad").returncode == 1


def test_people_and_repository_identities_join_across_providers(
    provider: Provider, tmp_path: Path, brain: Store
) -> None:
    root = tmp_path / "code"
    (root / "project/.git").mkdir(parents=True)
    provider.install(
        "git",
        [
            GIT_VERSION,
            {"match": ["remote"], "stdout": "git@github.com:Owner/Project.git\n"},
            {
                "match": ["log"],
                "stdout": "abc\u00002026-09-01T10:00:00Z\u0000Owner@Fmind.dev\u0000Keep evidence\u0000",
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
                            "organizer": {"email": "Owner@Fmind.dev"},
                            "attendees": [{"email": "colleague@example.invalid"}],
                        }
                    ],
                },
            }
        ],
    )
    events = provider.records("google-calendar.py", "primary", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    for source, records in [("git", commits), ("calendar", events)]:
        records_file(brain, source, records)
    result = search([brain], Query(text="person:email/owner@fmind.dev"))
    items = cast("list[dict[str, object]]", result["items"])
    assert {item["source"] for item in items} == {"git", "calendar"}


def test_calendar_keeps_organizer_separate_from_invited_attendees(provider: Provider) -> None:
    event = {
        **cast(list[dict], PAGE_ONE["items"])[0],
        "organizer": {"email": "OWNER@example.test"},
        "attendees": [{"email": "guest@example.test"}],
    }
    provider.install("gws", [{"match": ["events", "list"], "stdout": {"kind": "calendar#events", "items": [event]}}])
    record = provider.records("google-calendar.py", "team", "2026-09-01T00:00:00Z", "2026-09-03T00:00:00Z")[0]
    assert record.attributes["organizer_refs"] == ["person:email/owner@example.test"]
    assert record.attributes["attendee_refs"] == ["person:email/guest@example.test"]
    assert set(cast(list[str], record.attributes["participant_refs"])) == {
        "person:email/owner@example.test",
        "person:email/guest@example.test",
    }


@pytest.mark.parametrize("backdated_tip", [False, True])
def test_git_history_obeys_half_open_windows_with_out_of_order_dates(
    provider: Provider, tmp_path: Path, backdated_tip: bool
) -> None:
    root = tmp_path / "code"
    repository = root / "project"
    repository.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig")}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repository), *args], env=env, check=True, capture_output=True, timeout=30)  # noqa: S603,S607

    git("init", "-q", "-b", "main")
    git("config", "user.email", "owner@fmind.dev")
    git("config", "user.name", "Owner")
    dates = ["2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"]
    if backdated_tip:
        dates.append("2026-08-01T00:00:00Z")
    for when in dates:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        git("commit", "-q", "--allow-empty", "-m", "Evidence " + when)
    # BF substitutes canonical instants with microseconds for `{{start}}` and `{{end}}`; Git must accept them.
    start, end = "2026-09-01T00:00:00.000000Z", "2026-09-02T00:00:00.000000Z"
    records = provider.records("git-history.py", str(root), start, end)
    assert len(records) == 1
    assert datetime.fromisoformat(records[0].time) == datetime.fromisoformat(start)
    # Invalid windows fail even when the selected root contains no repositories.
    empty = tmp_path / "empty"
    empty.mkdir()
    for invalid in ((end, start), (start, "not-a-date"), ("2026-09-01", end)):
        result = provider.run("git-history.py", str(empty), *invalid)
        assert result.returncode == 1
        assert not result.stdout


def test_git_history_reads_history_refs_only(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "code"
    repository = root / "my project"
    repository.mkdir(parents=True)
    (root / "fresh").mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-09-01T10:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-09-01T10:00:00+00:00",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
    }

    def git(folder: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(folder), *args], env=env, check=True, capture_output=True)  # noqa: S603,S607

    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.email", "owner@fmind.dev")
    git(repository, "config", "user.name", "Owner")
    git(repository, "commit", "-q", "--allow-empty", "-m", "keep\tevidence")
    git(repository, "commit", "-q", "--allow-empty", "--allow-empty-message", "-m", "")
    (repository / "draft.md").write_text("draft\n")
    git(repository, "add", "draft.md")
    git(repository, "stash", "-q")
    git(repository, "notes", "add", "-m", "reviewed", "HEAD")
    # A new repository without commits has an unborn HEAD; it holds no history, not an error.
    git(root / "fresh", "init", "-q", "-b", "main")
    records = provider.records("git-history.py", str(root), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    # Stash and notes commits stay out; a tab and an empty message still give one-line titles.
    assert sorted(record.title for record in records) == ["my project:", "my project: keep evidence"]
    assert {record.aliases[0].partition("@")[0] for record in records} == {"commit:local/my%20project"}
    assert all("repo:local/my%20project" in record.links for record in records)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list every folder")
def test_git_history_skips_unreadable_and_unnameable_folders(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "home"
    (root / "code/project/.git").mkdir(parents=True)
    # A Latin-1 folder name is not UTF-8: Python reads it with a lone surrogate that JSON cannot carry.
    skipped = 2
    try:
        (root / os.fsdecode(b"code/caf\xe9/.git")).mkdir(parents=True)
    except OSError:
        skipped -= 1  # APFS stores only UTF-8 names
    (root / "code/bad\x01name/.git").mkdir(parents=True)
    private = root / "private"
    private.mkdir()
    provider.install(
        "git",
        [
            GIT_VERSION,
            # `log` comes before `remote`: its `--remotes` argument would also match `remote`.
            {"match": ["log"], "stdout": "abc\u00002026-09-01T10:00:00Z\u0000owner@fmind.dev\u0000Keep\u0000"},
            {"match": ["remote"], "code": 2, "repeat": True},
        ],
    )
    private.chmod(0)
    try:
        result = provider.run("git-history.py", str(root), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    finally:
        private.chmod(0o700)
    assert result.returncode == 0, result.stderr
    assert [record["id"] for record in json.loads(result.stdout)] == ["code/project@abc"]
    assert f"skipped {skipped + 1} " in result.stderr
    assert "caf" not in result.stderr


def test_git_history_names_the_limit_it_reached(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "code"
    for index in range(201):
        (root / f"project-{index}/.git").mkdir(parents=True)
    result = provider.run("git-history.py", str(root), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    assert result.returncode == 1
    assert not result.stdout
    assert result.stderr == (
        "Git history collection failed: more than 200 repositories; select a narrower ROOT or add --skip.\n"
    )


def test_git_history_names_the_repository_git_cannot_read(provider: Provider, tmp_path: Path) -> None:
    # A stale worktree, damaged objects or another owner's repository fails the run by name, so it can be skipped.
    root = tmp_path / "code"
    for name in ("healthy", "worktree-x9"):
        (root / name / ".git").mkdir(parents=True)
    provider.install(
        "git",
        [
            GIT_VERSION,
            {"match": ["worktree-x9", "log"], "code": 128, "stderr": "fatal: not a git repository\n", "repeat": True},
            {
                "match": ["log"],
                "stdout": "abc\u00002026-09-01T10:00:00Z\u0000owner@fmind.dev\u0000Keep\u0000",
                "repeat": True,
            },
            {"match": ["remote"], "code": 2, "repeat": True},
        ],
    )
    window = (str(root), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    result = provider.run("git-history.py", *window)
    assert result.returncode == 1
    assert not result.stdout
    assert result.stderr == (
        "Git history collection failed: worktree-x9: Git failed or timed out; repair the repository or add "
        "--skip worktree-x9.\n"
    )
    assert [record.id for record in provider.records("git-history.py", *window, "--skip", "worktree-x9")] == [
        "healthy@abc"
    ]


def test_git_history_names_a_repository_whose_remote_lookup_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stalled mount can hang any Git call, including the remote lookup before `git log`.
    spec = importlib.util.spec_from_file_location("git_history", ROOT / "examples/sensors/git-history.py")
    assert spec is not None
    assert spec.loader is not None
    sensor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sensor)
    root = tmp_path / "code"
    for name in ("healthy", "slow"):
        (root / name / ".git").mkdir(parents=True)

    def git(argv: list[str], _limit: int, _timeout: int) -> bytes:
        if argv[1:] == ["version"]:
            return b"git version 2.47.0\n"
        if argv[3] == "remote" and Path(argv[2]).name == "slow":
            raise TimeoutError("provider exceeded its timeout")
        if argv[3] == "remote":
            raise subprocess.CalledProcessError(2, argv)
        return b""

    monkeypatch.setattr(sensor, "run", git)
    window = (root, "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00")
    with pytest.raises(sensor.InvalidError, match=r"^slow: Git failed or timed out; repair .* add --skip slow$"):
        sensor.collect(*window)
    assert sensor.collect(*window, frozenset({"slow"})) == ([], 0)


@pytest.mark.parametrize("reply", ["git version 2.36.6\n", "not git\n", None])
def test_git_history_requires_git_2_37(provider: Provider, tmp_path: Path, reply: str | None) -> None:
    # `git log --since-as-filter` first shipped in Git 2.37: an older or missing Git fails once, by its requirement.
    (tmp_path / "code/project/.git").mkdir(parents=True)
    version = {"match": ["version"], "stdout": reply} if reply else {"match": ["version"], "code": 127}
    provider.install("git", [version, {"match": [], "code": 97, "repeat": True}])
    result = provider.run("git-history.py", str(tmp_path / "code"), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == "Git history collection failed: Git 2.37 or later is required; install it on PATH.\n"
    assert provider.calls("git") == [["version"]]


def test_git_history_drops_an_author_link_beyond_the_reference_bound(provider: Provider, tmp_path: Path) -> None:
    (tmp_path / "code/project/.git").mkdir(parents=True)
    author = "a" * 8300 + "@fmind.dev"
    log = f"abc\u00002026-09-01T10:00:00Z\u0000{author}\u0000Keep\u0000"
    provider.install(
        "git", [GIT_VERSION, {"match": ["log"], "stdout": log}, {"match": ["remote"], "code": 2, "repeat": True}]
    )
    records = provider.records("git-history.py", str(tmp_path / "code"), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    assert [(record.links, record.attributes["author_refs"]) for record in records] == [(["repo:local/project"], [])]


def test_git_history_does_not_follow_linked_ancestor_directories(provider: Provider, tmp_path: Path) -> None:
    root = tmp_path / "code"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "project/.git").mkdir(parents=True)
    (root / "owner").symlink_to(outside, target_is_directory=True)
    provider.install("git", [{"match": [], "code": 97, "repeat": True}])
    records = provider.records("git-history.py", str(root), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    assert records == []
    assert provider.calls("git") == []
