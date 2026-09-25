"""Retrieval stays correct at the edges: extreme dates, broken neighbors, shared aliases and odd names."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from bf import index, pages
from bf.markdown import note
from bf.models import Error, Knowledge, Query, Record
from bf.retrieve import read, search
from bf.storage import Store
from bf.validate import validate
from conftest import records_file


def refs(reply: dict[str, object], key: str = "items") -> list[str]:
    return [str(item["ref"]) for item in cast("list[dict[str, object]]", reply[key])]


def make(root: Path, name: str, files: dict[str, str]) -> Store:
    root.mkdir()
    store = Store(root)
    store.write("bf.yaml", f"version: 5\nname: {name}\n".encode())
    for path, text in files.items():
        store.write(path, text.encode())
    return store


def test_calendar_edge_dates_never_break_retrieval(brain: Store) -> None:
    for value in ("0001-01-01", "9999-12-31"):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            Knowledge.model_validate({"updated": value})
    brain.write("projects/far.md", b"---\nupdated: 9999-12-30\n---\n# Far\n\nretention plan\n")
    assert "projects/far.md" in refs(search([brain], Query(text="retention plan")))
    assert "projects/far.md" in refs(read([brain], "projects"))
    assert read([brain])["page"] == ""


def test_a_damaged_cache_is_an_error_not_a_crash(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def damaged(*_: object, **__: object) -> list[dict[str, object]]:
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(index, "search", damaged)
    monkeypatch.setattr(index, "listing", damaged)
    for operation in (lambda: search([brain], Query(text="offline")), lambda: read([brain], "projects")):
        with pytest.raises(Error, match="run bf build"):
            operation()


def test_one_invalid_brain_does_not_hide_the_others(tmp_path: Path) -> None:
    alpha = make(tmp_path / "alpha", "alpha", {"projects/x.md": "# X\n\nzebracorn\n"})
    beta = make(tmp_path / "beta", "beta", {})
    beta.write("bf.yaml", b"version: 5\nname: beta\nbogus: 1\n")
    for ref in ("projects/x.md", "bf://alpha/projects/x.md"):
        reply = read([alpha, beta], ref)
        assert reply["ref"] == "projects/x.md"
        assert "bf.yaml" in str(reply["problems"])
    with pytest.raises(Error, match=r"invalid bf\.yaml"):
        read([beta], "projects/x.md")


def test_a_shared_alias_never_merges_its_owners_links(brain: Store) -> None:
    alias = "bf://fixture/people/alice"
    brain.write("concepts/alice-one.md", f'---\ntype: person\naliases: ["{alias}"]\n---\n# Alice One\n'.encode())
    brain.write("concepts/alice-two.md", f'---\ntype: person\naliases: ["{alias}"]\n---\n# Alice Two\n'.encode())
    brain.write("projects/q.md", b"# Q\n\nWorks with [one](../concepts/alice-one.md).\n")
    brain.write("projects/r.md", b"# R\n\nWorks with [two](../concepts/alice-two.md).\n")
    found = search([brain], Query(text="works", **pages.scope(alias)))
    assert refs(found) == []
    assert "ambiguous" in str(found["problems"])
    with pytest.raises(Error, match="incomplete"):
        pages.identity([brain], alias)
    with pytest.raises(Error, match="ambiguous"):
        read([brain], alias)


def test_identity_search_finds_links_to_a_notes_sections(brain: Store) -> None:
    brain.write("projects/x.md", b'---\naliases: ["repo:ex/x"]\n---\n# X\n\n## Decision\n\ntext\n')
    brain.write("projects/y.md", b"# Y\n\nSee [decision](x.md#decision).\n")
    brain.write("projects/z.md", b"# Z\n\nSee [x](bf://fixture/projects/x.md#decision).\n")
    found = refs(search([brain], Query(text="bf://fixture/projects/x.md")))
    assert found[0] == "projects/x.md"
    assert {"projects/y.md", "projects/z.md"} <= set(found)


def test_period_pages_order_changed_items_by_modification(brain: Store) -> None:
    records_file(
        brain,
        "mail",
        "2026-01",
        [
            Record(
                id="late",
                title="Edited late",
                time="2026-01-05T00:00:00Z",
                attributes={"updated": "2026-09-29T00:00:00Z"},
            )
        ],
    )
    records_file(
        brain,
        "mail",
        "2026-08",
        [
            Record(
                id=f"early-{n}",
                title="Edited early",
                time="2026-08-01T00:00:00Z",
                attributes={"updated": "2026-09-01T00:00:00Z"},
            )
            for n in range(25)
        ],
    )
    changed = refs(read([brain], "2026-09"), "changed")
    assert changed[0] == "mail:late"
    assert len(changed) == pages.SECTION


def test_unknown_sources_are_missing_pages(brain: Store) -> None:
    for ref in ("memories/typo", "memories/typo/today", "memories/typo/2026-09"):
        with pytest.raises(Error, match="page not found"):
            read([brain], ref)
    assert read([brain], "memories/meetings/2026-08")["total"] == 2


def test_a_record_too_long_for_an_address_still_reads(brain: Store) -> None:
    record = Record(id="é" * 2000, title="Long identifier")
    records_file(brain, "mail", "undated", [record])
    reply = read([brain], f"mail:{record.id}")
    assert cast("dict[str, object]", reply["record"])["title"] == "Long identifier"
    assert "backlinks are unavailable" in str(reply["problems"])


def test_headings_and_file_names_stay_addressable(brain: Store) -> None:
    with pytest.raises(Error, match=r"cannot end in \.md"):
        note("projects/x.md", b"# X\n\n## Readme {#notes.md}\n")
    parsed = note("projects/x.md", b"# X\n\n## ???\n\nFirst.\n\n## !!!\n\nSecond.\n")
    assert [passage.fragment for passage in parsed.passages] == ["", "section", "section-1"]
    brain.write("actions/2026-09-25_import/inputs/data#1.csv", b"a,b\n")
    brain.write("actions/2026-09-25_import/ACTION.md", b"# Import\n\nSee [the data](inputs/data%231.csv).\n")
    assert validate(brain)["valid"], validate(brain)["problems"]
