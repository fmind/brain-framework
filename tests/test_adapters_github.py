"""GitHub search and notification adapters prove completeness against a fake gh and never leak provider output."""

from __future__ import annotations

import json

from conftest import Provider

SEARCH = "github-search.py"
NOTIFICATIONS = "github-notifications.py"
DAY = ("2026-09-01T00:00:00.000000Z", "2026-09-02T00:00:00.000000Z")


def issue(number: int, repository: str = "fmind/fkf", **extra: object) -> dict[str, object]:
    kind = "pull" if extra.get("pull") else "issues"
    return {
        "number": number,
        "title": f"  Keep durable\tevidence {number} ",
        "html_url": f"https://github.com/{repository}/{kind}/{number}",
        "state": "open",
        "body": "Because providers forget.\n\nDetails follow.",
        "created_at": "2026-08-30T10:00:00Z",
        "updated_at": "2026-09-01T10:00:00Z",
        "closed_at": None,
        "draft": bool(extra.get("pull")),
        "repository_url": f"https://api.github.com/repos/{repository}",
        "user": {"login": "owner"},
        "assignees": [{"login": "owner"}, {"login": "helper"}],
        "labels": ["bug", "retrieval"],
        "merged_at": None,
    }


def envelope(items: list[dict[str, object]], total: int | None = None) -> dict[str, object]:
    return {"total_count": len(items) if total is None else total, "incomplete_results": False, "items": items}


def test_issues_and_pull_requests_project_facts_and_bodies(provider: Provider) -> None:
    provider.install(
        "gh",
        [
            {
                "match": ["is:issue assignee:@me updated:2026-09-01T00:00:00Z..2026-09-01T23:59:59Z", "page=1"],
                "stdout": envelope([issue(1), issue(2, "fmind/brain")]),
            },
            {"match": ["is:pr involves:@me updated:", "page=1"], "stdout": envelope([issue(7, pull=True)])},
        ],
    )
    issues = provider.records(SEARCH, "issues", "assignee", *DAY)
    assert [record.id for record in issues] == [
        "https://github.com/fmind/brain/issues/2",
        "https://github.com/fmind/fkf/issues/1",
    ]
    first = issues[1]
    assert first.title == "fmind/fkf#1: Keep durable evidence 1"
    assert first.time == "2026-09-01T10:00:00.000000Z"
    for fact in (
        "Repository: fmind/fkf",
        "State: open",
        "Author: owner",
        "Assignees: owner, helper",
        "Labels: bug, retrieval",
        "Because providers forget.",
    ):
        assert fact in first.text
    assert first.url == "https://github.com/fmind/fkf/issues/1"
    assert first.links == ["repo:github.com/fmind/fkf"]
    assert first.aliases == ["github:fmind/fkf#1"]
    assert first.attributes["assignees"] == ["owner", "helper"]
    pulls = provider.records(SEARCH, "prs", "involves", *DAY)
    assert pulls[0].id == "https://github.com/fmind/fkf/pull/7"
    assert pulls[0].attributes["draft"] is True
    call = provider.calls("gh")[0]
    assert call[:5] == ["api", "--method", "GET", "/search/issues", "-H"]
    assert "q=is:issue assignee:@me updated:2026-09-01T00:00:00Z..2026-09-01T23:59:59Z" in call
    assert "sort=updated" in call
    assert "per_page=100" in call


def test_saturated_window_splits_until_complete_and_deduplicates(provider: Provider) -> None:
    shared = issue(3)
    provider.install(
        "gh",
        [
            {"match": ["updated:2026-09-01T00:00:00Z..2026-09-01T00:00:01Z"], "stdout": envelope([], 1000)},
            {"match": ["updated:2026-09-01T00:00:00Z..2026-09-01T00:00:00Z"], "stdout": envelope([issue(1), shared])},
            {"match": ["updated:2026-09-01T00:00:01Z..2026-09-01T00:00:01Z"], "stdout": envelope([shared, issue(2)])},
        ],
    )
    records = provider.records(SEARCH, "issues", "author", "2026-09-01T00:00:00Z", "2026-09-01T00:00:02Z")
    assert [record.attributes["number"] for record in records] == [1, 2, 3]
    assert len(provider.calls("gh")) == 3


def test_search_fails_closed(provider: Provider) -> None:
    scenarios = [
        [{"match": ["updated:"], "stdout": envelope([], 1000), "repeat": True}],
        [{"match": ["updated:"], "stdout": "", "stderr": "gh: token ghp_secret123 rejected for owner\n", "code": 1}],
        [{"match": ["updated:"], "stdout": "{not json"}],
        [
            {
                "match": ["updated:"],
                "stdout": {"total_count": 2, "incomplete_results": True, "items": [issue(1), issue(2)]},
            }
        ],
        [{"match": ["updated:"], "stdout": envelope([issue(1)], 2)}],
        [{"match": ["updated:"], "stdout": envelope([issue(1), issue(1)])}],
    ]
    for responses in scenarios:
        provider.install("gh", responses)
        result = provider.run(SEARCH, "issues", "assignee", "2026-09-01T00:00:00Z", "2026-09-01T00:00:01Z")
        assert result.returncode == 1, responses
        assert result.stdout == ""
        assert "ghp_secret123" not in result.stderr
        assert "owner" not in result.stderr
        assert "GitHub search collection failed" in result.stderr
    provider.install("gh", [])
    for arguments in [
        ("issues", "owner", *DAY),
        ("commits", "assignee", *DAY),
        ("prs", "assignee", DAY[1], DAY[0]),
        ("prs", "assignee", "2026-09-01", DAY[1]),
    ]:
        assert provider.run(SEARCH, *arguments).returncode == 1
    assert provider.calls("gh") == []


def notification(identifier: str, updated: str, subject: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "id": identifier,
        "unread": True,
        "reason": "review_requested",
        "updated_at": updated,
        "last_read_at": None,
        "subject": {
            "title": "  Keep durable evidence ",
            "url": "https://api.github.com/repos/fmind/fkf/pulls/7",
            "latest_comment_url": None,
            "type": "PullRequest",
            **(subject or {}),
        },
        "repository": {"full_name": "fmind/fkf", "html_url": "https://github.com/fmind/fkf"},
    }


def page(items: list[dict[str, object]], *, more: bool) -> str:
    link = '\nLink: <https://api.github.com/notifications?page=2>; rel="next"' if more else ""
    return f"HTTP/2.0 200 OK\nContent-Type: application/json{link}\n\n{json.dumps(items)}"


def test_notifications_follow_link_pages_filter_the_window_and_derive_urls(provider: Provider) -> None:
    provider.install(
        "gh",
        [
            {
                "match": ["/notifications", "page=1"],
                "stdout": page(
                    [
                        notification("1", "2026-09-01T08:00:00Z"),
                        notification("2", "2026-09-02T00:00:00Z"),
                        notification(
                            "3",
                            "2026-09-01T09:00:00Z",
                            subject={
                                "url": "https://api.github.com/repos/fmind/fkf/issues/9",
                                "type": "Issue",
                                "title": "",
                            },
                        ),
                    ],
                    more=True,
                ),
            },
            {
                "match": ["/notifications", "page=2"],
                "stdout": page(
                    [
                        notification(
                            "4",
                            "2026-09-01T10:00:00Z",
                            subject={"url": "https://api.github.com/repos/fmind/fkf/releases/5", "type": "Release"},
                        ),
                        notification("5", "2026-09-01T11:00:00Z", subject={"url": None, "type": "Discussion"}),
                        notification("6", "2026-08-31T23:59:59Z"),
                    ],
                    more=False,
                ),
            },
        ],
    )
    records = provider.records(NOTIFICATIONS, *DAY)
    assert [record.id for record in records] == ["1", "3", "4", "5"]
    first = records[0]
    assert first.title == "Keep durable evidence"
    assert first.time == "2026-09-01T08:00:00.000000Z"
    assert first.url == "https://github.com/fmind/fkf/pull/7"
    for fact in ("Repository: fmind/fkf", "Type: PullRequest", "Reason: review_requested", "Unread: yes"):
        assert fact in first.text
    assert first.links == ["repo:github.com/fmind/fkf"]
    assert first.attributes["subject_url"] == "https://api.github.com/repos/fmind/fkf/pulls/7"
    assert records[1].title == "Issue in fmind/fkf"
    assert records[1].url == "https://github.com/fmind/fkf/issues/9"
    assert records[2].url == "https://api.github.com/repos/fmind/fkf/releases/5"
    assert records[3].url == ""
    calls = provider.calls("gh")
    assert len(calls) == 2
    assert calls[0][:6] == ["api", "--method", "GET", "--include", "/notifications", "-F"]
    assert "since=2026-09-01T00:00:00Z" in calls[0]
    assert "before=2026-09-02T00:00:00Z" in calls[0]
    assert "page=2" in calls[1]


def test_notifications_fail_closed(provider: Provider) -> None:
    looping = page([notification("1", "2026-09-01T08:00:00Z")], more=True)
    scenarios = [
        [{"match": ["/notifications"], "stdout": "", "stderr": "gh: token ghp_secret123 rejected\n", "code": 1}],
        [{"match": ["/notifications"], "stdout": "HTTP/2.0 200 OK\n\n{not json"}],
        [{"match": ["/notifications"], "stdout": "[]"}],
        [{"match": ["/notifications"], "stdout": looping, "repeat": True}],
        [{"match": ["/notifications"], "stdout": page([{"id": "1", "updated_at": "yesterday"}], more=False)}],
    ]
    for responses in scenarios:
        provider.install("gh", responses)
        result = provider.run(NOTIFICATIONS, *DAY)
        assert result.returncode == 1, responses
        assert result.stdout == ""
        assert "ghp_secret123" not in result.stderr
        assert "GitHub notification collection failed" in result.stderr
    assert provider.run(NOTIFICATIONS, DAY[1], DAY[0]).returncode == 1
    assert provider.run(NOTIFICATIONS, "2026-09-01", DAY[1]).returncode == 1
