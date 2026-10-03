"""Validation reports every broken note, link and record at once, each located in its file."""

from __future__ import annotations

import subprocess
import sys
import time
from typing import cast

import pytest

from bf import markdown
from bf.markdown import note, parse, reference, section, validate_okf
from bf.models import Error, Query, Record, encode
from bf.records import path as record_path
from bf.retrieve import read, search
from bf.storage import Store
from bf.validate import validate
from conftest import records_file
from test_guarded_write import HELPER


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


def test_non_finite_note_fields_are_invalid_not_unanswerable(brain: Store) -> None:
    # YAML reads .nan and .inf as floats, which strict JSON replies cannot carry.
    brain.write("bf.yaml", b"version: 7\nname: fixture\nfields:\n  weight:\n    description: W.\n    type: number\n")
    for value in (b".nan", b"-.inf", b"[1, .inf]"):
        brain.write("projects/weight.md", b"---\ntype: project\nfields:\n  weight: " + value + b"\n---\n# Weight\n")
        assert "projects/weight.md: invalid frontmatter: fields" in "\n".join(located(validate(brain)))
        # Retrieval skips the note and reports it; every other reply still encodes.
        reply = search([brain], Query(text="offline"))
        assert any(p.get("file") == "projects/weight.md" for p in cast("list[dict[str, str]]", reply["problems"]))
        encode(reply)
        encode(read([brain], "projects"))


def test_link_schema_and_identity_problems_name_their_file_once(brain: Store) -> None:
    brain.write("bf.yaml", b"version: 7\nname: fixture\n")
    brain.write("concepts/foreign.md", b"---\ntype: person\nentity: bf://other/people/alice\n---\n# Foreign\n")
    brain.write(
        "concepts/relation.md",
        b"---\ntype: concept\n---\n# Relation\n\n[x](bf://fixture/projects/offline.md?rel=nope)\n",
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
    assert f"{record_path('fake', 'fields')}: field nope is not declared in bf.yaml fields" in problems
    assert f"{record_path('fake', 'alias')}: BF aliases must belong to their own brain namespace" in problems
    assert f"{record_path('fake', 'linked')}: unresolved BF target: bf://fixture/projects/absent.md" in problems
    assert (
        "concepts/foreign.md: BF entities, aliases and resources must belong to their own brain namespace" in problems
    )
    assert (
        "concepts/relation.md: line 6: link relation nope is undeclared; declare it in bf.yaml fields with type: "
        "identity and relation: true" in problems
    )
    assert {p.split(": ", 1)[0] for p in problems} == {
        "concepts/dup.md",
        "concepts/foreign.md",
        "concepts/relation.md",
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
    # A caller that already parsed the note passes its parse.
    validate_okf("concepts/log.md", parse("concepts/log.md", b"# Log\n\n## 2026-09-01\n\n- Change.\n"))
    with pytest.raises(Error, match="OKF log dates"):
        validate_okf("concepts/log.md", parse("concepts/log.md", b"# Log\n\n## September\n"))
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
    assert "projects/bad-url.md: links: invalid link" in located(result)


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
    assert any(p.startswith(f"{path}: ") and "declare it in bf.yaml fields" in p for p in located(validate(brain)))


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
        b"version: 7\nname: fixture\nfields:\n"
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


def test_an_interrupted_edit_warns_before_case_variants(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write("projects/x.md", b"---\ntype: project\naliases: [repo:example/x]\n---\n# X\n")
    brain.write("projects/y.md", b"---\ntype: project\naliases: [repo:example/X]\n---\n# Y\n")
    # An agent's guarded edit killed before its rename leaves its temporary file beside the note.
    kill = (
        "import os, runpy, sys; os.replace = lambda *_, **__: os._exit(9); sys.argv = sys.argv[1:]; "
        "runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    digest = str(read([brain], "projects/x.md")["sha256"])
    killed = subprocess.run(  # noqa: S603 - the bundled helper on a synthetic brain
        [sys.executable, "-c", kill, str(HELPER), "projects/x.md", "--expect-sha256", digest],
        cwd=brain.root,
        input="# Edited\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert killed.returncode == 9
    [leftover] = [path.name for path in (brain.root / "projects").iterdir() if path.name.startswith(".")]
    # Before, the helper's own temporary name went unnamed, and the case variants a large brain can hold many of
    # pushed a temporary file past the warnings a reply lists.
    monkeypatch.setattr("bf.validate.LIMIT", 1)
    result = validate(brain)
    assert (result["valid"], result["warnings_truncated"]) == (True, True)
    assert result["warnings"] == [
        {"warning": "an interrupted write left this temporary file; delete it", "file": f"projects/{leftover}"}
    ]


GRAPH = (
    b"version: 7\nname: fixture\nfields:\n"
    b"  owner:\n    description: Responsible person.\n    type: identity\n    cardinality: many\n"
    b'    relation: true\n    targets: ["person:email/"]\n'
    b"sensors:\n  calendar:\n    command: [gws]\n"
)


def test_validation_resolves_graph_links_through_declared_identities(brain: Store) -> None:
    brain.write("bf.yaml", GRAPH)
    # A provider alias names its record like the record ref does.
    records_file(brain, "calendar", [Record(id="evt-1", title="Kickoff", aliases=["calendar:primary/evt-1"])])
    # A typed link may name its target's owner by any identity that owner declares.
    brain.write(
        "concepts/alice.md",
        b"---\ntype: person\nentity: bf://fixture/people/alice\naliases: [person:email/alice@example.test]\n---\n"
        b"# Alice\n",
    )
    brain.write(
        "projects/launch.md",
        b"---\ntype: project\nsources:\n  - resource: all pull requests in the launch repository\n---\n# Launch\n\n"
        b"Owner: [Alice](bf://fixture/people/alice?rel=owner). Kickoff: [event](calendar:primary/evt-1).\n",
    )
    result = validate(brain)
    assert result["valid"], result["problems"]
    # An OKF source describing a population cites nothing and is no broken link.
    claims = cast("list[dict[str, str]]", read([brain], "projects/launch.md").get("claims", []))
    assert "cites" not in {claim["relation"] for claim in claims}
    # A target outside the declared prefixes, through every identity its owner declares, is still reported.
    brain.write("concepts/bob.md", b"---\ntype: person\nentity: bf://fixture/people/bob\n---\n# Bob\n")
    brain.write("projects/other.md", b"---\ntype: project\n---\n# Other\n\n[Bob](bf://fixture/people/bob?rel=owner)\n")
    assert "projects/other.md: owner link outside the declared targets: bf://fixture/people/bob" in located(
        validate(brain)
    )
    brain.delete("projects/other.md")
    # A relation query on another scheme would silently name a different identity.
    brain.write(
        "projects/typed.md", b"---\ntype: project\n---\n# Typed\n\n[Bob](person:email/bob@example.test?rel=owner)\n"
    )
    assert (
        "projects/typed.md: ?rel= types bf:// links only; set the relation in fields instead: "
        "person:email/bob@example.test?rel=owner"
    ) in located(validate(brain))
    brain.write("projects/typed.md", b"---\ntype: project\n---\n# Typed\n\n[Event](calendar:primary/evt-9)\n")
    assert "projects/typed.md: missing record calendar:primary/evt-9" in located(validate(brain))


def test_notes_type_their_claims_in_declared_fields(brain: Store) -> None:
    brain.write("bf.yaml", GRAPH.replace(b"sensors:\n  calendar:\n    command: [gws]\n", b""))
    brain.write(
        "projects/launch.md",
        b"---\ntype: project\nfields:\n  owner: [person:email/bob@example.test]\n---\n# Launch\n\nShip it.\n",
    )
    assert validate(brain)["valid"]
    # The identity needs no note: its page lists the project under the declared relation.
    page = read([brain], "person:email/bob@example.test")
    groups = {group["relation"]: group for group in cast("list[dict]", page["backlinks"])}
    assert [item["ref"] for item in groups["owner"]["items"]] == ["projects/launch.md"]
    claims = cast("list[dict[str, str]]", read([brain], "projects/launch.md")["claims"])
    assert (claims[0]["relation"], claims[0]["target"], claims[0]["origin"]) == (
        "owner",
        "person:email/bob@example.test",
        "bf://fixture/projects/launch.md",
    )
    # Values follow their declaration; retrieval keeps the note while validation names the field.
    for fields, error in (
        (b"  owner: person:email/bob@example.test\n", "fields.owner: expected a list"),
        (b"  owner: [repo:example/x]\n", "owner link outside the declared targets: repo:example/x"),
        (b"  unknown: x\n", "fields.unknown: not declared in bf.yaml fields"),
    ):
        brain.write("projects/launch.md", b"---\ntype: project\nfields:\n" + fields + b"---\n# Launch\n\nShip it.\n")
        assert any(error in problem for problem in located(validate(brain))), located(validate(brain))
        assert [item["ref"] for item in cast("list", search([brain], Query(text="ship"))["items"])] == [
            "projects/launch.md"
        ]
    # A declared relation at the top level asserts nothing: say where it belongs.
    brain.write("projects/launch.md", b"---\ntype: project\nowner: [person:email/bob@example.test]\n---\n# Launch\n")
    assert "projects/launch.md: owner: declared fields belong under fields:, such as fields: {owner: ...}" in located(
        validate(brain)
    )


def test_one_problem_never_hides_a_note_s_others_or_breaks_links_to_it(brain: Store) -> None:
    brain.write("projects/beta.md", b"---\ntype: project\nupdated: 2026-9-1\n---\n# Beta\n")
    brain.write(
        "projects/alpha.md", b"---\ntype: project\n---\n# Alpha\n\n[Beta](bf://fixture/projects/beta.md#beta)\n"
    )
    brain.write(
        "projects/gamma.md",
        b"---\ntype: project\n---\n# Gamma\n\n[Typed](bf://fixture/projects/alpha.md?rel=nope), [gone](missing.md)\n"
        b"and [heading](alpha.md#nowhere).\n",
    )
    problems = {
        (problem["file"], problem["error"]) for problem in cast("list[dict[str, str]]", validate(brain)["problems"])
    }
    # Before 17 the invalid date also made alpha's link unresolved, and the undeclared relation hid gamma's links.
    assert {file for file, _ in problems} == {"projects/beta.md", "projects/gamma.md"}
    assert any("updated" in error for file, error in problems if file == "projects/beta.md")
    assert {error for file, error in problems if file == "projects/gamma.md"} >= {
        "broken link: missing.md",
        "missing heading: alpha.md#nowhere",
    }
    assert any("nope" in error for file, error in problems if file == "projects/gamma.md")


def test_claim_errors_neither_hide_each_other_nor_unname_the_note(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 7\nname: fixture\nfields:\n"
        b"  owner: {description: Owner., type: identity, cardinality: many, relation: true}\n"
        b"  weight: {description: Weight., type: number}\n",
    )
    brain.write(
        "concepts/alice.md",
        b"---\ntype: person\nentity: bf://fixture/people/alice\nfields:\n  weight: heavy\n---\n# Alice\n\n"
        b"[Offline](bf://fixture/projects/offline.md?rel=works-on) [Again](bf://fixture/projects/offline.md?rel=links)\n"
        b"[Weighed](bf://fixture/projects/offline.md?rel=weight)\n\n## Work\n\nShips the website.\n",
    )
    brain.write(
        "projects/b.md",
        b"---\ntype: project\n---\n# B\n\n[Alice](bf://fixture/people/alice?rel=owner), [her work](bf://fixture/people/alice#work)\n",
    )
    brain.write("projects/c.md", b"---\ntype: project\n---\n# C\n\n[Alice](bf://fixture/people/alice)\n")
    # The first undeclared relation used to hide the note's other problems and break the links to its entity.
    assert located(validate(brain)) == [
        (
            "concepts/alice.md: line 9: link relation works-on is undeclared; declare it in bf.yaml fields with type: "
            "identity and relation: true"
        ),
        "concepts/alice.md: line 9: untyped links need no ?rel=; remove ?rel=links",
        "concepts/alice.md: line 10: field weight is not a relation; relations need type: identity and relation: true",
        "concepts/alice.md: fields.weight: expected number",
    ]
    # Validation and retrieval agree: the entity still names the note.
    alice = read([brain], "bf://fixture/people/alice")
    groups = {g["relation"]: [i["ref"] for i in g["items"]] for g in cast("list[dict]", alice["backlinks"])}
    assert groups == {"owner": ["projects/b.md"], "links": ["projects/c.md"]}


def test_a_problem_repeated_across_links_is_reported_once_where_it_first_occurs(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 7\nname: fixture\nfields:\n"
        b"  owner: {description: Owner., type: identity, cardinality: many, relation: true, targets: ['person:']}\n",
    )
    # A relation removed from bf.yaml leaves links naming it in frontmatter and in 250 sections, beside as many owner
    # claims outside the declared targets.
    body = "".join(
        f"## Part {n}\n\n[Plan](bf://fixture/projects/offline.md?rel=reviewer) "
        f"[Plan](bf://fixture/projects/offline.md?rel=owner)\n\n"
        for n in range(250)
    )
    frontmatter = "type: project\nlinks: ['bf://fixture/projects/offline.md?rel=reviewer']"
    brain.write("projects/atlas.md", f"---\n{frontmatter}\n---\n# Atlas\n\n{body}".encode())
    brain.write("projects/zeta.md", b"---\ntype: project\n---\n# Zeta\n\n[Missing](../concepts/missing.md)\n")
    # Before, the 250 copies of each filled the 200 problems a reply lists, and zeta's broken link went unreported.
    assert located(validate(brain)) == [
        (
            "projects/atlas.md: links: link relation reviewer is undeclared; declare it in bf.yaml fields with type: "
            "identity and relation: true"
        ),
        "projects/zeta.md: broken link: ../concepts/missing.md",
        "projects/atlas.md: owner link outside the declared targets: bf://fixture/projects/offline.md",
    ]


def test_a_link_no_bf_address_can_hold_hides_neither_its_note_nor_other_problems(brain: Store) -> None:
    # A Windows path in a copied document, or a control character, names no BF address: the link is broken, while its
    # note stays searchable, keeps its other problems and still names its entity.
    brain.write(
        "projects/web.md",
        b"---\ntype: project\nentity: bf://fixture/people/webby\n---\n# Web\n\n"
        b"webneedle [Owner](bf://fixture/projects/offline.md?rel=nope), [guide](docs\\guide.md), [x](a%01b.md)\n",
    )
    brain.write("projects/other.md", b"---\ntype: project\n---\n# Other\n\n[Webby](bf://fixture/people/webby)\n")
    vendor = "actions/2026-09-27_import/inputs/vendor.md"
    brain.write("actions/2026-09-27_import/ACTION.md", b"---\ntype: action\n---\n# Import\n")
    brain.write(vendor, b"# Vendor\n\nwinneedle [guide](docs\\guide.md)\n")
    assert located(validate(brain)) == [
        (
            "projects/web.md: line 7: link relation nope is undeclared; declare it in bf.yaml fields with type: "
            "identity and relation: true"
        ),
        "projects/web.md: broken link: a%01b.md",
        "projects/web.md: broken link: docs\\guide.md",
        f"{vendor}: broken link: docs\\guide.md",
    ]
    for word, ref in (("webneedle", "projects/web.md"), ("winneedle", vendor)):
        assert [i["ref"] for i in cast("list[dict]", search([brain], Query(text=word))["items"])] == [ref]
    webby = read([brain], "bf://fixture/people/webby")
    assert [i["ref"] for g in cast("list[dict]", webby["backlinks"]) for i in g["items"]] == ["projects/other.md"]


def test_every_problem_of_a_record_is_reported(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 7\nname: fixture\nfields:\n  owner: {description: Owner., type: identity, relation: true}\n",
    )
    records_file(
        brain,
        "mail",
        [
            Record(
                id="m1",
                title="Mail",
                fields={"owner": "Alice", "label": "x"},
                links=["bf://fixture/x?rel=first", "bf://fixture/y?rel=second"],
            )
        ],
    )
    # One record's mistakes are reported together; the same unquoted link problem appears once.
    assert located(validate(brain)) == [
        f"{record_path('mail', 'm1')}: field label is not declared in bf.yaml fields",
        f"{record_path('mail', 'm1')}: field owner: expected an explicit namespaced identity",
        (
            f"{record_path('mail', 'm1')}: link relation is undeclared; declare it in bf.yaml fields with type: "
            "identity and relation: true"
        ),
        f"{record_path('mail', 'm1')}: unresolved BF target: bf://fixture/x",
        f"{record_path('mail', 'm1')}: unresolved BF target: bf://fixture/y",
    ]
    # The ref bf:x of a source named bf reads as a malformed BF address: its file is named, the check goes on.
    records_file(brain, "bf", [Record(id="x", title="X")])
    assert [problem.split(": ", 1)[0] for problem in located(validate(brain))] == [
        record_path("bf", "x"),
        *[record_path("mail", "m1")] * 5,
    ]


def test_record_links_that_are_no_identities_warn_without_failing(brain: Store) -> None:
    records_file(
        brain,
        "mail",
        [
            Record(id="a", title="A", links=["Alice", "repo:example/project"]),
            Record(id="b", title="B", links=["plain words here"]),
            Record(id="c", title="C", url="www.example.test/c"),
            Record(id="d", title="D", links=["https://example.test/d"], url="https://example.test/d"),
        ],
    )
    result = validate(brain)
    assert (result["valid"], result["problems"]) == (True, [])
    # The sensor's text is never quoted: the warning names its source, a count and one file to inspect.
    assert result["warnings"] == [
        {
            "warning": "record links are not namespaced identities or URLs",
            "source": "mail",
            "records": 3,
            "file": min(record_path("mail", name) for name in "abc"),
        }
    ]


def test_scripts_run_through_an_interpreter_must_exist(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 7\nname: fixture\nsensors:\n"
        b"  brief:\n    command: [uv, run, --no-project, sensors/brief.py, '{{start}}']\n"
        b"  packaged:\n    command: [uv, run, --project, sensors/news, news]\n"
        b"  piped:\n    command: [sh, -c, sensors/a.sh | sensors/b.sh]\n"
        b"routines:\n  review:\n    command: [python3, routines/review.py, routines/report.md]\n",
    )
    assert located(validate(brain)) == [
        "bf.yaml: sensors.brief.command: sensors/brief.py is not a regular file or folder",
        "bf.yaml: sensors.packaged.command: sensors/news is not a regular file or folder",
        "bf.yaml: routines.review.command: routines/review.py is not a regular file or folder",
    ]
    # The interpreter runs the first brain path it is given, so it needs no executable bit; later arguments, such as
    # a file the routine may create, and shell command lines are not checked.
    brain.write("sensors/brief.py", b"print('[]')\n")
    brain.write("sensors/news/pyproject.toml", b"[project]\nname = 'news'\n")
    brain.write("routines/review.py", b"print('')\n")
    assert validate(brain)["valid"]


def test_record_refs_are_case_sensitive(brain: Store) -> None:
    # A URL scheme ignores case, so the scheme of Meetings:decision-1 reads as meetings, but a record ref never does.
    brain.write(
        "projects/web.md",
        b"---\ntype: project\n---\n# Web\n\n[the meeting](Meetings:decision-1) [site](HTTPS://example.test/)\n",
    )
    assert located(validate(brain)) == [
        "projects/web.md: record refs are case-sensitive; write meetings:decision-1, not Meetings:decision-1"
    ]


def test_validation_parses_each_note_once(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write("concepts/log.md", b"# Log\n\n## 2026-09-01\n\n- Change.\n")
    parsed: list[str] = []
    original = markdown.parse

    def counted(path: str, data: bytes) -> markdown.Markdown:
        parsed.append(path)
        return original(path, data)

    monkeypatch.setattr("bf.markdown.parse", counted)
    monkeypatch.setattr("bf.validate.parse", counted)
    assert validate(brain)["valid"]
    assert sorted(parsed) == ["concepts/evidence.md", "concepts/log.md", "projects/offline.md"]
