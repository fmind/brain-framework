"""Validation reports every broken note, link, citation and partition at once."""

from __future__ import annotations

from typing import cast

import pytest

from bf.markdown import note, section, validate_concept
from bf.models import Error
from bf.storage import Store
from bf.validate import validate


def test_a_clean_brain_is_valid(brain: Store) -> None:
    assert validate(brain) == {"valid": True, "notes": 2, "records": 2, "problems": []}


def test_problems_are_collected_not_fail_fast(brain: Store) -> None:
    brain.write(
        "projects/links.md",
        b"# Links\n\n[gone](absent.md) [heading](offline.md#absent) [ok](offline.md#decision) [dir](../concepts)\n"
        b"[up](../../outside.md) [cited](meetings:absent) [web](https://example.com) [self](#links)\n",
    )
    brain.write("projects/bad.md", b"---\nstatus: current\n---\n# Bad\n")
    brain.write("memories/meetings/2026-07.jsonl", b'{"id":"lunch","title":"Moved","time":"2026-08-01T00:00:00Z"}\n')
    brain.write("memories/other/2026-09.jsonl", b"{broken\n")
    result = validate(brain)
    assert not result["valid"]
    problems = "\n".join(cast("list[str]", result["problems"]))
    for expected in [
        "projects/links.md: broken link: absent.md",
        "projects/links.md: missing heading: offline.md#absent",
        "projects/links.md: link leaves the brain: ../../outside.md",
        "projects/links.md: missing record meetings:absent",
        "projects/bad.md: invalid frontmatter: status",
        "duplicate id 'lunch' in source meetings",
        "2026-07.jsonl: record 'lunch' belongs in 2026-08.jsonl",
        "memories/other/2026-09.jsonl:1: invalid JSON document",
    ]:
        assert expected in problems
    assert "decision" not in problems
    assert "example.com" not in problems


def test_okf_concept_structure() -> None:
    validate_concept("concepts/index.md", b'---\nokf_version: "0.2"\n---\n# Concepts\n')
    validate_concept("concepts/log.md", b"# Log\n\n## 2026-09-01\n\n- Change.\n")
    validate_concept(
        "concepts/a.md",
        b"---\ntype: concept\nsources:\n  - resource: https://x\nverified:\n  by: human:me\n  at: 2026-09-01T00:00:00Z\n---\n# A\n",
    )
    for path, data in [
        ("concepts/index.md", b"---\ntype: index\n---\n"),
        ("concepts/log.md", b"---\ntype: log\n---\n"),
        ("concepts/log.md", b"# Log\n\n## September\n"),
        ("concepts/a.md", b"# No type\n"),
        ("concepts/a.md", b"---\ntype: concept\nstatus: active\n---\n"),
        ("concepts/a.md", b"---\ntype: concept\nsources: [https://x]\n---\n"),
        ("concepts/a.md", b"---\ntype: concept\nverified: [{by: me}]\n---\n"),
    ]:
        with pytest.raises(Error, match="OKF"):
            validate_concept(path, data)


def test_note_projection_sections_and_titles() -> None:
    projection = note(
        "projects/p.md",
        b"---\nsummary: Short summary.\ntags: [x]\n---\n# Title\n\nIntro [link](other.md).\n\n## Same\n\nA\n\n### Same\n\nB\n\n## Last\n\nC\n",
    )
    assert projection.title == "Title"
    assert projection.knowledge.type == "project"
    assert projection.lead == "Short summary."
    assert [p.fragment for p in projection.passages] == ["", "same", "same-1", "last"]
    assert projection.links == ["projects/other.md"]
    assert note("concepts/some-name.md", b"No heading at all.\n").title == "some name"
    footnoted = note(
        "concepts/f.md", b"---\ntags: [2026, x]\n---\n# F\n\nClaim.[^a]\n\n[^a]: [Source](other.md#part).\n"
    )
    assert footnoted.targets == ["other.md#part"]
    assert footnoted.knowledge.tags == ["2026", "x"]
    assert note("actions/t/ACTION.md", b"# T\n\n## Only\n\nFirst section text.\n").lead == "Only First section text."
    data = "# T\r\n\r\n## A\r\n\r\nx\u2028y\r\n\r\n## B\r\n".encode()
    assert section("concepts/t.md", data, "a") == "## A\r\n\r\nx\u2028y\r\n\r\n"
    for bad in [b"---\ntitle: x\n", b"\xff", b"---\ntags: x\n---\n"]:
        with pytest.raises(Error):
            note("concepts/x.md", bad)


def test_malformed_links_report_one_file_without_blocking_validation(brain: Store) -> None:
    brain.write("projects/bad-url.md", b'---\nlinks: ["https://["]\n---\n# Invalid link\n')
    result = validate(brain)
    assert not result["valid"]
    assert "projects/bad-url.md: invalid link" in str(result["problems"])


def test_okf_sources_are_checked_explicit_relations(brain: Store) -> None:
    data = b'---\ntype: concept\nsources:\n  - resource: "meetings:absent"\n---\n# Concept\n'
    brain.write("concepts/concept.md", data)
    assert note("concepts/concept.md", data).links == ["meetings:absent"]
    assert "concepts/concept.md: missing record meetings:absent" in str(validate(brain)["problems"])


def test_duplicate_aliases_are_reported(brain: Store) -> None:
    brain.write("projects/duplicate.md", b'---\naliases: ["repo:example/project"]\n---\n# Duplicate owner\n')
    assert "ambiguous identity repo:example/project" in str(validate(brain)["problems"])


def test_local_links_decode_filename_escapes_once_after_splitting_fragments(brain: Store) -> None:
    brain.write("projects/C# guide.md", b"# Guide\n\n## Decision\n\nKeep the reference readable.\n")
    brain.write("projects/literal%20name.md", b"# Literal percent\n")
    data = b"# Links\n\n[guide](C%23%20guide.md#decision) [whole](C%23%20guide.md) [literal](literal%2520name.md)\n"
    brain.write("projects/links.md", data)
    assert note("projects/links.md", data).links == [
        "projects/C# guide.md",
        "projects/C# guide.md#decision",
        "projects/literal%20name.md",
    ]
    assert validate(brain)["valid"]
