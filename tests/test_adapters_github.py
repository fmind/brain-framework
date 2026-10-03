"""GitHub history collects the selected scope and preserves evidence on incomplete pagination."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from bf.cli import app
from conftest import ROOT, Provider

START, END = "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"
ISSUE = {
    "number": 1,
    "title": "Keep the original decision",
    "body": "We chose files so people can inspect the evidence.",
    "state": "closed",
    "created_at": "2015-01-01T00:00:00Z",
    "updated_at": START,
}
COMMIT: dict[str, Any] = {
    "sha": "a" * 40,
    "commit": {
        "message": "Document the decision\n\nKeep the rationale searchable.",
        "author": {"name": "Example Author"},
        "committer": {"date": START},
    },
}


def response(items: list[dict], *, more: bool = False) -> str:
    link = '\nLink: <https://api.github.com/repos/example/project/issues?page=2>; rel="next"' if more else ""
    return f"HTTP/2.0 200 OK{link}\n\n" + json.dumps(items)


def test_github_main_commits_respect_half_open_window(provider: Provider) -> None:
    later = {**COMMIT, "sha": "b" * 40, "commit": {**COMMIT["commit"], "committer": {"date": END}}}
    provider.install("gh", [{"match": ["commits"], "stdout": response([COMMIT, later])}])
    [record] = provider.records("github-history.py", "Example/Project", "commits", START, END)
    assert record.id == "example/project/commits/" + "a" * 40
    assert "Keep the rationale searchable." in record.text
    assert record.links == ["repo:github.com/example/project"]
    [call] = provider.calls("gh")
    assert {"sha=main", "github.com", "GET", "--include", "per_page=100", "since=2026-08-31T23:59:59+00:00"} <= set(
        call
    )
    provider.install("gh", [{"match": ["sha=release"], "stdout": response([COMMIT])}])
    assert provider.records("github-history.py", "example/project", "commits", START, END, "--branch", "release")


def pull(number: int, state: str, merged: str | None) -> dict[str, Any]:
    """A PR as the repository issues endpoint lists it, with its merge time."""
    url = f"https://api.github.com/repos/example/project/pulls/{number}"
    return {**ISSUE, "number": number, "state": state, "pull_request": {"url": url, "merged_at": merged}}


def test_github_old_closed_issues_and_merged_pulls_are_separate(provider: Provider) -> None:
    for kind, expected in (("issues", "closed"), ("pulls", "merged")):
        provider.install(
            "gh",
            [
                {"match": ["page=1"], "stdout": response([ISSUE], more=True)},
                {"match": ["page=2"], "stdout": response([pull(2, "closed", START)])},
            ],
        )
        [record] = provider.records("github-history.py", "example/project", kind, START, END)
        assert record.attributes["state"] == expected
        assert record.time.startswith("2015-01-01")
        assert record.updated.startswith("2026-09-01")
        assert "inspect the evidence" in record.text
        # The listing states the merge time: no request per PR.
        assert len(provider.calls("gh")) == 2
        assert {"state=all", "sort=updated", "direction=asc"} <= set(provider.calls("gh")[0])


def test_github_bad_pages_and_duplicate_ids_emit_no_partial_records(provider: Provider) -> None:
    for bad in (
        {"code": 1, "stderr": "private upstream response"},
        # Listed again with another modification time: it changed during the run.
        {"stdout": response([{**ISSUE, "updated_at": "2026-09-01T00:00:01Z"}])},
        {"stdout": "HTTP/2.0 200 OK\n\n{}"},
        {"stdout": response([], more=True)},
        {"stdout": "HTTP/2.0 200 OK\nLink: broken\n\n[]"},
        {"stdout": response([{**ISSUE, "number": 2, "updated_at": "bad private value"}])},
    ):
        provider.install(
            "gh",
            [{"match": ["page=1"], "stdout": response([ISSUE], more=True)}, {"match": ["page=2"], **bad}],
        )
        result = provider.run("github-history.py", "example/project", "issues", START, END)
        assert result.returncode == 1
        assert not result.stdout
        assert "private" not in result.stderr


@pytest.mark.parametrize(
    "link",
    [
        '<https://api.github.com/repos/example/project/issues?page=2>; rel="nex"',
        '<https://api.github.com/repos/example/project/issues?page=3>; rel="next"',
        '<https://api.github.com/repos/example/project/issues>; rel="next"',
        '<https://api.github.com/repos/example/project/issues?page=2&page=3>; rel="next"',
        '<https://private.example/issues?page=2>; rel="next"',
        (
            '<https://api.github.com/repos/example/project/issues?page=2>; rel="next", '
            '<https://api.github.com/repos/example/project/issues?page=3>; rel="next"'
        ),
    ],
)
def test_github_rejects_ambiguous_pagination_before_requesting_another_page(provider: Provider, link: str) -> None:
    provider.install(
        "gh",
        [
            {"match": ["page=1"], "stdout": "HTTP/2.0 200 OK\nLink: " + link + "\n\n" + json.dumps([ISSUE])},
            {"match": ["page=2"], "stdout": response([])},
        ],
    )
    result = provider.run("github-history.py", "example/project", "issues", START, END)
    assert result.returncode == 1
    assert not result.stdout
    assert "invalid pagination" in result.stderr
    assert "private" not in result.stderr
    assert len(provider.calls("gh")) == 1


@pytest.mark.parametrize(
    "failure",
    [
        {"code": 1, "stderr": "private upstream response"},
        {"stdout": 'HTTP/2.0 200 OK\nLink: <https://private.example/>; rel="nex"\n\n[]'},
    ],
)
def test_github_failure_preserves_collected_evidence(
    provider: Provider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: dict[str, object]
) -> None:
    brain = tmp_path / "brain"
    runner = CliRunner()
    assert runner.invoke(app, ["init", str(brain)]).exit_code == 0
    monkeypatch.chdir(brain)
    monkeypatch.setenv("PATH", f"{provider.bin}:{os.environ['PATH']}")
    config = yaml.safe_load((brain / "bf.yaml").read_text())
    config["sensors"] = {
        "github-issues": {
            "command": [
                "python3",
                str(ROOT / "examples/sensors/github-history.py"),
                "example/project",
                "issues",
                "{{start}}",
                "{{end}}",
            ],
        }
    }
    (brain / "bf.yaml").write_text(yaml.safe_dump(config))
    args = ["collect", "github-issues", "--since", START, "--until", END]
    provider.install("gh", [{"match": ["page=1"], "stdout": response([ISSUE])}])
    success = runner.invoke(app, args)
    assert success.exit_code == 0, success.output
    paths = list((brain / "memories/github-issues").rglob("*.json"))
    assert len(paths) == 1
    before = paths[0].read_bytes()
    provider.install(
        "gh",
        [
            {"match": ["page=1"], "stdout": response([{**ISSUE, "body": "changed"}], more=True)},
            {"match": ["page=2"], **failure},
        ],
    )
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert paths[0].read_bytes() == before
    assert "private upstream response" not in result.output
    assert runner.invoke(app, ["validate"]).exit_code == 0
    result = runner.invoke(app, ["search", "inspect the evidence"])
    assert result.exit_code == 0
    assert "original decision" in result.output


def test_github_pull_state_comes_from_the_listing(provider: Provider) -> None:
    pulls = [pull(1, "closed", START), pull(2, "closed", None), pull(3, "open", None), ISSUE]
    provider.install("gh", [{"match": ["issues"], "stdout": response(pulls)}])
    records = provider.records("github-history.py", "example/project", "pulls", START, END)
    assert [(record.id, record.attributes["state"]) for record in records] == [
        ("example/project/pulls/1", "merged"),
        ("example/project/pulls/2", "closed"),
        ("example/project/pulls/3", "open"),
    ]
    assert records[0].attributes["merged"] == START
    assert len(provider.calls("gh")) == 1
    # Without its merge time, a merged PR would be saved as closed: the run fails instead.
    unmerged = {
        **pull(4, "closed", None),
        "pull_request": {"url": "https://api.github.com/repos/example/project/pulls/4"},
    }
    provider.install("gh", [{"match": ["issues"], "stdout": response([unmerged])}])
    result = provider.run("github-history.py", "example/project", "pulls", START, END)
    assert (result.returncode, result.stdout) == (1, "")
    assert "without its merge time" in result.stderr


# A stateful issue listing: `since` is inclusive and pages hold 100 objects in modification order. Changes
# scheduled for a call apply before it is answered, as an edit between two requests would.
LISTING = """
import json, sys
from datetime import datetime
from pathlib import Path

state = Path(__file__).with_name("gh.json")
data = json.loads(state.read_text())
fields = dict(argument.split("=", 1) for argument in sys.argv[1:] if "=" in argument)
data["calls"].append(fields)
for change in data["changes"].pop(str(len(data["calls"])), []):
    next(issue for issue in data["issues"] if issue["number"] == change["number"]).update(change)
state.write_text(json.dumps(data))
since, number = datetime.fromisoformat(fields["since"]), int(fields["page"])
listed = sorted(
    (issue for issue in data["issues"] if datetime.fromisoformat(issue["updated_at"]) >= since),
    key=lambda issue: (datetime.fromisoformat(issue["updated_at"]), issue["number"]),
)
more = len(listed) > number * 100
link = f'\\nLink: <https://api.github.com/repos/example/project/issues?page={number + 1}>; rel="next"' if more else ""
sys.stdout.write(f"HTTP/2.0 200 OK{link}\\n\\n" + json.dumps(listed[(number - 1) * 100 : number * 100]))
"""


def listing(provider: Provider, issues: list[dict[str, Any]], changes: dict[str, list[dict[str, Any]]]) -> Path:
    """Install the stateful listing as `gh`; the returned state file records each call's fields."""
    executable = provider.bin / "gh"
    executable.write_text(f"#!{sys.executable}" + LISTING)
    executable.chmod(0o700)
    state = provider.bin / "gh.json"
    state.write_text(json.dumps({"issues": issues, "changes": changes, "calls": []}))
    return state


def minute(day: int, offset: int) -> str:
    return f"2026-09-{day:02}T{offset // 60:02}:{offset % 60:02}:00Z"


def test_github_issue_pages_survive_a_change_during_the_run(provider: Provider) -> None:
    # A backfill window of 150 issues, one modified per minute, and 120 modified after the window.
    issues = [{**ISSUE, "number": n, "updated_at": minute(1, n)} for n in range(1, 151)]
    issues += [{**ISSUE, "number": n, "updated_at": minute(3, n)} for n in range(151, 271)]
    # After the first page, issue 1 changes and moves to the end of the order. Page 2 by number would start one
    # object later, so issue 101, shifted onto the page already read, went missing while the run succeeded.
    state = listing(provider, issues, {"2": [{"number": 1, "updated_at": "2026-09-05T00:00:00Z"}]})
    records = provider.records("github-history.py", "example/project", "issues", START, END)
    assert sorted(int(record.id.rpartition("/")[2]) for record in records) == list(range(1, 151))
    calls = json.loads(state.read_text())["calls"]
    # The second request restarts after issue 100's modification time instead.
    assert [(call["since"], call["page"]) for call in calls] == [
        ("2026-08-31T23:59:59+00:00", "1"),
        ("2026-09-01T01:39:59+00:00", "1"),
    ]
    # In a window reaching the present, the edited issue comes back after END on the last page: it belongs to a
    # later window, and keyset pages hid nothing, so the run succeeds instead of reporting a repeated identity.
    listing(provider, issues[:150], {"2": [{"number": 1, "updated_at": "2026-09-05T00:00:00Z"}]})
    records = provider.records("github-history.py", "example/project", "issues", START, END)
    assert sorted(int(record.id.rpartition("/")[2]) for record in records) == list(range(1, 151))


def test_github_pages_through_a_busy_second_by_number(provider: Provider) -> None:
    # 130 issues share one modification second, as after a bulk label change: no restart can pass it.
    issues = [{**ISSUE, "number": n, "updated_at": "2026-09-01T10:00:00Z"} for n in range(1, 131)]
    issues.append({**ISSUE, "number": 131, "updated_at": "2026-09-01T11:00:00Z"})
    state = listing(provider, issues, {})
    records = provider.records("github-history.py", "example/project", "issues", START, END)
    assert len({record.id for record in records}) == 131
    calls = json.loads(state.read_text())["calls"]
    assert [(call["since"], call["page"]) for call in calls] == [
        ("2026-08-31T23:59:59+00:00", "1"),
        ("2026-08-31T23:59:59+00:00", "2"),
    ]


def test_github_empty_collection_and_invalid_scope(provider: Provider) -> None:
    provider.install("gh", [{"match": ["issues"], "stdout": response([])}])
    assert provider.records("github-history.py", "example/project", "issues", START, END) == []
    provider.install("gh", [])
    result = provider.run("github-history.py", "https://github.com/example/project", "issues", START, END)
    assert result.returncode == 2
    assert not provider.calls("gh")


def test_github_limits_reject_incomplete_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    module = runpy.run_path(str(ROOT / "examples/sensors/github-history.py"))
    collect = module["collect"]
    namespace = collect.__globals__
    monkeypatch.setitem(namespace, "page", lambda *_: ([ISSUE], True))
    monkeypatch.setitem(namespace, "MAX_PAGES", 1)
    with pytest.raises(ValueError, match="pages"):
        collect("example/project", "issues", START, END, "main")
    monkeypatch.setitem(namespace, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="limit"):
        collect("example/project", "issues", START, END, "main")
