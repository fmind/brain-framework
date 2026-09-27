"""Validation reports every broken note, link, citation and partition at once."""

from __future__ import annotations

from typing import cast

import pytest

from bf.markdown import note, section, validate_okf
from bf.models import Error
from bf.storage import Store
from bf.validate import validate


def test_a_clean_brain_is_valid(brain: Store) -> None:
    assert validate(brain) == {"valid": True, "notes": 2, "records": 2, "problems": []}


def test_action_folders_are_dated_and_resumable(brain: Store) -> None:
    brain.write("actions/README.md", b"# Actions\n")
    brain.write("actions/2026-09-24_pilot-review/ACTION.md", b"---\ntype: action\n---\n# Pilot\n")
    brain.write("actions/2026-09-24_pilot-review/outputs/answer.md", b"# Answer\n")
    brain.write("actions/2026-09-24_pilot-review/inputs/ACTION.md", b"# Supplied context\n")
    assert validate(brain)["valid"]
    for folder in ("2026-09-24-hyphen", "undated", "2026-02-30_bad-date", "2026-09-24_Upper"):
        brain.write(f"actions/{folder}/ACTION.md", b"---\ntype: action\n---\n# Misnamed\n")
    brain.write("actions/2026-09-24_no-action/inputs/request.md", b"# Request\n")
    problems = cast("list[str]", validate(brain)["problems"])
    assert problems == [
        "actions/2026-02-30_bad-date: name action folders YYYY-MM-DD_slug (lowercase slug, hyphens)",
        "actions/2026-09-24-hyphen: name action folders YYYY-MM-DD_slug (lowercase slug, hyphens)",
        "actions/2026-09-24_Upper: name action folders YYYY-MM-DD_slug (lowercase slug, hyphens)",
        "actions/2026-09-24_no-action: missing ACTION.md",
        "actions/undated: name action folders YYYY-MM-DD_slug (lowercase slug, hyphens)",
    ]


def test_problems_are_collected_not_fail_fast(brain: Store) -> None:
    brain.write(
        "projects/links.md",
        b"# Links\n\n[gone](absent.md) [heading](offline.md#absent) [ok](offline.md#decision) [dir](../concepts)\n"
        b"[up](../../outside.md) [cited](meetings:absent) [web](https://example.com) [self](#links)\n",
    )
    brain.write("projects/bad.md", b"---\nstatus: current\n---\n# Bad\n")
    brain.write(
        "memories/meetings/8810ad581e59f2bc3928b261707a71308f7e139eb04820366dc4d5c18d980225.json",
        b'{"id":"lunch","title":"Moved","time":"2026-08-01T00:00:00Z"}\n',
    )
    brain.write("memories/other/2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881.json", b"{broken\n")
    result = validate(brain)
    assert not result["valid"]
    problems = "\n".join(cast("list[str]", result["problems"]))
    for expected in [
        "projects/links.md: broken link: absent.md",
        "projects/links.md: missing heading: offline.md#absent",
        "projects/links.md: link leaves the brain: ../../outside.md",
        "projects/links.md: missing record meetings:absent",
        "projects/bad.md: invalid frontmatter: status",
        "record id does not match its SHA-256 filename",
        "memories/other/2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881.json: invalid JSON document",
    ]:
        assert expected in problems
    assert "decision" not in problems
    assert "example.com" not in problems


def test_okf_concept_structure() -> None:
    validate_okf("concepts/index.md", b'---\nokf_version: "0.2"\n---\n# Concepts\n')
    validate_okf("concepts/log.md", b"# Log\n\n## 2026-09-01\n\n- Change.\n")
    validate_okf(
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
            validate_okf(path, data)


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
    assert note("actions/t/ACTION.md", b"# T\n\n## Only\n\nFirst section text.\n").lead == "First section text."
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


@pytest.mark.parametrize("path", ["projects/new.md", "concepts/new.md", "actions/2026-09-27_new/ACTION.md"])
def test_okf_sources_are_checked_explicit_relations(brain: Store, path: str) -> None:
    data = b'---\ntype: concept\nsources:\n  - resource: "meetings:absent"\n---\n# Concept\n'
    brain.write(path, data)
    assert note(path, data).links == ["meetings:absent"]
    assert f"{path}: missing record meetings:absent" in str(validate(brain)["problems"])


@pytest.mark.parametrize("path", ["projects/new.md", "concepts/new.md", "actions/2026-09-27_new/ACTION.md"])
def test_okf_source_relationships_must_be_declared(brain: Store, path: str) -> None:
    brain.write(
        path,
        b"---\ntype: note\nsources:\n  - resource: bf://fixture/projects/offline.md?rel=undeclared\n---\n# Source\n",
    )
    assert "declare an identity relationship" in str(validate(brain)["problems"])


def test_duplicate_aliases_are_reported(brain: Store) -> None:
    brain.write("projects/duplicate.md", b'---\naliases: ["repo:example/project"]\n---\n# Duplicate owner\n')
    assert "ambiguous identity repo:example/project" in str(validate(brain)["problems"])


def test_local_links_decode_filename_escapes_once_after_splitting_fragments(brain: Store) -> None:
    brain.write(
        "projects/C# guide.md", b"---\ntype: project\n---\n# Guide\n\n## Decision\n\nKeep the reference readable.\n"
    )
    brain.write("projects/literal%20name.md", b"---\ntype: project\n---\n# Literal percent\n")
    data = b"---\ntype: project\n---\n# Links\n\n[guide](C%23%20guide.md#decision) [whole](C%23%20guide.md) [literal](literal%2520name.md)\n"
    brain.write("projects/links.md", data)
    assert note("projects/links.md", data).links == [
        "projects/C# guide.md",
        "projects/C# guide.md#decision",
        "projects/literal%20name.md",
    ]
    assert validate(brain)["valid"]


@pytest.mark.parametrize("path", ["projects/new.md", "concepts/new.md", "actions/2026-09-27_new/ACTION.md"])
@pytest.mark.parametrize(
    ("metadata", "problem"),
    [
        ("", "nonempty type"),
        ("type: ' '", "nonempty type"),
        *[
            (f"type: project\nstatus: {status}", "OKF status")
            for status in ("active", "paused", "blocked", "done", "archived")
        ],
        ("type: project\nsources: [https://example.com]", "sources require mappings"),
        ("type: project\nsources: [{resource: ' '}]", "nonempty resource"),
        ("type: project\nverified: [{by: human:owner}]", "require by and at"),
    ],
)
def test_validate_enforces_okf_for_authored_notes(brain: Store, path: str, metadata: str, problem: str) -> None:
    brain.write(path, f"---\n{metadata}\n---\n# New note\n".encode())
    result = validate(brain)
    assert not result["valid"]
    assert any(path in entry and problem in entry for entry in cast("list[str]", result["problems"]))


@pytest.mark.parametrize("status", ["", "draft", "stable", "deprecated"])
def test_action_okf_status_does_not_replace_task_progress(brain: Store, status: str) -> None:
    metadata = "type: action\n" + (f"status: {status}\n" if status else "")
    brain.write(
        "actions/2026-09-27_review/ACTION.md",
        f"---\n{metadata}---\n# Review\n\n- [x] Inspect evidence.\n- [ ] Record the decision.\n".encode(),
    )
    assert validate(brain)["valid"]


@pytest.mark.parametrize("folder", ["projects", "concepts"])
@pytest.mark.parametrize("status", ["", "draft", "stable", "deprecated"])
def test_okf_status_and_reserved_files_are_valid(brain: Store, folder: str, status: str) -> None:
    metadata = "type: project\n" + (f"status: {status}\n" if status else "")
    brain.write(f"{folder}/new.md", f"---\n{metadata}---\n# New note\n".encode())
    brain.write(f"{folder}/index.md", b'---\nokf_version: "0.2"\n---\n# Index\n')
    brain.write(f"{folder}/nested/index.md", b"# Nested index\n")
    brain.write(f"{folder}/log.md", b"# Changes\n\n## 2026-09-27\n\nUpdated the note.\n")
    assert validate(brain)["valid"]
    brain.write(f"{folder}/nested/index.md", b'---\nokf_version: "0.2"\n---\n# Index\n')
    assert not validate(brain)["valid"]
