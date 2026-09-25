"""Brain-local discovery is portable, bounded and independent of collection trust."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml
from mcp.types import CallToolResult
from typer.testing import CliRunner

from bf.cli import app
from bf.config import load, may_collect, one, related, select, user_path
from bf.evaluate import evaluate
from bf.mcp import server
from bf.models import Error, Query
from bf.retrieve import read, search
from bf.storage import Store
from bf.update import update


def make(root: Path, name: str, references: dict[str, str] | None = None) -> Store:
    root.mkdir()
    store = Store(root)
    store.write(
        "bf.yaml",
        yaml.safe_dump(
            {
                "version": 4,
                "name": name,
                "brains": {key: {"path": value} for key, value in (references or {}).items()},
            }
        ).encode(),
    )
    store.write(
        "projects/example.md",
        f"# Shared evidence\n\n{name} decision.\n\n## Decision {{#decision}}\n\n{name} durable answer.\n".encode(),
    )
    return store


def test_direct_scope_without_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = make(tmp_path / "first", "first", {"second": "../second"})
    second = make(tmp_path / "second", "second", {"third": "../third", "first": "../first"})
    make(tmp_path / "third", "third")
    # Invalid global configuration must not interfere with local discovery.
    user_path().parent.mkdir(parents=True, exist_ok=True)
    user_path().write_text("invalid: [")
    monkeypatch.chdir(first.root / "projects")
    assert one().root == first.root
    assert one("first").root == first.root
    assert one("second").root == second.root
    assert one(str(first.root)).root == first.root
    found = cast(dict, search(select(), Query(text="durable")))
    assert {item["brain"] for item in found["items"]} == {"first", "second"}
    assert "problems" not in found
    second.write("memories/demo/undated.jsonl", b'{"id":"one","title":"Record evidence","text":"durable record"}\n')
    record_results = cast(dict, search(select(), Query(text="durable", source="demo")))
    assert len(record_results["items"]) == 1
    assert "problems" not in record_results
    assert "second durable answer" in str(read(select(), "bf://second/projects/example.md#decision")["text"])
    with pytest.raises(Error, match="brain-qualified"):
        read(select(), "projects/example.md")
    assert "second durable answer" in str(read(select(), "projects/example.md", "second")["text"])
    with pytest.raises(Error, match="unknown brain third"):
        read(select(), "bf://third/projects/example.md")
    cli = CliRunner().invoke(app, ["search", "durable"])
    assert cli.exit_code == 0, cli.output
    assert {item["brain"] for item in json.loads(cli.stdout)["items"]} == {"first", "second"}


def test_missing_and_mismatched_references_keep_available_answers(tmp_path: Path) -> None:
    first = make(tmp_path / "first", "first", {"missing": "../absent", "wrong": "../second"})
    make(tmp_path / "second", "second")
    found = cast(dict, search([first], Query(text="durable")))
    assert {item["brain"] for item in found["items"]} == {"first"}
    assert {problem["reference"] for problem in found["problems"]} == {"missing", "wrong"}
    assert str(tmp_path) not in json.dumps(found)
    assert read([first], "projects/example.md")["problems"] == found["problems"]
    with pytest.raises(Error, match="unknown brain wrong"):
        read([first], "bf://wrong/projects/example.md")
    first.write(
        "evals/retrieval.yaml",
        b"version: 4\ncases:\n- name: incomplete\n  query: durable\n  expect: [projects/example.md]\n",
    )
    assert not evaluate(first)["passed"]


def test_conflicting_names_are_excluded_and_paths_deduplicated(tmp_path: Path) -> None:
    first = make(tmp_path / "first", "first", {"shared": "../shared"})
    second = make(tmp_path / "second", "second", {"shared": "../impostor"})
    shared = make(tmp_path / "shared", "shared")
    make(tmp_path / "impostor", "shared")
    found = cast(dict, search([first, second], Query(text="durable")))
    assert {item["brain"] for item in found["items"]} == {"first", "second"}
    assert "ambiguous brain name" in found["problems"][0]["error"]
    with pytest.raises(Error, match="unknown brain shared"):
        read([first, second], "bf://shared/projects/example.md")
    stores, problems = related([first, shared, first])
    assert {store.root for store in stores} == {first.root, shared.root}
    assert not problems


def test_absolute_and_home_paths_and_no_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    first = make(tmp_path / "first", "first", {"second": "~/second", "third": str(tmp_path / "third")})
    second = make(tmp_path / "second", "second")
    make(tmp_path / "third", "third")
    assert len(cast(dict, search([first], Query(text="durable")))["items"]) == 3
    assert not may_collect(second)
    monkeypatch.chdir(first.root)
    # Mutation services select only roots, regardless of configured references.
    report = cast(dict, update(select(), dry_run=True))
    assert len(report["brains"]) == 1
    assert report["brains"][0]["brain"] == "first"


def test_init_never_writes_global_config_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "new"
    result = CliRunner().invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.output
    assert not user_path().exists()
    monkeypatch.chdir(target)
    assert search(select(), Query(text="welcome"))["items"]
    assert "direct references only" in (target / "AGENTS.md").read_text()


@pytest.mark.parametrize(
    "references",
    [
        {"first": {"path": "../first"}},
        {"second": {"path": ""}},
        {"second": {"path": " \n"}},
        {"second": {"path": "../second", "collect": True}},
        {"Upper": {"path": "../second"}},
        {f"brain-{i}": {"path": f"../{i}"} for i in range(33)},
    ],
)
def test_reference_schema_rejects_invalid_declarations(tmp_path: Path, references: dict) -> None:
    store = make(tmp_path / "first", "first")
    store.write("bf.yaml", yaml.safe_dump({"version": 4, "name": "first", "brains": references}).encode())
    with pytest.raises(Error, match=r"invalid bf\.yaml"):
        load(store)


def test_mcp_and_evaluations_share_local_scope(tmp_path: Path) -> None:
    import asyncio

    first = make(tmp_path / "first", "first", {"second": "../second"})
    second = make(tmp_path / "second", "second")
    first.write(
        "evals/retrieval.yaml",
        b"version: 4\ncases:\n- name: cross-brain\n  query: durable\n  expect: [bf://second/projects/example.md]\n",
    )
    assert evaluate(first)["passed"]
    mcp = server([first])

    async def check() -> None:
        result = await mcp.call_tool("search", {"query": "durable"})
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None
        assert {item["brain"] for item in result.structured_content["items"]} == {"first", "second"}
        result = await mcp.call_tool("read", {"ref": "bf://second/projects/example.md#decision"})
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None
        assert "second durable answer" in result.structured_content["text"]
        # A long-running host rereads direct declarations at request time.
        second.write("bf.yaml", b"version: 4\nname: changed\n")
        result = await mcp.call_tool("search", {"query": "durable"})
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None
        assert result.structured_content["problems"]

    asyncio.run(check())
