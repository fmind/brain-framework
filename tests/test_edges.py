"""Retrieval stays correct at the edges: extreme dates, broken neighbors, shared aliases and odd names."""

from __future__ import annotations

import json
import re
import runpy
import sqlite3
import unicodedata
from pathlib import Path
from typing import cast
from urllib.parse import quote

import pytest
from pydantic import ValidationError

from bf import index, links, models, pages
from bf.markdown import note
from bf.models import Error, Knowledge, Query, Record, SchemaField, invisible, printable, terminal
from bf.retrieve import read, search
from bf.storage import Store
from bf.validate import validate
from bf.watch import clean
from conftest import records_file


def refs(reply: dict[str, object], key: str = "items") -> list[str]:
    return [str(item["ref"]) for item in cast("list[dict[str, object]]", reply[key])]


def make(root: Path, name: str, files: dict[str, str]) -> Store:
    root.mkdir()
    store = Store(root)
    store.write("bf.yaml", f"version: 7\nname: {name}\n".encode())
    for path, text in files.items():
        store.write(path, text.encode())
    return store


@pytest.mark.parametrize("status", ["", "draft", "stable"])
def test_calendar_edge_dates_never_break_retrieval(brain: Store, status: str) -> None:
    for value in ("0001-01-01", "9999-12-31"):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            Knowledge.model_validate({"updated": value})
    metadata = "type: project\n" + (f"status: {status}\n" if status else "")
    brain.write("projects/far.md", f"---\n{metadata}updated: 9999-12-30\n---\n# Far\n\nretention plan\n".encode())
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
    beta.write("bf.yaml", b"version: 7\nname: beta\nbogus: 1\n")
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


def test_every_record_id_fits_a_bf_address(brain: Store) -> None:
    # Percent-encoding makes each "é" six characters: 2,000 of them cannot fit an 8,192-character address.
    with pytest.raises(ValidationError, match="percent-encoded"):
        Record(id="é" * 2000, title="Long identifier")
    record = Record(id="é" * 1300, title="Long identifier")
    records_file(brain, "mail", [record])
    reply = read([brain], links.address("fixture", f"mail:{record.id}"))
    assert cast("dict[str, object]", reply["record"])["title"] == "Long identifier"
    assert "problems" not in reply
    assert validate(brain)["valid"]


def test_a_note_path_too_long_for_an_address_is_named_and_still_reads(brain: Store) -> None:
    # Each CJK character percent-encodes to nine characters: the path fits the filesystem but not an address.
    path = "projects/" + "/".join(["漢" * 80] * 12) + "/note.md"
    brain.write(path, b"# Long path\n\nlongpathneedle\n")
    reply = read([brain], path)
    assert "longpathneedle" in str(reply["text"])
    assert reply["problems"] == [
        {"brain": "fixture", "error": "backlinks are unavailable for a path this long; shorten it"}
    ]
    for problems in (validate(brain)["problems"], search([brain], Query(text="longpathneedle"))["problems"]):
        assert path in str(problems)
        assert "path exceeds 7988 characters once percent-encoded in a BF address" in str(problems)
    with pytest.raises(Error, match="links are unavailable for a path this long"):
        read([brain], path, rel="links")


def test_a_note_path_with_a_control_character_still_reads(brain: Store) -> None:
    # No BF address can hold a tab, so search skips the note; its exact bytes stay readable for repair.
    path = "projects/tab\tname.md"
    brain.write(path, b"# Tab\n\ntabneedle\n")
    skipped = cast("list[dict[str, object]]", search([brain], Query(text="tabneedle"))["problems"])
    assert [(problem["file"], problem["error"]) for problem in skipped] == [
        (path, "path holds a control character or backslash, which no BF address can; rename it")
    ]
    reply = read([brain], path)
    assert reply["text"] == "# Tab\n\ntabneedle\n"
    assert reply["problems"] == [
        {"brain": "fixture", "file": path, "error": "backlinks are unavailable for this path; rename it"}
    ]
    with pytest.raises(Error, match="links are unavailable for this path; rename it"):
        read([brain], path, rel="links")
    # Validation names the file instead of aborting, and still reports the brain's other problems.
    brain.write("projects/broken.md", b"---\ntype: project\n---\n# Broken\n\n[gone](absent.md)\n")
    assert [(p["file"], p["error"]) for p in cast("list[dict]", validate(brain)["problems"])] == [
        (path, "path holds a control character or backslash, which no BF address can; rename it"),
        (path, "OKF documents require a nonempty type in YAML frontmatter"),
        ("projects/broken.md", "broken link: absent.md"),
    ]


def test_headings_and_file_names_stay_addressable(brain: Store) -> None:
    with pytest.raises(Error, match=r"cannot end in \.md"):
        note("projects/x.md", b"# X\n\n## Readme {#notes.md}\n")
    # A suffix outside the anchor syntax is ordinary title text.
    suffixed = note("projects/x.md", b"# X\n\n## Part {#-bad}\n").passages
    assert [(passage.fragment, passage.heading) for passage in suffixed] == [("", "X"), ("part--bad", "Part {#-bad}")]
    parsed = note("projects/x.md", b"# X\n\n## ???\n\nFirst.\n\n## !!!\n\nSecond.\n")
    assert [passage.fragment for passage in parsed.passages] == ["", "section", "section-1"]
    brain.write("actions/2026-09-25_import/inputs/data#1.csv", b"a,b\n")
    brain.write(
        "actions/2026-09-25_import/ACTION.md",
        b"---\ntype: action\n---\n# Import\n\nSee [the data](inputs/data%231.csv).\n",
    )
    assert validate(brain)["valid"], validate(brain)["problems"]


def test_a_note_with_1000_aliases_reads_with_all_its_names(brain: Store) -> None:
    aliases = ", ".join(f"person:alias-{n}" for n in range(1000))
    head = "---\ntype: person\nentity: bf://fixture/people/many\nresource: https://example.test/many\n"
    brain.write("concepts/many.md", f"{head}aliases: [{aliases}]\n---\n# Many\n".encode())
    brain.write("projects/x.md", b"---\ntype: project\n---\n# X\n\n[Many](person:alias-999)\n")
    assert validate(brain)["valid"]
    # Its path, entity, resource and 1,000 aliases all expand, like a record's ref and its 1,000 aliases.
    for ref in ("bf://fixture/people/many", "concepts/many.md"):
        reply = read([brain], ref)
        assert "problems" not in reply
        assert [i["ref"] for g in cast("list[dict]", reply["backlinks"]) for i in g["items"]] == ["projects/x.md"]


@pytest.mark.parametrize(
    "character", ["\u200b", "\u200e", "\u202e", "\u2060", "\ufeff", "\u00ad", "\u200c", "\U000e0041"]
)
def test_identities_reject_invisible_format_characters(brain: Store, character: str) -> None:
    # Two identities that look alike must not silently differ: every format character counts, even a joiner that
    # Persian spelling or an emoji sequence uses.
    lookalike = f"bob{character}"
    for key, value in (
        ("entity", f"bf://fixture/people/{lookalike}"),
        ("aliases", f"[person:{lookalike}]"),
        ("resource", f"https://example.test/{lookalike}"),
    ):
        brain.write("concepts/bob.md", f"---\ntype: person\n{key}: {value}\n---\n# Bob\n".encode())
        assert [problem["error"] for problem in cast("list[dict]", validate(brain)["problems"])] == [
            f"invalid frontmatter: {key}: identities must not contain invisible format characters, such as U+200B"
        ]
    # A resource describing a population, with spaces, is data rather than an identity.
    brain.write("concepts/bob.md", f"---\ntype: person\nresource: photos of {lookalike} family\n---\n# Bob\n".encode())
    assert validate(brain)["valid"]
    with pytest.raises(ValidationError, match="invisible format characters"):
        Record(id="x", title="X", aliases=[f"person:{lookalike}"])
    with pytest.raises(ValueError, match="invisible format characters"):
        SchemaField(description="Owner.", type="identity").normalize(f"person:{lookalike}")
    # A BF address decodes its path: the percent-encoded form names the same lookalike subject.
    encoded = quote(character)
    brain.write(
        "concepts/bob.md", f"---\ntype: person\nentity: bf://fixture/people/bob{encoded}\n---\n# Bob\n".encode()
    )
    assert not validate(brain)["valid"]
    # Other schemes stay opaque: their percent signs are visible text.
    brain.write(
        "concepts/bob.md", f"---\ntype: person\nresource: https://example.test/bob{encoded}\n---\n# Bob\n".encode()
    )
    assert validate(brain)["valid"]


def test_format_characters_have_one_definition_that_matches_unicode() -> None:
    # bf lists Unicode's format characters (Cf) rather than scan every code point in each command: the list must be
    # the running Python's. Identities reject them; replies, diagnostics and the evidence helper escape them.
    everything = "".join(map(chr, range(0x110000)))
    formats = "".join(character for character in everything if unicodedata.category(character) == "Cf")
    assert "".join(re.findall(f"[{models._FORMAT}]", everything)) == formats  # noqa: SLF001 - the listed contract
    assert all(map(invisible, formats))
    assert not invisible("person:name/علیرضا")
    helper = runpy.run_path(str(Path(__file__).parents[1] / "src/bf/skills/bf-use/scripts/evidence.py"))
    assert helper["CONTROLS"].pattern == models._TERMINAL.pattern  # noqa: SLF001 - the helper mirrors it
    for escaped in (terminal(formats), helper["terminal"](formats)):
        assert escaped.isascii()
        assert json.loads(escaped) == formats
    assert printable(formats).isascii()
    # Beyond U+FFFF, JSON spells a character as a surrogate pair: `\ue0041` would decode as U+E004 and "1".
    assert terminal("\U000e0041") == helper["terminal"]("\U000e0041") + b"\n" == b'"\\udb40\\udc41"\n'
    assert printable("tag \U000e0041") == "tag \\udb40\\udc41"
    # The watch dashboard blanks every character it cannot print, these included.
    assert clean(formats) == " " * len(formats)
