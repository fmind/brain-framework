"""GitHub history collects the selected scope and preserves evidence on incomplete pagination."""

from __future__ import annotations

import json
import os
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


def test_github_old_closed_issues_and_merged_pulls_are_separate(provider: Provider) -> None:
    pull = {**ISSUE, "number": 2, "pull_request": {"url": "https://api.github.com/repos/example/project/pulls/2"}}
    for kind, expected in (("issues", "closed"), ("pulls", "merged")):
        provider.install(
            "gh",
            [
                {"match": ["page=1"], "stdout": response([ISSUE], more=True)},
                {"match": ["page=2"], "stdout": response([pull])},
                {
                    "match": ["pulls/2"],
                    "stdout": "HTTP/2.0 200 OK\n\n" + json.dumps({**ISSUE, "number": 2, "merged_at": START}),
                },
            ],
        )
        [record] = provider.records("github-history.py", "example/project", kind, START, END)
        assert record.attributes["state"] == expected
        assert record.time.startswith("2015-01-01")
        assert record.updated.startswith("2026-09-01")
        assert "inspect the evidence" in record.text
        assert len(provider.calls("gh")) == (3 if kind == "pulls" else 2)
        assert {"state=all", "sort=updated", "direction=asc"} <= set(provider.calls("gh")[0])


def test_github_bad_pages_and_duplicate_ids_emit_no_partial_records(provider: Provider) -> None:
    for bad in (
        {"code": 1, "stderr": "private upstream response"},
        {"stdout": response([ISSUE])},
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


def test_github_pull_detail_failure_does_not_publish_the_listing(provider: Provider) -> None:
    pull = {**ISSUE, "pull_request": {"url": "https://api.github.com/repos/example/project/pulls/1"}}
    for detail in (
        {},
        {**ISSUE, "merged_at": None, "number": 99},
        {**ISSUE, "merged_at": None, "updated_at": "2010-01-01T00:00:00Z"},
        {**ISSUE, "merged_at": None, "updated_at": END},
        {**ISSUE, "merged_at": None, "updated_at": "2026-09-03T00:00:00Z"},
    ):
        provider.install(
            "gh",
            [
                {"match": ["issues"], "stdout": response([pull])},
                {"match": ["pulls/1"], "stdout": "HTTP/2.0 200 OK\n\n" + json.dumps(detail)},
            ],
        )
        result = provider.run("github-history.py", "example/project", "pulls", START, END)
        assert result.returncode == 1
        assert result.stdout == ""


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
