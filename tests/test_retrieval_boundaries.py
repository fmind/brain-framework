"""Public retrieval remains navigable, lossless and explicit about unavailable evidence."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from mcp.types import CallToolResult
from typer.testing import CliRunner

from bf import graph, health, index, retrieve
from bf.cli import app
from bf.collect import collect
from bf.config import user_path
from bf.evaluate import evaluate
from bf.mcp import server
from bf.models import Error, NotFoundError, Query, Record, digest, encode
from bf.storage import Store
from bf.validate import validate
from conftest import records_file


def corpus(brain: Store, tmp_path: Path) -> list[Store]:
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 6\nname: team\n")
    for n, store in enumerate([brain, team]):
        records_file(
            store,
            "mail",
            [
                Record(
                    id=f"{i:03}",
                    title="Shared incident",
                    text="Common evidence",
                    time="2026-09-26T09:00:00Z",
                    links=["topic:incident"],
                )
                for i in range(n, 123, 2)
            ],
        )
    return [brain, team]


@pytest.mark.parametrize("ref", ["2026-09-26", "memories/mail", "memories/mail/2026-09"])
def test_pages_visit_every_record_across_brains(brain: Store, tmp_path: Path, ref: str) -> None:
    stores = corpus(brain, tmp_path)
    offset = 0
    refs: list[str] = []
    while True:
        reply = retrieve.read(stores, ref, offset=offset)
        assert reply["total"] == 123
        items = cast("list[dict]", reply["items"])
        assert len(items) <= 50
        refs.extend(item["uri"] for item in items)
        if "next_offset" not in reply:
            break
        offset = cast("int", reply["next_offset"])
    assert len(refs) == len(set(refs)) == 123
    assert [ref.rsplit(":", 1)[1] for ref in refs] == [f"{i:03}" for i in reversed(range(123))]
    assert retrieve.read(stores, ref, offset=2**63 - 1)["items"] == []


@pytest.mark.parametrize("query", ["incident", "topic:incident"])
def test_search_continues_lexical_and_identity_results(brain: Store, tmp_path: Path, query: str) -> None:
    stores = corpus(brain, tmp_path)
    offset = 0
    refs: list[str] = []
    while True:
        reply = retrieve.search(stores, Query(text=query, limit=7, offset=offset))
        items = cast("list[dict]", reply["items"])
        assert len(items) <= 7
        assert all("_rank" not in item for item in items)
        refs.extend(item["uri"] for item in items)
        assert "more" not in reply
        if "next_offset" not in reply:
            break
        offset = cast("int", reply["next_offset"])
    assert len(refs) == len(set(refs)) == 123
    assert retrieve.search(stores, Query(text=query, offset=2**63 - 1))["items"] == []


def test_authored_folder_continuation(brain: Store) -> None:
    for n in range(215):
        brain.write(f"concepts/page-{n:03}.md", f"# Page {n:03}\n".encode())
    first = retrieve.read([brain], "concepts")
    second = retrieve.read([brain], "concepts", offset=cast("int", first["next_offset"]))
    items = cast("list[dict]", first["items"]) + cast("list[dict]", second["items"])
    assert len(items) == first["total"] == 216
    assert len({item["ref"] for item in items}) == 216
    assert "next_offset" not in second


def test_empty_search_describes_failed_and_never_collected_sources(brain: Store) -> None:
    brain.write("bf.yaml", b'version: 6\nname: fixture\nsensors:\n  mail:\n    command: ["fake"]\n    refresh: 3600\n')

    def failed(*_args: object) -> bytes:
        raise Error("synthetic failure")

    with pytest.raises(Error):
        collect(brain, "mail", start="2026-09-25T00:00:00Z", end="2026-09-26T00:00:00Z", runner=failed)
    reply = retrieve.search([brain], Query(text="absentunique"))
    assert reply["items"] == []
    coverage = {source["source"]: source for source in cast("list[dict]", reply["sources"])}
    assert coverage["mail"]["failed"] is True
    assert coverage["mail"]["freshness"] == "never"
    # Authored-only searches do not imply that a provider's records were searched.
    assert "sources" not in retrieve.search([brain], Query(text="absentunique", prefix="projects"))


@pytest.mark.parametrize("name", ["shared", "shared.bin"])
def test_skipped_directories_are_visible_without_following_them(brain: Store, tmp_path: Path, name: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("# Private\nNeverfollowthis\n")
    (brain.root / "projects" / name).symlink_to(outside, target_is_directory=True)
    reply = retrieve.search([brain], Query(text="Neverfollowthis"))
    assert reply["items"] == []
    assert f"projects/{name}" in str(reply["problems"])
    assert str(outside) not in str(reply)
    assert not validate(brain)["valid"]


def test_oversized_record_reassembles_exact_json(brain: Store) -> None:
    record = Record(id="large", title="Unicode evidence", text='é "\\\n' * 40000)
    records_file(brain, "docs", [record])
    chunks: list[str] = []
    offset = 0
    hashes = set()
    while True:
        reply = retrieve.read([brain], "bf://fixture/docs:large", offset=offset)
        assert reply["format"] == "json"
        assert "external" not in reply
        assert len(encode(reply)) <= retrieve.MAX_REPLY
        assert reply["offset"] == offset
        assert len(cast("str", reply["chunk"])) == retrieve.CHUNK or "next_offset" not in reply
        hashes.add(reply["sha256"])
        chunks.append(cast("str", reply["chunk"]))
        if "next_offset" not in reply:
            break
        offset = cast("int", reply["next_offset"])
    raw = "".join(chunks)
    assert hashes == {digest(raw.encode())}
    assert json.loads(raw)["record"] == record.model_dump(exclude_defaults=True)
    with pytest.raises(Error, match="beyond"):
        retrieve.read([brain], "docs:large", offset=len(raw) + 1000)


def test_invalid_optional_registry_does_not_affect_explicit_reads(brain: Store) -> None:
    user_path().write_text("brains: [broken]\n")
    reply = retrieve.read([brain])
    assert cast("list[dict]", reply["projects"])[0]["ref"] == "projects/offline.md"
    assert reply["attention"] == []
    assert "problems" not in reply


def test_cli_and_mcp_accept_continuations(brain: Store, tmp_path: Path) -> None:
    corpus(brain, tmp_path)
    arguments = ["search", "incident", "--brain", str(brain.root), "--limit", "2"]
    first = CliRunner().invoke(app, arguments)
    assert first.exit_code == 0, first.output
    reply = json.loads(first.stdout)
    second = CliRunner().invoke(app, [*arguments, "--offset", str(reply["next_offset"])])
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["items"][0]["ref"] != reply["items"][0]["ref"]
    page = CliRunner().invoke(app, ["read", "memories/mail", "--brain", str(brain.root), "--offset", "20"])
    assert page.exit_code == 0, page.output
    assert json.loads(page.stdout)["next_offset"] == 40
    mcp = server([brain])

    async def check() -> None:
        result = await mcp.call_tool("search", {"query": "incident", "limit": 2, "offset": reply["next_offset"]})
        assert isinstance(result, CallToolResult)
        assert result.structured_content == json.loads(second.stdout)
        result = await mcp.call_tool("read", {"ref": "memories/mail", "offset": 20})
        assert isinstance(result, CallToolResult)
        assert result.structured_content == json.loads(page.stdout)

    asyncio.run(check())


@pytest.mark.parametrize("ref", ["", "memories", "repo:example/project", "projects/offline.md#decision"])
def test_summary_pages_do_not_silently_ignore_offsets(brain: Store, ref: str) -> None:
    with pytest.raises(Error, match="offset"):
        retrieve.read([brain], ref, offset=1)


@pytest.mark.parametrize("ref", ["topic:missing", "projects/absent.md", "gmail:absent"])
def test_missing_refs_are_not_found_whatever_the_offset(brain: Store, ref: str) -> None:
    with pytest.raises(NotFoundError):
        retrieve.read([brain], ref, offset=5)


def test_read_rejects_invalid_service_offsets(brain: Store) -> None:
    with pytest.raises(Error, match="offset"):
        retrieve.read([brain], "projects", offset=-1)


def test_evaluation_assembles_chunked_exact_reads_before_checking_them(brain: Store) -> None:
    records_file(brain, "docs", [Record(id="large", title="Large", text="evidence " * 20000 + "closing clause")])
    brain.write(
        "evals/large.yaml",
        b"version: 5\ncases:\n- name: large\n  read: docs:large\n  expect: [docs:large]\n  text: [closing clause]\n",
    )
    assert "chunk" in retrieve.read([brain], "docs:large")
    reply = evaluate(brain)
    assert reply["passed"] is True, reply["cases"]


def test_evaluation_rejects_a_chunked_read_that_changes_between_chunks(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    records_file(brain, "docs", [Record(id="large", title="Large", text="evidence " * 20000)])
    brain.write("evals/large.yaml", b"version: 5\ncases:\n- name: large\n  read: docs:large\n  expect: [docs:large]\n")
    original = retrieve.read

    def changing(stores: list[Store], ref: str = "", *, offset: int = 0, counted: bool = True) -> dict[str, object]:
        reply = original(stores, ref, offset=offset, counted=counted)
        return {**reply, "sha256": "0" * 64} if offset else reply

    monkeypatch.setattr("bf.evaluate.read", changing)
    reply = evaluate(brain)
    assert reply["passed"] is False
    assert "changed during evaluation" in str(reply["cases"])


@pytest.mark.parametrize("path", ["concepts", "memories/meetings", "memories"])
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_linked_evidence_roots_preserve_other_retrieval(brain: Store, tmp_path: Path, path: str, kind: str) -> None:
    outside = tmp_path / "outside"
    (brain.root / path).rename(outside)
    if kind == "symlink":
        (brain.root / path).symlink_to(outside, target_is_directory=True)
    else:
        os.mkfifo(brain.root / path)
    reply = retrieve.search([brain], Query(text="retention"))
    assert any(item["ref"].startswith("projects/offline.md") for item in cast("list[dict]", reply["items"]))
    assert path in str(reply["problems"])
    assert str(outside) not in str(reply)
    assert validate(brain)["valid"] is False
    if path.startswith("memories"):
        # A linked memories root is named: it cannot prove the record absent.
        with pytest.raises(Error, match=r"^memories: expected a directory|unreadable|incomplete"):
            retrieve.read([brain], "meetings:missing")


def test_linked_transaction_directory_still_blocks_reads(brain: Store, tmp_path: Path) -> None:
    outside = tmp_path / "transaction"
    outside.mkdir()
    (brain.root / "memories/.pending").symlink_to(outside, target_is_directory=True)
    with pytest.raises(Error, match=r"^memories/\.pending: expected a directory; symlinks"):
        retrieve.search([brain], Query(text="retention"))


def test_claim_preview_reports_truncation(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 6\nname: fixture\nschema:\n  depends-on:\n    description: Needs context.\n"
        b"    type: identity\n    cardinality: many\n    relation: true\n",
    )
    links = "\n".join(f"[Evidence {n}](bf://fixture/concepts/target-{n}?rel=depends-on)" for n in range(51))
    brain.write("projects/many.md", f"# Many claims\n\n{links}\n".encode())
    reply = retrieve.read([brain], "projects/many.md")
    assert len(cast("list", reply["claims"])) == 50
    assert reply["claims_truncated"] is True


def test_every_chunk_of_a_mid_sized_exact_read_is_reachable_through_cli_and_mcp(brain: Store) -> None:
    brain.write("projects/mid.md", b"# Mid\n\n" + b"evidence " * 22_000)
    whole = retrieve.read([brain], "projects/mid.md", counted=False)
    assert whole["format"] == "json"
    assert (whole["offset"], whole["next_offset"]) == (0, retrieve.CHUNK)

    def assemble(read: Callable[[int], dict]) -> str:
        chunks, offset = [], 0
        while True:
            reply = read(offset)
            chunks.append(reply["chunk"])
            if "next_offset" not in reply:
                return "".join(chunks)
            offset = reply["next_offset"]

    def cli(offset: int) -> dict:
        arguments = ["read", "projects/mid.md", "--brain", str(brain.root), "--offset", str(offset)]
        result = CliRunner().invoke(app, arguments)
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    mcp = server([brain])

    def tool(offset: int) -> dict:
        result = asyncio.run(mcp.call_tool("read", {"ref": "projects/mid.md", "offset": offset}))
        assert isinstance(result, CallToolResult)
        return cast("dict", result.structured_content)

    for raw in (assemble(cli), assemble(tool)):
        assert digest(raw.encode()) == whole["sha256"]
        assert json.loads(raw)["text"].startswith("# Mid")
    with pytest.raises(Error, match="not chunked"):
        retrieve.read([brain], "projects/offline.md", offset=1)


def test_typed_links_read_their_unowned_target_identity(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 6\nname: fixture\nschema:\n  owner:\n    description: Owner.\n    type: identity\n    relation: true\n",
    )
    brain.write("projects/a.md", b"---\ntype: project\n---\n# A\n\n[Alice](bf://fixture/people/alice?rel=owner)\n")
    plain = retrieve.read([brain], "bf://fixture/people/alice")
    assert cast("list[dict]", plain["backlinks"])[0]["relation"] == "owner"
    assert retrieve.read([brain], "bf://fixture/people/alice?rel=owner") == plain
    with pytest.raises(Error, match="offset applies"):
        retrieve.read([brain], "bf://fixture/people/alice", offset=1)
    with pytest.raises(NotFoundError):
        retrieve.read([brain], "bf://fixture/people/bob?rel=owner")


def test_identity_problems_name_the_configured_brain(
    brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "team-folder"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 6\nname: team\n")
    original = index.database
    opened: list[Path] = []

    def unavailable_later(store: Store) -> contextlib.AbstractContextManager[tuple[sqlite3.Connection, str]]:
        opened.append(store.root)
        # The second visit to the team brain checks shared aliases, after the owner was resolved.
        if opened.count(root) > 1:
            raise Error("simulated unavailable cache")
        return original(store)

    monkeypatch.setattr(index, "database", unavailable_later)
    _targets, problems = graph.expand([brain, team], "repo:example/project")
    assert {"brain": "team", "error": "identity cache unavailable"} in problems


# Process and network events that retrieval must never cause. Audit hooks cannot be removed, so a flag gates them.
_EXECUTION = {
    "subprocess.Popen",
    "os.posix_spawn",
    "os.fork",
    "os.exec",
    "os.system",
    "socket.connect",
    "socket.getaddrinfo",
}
_WATCHING: list[set[str]] = []


def _audit(event: str, _arguments: tuple[object, ...]) -> None:
    if _WATCHING and event in _EXECUTION:
        _WATCHING[-1].add(event)


sys.addaudithook(_audit)


def test_retrieval_never_starts_programs_or_opens_connections(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 6\nname: fixture\nsensors:\n  mail:\n    command: [sensors/marker.sh]\n    refresh: 60\n"
        b"routines:\n  digest:\n    command: [routines/marker.sh]\n    refresh: 60\n",
    )
    marker = brain.root / "ran"
    for folder in ("sensors", "routines"):
        script = brain.root / folder / "marker.sh"
        script.parent.mkdir()
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\necho '[]'\n")
        script.chmod(0o700)
    brain.write(
        "evals/retrieval.yaml",
        b"version: 5\ncases:\n- name: decision\n  query: retention\n  expect: [projects/offline.md]\n",
    )
    mcp = server([brain])
    events: set[str] = set()
    _WATCHING.append(events)
    try:
        retrieve.search([brain], Query(text="offline"))
        for ref in ("", "today", "tasks", "memories", "memories/mail", "projects", "meetings:decision-1"):
            retrieve.read([brain], ref)
        retrieve.read([brain], "projects/offline.md")
        health.source_health(brain)
        health.report([brain])
        assert validate(brain)["valid"]
        assert evaluate(brain)["score"] == "1/1"
        # Due, never-collected programs fail the status check without running.
        for arguments, code in ((["search", "offline"], 0), (["read", "today"], 0), (["status", "--check"], 1)):
            result = CliRunner().invoke(app, [*arguments, "--brain", str(brain.root)])
            assert result.exit_code == code, result.output
        for tool, arguments in (("search", {"query": "offline"}), ("read", {"ref": "projects/offline.md"})):
            reply = asyncio.run(mcp.call_tool(tool, arguments))
            assert isinstance(reply, CallToolResult)
            assert not reply.is_error, reply.content
    finally:
        _WATCHING.pop()
    assert events == set()
    assert not marker.exists()
