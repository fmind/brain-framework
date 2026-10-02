"""Portable identities, link evidence and selected-brain boundaries through real files and SQLite."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from mcp.types import CallToolResult

from bf import index, links, pages
from bf.collect import collect
from bf.config import load
from bf.evaluate import evaluate
from bf.markdown import note, section
from bf.mcp import server
from bf.models import Error, NotFoundError, Query, Record, encode
from bf.records import path as record_path
from bf.retrieve import read, search
from bf.storage import Store, writer
from bf.validate import validate
from conftest import records_file
from test_interfaces import invoke

CONFIG = b"""version: 7
name: fixture
fields:
  friend:
    description: Explicit friendship, from subject to target.
    type: identity
    relation: true
  author:
    description: Explicit author, not owner.
    type: identity
    cardinality: many
    relation: true
  weight:
    description: Reviewed weight, not inferred confidence.
    type: number
"""


def people(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    brain.write(
        "projects/alice.md",
        b"""---
type: person
entity: bf://fixture/people/alice
aliases: [person:alice]
---
# Alice
## Relationships {#friends}
[Bob](bf://fixture/people/bob?rel=friend)
""",
    )
    brain.write(
        "projects/bob.md",
        b"""---
type: person
entity: bf://fixture/people/bob
aliases: [person:bob]
---
# Bob
## About Bob {#about}
A reviewed profile.
""",
    )


def test_bf_links_percent_encode_spaces_and_percent_signs() -> None:
    # Destinations are read as written, so angle brackets do not make a space part of a BF address.
    for destination in ("<bf://fixture/projects/launch plan.md>", "bf://fixture/projects/100%.md"):
        with pytest.raises(Error, match=r"^invalid BF link; .* percent-encoded as %20 and %25"):
            note("projects/web.md", f"# Web\n\n[plan]({destination})\n".encode())
    encoded = note("projects/web.md", b"# Web\n\n[plan](bf://fixture/projects/launch%20plan.md)\n")
    assert encoded.targets == ["bf://fixture/projects/launch%20plan.md"]


@pytest.mark.parametrize(
    "value",
    [
        "bf:people/marc",
        "bf:///people/marc",
        "bf://user@brain/people/marc",
        "bf://brain:42/people/marc",
        "bf://brain/../outside",
        "bf://brain/%2e%2e/outside",
        "bf://brain/a/%2F../x",
        "bf://brain/a//b",
        "bf://brain/a\\b",
        "bf://brain/a%00b",
        "bf://brain/a#%0a",
        "bf://brain/a?rel=friend&rel=author",
        "bf://brain/a?owner=me",
        "bf://brain/a?rel=",
        "bf://brain/a?rel=friend&weight=%zz",
        "bf://brain/a%FF",
        "bf://brain/a?rel=friend&x",
        # Only the relationship is a link query: subjects, evidence and attributes are not claims a link makes.
        "bf://brain/a?rel=friend&weight=1",
        "bf://brain/a?subject=person:alice&rel=friend",
        "bf://brain/a?rel=friend&asserted-by=person:alice",
        "bf://brain/a?rel=friend&evidence=https%3A%2F%2Fexample.test",
        "bf://brain/a?rel=Friend",
    ],
)
def test_reject_ambiguous_and_unsafe_addresses(value: str) -> None:
    with pytest.raises(Error, match="invalid BF link"):
        links.parse(value)


def test_component_encoding_and_external_urls() -> None:
    uri = links.address("team", "mail:a/b#c?d%20", "fmind.dev")
    parsed = links.parse(uri)
    assert parsed is not None
    assert parsed.path == "mail:a/b#c?d%20"
    assert parsed.fragment == "fmind.dev"
    external = "https://example.test/a?rel=friend&source=x%26y#part"
    assert links.target(external) == external
    assert links.parse(external) is None
    with pytest.raises(Error, match="link relation"):
        links.identity("bf://team/people/marc?rel=friend")


def group(reply: dict, relation: str = "links") -> dict:
    """One relationship group of a read's backlinks; `links` groups untyped links."""
    return next(g for g in cast(list[dict], reply["backlinks"]) if g["relation"] == relation)


def explained(stores: list[Store], identity: str, ref: str) -> dict:
    """The item an identity search returns for `ref`, with the claims by which it links to the identity.

    Backlink previews name items only; the identity search explains each link.
    """
    items = cast(list[dict], search(stores, Query(text=identity, limit=50))["items"])
    return next(item for item in items if item["ref"] == ref)


def test_link_claims_backlinks_subjects_and_file_evidence(brain: Store) -> None:
    people(brain)
    bob = read([brain], "bf://fixture/people/bob")
    assert bob["ref"] == "projects/bob.md"
    items = group(bob, "friend")["items"]
    assert [i["ref"] for i in items] == ["projects/alice.md"]
    claim = explained([brain], "person:bob", "projects/alice.md")["relations"][0]
    assert claim == {
        "subject": "bf://fixture/people/alice",
        "relation": "friend",
        "target": "bf://fixture/people/bob",
        "origin": "bf://fixture/projects/alice.md#friends",
    }
    assert "Bob" in str(read([brain], claim["origin"])["text"])
    assert read([brain], "person:bob")["backlinks"] == bob["backlinks"]
    # A typed claim is also listed on its subject, where it was asserted.
    alice = read([brain], "person:alice")
    assert alice["claims"] == [claim]
    assert alice["backlinks"] == []
    # The identity's scope holds its owning note and what links to it, with the linking claims. The note answers
    # its own title before its "About Bob" section, which names Bob under that title too.
    scoped = search([brain], Query(text="bob", target="person:bob"))["items"]
    assert [(i["ref"], i.get("relations")) for i in cast(list[dict], scoped)] == [
        ("projects/bob.md", None),
        ("projects/alice.md#friends", [claim]),
    ]
    assert validate(brain)["valid"]
    assert read([brain], "bf://fixture/people/bob#about")["text"] == "## About Bob {#about}\nA reviewed profile.\n"
    assert read([brain], "bf://fixture/people/bob?rel=friend#about")["ref"] == "projects/bob.md#about"


def test_independent_support_and_no_claims_from_frontmatter_fields(brain: Store) -> None:
    people(brain)
    brain.write(
        "projects/evidence.md",
        b"""---
fields:
  friend: [person:bob]
---
# Evidence
## Meeting
[Bob](bf://fixture/people/bob?rel=friend)
""",
    )
    # Each file supports its own claim; frontmatter `fields` are ordinary data, not relationships.
    friends = group(read([brain], "person:bob"), "friend")
    assert friends["total"] == 2
    assert "projects/evidence.md" in {i["ref"] for i in friends["items"]}
    evidence = explained([brain], "person:bob", "projects/evidence.md")
    assert evidence["relations"] == [
        {
            "subject": "bf://fixture/projects/evidence.md",
            "relation": "friend",
            "target": "bf://fixture/people/bob",
            "origin": "bf://fixture/projects/evidence.md#meeting",
        }
    ]
    subjects = cast(list[dict], read([brain], "person:alice")["claims"])
    assert [(c["relation"], c["origin"]) for c in subjects] == [("friend", "bf://fixture/projects/alice.md#friends")]
    brain.delete("projects/evidence.md")
    assert group(read([brain], "person:bob"), "friend")["total"] == 1
    with writer(brain):
        brain.delete(index.CACHE)
    assert group(read([brain], "person:bob"), "friend")["total"] == 1
    brain.write("projects/alice.md", b"# Alice\nNo assertion remains.\n")
    assert read([brain], "person:bob")["backlinks"] == []


@pytest.mark.parametrize("path", ["projects/new.md", "concepts/new.md", "actions/2026-09-27_new/ACTION.md"])
def test_okf_source_claims_keep_their_file_origin(brain: Store, path: str) -> None:
    people(brain)
    brain.write(
        path,
        b"---\ntype: note\nsources:\n  - resource: bf://fixture/people/bob?rel=friend\n---\n# Source\n",
    )
    friends = group(read([brain], "person:bob"), "friend")
    assert path in {item["ref"] for item in friends["items"]}
    evidence = explained([brain], "person:bob", path)
    assert evidence["relations"] == [
        {
            "subject": f"bf://fixture/{path}",
            "relation": "friend",
            "target": "bf://fixture/people/bob",
            "origin": f"bf://fixture/{path}",
        }
    ]
    assert validate(brain)["valid"]


def test_links_with_removed_attributes_are_reported(brain: Store) -> None:
    people(brain)
    brain.write("projects/bad.md", b"# Old\n[Bob](bf://fixture/people/bob?rel=friend&subject=person:alice)\n")
    assert search([brain], Query(text="old")).get("problems")
    assert any("invalid BF link" in str(p) for p in cast(list, validate(brain)["problems"]))


def test_federation_and_no_implicit_brain_access(brain: Store, tmp_path: Path) -> None:
    people(brain)
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", CONFIG.replace(b"name: fixture", b"name: team"))
    team.write("projects/shared.md", b"# Shared\n[Bob](bf://fixture/people/bob?rel=friend)\n")
    with pytest.raises(Error, match="not found"):
        read([team], "person:bob")
    reply = read([brain, team], "person:bob")
    assert {(i["brain"], i["ref"]) for i in group(reply, "friend")["items"]} == {
        ("fixture", "projects/alice.md"),
        ("team", "projects/shared.md"),
    }
    # An identity without an owning note still lists what links to it across the selected brains.
    team.write("projects/carol.md", b"# Met Carol\n[Carol](person:carol)\n")
    records_file(brain, "mail", [Record(id="c", title="From Carol", links=["person:carol"])])
    orphan = read([brain, team], "person:carol")
    assert orphan["page"] == "person:carol"
    assert {(i["brain"], i["ref"]) for i in group(orphan)["items"]} == {
        ("fixture", "mail:c"),
        ("team", "projects/carol.md"),
    }
    with pytest.raises(Error, match="not found"):
        read([brain, team], "person:nobody")
    with pytest.raises(Error, match="unknown brain"):
        read([team], "bf://fixture/people/bob")
    assert read([brain, team], "bf://fixture/people/bob")["brain"] == "fixture"
    assert validate(team)["unresolved"] == ["bf://fixture/people/bob"]
    team.write("projects/collision.md", b"---\naliases: [person:bob]\n---\n# A different Bob\n")
    with pytest.raises(Error, match="several brains"):
        read([brain, team], "person:bob")
    ambiguous = search([brain, team], Query(text="bob", target="person:bob"))
    assert ambiguous.get("problems")
    assert not any(i["brain"] == "team" for i in cast(list[dict], ambiguous["items"]))
    assert group(read([brain, team], "bf://fixture/people/bob"), "friend")["items"]


def test_validation_and_skip_invalid_links(brain: Store) -> None:
    people(brain)
    brain.write("projects/bad.md", b"# Broken\n[Unknown](bf://fixture/people/nobody?rel=friend)\n")
    assert not validate(brain)["valid"]
    brain.write("projects/bad.md", b"# Broken\n[Bob](bf://fixture/people/bob?rel=unmapped)\n")
    # One mistyped relation never hides its note: retrieval keeps the link untyped, validation names it.
    found = search([brain], Query(text="broken"))
    assert [item["ref"] for item in cast(list, found["items"])] == ["projects/bad.md"]
    assert "problems" not in found
    assert (
        "projects/bad.md: link relation unmapped is undeclared; declare it in bf.yaml fields with relation: true"
        in [f"{p['file']}: {p['error']}" for p in cast(list, validate(brain)["problems"])]
    )
    backlinks = cast(list, read([brain], "bf://fixture/people/bob")["backlinks"])
    assert ("links", 1) in [(group["relation"], group["total"]) for group in backlinks]
    assert "unmapped" not in [group["relation"] for group in backlinks]
    brain.write("projects/bad.md", b"---\nentity: bf://other/people/bob\n---\n# Wrong authority\n")
    assert not validate(brain)["valid"]
    brain.write("projects/bad.md", b"# Broken\n[Bob](bf://fixture/people/bob#missing)\n")
    assert any("section" in str(p) for p in cast(list, validate(brain)["problems"]))


@pytest.mark.parametrize("record_id", ["a/b#c?d", "https://example.test/a//../b\\name#? "])
def test_record_ids_preserve_delimiters_and_require_no_cache_for_exact_read(brain: Store, record_id: str) -> None:
    records_file(brain, "mail", [Record(id=record_id, title="Exact")])
    ref = links.address("fixture", "mail:" + record_id)
    assert cast(dict, read([brain], ref)["record"])["id"] == record_id
    with pytest.raises(Error, match="Markdown"):
        read([brain], ref + "#part")


def test_explicit_anchors_are_stable_and_duplicates_fail() -> None:
    data = b"# Directory\n## Marc Renamed {#marc}\nDetails\n## Website {#fmind.dev}\nSite\n"
    parsed = note("projects/directory.md", data)
    assert parsed.slugs == {"directory", "marc", "fmind.dev"}
    assert section("projects/directory.md", data, "marc").endswith("Details\n")
    for data in [b"# A {#same}\n# B {#same}\n", b"# Same\n# B {#same}\n"]:
        with pytest.raises(Error, match="duplicate explicit"):
            note("projects/directory.md", data)


def test_sensor_link_failure_is_atomic(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG + b"sensors:\n  fake:\n    command: [fake-provider]\n")
    with pytest.raises(Error, match="invalid BF link"):
        collect(
            brain,
            "fake",
            start="2026-01-01T00:00:00Z",
            end="2026-02-01T00:00:00Z",
            runner=lambda *_: encode(
                [
                    {"id": "good", "title": "Good"},
                    {"id": "bad", "title": "Bad", "links": ["bf://fixture/people/bob?rel=friend&weight=wrong"]},
                ]
            ),
        )
    assert not brain.files("memories/fake")


def test_cli_mcp_and_evals_expose_links(brain: Store) -> None:
    people(brain)
    cli = invoke("read", "bf://fixture/people/bob")
    assert cli["ref"] == "projects/bob.md"

    async def call() -> dict:
        result = await server([brain]).call_tool("read", {"ref": "bf://fixture/people/bob"})
        assert isinstance(result, CallToolResult)
        return cast(dict, result.structured_content)

    assert asyncio.run(call())["backlinks"] == cli["backlinks"]
    brain.write(
        "evals/links.yaml",
        b"version: 7\ncases:\n- name: friend\n  read: person:bob\n  expect: [projects/alice.md]\n"
        b"- name: stranger\n  read: person:nobody\n  empty: true\n",
    )
    assert evaluate(brain)["passed"]


def test_explanations_preserve_origin_and_report_truncation(brain: Store) -> None:
    people(brain)
    body = "---\ntype: person\nentity: bf://fixture/people/alice\n---\n# Alice\n" + "\n".join(
        f"## Event {i}\nMet at event {i}: [Bob](bf://fixture/people/bob?rel=friend)" for i in range(51)
    )
    brain.write("projects/alice.md", body.encode())
    assert group(read([brain], "person:bob"), "friend")["items"][0]["ref"] == "projects/alice.md"
    result = explained([brain], "person:bob", "projects/alice.md")
    assert result["relations_truncated"]
    assert len(result["relations"]) == 50
    claim = result["relations"][0]
    assert claim["origin"].startswith("bf://fixture/projects/alice.md#event-")
    assert "Met at event" in str(read([brain], claim["origin"])["text"])


def test_same_brain_ambiguity_does_not_expand_and_qualified_refs_still_work(brain: Store) -> None:
    people(brain)
    brain.write("projects/other.md", b"---\naliases: [person:bob]\n---\n# Another Bob\n")
    result = search([brain], Query(text="bob", target="person:bob"))
    assert result["problems"]
    assert result["items"] == []
    with pytest.raises(Error, match="ambiguous"):
        read([brain], "person:bob")
    assert group(read([brain], "bf://fixture/people/bob"), "friend")["items"]
    assert not validate(brain)["valid"]


def test_shared_aliases_never_merge_backlinks_and_entity_sections_link(brain: Store, tmp_path: Path) -> None:
    people(brain)
    brain.write("projects/about.md", b"# About\nSee [Bob's profile](bf://fixture/people/bob#about).\n")
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", CONFIG.replace(b"name: fixture", b"name: team"))
    team.write("projects/other.md", b"---\naliases: [person:bob]\n---\n# Another Bob\n")
    team.write("projects/talk.md", b"# Talk\n[Bob](person:bob)\n")
    alone = {i["ref"] for g in cast(list[dict], read([brain], "projects/bob.md")["backlinks"]) for i in g["items"]}
    assert alone == {"projects/alice.md", "projects/about.md"}
    # person:bob also names a note in team: it identifies neither Bob, so team's link to it is not a backlink.
    reply = read([brain, team], "bf://fixture/projects/bob.md")
    linked = {(i["brain"], i["ref"]) for g in cast(list[dict], reply["backlinks"]) for i in g["items"]}
    assert ("team", "projects/talk.md") not in linked
    assert "claimed elsewhere" in str(reply["problems"])


def test_canonical_record_backlinks_keep_evidence(brain: Store) -> None:
    reply = read([brain], "bf://fixture/meetings:decision-1")
    assert reply["ref"] == "meetings:decision-1"
    assert group(reply)["items"][0]["ref"] == "projects/offline.md"
    item = explained([brain], "bf://fixture/meetings:decision-1", "projects/offline.md")
    assert item["relations"][0]["target"] == "meetings:decision-1"
    scoped = search([brain], Query(text="decided", target="bf://fixture/meetings:decision-1"))["items"]
    assert [i["ref"] for i in cast(list[dict], scoped)] == ["projects/offline.md"]


def test_repeated_links_in_one_section_are_one_claim(brain: Store) -> None:
    people(brain)
    brain.write(
        "projects/carol.md",
        b"# Carol\n## Friends\n[Bob](bf://fixture/people/bob?rel=friend) and again [Bob](bf://fixture/people/bob?rel=friend)\n",
    )
    assert "projects/carol.md" in {i["ref"] for i in group(read([brain], "person:bob"), "friend")["items"]}
    assert len(explained([brain], "person:bob", "projects/carol.md")["relations"]) == 1


def test_notes_cannot_claim_another_brains_alias(brain: Store) -> None:
    brain.write(
        "projects/foreign.md",
        b"---\naliases: [bf://other/people/alice]\n---\n# Foreign ownership\n",
    )
    result = validate(brain)
    assert not result["valid"]
    assert "own brain namespace" in str(result["problems"])
    found = search([brain], Query(text="foreign ownership"))
    assert found["items"] == []
    assert "own brain namespace" in str(found["problems"])
    # Exact bytes remain available to repair the invalid note.
    assert "Foreign ownership" in str(read([brain], "projects/foreign.md")["text"])


def problems(store: Store) -> list[str]:
    return [f"{p['file']}: {p['error']}" for p in cast("list[dict[str, str]]", validate(store)["problems"])]


def test_display_name_aliases_never_resolve_and_only_okf_notes_reject_them(brain: Store) -> None:
    # Imported Markdown, such as an Obsidian note, often carries display-name aliases; ordinary Markdown ignores them.
    attachment = "actions/2026-01-01_demo/inputs/imported.md"
    brain.write("actions/2026-01-01_demo/ACTION.md", b"---\ntype: action\nstatus: draft\n---\n# Demo\n")
    brain.write(attachment, b"---\naliases: [Imported Meeting]\n---\n# Imported\n\nattachmentneedle\n")
    assert [item["ref"] for item in cast(list, search([brain], Query(text="attachmentneedle"))["items"])] == [
        attachment
    ]
    assert validate(brain)["valid"]
    # A display name never resolves to the file, unlike a namespaced alias.
    with pytest.raises(NotFoundError):
        read([brain], "Imported Meeting")
    brain.write("projects/site.md", b"---\ntype: project\naliases: [Website]\n---\n# Site\n\nprojectneedle\n")
    found = cast(list, search([brain], Query(text="projectneedle"))["items"])
    assert [item["ref"] for item in found] == ["projects/site.md"]
    with pytest.raises(NotFoundError):
        read([brain], "Website")
    assert any(
        p.startswith("projects/site.md: ") and "OKF aliases must be namespaced identities" in p for p in problems(brain)
    )


def test_body_links_keep_their_written_form(brain: Store) -> None:
    alias = "person:email/éric@example.test"
    brain.write("concepts/eric.md", f"---\ntype: person\naliases: [{alias}]\n---\n# Éric\n".encode())
    brain.write("projects/web.md", f"---\ntype: project\n---\n# Web\n\n## Team\n\n[Éric]({alias})\n".encode())
    ids = ["docs/Réunion.txt", "docs/Meeting notes.txt", "docs/100%.txt"]
    records_file(brain, "local", [Record(id=value, title=f"Document {n}") for n, value in enumerate(ids)])
    brain.write(
        "projects/docs.md",
        f"---\ntype: project\n---\n# Docs\n\n[a](local:{ids[0]}) [b](<local:{ids[1]}>) [c](local:{ids[2]})\n".encode(),
    )
    assert validate(brain)["valid"]
    eric = read([brain], alias)
    assert eric["ref"] == "concepts/eric.md"
    assert [i["ref"] for i in group(eric)["items"]] == ["projects/web.md"]
    scoped = cast(list[dict], search([brain], Query(text="team", target=alias))["items"])
    assert [i["ref"] for i in scoped] == ["projects/web.md#team"]
    for value in ids:
        assert [i["ref"] for i in group(read([brain], f"local:{value}"))["items"]] == ["projects/docs.md"]
    # BF addresses are parsed as written, in the body as in frontmatter: a bare % is malformed encoding.
    for data in (
        b"# Bad\n\n[c](bf://fixture/projects/100%.md)\n",
        b'---\nlinks: ["bf://fixture/projects/100%.md"]\n---\n# Bad\n',
    ):
        brain.write("projects/bad.md", data)
        assert any(p.startswith("projects/bad.md: invalid BF link") for p in problems(brain))


def test_ordinary_markdown_declares_no_identity_or_membership(brain: Store) -> None:
    people(brain)
    brain.write("actions/2026-09-27_import/ACTION.md", b"---\ntype: action\n---\n# Import\n")
    capture = "actions/2026-09-27_import/inputs/vendor-doc.md"
    brain.write(
        capture,
        b"---\nentity: bf://fixture/people/alice\naliases: [person:bob]\ntags: [website, team/web]\n"
        b"status: published\ntitle: 2024\n---\n# Vendor capture\n\nokapi facts.\n\n## Claims\n\n"
        b"[Bob](bf://fixture/people/bob?rel=friend)\n",
    )
    assert validate(brain)["valid"]
    # The copied document neither shadows an identity nor asserts claims in another entity's name.
    assert read([brain], "bf://fixture/people/alice")["ref"] == "projects/alice.md"
    assert read([brain], "person:bob")["ref"] == "projects/bob.md"
    assert capture in {i["ref"] for i in group(read([brain], "person:bob"), "friend")["items"]}
    assert explained([brain], "person:bob", capture)["relations"][0]["subject"] == f"bf://fixture/{capture}"
    assert read([brain], "bf://fixture/tags/website")["items"] == []
    found = cast(list[dict], search([brain], Query(text="okapi"))["items"])
    assert [(i["ref"], i["title"]) for i in found] == [(capture, "Vendor capture")]
    # Valid retrieval metadata still applies, so a retired decision output stays retired.
    decision = note(
        "actions/2026-09-27_import/outputs/decision.md", b"---\ntype: decision\nstatus: deprecated\n---\n# Old\n"
    )
    assert (decision.knowledge.type, decision.knowledge.status) == ("decision", "deprecated")
    # Only display and lifecycle metadata apply: frontmatter links and review dates stay the document's own data.
    helper = note(
        "actions/2026-09-27_import/inputs/doc.md",
        b'---\ndescription: Vendor summary\nlinks: ["jira:PROJ-9"]\nstale_after: 2026-10-01T00:00:00Z\n---\n# D\n',
    )
    assert (helper.lead, helper.targets, helper.knowledge.stale_after) == ("Vendor summary", [], "")


def test_an_overlong_or_blank_title_fails_only_okf_notes() -> None:
    heading = b"# " + b"x" * 4097 + b"\n\nbody\n"
    with pytest.raises(Error, match=r"concepts/long\.md: title exceeds 4096 characters"):
        note("concepts/long.md", heading)
    # A copied document stays searchable under its file name.
    assert note("actions/2026-09-27_import/inputs/long-capture.md", heading).title == "long capture"
    assert note("concepts/long.md", b"# " + b"x" * 4096 + b"\n").title == "x" * 4096
    # An image-only heading or a blank frontmatter title leaves no words either.
    for blank in (b'# <img src="logo.png" alt="Logo">\n\nwalrus\n', b'---\ntitle: "  "\n---\nwalrus\n'):
        with pytest.raises(Error, match=r"concepts/blank\.md: title must be nonempty"):
            note("concepts/blank.md", blank)
        assert note("actions/2026-09-27_import/inputs/read-me.md", blank).title == "read me"


def test_a_note_alias_repeating_a_record_ref_is_ambiguous(brain: Store) -> None:
    records_file(brain, "jira", [Record(id="PROJ-1", title="Launch issue")])
    records_file(brain, "mail", [Record(id="m1", title="About the launch", links=["jira:PROJ-1"])])
    brain.write("projects/launch.md", b"---\ntype: project\naliases: [jira:PROJ-1]\n---\n# Launch\n")
    assert problems(brain) == ["projects/launch.md: ambiguous identity jira:PROJ-1: jira:PROJ-1, projects/launch.md"]
    # The exact record and links to its ref stay visible; the ambiguous alias adds the note to neither.
    scoped = search([brain], Query(text="launch", target="jira:PROJ-1"))
    assert [i["ref"] for i in cast(list[dict], scoped["items"])] == ["jira:PROJ-1", "mail:m1"]
    assert "ambiguous" in str(scoped["problems"])
    launch = read([brain], "projects/launch.md")
    assert "claimed elsewhere" in str(launch["problems"])
    assert not launch.get("backlinks")


def test_the_same_record_in_two_brains_keeps_qualified_reads_exact(brain: Store, tmp_path: Path) -> None:
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 7\nname: team\n")
    for store in (brain, team):
        records_file(store, "jira", [Record(id="PROJ-1", title="Launch issue")])
    brain.write("projects/launch.md", b"---\ntype: project\n---\n# Launch\n\n[Issue](jira:PROJ-1)\n")
    reply = read([brain, team], "bf://fixture/jira:PROJ-1")
    assert "problems" not in reply
    assert [i["ref"] for i in group(reply)["items"]] == ["projects/launch.md"]


def test_page_addresses_are_link_targets(brain: Store) -> None:
    brain.write("actions/2026-09-27_import/ACTION.md", b"---\ntype: action\n---\n# Import\n")
    brain.write("projects/archive/old.md", b"---\ntype: project\n---\n# Old\n")
    decision = record_path("meetings", "decision-1")
    paths = [
        *("", "tasks", "tags", "tags/retention", "memories", "memories/meetings", "memories/meetings/2026-08"),
        *("memories/meetings/undated", decision),
        *("projects", "projects/archive", "concepts", "actions", "actions/2026-09-27_import"),
        *("today", "7d", "2026-09", "2026-09-27"),
    ]
    body = " ".join(f"[{n}](bf://fixture/{path})" for n, path in enumerate(paths))
    brain.write("projects/hub.md", f"---\ntype: project\n---\n# Hub\n\nhubneedle {body}\n".encode())
    assert validate(brain)["valid"]
    assert [i["ref"] for i in cast(list[dict], search([brain], Query(text="hubneedle"))["items"])] == [
        "projects/hub.md"
    ]
    for path in paths:
        assert read([brain], f"bf://fixture/{path}")
    brain.write(
        "projects/hub.md",
        b"---\ntype: project\n---\n# Hub\n\n[a](bf://fixture/projects/absent) [b](bf://fixture/memories/absent)\n"
        b"[c](bf://fixture/projects#part) [d](bf://fixture/meetings:decision-1#part)\n",
    )
    assert problems(brain) == [
        "projects/hub.md: record cannot select a Markdown section: bf://fixture/meetings:decision-1#part",
        "projects/hub.md: unresolved BF target: bf://fixture/memories/absent",
        "projects/hub.md: unresolved BF target: bf://fixture/projects#part",
        "projects/hub.md: unresolved BF target: bf://fixture/projects/absent",
    ]
    with pytest.raises(Error, match="invalid BF link"):
        links.parse("bf://fixture/#part")


def test_page_links_follow_the_routing_of_bf_read(brain: Store) -> None:
    # Each target is a page `bf read` rejects or that no evidence backs, so validation reports it.
    absent = "memories/meetings/" + "0" * 64 + ".json"
    failing = [
        *("tags/retention/extra", "2026-13", "2026-02-30"),
        *("memories/meetings/garbage", "memories/meetings/2026-08/extra", "memories/unknown"),
    ]
    window = "\u0661\u0662h"  # 12h in Arabic-Indic digits
    paths = [*failing, "tags/unused", absent, window]
    body = " ".join(f"[{n}](bf://fixture/{path})" for n, path in enumerate(paths))
    brain.write("projects/hub.md", f"---\ntype: project\n---\n# Hub\n\n{body}\n".encode())
    assert problems(brain) == sorted(
        f"projects/hub.md: unresolved BF target: {links.address('fixture', path)}" for path in paths
    )
    for path in failing:
        with pytest.raises(Error):
            read([brain], f"bf://fixture/{path}")
    # These open empty pages, so a misspelled tag or record file would otherwise go unnoticed.
    assert read([brain], "bf://fixture/tags/unused")["items"] == []
    assert read([brain], f"bf://fixture/{absent}")["items"] == []
    # Page routing and the reserved namespace agree on ASCII digits: this unowned entity path is no timeline.
    assert pages.period(window) is None
    assert not links.computed(f"bf://fixture/{window}")
    assert "since" not in read([brain], f"bf://fixture/{window}")


@pytest.mark.parametrize(
    "entity",
    [
        *("bf://fixture/", "bf://fixture/today", "bf://fixture/2026-09-27", "bf://fixture/12h", "bf://fixture/tasks"),
        *("bf://fixture/projects", "bf://fixture/tags", "bf://fixture/tags/x", "bf://fixture/memories/team"),
    ],
)
def test_page_addresses_cannot_be_claimed(brain: Store, entity: str) -> None:
    brain.write("concepts/claim.md", f"---\ntype: person\nentity: {entity}\n---\n# Claim\n".encode())
    assert problems(brain) == [
        (
            "concepts/claim.md: home, folder roots, tasks, periods, tags and memories are computed page addresses "
            "and cannot be entities or aliases; link to the page instead"
        )
    ]
    brain.delete("concepts/claim.md")
    records_file(brain, "mail", [Record(id="x", title="X", aliases=[entity])])
    assert any("computed page addresses and cannot be aliases" in p for p in problems(brain))


def test_record_addresses_cannot_be_claimed(brain: Store) -> None:
    # bf read resolves a first path segment with ':' as a source:id record, so such a claim never reaches its note.
    brain.write("concepts/team.md", b"---\ntype: concept\nentity: bf://fixture/team:core\n---\n# Team\n")
    brain.write("concepts/web.md", b"---\ntype: concept\naliases: [bf://fixture/team:web/app]\n---\n# Web\n")
    record = "a BF path whose first segment contains ':' names a source:id record"
    assert problems(brain) == [
        f"concepts/{name}.md: {record} and cannot be an entity or alias; link to the record instead"
        for name in ("team", "web")
    ]
    brain.delete("concepts/team.md")
    brain.delete("concepts/web.md")
    records_file(brain, "mail", [Record(id="x", title="X", aliases=["bf://fixture/team:core"])])
    assert any(f"{record} and cannot be an alias" in p for p in problems(brain))
    # A colon in a later segment names an ordinary entity path.
    brain.write("concepts/alice.md", b"---\ntype: person\nentity: bf://fixture/people/alice:ops\n---\n# Alice\n")
    assert not any("concepts/alice.md" in p for p in problems(brain))


def test_folders_and_tag_membership_are_not_claimed(brain: Store) -> None:
    people(brain)
    brain.write("projects/archive/old.md", b"---\ntype: project\n---\n# Old\n")
    brain.write("concepts/archive.md", b"---\ntype: concept\nentity: bf://fixture/projects/archive\n---\n# Archive\n")
    assert problems(brain) == [
        "concepts/archive.md: identity bf://fixture/projects/archive is also a folder page; rename the entity or folder"
    ]
    brain.delete("concepts/archive.md")
    brain.write(
        "projects/tagged.md", b"---\ntype: project\n---\n# Tagged\n\n[x](bf://fixture/tags/x?rel=tagged-with)\n"
    )
    assert any("tagged-with is reserved for tag membership" in p for p in problems(brain))
    brain.delete("projects/tagged.md")
    records_file(brain, "mail", [Record(id="t", title="Tagged", fields={"tagged-with": "bf://fixture/tags/x"})])
    assert any("field tagged-with is reserved" in p for p in problems(brain))
    # bf.yaml cannot declare it either.
    field = "  tagged-with: {description: Tags, type: identity, relation: true}\n"
    brain.write("bf.yaml", b"version: 7\nname: fixture\nfields:\n" + field.encode())
    with pytest.raises(Error, match=r"fields\.tagged-with is reserved for tag membership"):
        load(brain)
    # `links` names the backlink group and role page of untyped links.
    brain.write("bf.yaml", b"version: 7\nname: fixture\nfields:\n" + field.replace("tagged-with", "links").encode())
    with pytest.raises(Error, match=r"fields\.links is reserved for untyped backlinks"):
        load(brain)


def test_okf_sources_cite_their_resources(brain: Store) -> None:
    people(brain)
    brain.write(
        "concepts/lesson.md",
        b"---\ntype: concept\nentity: bf://fixture/lessons/offline\nsources:\n"
        b"  - resource: ../projects/offline.md#decision\n  - resource: meetings:decision-1\n---\n"
        b"# Lesson\n\nSee [the decision](../projects/offline.md).\n",
    )
    # A resource the note also links in its body is one backlink, under `cites`; the record links it untyped.
    project = read([brain], "projects/offline.md")
    assert [(g["relation"], [i["ref"] for i in g["items"]]) for g in cast(list[dict], project["backlinks"])] == [
        ("cites", ["concepts/lesson.md"]),
        ("links", ["meetings:decision-1"]),
    ]
    assert invoke("read", "meetings:decision-1", "--rel", "cites")["items"][0]["ref"] == "concepts/lesson.md"
    claims = cast(list[dict], read([brain], "bf://fixture/lessons/offline")["claims"])
    assert [(c["relation"], c["target"], c["origin"]) for c in claims if c["relation"] == "cites"] == [
        ("cites", "bf://fixture/projects/offline.md#decision", "bf://fixture/concepts/lesson.md"),
        ("cites", "meetings:decision-1", "bf://fixture/concepts/lesson.md"),
    ]
    # A link can name the built-in role too; a read following it reports nothing undeclared.
    brain.write("projects/plan.md", b"---\ntype: plan\n---\n# Plan\n\n[Bob](bf://fixture/people/bob?rel=cites)\n")
    assert group(read([brain], "person:bob"), "cites")["items"][0]["ref"] == "projects/plan.md"
    assert "problems" not in read([brain], "bf://fixture/people/bob?rel=cites")
    assert validate(brain)["valid"]
    field = "  cites: {description: Derived from, type: identity, relation: true}\n"
    brain.write("bf.yaml", b"version: 7\nname: fixture\nfields:\n" + field.encode())
    with pytest.raises(Error, match=r"fields\.cites is reserved for OKF sources"):
        load(brain)


def test_an_okf_resource_names_its_note(brain: Store) -> None:
    url = "https://github.com/example/site/pull/42"
    brain.write("concepts/pull.md", f"---\ntype: change\nresource: {url}\n---\n# Pull 42\n\nMerged.\n".encode())
    records_file(brain, "github", [Record(id="pr-42", title="Merged", url=url)])
    reply = read([brain], url)
    # The URI the note describes is one of its identities: it opens the note, and records naming it link to it.
    assert reply["ref"] == "concepts/pull.md"
    groups = {group["relation"]: group for group in cast("list[dict]", reply["backlinks"])}
    assert [item["ref"] for item in groups["links"]["items"]] == ["github:pr-42"]
