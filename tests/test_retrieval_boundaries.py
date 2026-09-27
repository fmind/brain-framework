"""Public retrieval remains navigable, lossless and explicit about unavailable evidence."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import cast

import pytest
from mcp.types import CallToolResult
from typer.testing import CliRunner

from bf import retrieve
from bf.cli import app
from bf.collect import collect
from bf.config import user_path
from bf.evaluate import evaluate
from bf.mcp import server
from bf.models import Error, Query, Record, digest, encode
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
            "2026-09",
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
        if "next_offset" not in reply:
            assert "more" not in reply
            break
        assert reply["more"] is True
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


def test_oversized_record_reassembles_exact_json(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieve, "MAX_REPLY", 128 << 10)
    record = Record(id="large", title="Unicode evidence", text='é "\\\n' * 40000)
    records_file(brain, "docs", "undated", [record])
    chunks: list[str] = []
    offset = 0
    hashes = set()
    while True:
        reply = retrieve.read([brain], "bf://fixture/docs:large", offset=offset)
        assert reply["format"] == "json"
        assert "external" not in reply
        assert len(encode(reply)) <= retrieve.MAX_REPLY
        assert reply["offset"] == offset
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


@pytest.mark.parametrize("ref", ["", "memories", "topic:missing"])
def test_summary_pages_do_not_silently_ignore_offsets(brain: Store, ref: str) -> None:
    with pytest.raises(Error, match="offset"):
        retrieve.read([brain], ref, offset=1)


def test_read_rejects_invalid_service_offsets(brain: Store) -> None:
    with pytest.raises(Error, match="offset"):
        retrieve.read([brain], "projects", offset=-1)


def test_evaluation_never_accepts_one_chunk_as_a_complete_read(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieve, "MAX_REPLY", 128 << 10)
    records_file(brain, "docs", "undated", [Record(id="large", title="Large", text="evidence " * 20000)])
    brain.write("evals/large.yaml", b"version: 5\ncases:\n- name: large\n  read: docs:large\n  expect: [docs:large]\n")
    reply = evaluate(brain)
    assert reply["passed"] is False
    assert "chunk assembly" in str(reply["cases"])


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
        with pytest.raises(Error, match=r"unreadable|incomplete"):
            retrieve.read([brain], "meetings:missing")


def test_linked_transaction_directory_still_blocks_reads(brain: Store, tmp_path: Path) -> None:
    outside = tmp_path / "transaction"
    outside.mkdir()
    (brain.root / "memories/.pending").symlink_to(outside, target_is_directory=True)
    with pytest.raises((Error, OSError)):
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
