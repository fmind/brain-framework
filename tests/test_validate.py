"""Validation reports every broken note, link and record at once, each located in its file."""

from __future__ import annotations

import time
from typing import cast

import pytest

from bf.markdown import note, reference, section, validate_okf
from bf.models import Error, Query, Record
from bf.records import path as record_path
from bf.retrieve import read, search
from bf.storage import Store
from bf.validate import validate
from conftest import records_file


def located(result: dict[str, object]) -> list[str]:
    """Each problem as `file: error`; every problem names the file or folder to repair."""
    problems = cast("list[dict[str, str]]", result["problems"])
    assert all(set(problem) == {"file", "error"} and problem["file"] for problem in problems)
    return [f"{problem['file']}: {problem['error']}" for problem in problems]


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
    assert located(validate(brain)) == [
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
    brain.write("projects/bad.md", b"---\ntags: [a/b]\n---\n# Bad\n")
    meeting = "memories/meetings/8810ad581e59f2bc3928b261707a71308f7e139eb04820366dc4d5c18d980225.json"
    brain.write(meeting, b'{"id":"lunch","title":"Moved","time":"2026-08-01T00:00:00Z"}\n')
    brain.write("memories/other/2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881.json", b"{broken\n")
    result = validate(brain)
    assert not result["valid"]
    problems = "\n".join(located(result))
    for expected in [
        "projects/links.md: broken link: absent.md",
        "projects/links.md: missing heading: offline.md#absent",
        "projects/links.md: link leaves the brain: ../../outside.md",
        "projects/links.md: missing record meetings:absent",
        "projects/bad.md: invalid frontmatter: tags",
        f"{meeting}: record id does not match its SHA-256 filename",
        "memories/other/2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881.json: invalid JSON document",
    ]:
        assert expected in problems
    assert "decision" not in problems
    assert "example.com" not in problems


def test_link_schema_and_identity_problems_name_their_file_once(brain: Store) -> None:
    brain.write("bf.yaml", b"version: 6\nname: fixture\n")
    brain.write("concepts/foreign.md", b"---\ntype: person\nentity: bf://other/people/alice\n---\n# Foreign\n")
    brain.write(
        "concepts/role.md", b"---\ntype: concept\n---\n# Role\n\n[x](bf://fixture/projects/offline.md?rel=nope)\n"
    )
    brain.write(
        "concepts/dup.md", b"---\ntype: concept\n---\n# Dup\n\n[x](../projects/absent.md) [y](evidence.md#missing)\n"
    )
    records_file(
        brain,
        "fake",
        [
            Record(id="fields", title="Fields", fields={"nope": "value"}),
            Record(id="alias", title="Alias", aliases=["bf://other/items/alias"]),
            Record(id="linked", title="Linked", links=["bf://fixture/projects/absent.md"]),
        ],
    )
    brain.write("projects/cites.md", b"---\ntype: project\n---\n# Cites\n\n[record](fake:fields)\n")
    result = validate(brain)
    problems = located(result)
    assert f"{record_path('fake', 'fields')}: undeclared schema field nope" in problems
    assert f"{record_path('fake', 'alias')}: BF aliases must belong to their own brain namespace" in problems
    assert f"{record_path('fake', 'linked')}: unresolved BF target: bf://fixture/projects/absent.md" in problems
    assert "concepts/foreign.md: a note entity must belong to its own brain namespace" in problems
    assert "concepts/role.md: link relation is undeclared; declare an identity relationship in schema" in problems
    assert {p.split(": ", 1)[0] for p in problems} == {
        "concepts/dup.md",
        "concepts/foreign.md",
        "concepts/role.md",
        record_path("fake", "alias"),
        record_path("fake", "fields"),
        record_path("fake", "linked"),
    }
    # A relative link is checked once, as written; it is never reported again as a qualified BF address.
    assert [p for p in problems if p.startswith("concepts/dup.md")] == [
        "concepts/dup.md: broken link: ../projects/absent.md",
        "concepts/dup.md: missing heading: evidence.md#missing",
    ]
    # A record with a field the schema no longer declares still exists: citing it is not a missing record.
    assert result["records"] == 5
    assert not any("missing record" in p for p in problems)


def test_problem_lists_are_capped_with_a_flag(brain: Store) -> None:
    body = "".join(f"[gone](absent-{i}.md) [far](bf://other/n/{i})\n" for i in range(201))
    brain.write("projects/many.md", f"---\ntype: project\n---\n# Many\n\n{body}".encode())
    result = validate(brain)
    assert len(cast("list", result["problems"])) == 200
    assert result["problems_truncated"] is True
    assert len(cast("list", result["unresolved"])) == 200
    assert result["unresolved_truncated"] is True
    brain.write("projects/many.md", b"---\ntype: project\n---\n# Few\n\n[gone](absent.md) [far](bf://other/n)\n")
    result = validate(brain)
    assert "problems_truncated" not in result
    assert "unresolved_truncated" not in result


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
        ("concepts/a.md", b"---\ntype: concept\nstatus: published\n---\n"),
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
    for bad in [b"---\ntitle: x\n", b"\xff", b"---\ntags: x\n---\n", f"# {'x' * 4097}\n".encode()]:
        with pytest.raises(Error):
            note("concepts/x.md", bad)


@pytest.mark.parametrize("frontmatter", ["", "---\ntype: concept\n---\n"])
def test_a_rule_below_the_title_is_not_frontmatter(frontmatter: str) -> None:
    data = f"{frontmatter}# Title\n---\nIntro text.\n\n---\n\n## Next\n\nBody\n".encode()
    projection = note("concepts/x.md", data)
    assert projection.title == "Title"
    assert projection.lead == "Intro text."
    assert [p.fragment for p in projection.passages] == ["", "next"]


def test_a_byte_order_mark_does_not_hide_frontmatter(brain: Store) -> None:
    data = b"\xef\xbb\xbf---\ntype: project\nstatus: draft\ntags: [beta]\n---\n# Bom project\n\nOkapi notes.\n"
    assert note("projects/bom.md", data).knowledge.tags == ["beta"]
    brain.write("projects/bom.md", data)
    assert validate(brain)["valid"]
    assert [i["ref"] for i in cast("list[dict]", search([brain], Query(text="okapi"))["items"])] == ["projects/bom.md"]
    assert cast("dict", read([brain], "bf://fixture/tags/beta"))["total"] == 1


def test_indented_conflict_markers_are_literal_examples() -> None:
    example = b"# Git\n\n```text\n <<<<<<< HEAD\n ours\n =======\n theirs\n >>>>>>> feature\n```\n"
    assert note("concepts/git.md", example).title == "Git"
    with pytest.raises(Error, match=r"line 4: unresolved merge conflict marker; .* indent a literal example"):
        note("concepts/git.md", example.replace(b" <<<", b"<<<"))


def test_adversarial_markdown_parses_in_linear_time() -> None:
    started = time.perf_counter()
    note("concepts/links.md", b"# Links\n\n" + b"[" * 100_000 + b"]\n")
    note("concepts/links.md", b"# Links\n\n- [ ] " + b"[](" * 30_000 + b"\n")
    note("concepts/heading.md", b"# Heading\n\n## a" + b" " * 100_000 + b"b\n")
    # The former backtracking patterns took over a minute on these inputs.
    assert time.perf_counter() - started < 10


def test_images_are_checked_links(brain: Store) -> None:
    brain.write("assets/logo.svg", b"<svg/>")
    brain.write(
        "concepts/media.md",
        b"---\ntype: concept\n---\n# Media\n\n![Logo](../assets/logo.svg) ![Pixel](data:image/png;base64,AAAA)\n",
    )
    assert note("concepts/media.md", brain.read("concepts/media.md")).links == ["assets/logo.svg"]
    assert validate(brain)["valid"]
    brain.write("concepts/media.md", b"---\ntype: concept\n---\n# Media\n\n![Logo](../assets/missing.svg)\n")
    assert located(validate(brain)) == ["concepts/media.md: broken link: ../assets/missing.svg"]


@pytest.mark.parametrize("bundle", ["projects", "concepts"])
def test_okf_notes_resolve_a_leading_slash_from_their_bundle_root(brain: Store, bundle: str) -> None:
    # OKF's recommended bundle-relative form survives moving the linking note within its bundle.
    brain.write(f"{bundle}/tables/customers.md", b"---\ntype: table\n---\n# Customers\n\n## Join key\n")
    orders = f"{bundle}/sales/orders.md"
    brain.write(orders, b"---\ntype: table\n---\n# Orders\n\nSee [customers](/tables/customers.md#join-key).\n")
    assert reference(orders, "/tables/customers.md") == f"{bundle}/tables/customers.md"
    assert validate(brain)["valid"]
    backlinks = cast("list[dict[str, object]]", read([brain], f"{bundle}/tables/customers.md")["backlinks"])
    assert [item["ref"] for group in backlinks for item in cast("list[dict]", group["items"])] == [orders]
    brain.write(orders, b"---\ntype: table\n---\n# Orders\n\n[a](/tables/absent.md) [b](/../../outside.md)\n")
    assert located(validate(brain)) == [
        f"{orders}: link leaves the brain: /../../outside.md",
        f"{orders}: broken link: /tables/absent.md",
    ]


def test_actions_and_their_attachments_have_no_bundle_root(brain: Store) -> None:
    brain.write("actions/2026-09-27_x/ACTION.md", b"---\ntype: action\n---\n# X\n\n[root](/evidence.md)\n")
    brain.write("actions/2026-09-27_x/outputs/doc.md", b"# Doc\n\n[root](/evidence.md)\n")
    assert located(validate(brain)) == [
        "actions/2026-09-27_x/ACTION.md: absolute link; use a path relative to this file: /evidence.md",
        "actions/2026-09-27_x/outputs/doc.md: absolute link; use a path relative to this file: /evidence.md",
    ]
    brain.write(
        "actions/2026-09-27_x/ACTION.md", b"---\ntype: action\n---\n# X\n\n[root](../../concepts/evidence.md)\n"
    )
    brain.delete("actions/2026-09-27_x/outputs/doc.md")
    assert validate(brain)["valid"]


def test_link_case_must_match_the_file_name(brain: Store) -> None:
    # A case-insensitive volume would find the file, but the graph matches exact names on every platform.
    brain.write("projects/cased.md", b"---\ntype: project\n---\n# Cased\n\n[x](Offline.md) [y](../Concepts)\n")
    assert located(validate(brain)) == [
        "projects/cased.md: broken link: ../Concepts",
        "projects/cased.md: broken link: Offline.md",
    ]


def test_malformed_links_report_one_file_without_blocking_validation(brain: Store) -> None:
    brain.write("projects/bad-url.md", b'---\nlinks: ["https://["]\n---\n# Invalid link\n')
    result = validate(brain)
    assert not result["valid"]
    assert "projects/bad-url.md: invalid link" in located(result)


@pytest.mark.parametrize("path", ["projects/new.md", "concepts/new.md", "actions/2026-09-27_new/ACTION.md"])
def test_okf_sources_are_checked_explicit_relations(brain: Store, path: str) -> None:
    data = b'---\ntype: concept\nsources:\n  - resource: "meetings:absent"\n---\n# Concept\n'
    brain.write(path, data)
    assert note(path, data).links == ["meetings:absent"]
    assert f"{path}: missing record meetings:absent" in located(validate(brain))


@pytest.mark.parametrize("path", ["projects/new.md", "concepts/new.md", "actions/2026-09-27_new/ACTION.md"])
def test_okf_source_relationships_must_be_declared(brain: Store, path: str) -> None:
    brain.write(
        path,
        b"---\ntype: note\nsources:\n  - resource: bf://fixture/projects/offline.md?rel=undeclared\n---\n# Source\n",
    )
    assert any(p.startswith(f"{path}: ") and "declare an identity relationship" in p for p in located(validate(brain)))


def test_duplicate_aliases_are_reported(brain: Store) -> None:
    brain.write(
        "projects/duplicate.md", b'---\ntype: project\naliases: ["repo:example/project"]\n---\n# Duplicate owner\n'
    )
    assert located(validate(brain)) == [
        "projects/duplicate.md: ambiguous identity repo:example/project: projects/duplicate.md, projects/offline.md"
    ]


def test_local_links_decode_filename_escapes_once_after_splitting_fragments(brain: Store) -> None:
    brain.write(
        "projects/C# guide.md", b"---\ntype: project\n---\n# Guide\n\n## Decision\n\nKeep the reference readable.\n"
    )
    brain.write("projects/literal%20name.md", b"---\ntype: project\n---\n# Literal percent\n")
    brain.write("projects/100%.md", b"---\ntype: project\n---\n# Percent\n")
    brain.write("projects/Réunion notes.md", b"---\ntype: project\n---\n# Meeting\n")
    data = (
        "---\ntype: project\n---\n# Links\n\n[guide](C%23%20guide.md#decision) [whole](C%23%20guide.md) "
        "[literal](literal%2520name.md) [raw](100%.md) [spaced](<Réunion notes.md>)\n"
    ).encode()
    brain.write("projects/links.md", data)
    assert note("projects/links.md", data).links == [
        "projects/100%.md",
        "projects/C# guide.md",
        "projects/C# guide.md#decision",
        "projects/Réunion notes.md",
        "projects/literal%20name.md",
    ]
    assert validate(brain)["valid"]


@pytest.mark.parametrize("path", ["projects/new.md", "concepts/new.md", "actions/2026-09-27_new/ACTION.md"])
@pytest.mark.parametrize(
    ("metadata", "problem"),
    [
        ("", "nonempty type"),
        ("type: ' '", "nonempty type"),
        *[(f"type: project\nstatus: {status}", "OKF status") for status in ("published", "current", "''")],
        ("type: project\nsources: [https://example.com]", "sources require mappings"),
        ("type: project\nsources: [{resource: ' '}]", "nonempty resource"),
        ("type: project\nverified: [{by: human:owner}]", "require by and at"),
    ],
)
def test_validate_enforces_okf_for_authored_notes(brain: Store, path: str, metadata: str, problem: str) -> None:
    brain.write(path, f"---\n{metadata}\n---\n# New note\n".encode())
    result = validate(brain)
    assert not result["valid"]
    assert any(entry.startswith(f"{path}: ") and problem in entry for entry in located(result))


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


def test_case_variant_identities_warn_without_failing(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write(
        "bf.yaml",
        b"version: 6\nname: fixture\nschema:\n"
        b"  repository: {description: Repository., type: identity, cardinality: many, relation: true}\n",
    )
    records_file(
        brain,
        "github",
        [
            Record(id="pr-1", title="Fix", fields={"repository": ["repo:github.com/Team/x"]}),
            Record(id="pr-2", title="Docs", links=["repo:github.com/team/x"], aliases=["person:email/Bo@example.test"]),
        ],
    )
    brain.write("projects/x.md", b"---\ntype: project\naliases: [repo:github.com/team/x]\n---\n# X\n")
    brain.write("concepts/bo.md", b"---\ntype: person\naliases: [person:email/bo@example.test]\n---\n# Bo\n")
    result = validate(brain)
    assert (result["valid"], result["problems"]) == (True, [])
    # Most referenced first, each spelling with the number of files naming it.
    assert result["warnings"] == [
        {
            "warning": "identities differ only by letter case",
            "identities": [
                {"identity": "repo:github.com/team/x", "files": 2},
                {"identity": "repo:github.com/Team/x", "files": 1},
            ],
        },
        {
            "warning": "identities differ only by letter case",
            "identities": [
                {"identity": "person:email/Bo@example.test", "files": 1},
                {"identity": "person:email/bo@example.test", "files": 1},
            ],
        },
    ]
    monkeypatch.setattr("bf.validate.LIMIT", 1)
    monkeypatch.setattr("bf.validate.VARIANTS", 1)
    bounded = validate(brain)
    assert bounded["warnings_truncated"] is True
    assert bounded["warnings"] == [
        {
            "warning": "identities differ only by letter case",
            "identities": [{"identity": "repo:github.com/team/x", "files": 2}],
        }
    ]
