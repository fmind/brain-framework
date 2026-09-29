"""Explicit tag membership stays local, pageable and distinct from words and graph links."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from bf import index, links, pages
from bf.models import Error, Query
from bf.retrieve import read, search
from bf.storage import Store
from bf.validate import validate


def items(reply: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", reply["items"])


def test_tags_distinguish_membership_from_mentions_and_links(brain: Store) -> None:
    brain.write("projects/mention.md", b"---\ntype: project\n---\n# Retention\n\nKeep evidence without this tag.\n")
    brain.write(
        "concepts/tag-guide.md",
        b"---\ntype: concept\n---\n# Retention guide\n\n[Retention](bf://fixture/tags/retention) evidence.\n",
    )
    tag = "bf://fixture/tags/retention"
    assert items(read([brain], "tags")) == [{"tag": "retention", "ref": tag, "total": 1}]
    assert {item["ref"] for item in items(read([brain], tag))} == {"projects/offline.md"}
    assert read([brain], "tags/retention") == read([brain], tag)
    for query in (Query(text=tag), Query(text="evidence", **pages.scope(tag))):
        reply = search([brain], query)
        assert len(items(reply)) == 1
        assert str(items(reply)[0]["ref"]).startswith("projects/offline.md")
        relations = cast("list[dict[str, object]]", items(reply)[0]["relations"])
        assert relations[0]["relation"] == "tagged-with"
    assert validate(brain)["valid"]
    claims = cast("list[dict[str, object]]", read([brain], "projects/offline.md")["claims"])
    # A claim carries the date of the note asserting it.
    assert {
        "time": "2026-09-01T00:00:00.000000Z",
        "date": "2026-09-01",
        "subject": "bf://fixture/projects/offline.md",
        "relation": "tagged-with",
        "target": tag,
        "origin": "bf://fixture/projects/offline.md",
    } in claims
    brain.write("projects/absent.md", b"# Unknown tag absent\n")
    assert not items(search([brain], Query(text="bf://fixture/tags/absent")))
    assert read([brain], "tags/absent")["total"] == 0


def test_tag_counts_refresh_deduplicate_and_rebuild(brain: Store) -> None:
    brain.write("concepts/tagged.md", b"---\ntype: concept\ntags: [retention, retention, evidence]\n---\n# Tagged\n")
    assert {item["tag"]: item["total"] for item in items(read([brain], "tags"))} == {"evidence": 1, "retention": 2}
    brain.write("concepts/tagged.md", b"---\ntype: concept\ntags: [evidence]\n---\n# Changed\n")
    assert read([brain], "tags/retention")["total"] == 1
    brain.delete("concepts/tagged.md")
    assert read([brain], "tags/evidence")["total"] == 0
    index.refresh(brain, full=True)
    assert read([brain], "tags/retention")["total"] == 1


def test_tag_pages_paginate_without_merging_brain_identities(brain: Store, tmp_path: Path) -> None:
    (tmp_path / "team").mkdir()
    other = Store(tmp_path / "team")
    other.write("bf.yaml", b"version: 7\nname: team\n")
    for store in (brain, other):
        for n in range(110):
            store.write(f"projects/tagged-{n:03}.md", f"---\ntags: [retention, tag-{n:03}]\n---\n# Evidence\n".encode())
    selection = [brain, other]
    directory = read(selection, "tags")
    assert len(items(directory)) == 200
    remaining = read(selection, "tags", offset=cast("int", directory["next_offset"]))
    assert len(items(remaining)) == 22
    assert len({i["uri"] for i in items(directory) + items(remaining)}) == 222
    members = read(selection, "tags/retention")
    assert members["total"] == 221
    rest = read(selection, "tags/retention", offset=cast("int", members["next_offset"]))
    assert len({(i["brain"], i["ref"]) for i in items(members) + items(rest)}) == 221
    for name, total in (("fixture", 111), ("team", 110)):
        tag = f"bf://{name}/tags/retention"
        assert read(selection, tag)["total"] == total
        result = search(selection, Query(text="evidence", limit=50, **pages.scope(tag)))
        assert {i["brain"] for i in items(result)} == {name}
        assert len(items(result)) == 50
        assert result["next_offset"] == 50


@pytest.mark.parametrize("tag", ["C#", "machine learning", "évidence", "x%_?", "2026", "Security", "security"])
def test_tag_addresses_preserve_literal_labels(brain: Store, tag: str) -> None:
    brain.write("projects/literal.md", f"---\ntags: [{json.dumps(tag)}]\n---\n# Literal\n".encode())
    address = links.address("fixture", f"tags/{tag}")
    assert read([brain], address)["total"] == 1
    assert len(items(search([brain], Query(text="literal", **pages.scope(address))))) == 1


@pytest.mark.parametrize("tag", ["", " padded ", ".", "..", "a/b", "a\\b", "a\nb", "x" * 129])
def test_invalid_tags_fail_validation_without_leaking_content(brain: Store, tag: str) -> None:
    brain.write("projects/bad.md", f"---\ntags: [{json.dumps(tag)}]\n---\n# Bad\n".encode())
    assert not validate(brain)["valid"]
    reply = read([brain], "tags")
    assert reply["problems"]
    assert len(items(reply)) == 1


def test_tag_pages_preserve_incomplete_evidence_signals(brain: Store) -> None:
    brain.write("projects/bad.md", b"---\nstale_after: typo\n---\n# Missing\n")
    for ref in ("tags", "tags/absent"):
        assert read([brain], ref)["problems"]
    assert search([brain], Query(text="needle", **pages.scope("bf://fixture/tags/absent")))["problems"]
    for ref in (
        "bf://fixture/tags/retention#section",
        "bf://fixture/tags/a/b",
        "bf://fixture/tags/retention?rel=depends-on#section",
        "bf://fixture/tags/a/b?rel=depends-on",
    ):
        with pytest.raises(Error, match="tag address"):
            read([brain], ref)
        with pytest.raises(Error, match="tag address"):
            pages.scope(ref)
    with pytest.raises(Error, match="tag address"):
        pages.scope("bf://fixture/tags/retention?rel=depends-on")


def test_tag_addresses_cannot_be_claimed_as_note_identities(brain: Store) -> None:
    brain.write("concepts/owner.md", b"---\ntype: concept\naliases: [bf://fixture/tags/retention]\n---\n# Owner\n")
    assert not validate(brain)["valid"]
    assert read([brain], "tags/retention")["total"] == 1
