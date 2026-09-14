"""Current decisions, exact provenance, separate bases and explicit source structure."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from typer.testing import CliRunner

from fkf.cli import app
from fkf.config import load
from fkf.index import CACHE, build
from fkf.markdown import note
from fkf.mcp import server
from fkf.models import Collection, Error, Query, Record, encode, qualify
from fkf.retrieve import context as service_context
from fkf.retrieve import find as service_find
from fkf.retrieve import read as service_read
from fkf.storage import Store


def context(store: Store, query: Query, budget: int = 850):
    return json.loads(encode(service_context(store, query, budget)))


def find(store: Store, query: Query):
    return json.loads(encode(service_find(store, query)))


def read(store: Store, uri: str):
    return json.loads(encode(service_read(store, uri)))


def test_flat_initialization_and_portable_base_identity(tmp_path: Path) -> None:
    first, second, copied = (tmp_path / name for name in ("first", "second", "copied"))
    runner = CliRunner()
    for path in (first, second):
        result = runner.invoke(app, ["init", str(path), "--name", "same-name"])
        assert result.exit_code == 0, result.output
        assert all((path / name).is_dir() for name in ("projects", "tasks", "wiki", "sources", "scripts", "records"))
        assert not (path / "notes").exists()
    origin, other = Store(first), Store(second)
    identity = load(origin).id
    assert load(other).id != identity
    reference = read(origin, "wiki/welcome.md")["ref"]
    shutil.copytree(first, copied)
    assert read(Store(copied), reference)["base"]["id"] == identity
    for operation in (
        lambda: read(other, reference),
        lambda: context(other, Query(text=reference)),
        lambda: find(other, Query(text="*", within=reference)),
    ):
        with pytest.raises(Error, match="another base"):
            operation()


@pytest.mark.parametrize("state", ["missing", "ready", "stale", "corrupt"])
def test_sections_deliver_current_decisions_and_explicit_history(base: Store, state: str) -> None:
    base.write(
        "projects/retention.md",
        b"""---
type: decision
status: accepted
reviewed: 2026-09-12
effective: 2026-09-10
---
# Retention

## Decision

Zirconium: preserve original evidence because upstream content can disappear.

## History

Zirconium: purge originals. This policy is superseded.

### Earlier trial

Zirconium obsolete trial.
""",
    )
    if state != "missing":
        build(base)
    if state == "stale":
        base.write("wiki/change.md", b"# Unrelated")
    if state == "corrupt":
        base.write(CACHE, b"broken")
    result = context(base, Query(text="zirconium", type="decision", status="accepted"))
    assert result["index"] == state
    item = result["items"][0]
    assert item["fragment"] == "decision"
    assert "preserve original evidence" in item["excerpt"]
    assert "purge originals" not in item["excerpt"]
    assert item["reviewed"] == "2026-09-12"
    assert "preserve original evidence" in read(base, item["ref"])["text"]
    assert not find(base, Query(text="obsolete", type="decision"))["items"]
    old = find(base, Query(text="obsolete", type="decision", history=True))["items"][0]
    assert old["status"] == "superseded"
    assert "obsolete trial" in read(base, old["ref"])["text"]


def test_accepted_supersession_changes_retrieval_but_preserves_exact_reads(base: Store) -> None:
    base.write("wiki/old.md", b"---\nstatus: accepted\naliases: [decision:old]\n---\n# Old\n\nXenon old policy.")
    base.write("wiki/new.md", b"---\nstatus: proposed\nsupersedes: [decision:old]\n---\n# New\n\nXenon new policy.")
    assert "wiki/old.md" in {x["uri"] for x in find(base, Query(text="xenon"))["items"]}
    base.write("wiki/new.md", base.read("wiki/new.md").replace(b"status: proposed", b"status: accepted"))
    result = find(base, Query(text="xenon"))
    assert [x["uri"] for x in result["items"]] == ["wiki/new.md"]
    assert "old policy" in read(base, "decision:old")["text"]
    old = find(base, Query(text="xenon", status="superseded"))["items"]
    assert [x["uri"] for x in old] == ["wiki/old.md"]


@pytest.mark.parametrize("conflict", ["missing", "ambiguous", "self", "cycle"])
def test_conflicting_accepted_decisions_fail_without_replacing_evidence(base: Store, conflict: str) -> None:
    base.write("wiki/old.md", b"---\nstatus: accepted\naliases: [decision:old]\n---\n# Old\n\nKeep the evidence.")
    build(base)
    original = base.read("wiki/old.md")
    target = "decision:missing" if conflict == "missing" else "decision:old"
    if conflict == "ambiguous":
        base.write("wiki/other.md", b"---\naliases: [decision:old]\n---\n# Other")
    if conflict == "self":
        target = "decision:new"
    if conflict == "cycle":
        base.write(
            "wiki/old.md", original.replace(b"status: accepted", b"status: accepted\nsupersedes: [decision:new]")
        )
    base.write(
        "wiki/new.md",
        f"---\nstatus: accepted\naliases: [decision:new]\nsupersedes: [{target}]\n---\n# New".encode(),
    )
    before = base.read("wiki/old.md")
    for operation in (lambda: build(base), lambda: find(base, Query(text="evidence"))):
        with pytest.raises(Error, match="supersession"):
            operation()
    assert base.read("wiki/old.md") == before


@pytest.mark.parametrize(
    "metadata",
    ["status: invented", "reviewed: 2026-02-30", "effective: yesterday", "supersedes: [42]", "type: [decision]"],
)
def test_known_metadata_is_strict_but_private_values_stay_out_of_errors(metadata: str) -> None:
    with pytest.raises(Error, match="invalid knowledge metadata") as caught:
        note("wiki/invalid.md", f"---\n{metadata}\n---\n# Note".encode())
    assert metadata not in str(caught.value)


def structure(base: Store, *, day: int, parent: str) -> None:
    capture = Collection(
        source="files",
        captured=f"2026-09-{day:02d}T00:00:00Z",
        records=[
            Record(id="root", kind="container", title="Work", aliases=["folder:root"]),
            Record(id="child", kind="container", title="Research", parents=["folder:root"], aliases=["folder:child"]),
            Record(id="other", kind="container", title="Research", aliases=["folder:other"]),
            Record(
                id="doc", title="Zirconium decision", text="Keep exact evidence.", parents=[parent], aliases=["doc:one"]
            ),
            Record(id="noise", title="Mentions folder:root", text="Prose is not a membership edge."),
        ],
    )
    base.write(f"records/files/{day}.json", encode(capture.model_dump()))


@pytest.mark.parametrize("indexed", [False, True])
def test_container_membership_follows_explicit_links_and_current_observations(base: Store, indexed: bool) -> None:
    structure(base, day=1, parent="folder:child")
    if indexed:
        build(base)
    result = find(base, Query(text="*", within="folder:root"))
    assert {item["title"] for item in result["items"]} == {"Research", "Zirconium decision"}
    assert "Keep exact evidence" in context(base, Query(text="zirconium", within="folder:root"))["items"][0]["excerpt"]
    exact = read(base, "doc:one")["ref"]
    structure(base, day=2, parent="folder:other")
    assert not find(base, Query(text="zirconium", within="folder:root"))["items"]
    assert find(base, Query(text="zirconium", within="folder:other"))["items"]
    assert read(base, exact)["record"]["parents"] == ["folder:child"]
    build(base)
    exported = json.loads(base.read("indexes/structures.json"))
    doc = next(item for item in exported["items"] if item["title"] == "Zirconium decision")
    assert doc["parents"] == ["folder:other"]
    assert read(base, doc["ref"])["record"]["text"] == "Keep exact evidence."
    assert exported["base"]["id"] == load(base).id


def test_filters_and_wrong_base_are_shared_by_cli_and_mcp(base: Store) -> None:
    base.write("projects/current.md", b"---\nstatus: accepted\n---\n# Xenon\n\nUseful decision.")
    response = CliRunner().invoke(
        app, ["context", "xenon", "--base", str(base.root), "--type", "project", "--status", "accepted"]
    )
    assert response.exit_code == 0, response.output
    assert json.loads(response.stdout)["items"][0]["uri"] == "projects/current.md"

    async def check() -> None:
        result = await server(base).call_tool(
            "context", {"query": "xenon", "note_type": "project", "status": "accepted"}
        )
        assert isinstance(result, CallToolResult)
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["items"][0]["uri"] == "projects/current.md"
        denied = await server(base).call_tool("read", {"uri": qualify("f" * 32, "wiki/project.md")})
        assert isinstance(denied, CallToolResult)
        assert denied.is_error

    asyncio.run(check())


def test_empty_complete_snapshot_removes_missing_items_without_losing_history(base: Store) -> None:
    structure(base, day=1, parent="folder:child")
    exact = read(base, "doc:one")["ref"]
    empty = Collection(source="files", captured="2026-09-02T00:00:00Z", mode="snapshot", records=[])
    base.write("records/files/empty.json", encode(empty.model_dump()))
    for indexed in (False, True):
        if indexed:
            build(base)
        assert not find(base, Query(text="zirconium", source="files"))["items"]
        assert find(base, Query(text="zirconium", source="files", history=True))["items"]
        assert read(base, exact)["snapshot"] == "historical"
