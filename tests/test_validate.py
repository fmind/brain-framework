"""Validation reports every broken note, link and record at once, each located in its file."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, timedelta
from typing import cast

import pytest
from typer.testing import CliRunner

from bf import markdown
from bf.cli import app
from bf.config import load
from bf.markdown import note, parse, reference, validate_okf
from bf.models import MAX_PROMPT, Error, Query, Record, encode
from bf.records import path as record_path
from bf.retrieve import read, search
from bf.storage import LINKED, Store
from bf.validate import broken_links, validate, validate_action_markdown
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


def test_non_finite_note_attributes_are_invalid_not_unanswerable(brain: Store) -> None:
    # YAML reads .nan and .inf as floats, which strict JSON replies cannot carry.
    brain.write("bf.yaml", b"version: 10\nname: fixture\n")
    for value in (b".nan", b"-.inf", b"[1, .inf]"):
        brain.write("projects/weight.md", b"---\ntype: project\nattributes:\n  weight: " + value + b"\n---\n# Weight\n")
        assert "projects/weight.md: invalid frontmatter: attributes" in "\n".join(located(validate(brain)))
        # Retrieval skips the note and reports it; every other reply still encodes.
        reply = search(brain, Query(text="offline"))
        assert any(p.get("file") == "projects/weight.md" for p in cast("list[dict[str, str]]", reply["problems"]))
        encode(reply)
        encode(read(brain, "projects"))


def test_link_schema_and_identity_problems_name_their_file_once(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\nfields:\n  owner:\n    description: Owner.\n    targets: ['person:']\n",
    )
    brain.write("concepts/foreign.md", b"---\ntype: person\nentity: bf://other/people/alice\n---\n# Foreign\n")
    brain.write(
        "concepts/relation.md",
        b"---\ntype: concept\n---\n# Relation\n\n[x](bf://fixture/projects/offline.md?rel=BadRel)\n",
    )
    brain.write(
        "concepts/dup.md", b"---\ntype: concept\n---\n# Dup\n\n[x](../projects/absent.md) [y](evidence.md#missing)\n"
    )
    records_file(
        brain,
        "fake",
        [
            Record(id="fields", title="Fields", relations={"owner": "team:nope"}),
            Record(id="alias", title="Alias", aliases=["bf://other/items/alias"]),
            Record(id="linked", title="Linked", links=["bf://fixture/projects/absent.md"]),
        ],
    )
    brain.write("projects/cites.md", b"---\ntype: project\n---\n# Cites\n\n[record](fake:fields)\n")
    result = validate(brain)
    problems = located(result)
    assert f"{record_path('fake', 'fields')}: relation owner: identity outside the declared targets" in problems
    assert f"{record_path('fake', 'alias')}: BF aliases must belong to their own brain namespace" in problems
    assert f"{record_path('fake', 'linked')}: unresolved BF target: bf://fixture/projects/absent.md" in problems
    assert (
        "concepts/foreign.md: BF entities, aliases and resources must belong to their own brain namespace" in problems
    )
    assert (
        "concepts/relation.md: line 6: invalid BF link; use bf://brain/path?rel=relation#section, with rel as the only query, "
        "spaces and a literal % percent-encoded as %20 and %25, and without userinfo or traversal" in problems
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


def test_a_byte_order_mark_does_not_hide_frontmatter(brain: Store) -> None:
    data = b"\xef\xbb\xbf---\ntype: project\nstatus: draft\ntags: [beta]\n---\n# Bom project\n\nOkapi notes.\n"
    assert note("projects/bom.md", data).knowledge.tags == ["beta"]
    brain.write("projects/bom.md", data)
    assert validate(brain)["valid"]
    assert [i["ref"] for i in cast("list[dict]", search(brain, Query(text="okapi"))["items"])] == ["projects/bom.md"]
    assert cast("dict", read(brain, "bf://fixture/tags/beta"))["total"] == 1


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
    backlinks = cast("list[dict[str, object]]", read(brain, f"{bundle}/tables/customers.md")["backlinks"])
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


def test_a_link_to_an_alias_a_later_note_declares_is_no_missing_record(brain: Store) -> None:
    # Notes are checked in path order: the alias used to be registered only after the earlier note's links failed.
    brain.write(
        "projects/a-plan.md", b"---\ntype: project\nstatus: draft\n---\n# Plan\n\nSee [review](meetings:review).\n"
    )
    brain.write(
        "projects/z-review.md", b'---\ntype: project\nstatus: draft\naliases: ["meetings:review"]\n---\n# Review\n'
    )
    assert validate(brain)["problems"] == []
    # Exact retrieval resolves the alias the same way.
    assert read(brain, "meetings:review")["ref"] == "projects/z-review.md"


@pytest.mark.parametrize("path", ["projects/new.md", "concepts/new.md", "actions/2026-09-27_new/ACTION.md"])
def test_okf_source_relations_work_without_declaration_and_reject_reserved(brain: Store, path: str) -> None:
    brain.write(
        path,
        b"---\ntype: concept\nsources:\n  - resource: bf://fixture/projects/offline.md?rel=undeclared\n---\n# Source\n",
    )
    assert validate(brain)["valid"]
    brain.write(
        path,
        b"---\ntype: concept\nsources:\n  - resource: bf://fixture/projects/offline.md?rel=links\n---\n# Source\n",
    )
    assert any(p.startswith(f"{path}: ") and "untyped links need no ?rel=" in p for p in located(validate(brain)))


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
    brain.write("bf.yaml", b"version: 10\nname: fixture\nfields:\n  repository: {description: Repository.}\n")
    records_file(
        brain,
        "github",
        [
            Record(id="pr-1", title="Fix", relations={"repository": ["repo:github.com/Team/x"]}),
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
    # An atomic write killed before its rename leaves its temporary file beside the note.
    kill = (
        "import os, sys; from pathlib import Path; from bf.storage import Store; "
        "os.replace = lambda *_, **__: os._exit(9); "
        "Store(Path(sys.argv[1])).write('projects/x.md', b'# Edited\\n')"
    )
    killed = subprocess.run(  # noqa: S603 - synthetic interrupted Store.write in a temporary brain
        [sys.executable, "-c", kill, str(brain.root)],
        cwd=brain.root,
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
    b"version: 10\nname: fixture\nfields:\n"
    b'  owner:\n    description: Responsible person.\n    targets: ["person:email/"]\n'
    b"jobs:\n  calendar:\n    run: [gws]\n    emit: records\n"
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
    claims = cast("list[dict[str, str]]", read(brain, "projects/launch.md").get("claims", []))
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
        "projects/typed.md: ?rel= types bf:// links only; set the relation in relations instead: "
        "person:email/bob@example.test?rel=owner"
    ) in located(validate(brain))
    brain.write("projects/typed.md", b"---\ntype: project\n---\n# Typed\n\n[Event](calendar:primary/evt-9)\n")
    assert "projects/typed.md: missing record calendar:primary/evt-9" in located(validate(brain))


def test_notes_type_claims_in_relations_without_declaring_every_relation(brain: Store) -> None:
    brain.write("bf.yaml", GRAPH.replace(b"jobs:\n  calendar:\n    run: [gws]\n    emit: records\n", b""))
    brain.write(
        "projects/launch.md",
        b"---\ntype: project\nrelations:\n  owner: [person:email/bob@example.test]\n  partner: person:email/ada@example.test\n---\n# Launch\n\nShip it.\n",
    )
    assert validate(brain)["valid"]
    # The identity needs no note: its page lists the project under both declared and undeclared relations.
    page = read(brain, "person:email/bob@example.test")
    groups = {group["relation"]: group for group in cast("list[dict]", page["backlinks"])}
    assert [item["ref"] for item in groups["owner"]["items"]] == ["projects/launch.md"]
    partner_groups = {
        group["relation"]: group
        for group in cast("list[dict]", read(brain, "person:email/ada@example.test")["backlinks"])
    }
    assert [item["ref"] for item in partner_groups["partner"]["items"]] == ["projects/launch.md"]
    claims = cast("list[dict[str, str]]", read(brain, "projects/launch.md")["claims"])
    assert ("owner", "person:email/bob@example.test", "bf://fixture/projects/launch.md") in [
        (c["relation"], c["target"], c["origin"]) for c in claims
    ]
    # Constraints apply when declared; invalid identities and reserved relations fail frontmatter cleanly.
    for relations_yaml, error, indexed in (
        (b"  owner: Alice\n", "invalid frontmatter: relations", []),
        (
            b"  owner: [repo:example/x]\n",
            "owner link outside the declared targets: repo:example/x",
            ["projects/launch.md"],
        ),
        (b"  links: [person:email/bob@example.test]\n", "relations: relation links is reserved", []),
    ):
        brain.write(
            "projects/launch.md", b"---\ntype: project\nrelations:\n" + relations_yaml + b"---\n# Launch\n\nShip it.\n"
        )
        assert any(error in problem for problem in located(validate(brain))), located(validate(brain))
        assert [item["ref"] for item in cast("list", search(brain, Query(text="ship"))["items"])] == indexed
    # A declared relation at the top level asserts nothing: say where it belongs.
    brain.write("projects/launch.md", b"---\ntype: project\nowner: [person:email/bob@example.test]\n---\n# Launch\n")
    assert "projects/launch.md: owner: relations belong under relations:, such as relations: {owner: ...}" in located(
        validate(brain)
    )


def test_one_problem_never_hides_a_note_s_others_or_breaks_links_to_it(brain: Store) -> None:
    brain.write("projects/beta.md", b"---\ntype: project\nupdated: 2026-9-1\n---\n# Beta\n")
    brain.write(
        "projects/alpha.md", b"---\ntype: project\n---\n# Alpha\n\n[Beta](bf://fixture/projects/beta.md#beta)\n"
    )
    brain.write(
        "projects/gamma.md",
        b"---\ntype: project\n---\n# Gamma\n\n[Typed](bf://fixture/projects/alpha.md?rel=links), [gone](missing.md)\n"
        b"and [heading](alpha.md#nowhere).\n",
    )
    problems = {
        (problem["file"], problem["error"]) for problem in cast("list[dict[str, str]]", validate(brain)["problems"])
    }
    assert {file for file, _ in problems} == {"projects/beta.md", "projects/gamma.md"}
    assert any("updated" in error for file, error in problems if file == "projects/beta.md")
    assert {error for file, error in problems if file == "projects/gamma.md"} >= {
        "broken link: missing.md",
        "missing heading: alpha.md#nowhere",
    }
    assert any("?rel=links" in error for file, error in problems if file == "projects/gamma.md")


def test_claim_errors_neither_hide_each_other_nor_unname_the_note(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\nfields:\n  owner: {description: Owner., targets: ['person:']}\n",
    )
    brain.write(
        "concepts/alice.md",
        b"---\ntype: person\nentity: bf://fixture/people/alice\n---\n# Alice\n\n"
        b"[Offline](bf://fixture/projects/offline.md?rel=works-on) [Again](bf://fixture/projects/offline.md?rel=links)\n"
        b"[BadTarget](bf://fixture/projects/offline.md?rel=owner)\n\n## Work\n\nShips the website.\n",
    )
    brain.write(
        "projects/b.md",
        b"---\ntype: project\n---\n# B\n\n[Alice](bf://fixture/people/alice?rel=works-on), [her work](bf://fixture/people/alice#work)\n",
    )
    brain.write("projects/c.md", b"---\ntype: project\n---\n# C\n\n[Alice](bf://fixture/people/alice)\n")
    # Undeclared valid relations pass cleanly while invalid/reserved relations and target mismatches fail together.
    assert located(validate(brain)) == [
        "concepts/alice.md: line 7: untyped links need no ?rel=; remove ?rel=links",
        "concepts/alice.md: owner link outside the declared targets: bf://fixture/projects/offline.md",
    ]
    # Validation and retrieval agree: the entity still names the note.
    alice = read(brain, "bf://fixture/people/alice")
    groups = {g["relation"]: [i["ref"] for i in g["items"]] for g in cast("list[dict]", alice["backlinks"])}
    assert groups == {"works-on": ["projects/b.md"], "links": ["projects/c.md"]}


def test_a_problem_repeated_across_links_is_reported_once_where_it_first_occurs(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\nfields:\n  owner: {description: Owner., targets: ['person:']}\n",
    )
    # A reserved relation in frontmatter and 250 sections, beside as many owner claims outside declared targets.
    body = "".join(
        f"## Part {n}\n\n[Plan](bf://fixture/projects/offline.md?rel=links) "
        f"[Plan](bf://fixture/projects/offline.md?rel=owner)\n\n"
        for n in range(250)
    )
    frontmatter = "type: project\nlinks: ['bf://fixture/projects/offline.md?rel=links']"
    brain.write("projects/atlas.md", f"---\n{frontmatter}\n---\n# Atlas\n\n{body}".encode())
    brain.write("projects/zeta.md", b"---\ntype: project\n---\n# Zeta\n\n[Missing](../concepts/missing.md)\n")
    # Before, the 250 copies of each filled the 200 problems a reply lists, and zeta's broken link went unreported.
    assert located(validate(brain)) == [
        "projects/atlas.md: links: untyped links need no ?rel=; remove ?rel=links",
        "projects/zeta.md: broken link: ../concepts/missing.md",
        "projects/atlas.md: owner link outside the declared targets: bf://fixture/projects/offline.md",
    ]


def test_a_link_no_bf_address_can_hold_hides_neither_its_note_nor_other_problems(brain: Store) -> None:
    # A Windows path in a copied document, or a control character, names no BF address: the link is broken, while its
    # note stays searchable, keeps its other problems and still names its entity.
    brain.write(
        "projects/web.md",
        b"---\ntype: project\nentity: bf://fixture/people/webby\n---\n# Web\n\n"
        b"webneedle [Owner](bf://fixture/projects/offline.md?rel=links), [guide](docs\\guide.md), [x](a%01b.md)\n",
    )
    brain.write("projects/other.md", b"---\ntype: project\n---\n# Other\n\n[Webby](bf://fixture/people/webby)\n")
    vendor = "actions/2026-09-27_import/inputs/vendor.md"
    brain.write("actions/2026-09-27_import/ACTION.md", b"---\ntype: action\n---\n# Import\n")
    brain.write(vendor, b"# Vendor\n\nwinneedle [guide](docs\\guide.md)\n")
    assert located(validate(brain)) == [
        "projects/web.md: line 7: untyped links need no ?rel=; remove ?rel=links",
        "projects/web.md: broken link: a%01b.md",
        "projects/web.md: broken link: docs\\guide.md",
        f"{vendor}: broken link: docs\\guide.md",
    ]
    for word, ref in (("webneedle", "projects/web.md"), ("winneedle", vendor)):
        assert [i["ref"] for i in cast("list[dict]", search(brain, Query(text=word))["items"])] == [ref]
    webby = read(brain, "bf://fixture/people/webby")
    assert [i["ref"] for g in cast("list[dict]", webby["backlinks"]) for i in g["items"]] == ["projects/other.md"]


def test_every_problem_of_a_record_is_reported(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\nfields:\n  owner: {description: Owner., targets: ['person:']}\n",
    )
    brain.write(
        record_path("mail", "m1"),
        encode(
            {
                "id": "m1",
                "title": "Mail",
                "relations": {"owner": "team:nope", "links": "person:alice"},
                "links": ["bf://fixture/x?rel=links", "bf://fixture/y?rel=second"],
            }
        ),
    )
    # Raw corrupt record reports its validation error without crashing collection reads.
    assert located(validate(brain)) == [
        f"{record_path('mail', 'm1')}: invalid record: relations: relation links is reserved",
    ]
    records_file(
        brain,
        "mail",
        [
            Record(
                id="m1",
                title="Mail",
                relations={"owner": "team:nope"},
                links=["bf://fixture/x?rel=links", "bf://fixture/y?rel=second"],
            )
        ],
    )
    assert located(validate(brain)) == [
        f"{record_path('mail', 'm1')}: relation owner: identity outside the declared targets",
        f"{record_path('mail', 'm1')}: untyped links need no ?rel=; remove ?rel=links",
        f"{record_path('mail', 'm1')}: unresolved BF target: bf://fixture/x",
        f"{record_path('mail', 'm1')}: unresolved BF target: bf://fixture/y",
    ]
    # The ref bf:x of a source named bf reads as a malformed BF address: its file is named, the check goes on.
    records_file(brain, "bf", [Record(id="x", title="X")])
    assert [problem.split(": ", 1)[0] for problem in located(validate(brain))] == [
        record_path("bf", "x"),
        *[record_path("mail", "m1")] * 4,
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
        b"version: 10\nname: fixture\njobs:\n"
        b"  brief:\n    emit: records\n    run: [uv, run, --no-project, sensors/brief.py, '{{start}}']\n"
        b"  packaged:\n    emit: records\n    run: [uv, run, --project, sensors/news, news]\n"
        b"  piped:\n    emit: records\n    run: [sh, -c, sensors/a.sh | sensors/b.sh]\n"
        b"  review:\n    run: [python3, routines/review.py, routines/report.md]\n",
    )
    assert located(validate(brain)) == [
        "bf.yaml: jobs.brief.run: sensors/brief.py is not a regular file or folder",
        "bf.yaml: jobs.packaged.run: sensors/news is not a regular file or folder",
        "bf.yaml: jobs.review.run: routines/review.py is not a regular file or folder",
    ]
    # The interpreter runs the first brain path it is given, so it needs no executable bit; later arguments, such as
    # a file the routine may create, and shell command lines are not checked.
    brain.write("sensors/brief.py", b"print('[]')\n")
    brain.write("sensors/news/pyproject.toml", b"[project]\nname = 'news'\n")
    brain.write("routines/review.py", b"print('')\n")
    assert validate(brain)["valid"]


def test_an_enabled_job_gate_is_checked_like_any_program(brain: Store) -> None:
    gated = (
        b"version: 10\nname: fixture\njobs:\n  helper:\n    prompt: prompts/a.md\n    harness: [agent]\n"
        b"    on: {every: 300}\n"
        b'    gate: [routines/missing-gate.py, "{{lines:settings/missing.txt}}"]\n'
    )
    brain.write("bf.yaml", gated)
    brain.write("prompts/a.md", b"Review.\n")
    gate = "bf.yaml: jobs.helper.gate"
    assert located(validate(brain)) == [
        f"{gate}: routines/missing-gate.py is not an executable regular file",
        f"{gate}: settings/missing.txt does not exist; restore it or correct bf.yaml",
    ]
    # A disabled job runs no gate, like any disabled program.
    brain.write("bf.yaml", gated.replace(b"    harness: [agent]\n", b"    harness: [agent]\n    enabled: false\n"))
    assert validate(brain)["valid"]


def test_a_linked_folder_is_told_to_become_a_regular_folder(brain: Store) -> None:
    # Linking a synced folder into concepts/ is the common case: a regular file is not the only remedy.
    shared = brain.root.parent / "shared"
    shared.mkdir()
    (shared / "note.md").write_text("# Shared\n")
    (brain.root / "concepts/shared").symlink_to(shared, target_is_directory=True)
    assert located(validate(brain)) == [f"concepts/shared: {LINKED}"]
    assert {"file": "concepts/shared", "error": LINKED} in cast(
        "list[dict[str, str]]", search(brain, Query(text="offline"))["problems"]
    )


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


def test_validate_reports_a_prompt_file_every_run_would_reject(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\njobs:\n  assistant:\n    prompt: prompts/a.md\n    harness: [agent]\n",
    )
    brain.write("prompts/a.md", b"x" * (MAX_PROMPT + 1))
    problems = cast("list[dict[str, str]]", validate(brain)["problems"])
    assert {
        "file": "bf.yaml",
        "error": f"jobs.assistant.prompt: prompts/a.md exceeds {MAX_PROMPT} bytes",
    } in problems


def test_declarative_validate_policies_and_line_files(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\n"
        b"validate:\n"
        b"  max_stale_days: 30\n"
        b"  warn_stale_days: 7\n"
        b"  index_note: concepts/index.md\n"
        b"  required_status: [draft, stable]\n"
        b"  forbid_patterns:\n"
        b"    - '/Users/[A-Za-z0-9._-]+'\n"
        b"    - '^Done$'\n"
        b"jobs:\n"
        b"  scoped:\n"
        b'    run: [echo, "{{lines:settings/missing.txt}}"]\n',
    )
    brain.write("concepts/index.md", b"# Concepts\n")
    brain.write(
        "concepts/evidence.md",
        b"---\ntype: concept\nstale_after: 2099-01-01T00:00:00Z\n---\n# Evidence\n\nPath /Users/alice/secret.\n",
    )
    # Markdown, like an editor, breaks lines at CR/LF only, not at a form feed or U+2028 pasted from the web.
    soon = (date.today() + timedelta(days=3)).isoformat()
    brain.write(
        "concepts/drift.md",
        f"---\ntype: concept\nstale_after: {soon}T00:00:00Z\n---\n# Drift\r\n\r\nA\u2028B\x0cC\r\n\r\nPath /Users/bob\r\nDone\r\n".encode(),
    )
    report = validate(brain)
    errors = {f"{item['file']}: {item['error']}" for item in cast("list[dict[str, str]]", report["problems"])}
    assert "concepts/drift.md: line 9: matches forbidden pattern /Users/[A-Za-z0-9._-]+" in errors
    assert "concepts/drift.md: line 10: matches forbidden pattern ^Done$" in errors
    assert {"warning": "stale_after is within 7 days", "file": "concepts/drift.md", "stale_after": soon} in cast(
        "list[dict[str, str]]", report["warnings"]
    )
    assert "bf.yaml: jobs.scoped.run: settings/missing.txt does not exist; restore it or correct bf.yaml" in errors
    assert "concepts/index.md: missing link to concepts/evidence.md" in errors
    assert "concepts/evidence.md: frontmatter status must be explicitly set to one of: draft, stable" in errors
    assert "concepts/evidence.md: stale_after exceeds max_stale_days (30 days)" in errors
    assert "concepts/evidence.md: line 7: matches forbidden pattern /Users/[A-Za-z0-9._-]+" in errors


def test_action_folder_links_validate_sections_and_forbid_action_folder_identities(brain: Store) -> None:
    brain.write(
        "actions/2026-09-25_audit/ACTION.md",
        b"---\ntype: action\nstatus: draft\n---\n# Audit\n\n## Context {#context}\n\nScope.\n",
    )
    brain.write(
        "projects/good.md",
        b"---\ntype: project\n---\n# Good\n\nSee [ok](../actions/2026-09-25_audit#context) and "
        b"[bf](bf://fixture/actions/2026-09-25_audit#context).\n",
    )
    assert validate(brain)["valid"]

    brain.write(
        "projects/bad.md",
        b"---\ntype: project\nentity: bf://fixture/actions/2026-09-25_audit\n---\n# Bad\n\n"
        b"[rel](../actions/2026-09-25_audit#missing) [bf](bf://fixture/actions/2026-09-25_audit#missing)\n",
    )
    problems = located(result_bad := validate(brain))
    assert any("link to the action instead" in item for item in problems)
    assert "projects/bad.md: missing heading: ../actions/2026-09-25_audit#missing" in problems
    assert "projects/bad.md: missing BF section: bf://fixture/actions/2026-09-25_audit#missing" in problems
    assert not result_bad["valid"]
    assert CliRunner().invoke(app, ["validate", "--brain", str(brain.root)]).exit_code == 1


def test_missing_action_links_report_unresolved_with_exit_zero(brain: Store) -> None:
    # Actions are gitignored and local; notes pointing to absent action folders warn in `unresolved` without failing.
    brain.write(
        "projects/soft.md",
        b"---\ntype: project\n---\n# Soft\n\n"
        b"See [folder](../actions/2026-09-25_missing), [doc](../actions/2026-09-25_missing/ACTION.md#next), "
        b"and [bf](bf://fixture/actions/2026-09-25_other#next).\n",
    )
    result = validate(brain)
    assert result["valid"] is True
    assert result["problems"] == []
    assert set(cast("list[str]", result["unresolved"])) == {
        "actions/2026-09-25_missing",
        "actions/2026-09-25_missing/ACTION.md#next",
        "bf://fixture/actions/2026-09-25_other#next",
    }
    cli = CliRunner().invoke(app, ["validate", "--brain", str(brain.root)])
    assert cli.exit_code == 0, cli.output


def test_validate_policy_limits_canonical_actions_and_direct_helpers(brain: Store) -> None:
    assert broken_links(brain, note("projects/p.md", b"# P\n\n[self](#) [ext](repo:a)\n")) == []
    with pytest.raises(Error, match="exceeds 4 MiB"):
        validate_action_markdown(brain, load(brain), "actions/2026-09-25_big/ACTION.md", b"x" * (4 * 1024 * 1024 + 1))

    brain.write("settings/many.txt", "\n".join(f"arg-{i}" for i in range(130)).encode())
    brain.write("concepts/sec-target.md", b"---\ntype: concept\nstatus: draft\n---\n# Target\n\n## Ok\n")
    brain.write("actions/2026-09-25_audit/ACTION.md", b"---\ntype: action\nstatus: draft\n---\n# Audit\n")
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\n"
        b"fields:\n  follows:\n    description: Action relation.\n    targets: ['bf://fixture/actions/other']\n"
        b"defaults:\n  harness: [routines/missing.py]\n"
        b"validate:\n  index_note: concepts/missing-index.md\n"
        b"jobs:\n  expanded:\n    run: [echo, '{{lines:settings/many.txt}}']\n    cwd: settings/missing-dir\n"
        b"  bad-prompt:\n    prompt: prompts/missing-prompt.md\n"
        b"  valid-cwd:\n    run: [echo, ok]\n    cwd: concepts\n",
    )
    brain.write("memories/topics/.forgotten", b"{not json\n")
    brain.write(
        "projects/relcheck.md",
        b"---\ntype: project\nrelations:\n  owner: bf://fixture/meetings:lunch\n---\n"
        b"# RelCheck\n\n[follow](bf://fixture/actions/2026-09-25_audit?rel=follows)"
        b" [absent](bf://fixture/actions/2026-09-25_absent)"
        b" [sec](../concepts/sec-target.md#ok)"
        b" [lunchsec](bf://fixture/meetings:lunch#sec)\n",
    )
    records_file(brain, "mail", [Record(id="m1", title="Mail", links=["bf://fixture/actions/2026-09-25_absent"])])
    records_file(brain, "bf", [Record(id="badsrc", title="Reserved source")])

    problems = located(validate(brain))
    assert "bf.yaml: defaults.harness: routines/missing.py is not an executable regular file" in problems
    assert "bf.yaml: jobs.bad-prompt.prompt: prompts/missing-prompt.md is not a regular file" in problems
    assert "bf.yaml: jobs.expanded.cwd: settings/missing-dir is not a directory" in problems
    assert "bf.yaml: jobs.expanded.run: command arguments exceed 128 items after expanding line files" in problems
    assert "bf.yaml: validate.index_note: concepts/missing-index.md does not exist" in problems
    assert (
        "projects/relcheck.md: follows link outside the declared targets: bf://fixture/actions/2026-09-25_audit"
        in problems
    )
    assert "projects/relcheck.md: record cannot select a Markdown section: bf://fixture/meetings:lunch#sec" in problems
    assert any(
        p.startswith("memories/mail/") and "unresolved BF target: bf://fixture/actions/2026-09-25_absent" in p
        for p in problems
    )
    assert any(p.startswith("memories/bf/") and "reserved" in p for p in problems)
    assert any(p.startswith("memories/topics/.forgotten:") for p in problems)

    cfg = load(brain)
    validate_action_markdown(
        brain,
        cfg,
        "actions/2026-09-25_audit/ACTION.md",
        b"---\ntype: action\nstatus: draft\n---\n# Audit\n\n[ok](../../concepts/sec-target.md#ok)\n",
        require_draft=True,
    )
    with pytest.raises(Error, match="requires type: action"):
        validate_action_markdown(brain, cfg, "actions/2026-09-25_a/ACTION.md", b"---\ntype: project\n---\n# A\n")
    with pytest.raises(Error, match="requires status: draft"):
        validate_action_markdown(
            brain,
            cfg,
            "actions/2026-09-25_a/ACTION.md",
            b"---\ntype: action\nstatus: stable\n---\n# A\n",
            require_draft=True,
        )
    with pytest.raises(Error, match="relations belong under relations:"):
        validate_action_markdown(
            brain,
            cfg,
            "actions/2026-09-25_a/ACTION.md",
            b"---\ntype: action\nstatus: draft\nfollows: x\n---\n# A\n",
        )
    with pytest.raises(Error, match="and 1 more"):
        validate_action_markdown(
            brain,
            cfg,
            "actions/2026-09-25_a/ACTION.md",
            b"---\ntype: action\nstatus: draft\n---\n# A\n\n[a](missing-a.md) [b](missing-b.md)\n",
        )

    brain.write("concepts/bad-index.md", b"---\ntype: [invalid\n---\n")
    brain.write("bf.yaml", b"version: 10\nname: fixture\nvalidate:\n  index_note: concepts/bad-index.md\n")
    invalid_index_problems = located(validate(brain))
    assert "bf.yaml: validate.index_note: concepts/bad-index.md does not exist" not in invalid_index_problems
    assert any(p.startswith("concepts/bad-index.md:") for p in invalid_index_problems)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_validate_reports_inaccessible_record_and_forgotten_files(brain: Store) -> None:
    rec = record_path("meetings", "lunch")
    brain.write("memories/meetings/.forgotten", b"[]\n")
    (brain.root / rec).chmod(0o000)
    (brain.root / "memories/meetings/.forgotten").chmod(0o000)
    try:
        problems = located(validate(brain))
    finally:
        (brain.root / rec).chmod(0o600)
        (brain.root / "memories/meetings/.forgotten").chmod(0o600)
    assert f"{rec}: inaccessible file; check permissions" in problems
    assert "memories/meetings/.forgotten: inaccessible file; check permissions" in problems
