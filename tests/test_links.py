"""Portable identities, link evidence and selected-brain boundaries through real files and SQLite."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from mcp.types import CallToolResult

from bf import index, links
from bf.collect import collect
from bf.evaluate import evaluate
from bf.markdown import note, section
from bf.mcp import server
from bf.models import Error, Query, Record, encode
from bf.retrieve import read, search
from bf.storage import Store, writer
from bf.validate import validate
from conftest import records_file
from test_interfaces import invoke

CONFIG = b"""version: 5
name: fixture
schema:
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
entity: bf://fixture/people/bob
aliases: [person:bob]
---
# Bob
## About Bob {#about}
A reviewed profile.
""",
    )


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
    with pytest.raises(Error, match="relationship"):
        links.identity("bf://team/people/marc?rel=friend")


def group(reply: dict, relation: str = "") -> dict:
    """One relationship group of a read's backlinks; the empty relation selects untyped links."""
    return next(g for g in cast(list[dict], reply["backlinks"]) if g.get("relation", "") == relation)


def test_link_claims_backlinks_subjects_and_file_evidence(brain: Store) -> None:
    people(brain)
    bob = read([brain], "bf://fixture/people/bob")
    assert bob["ref"] == "projects/bob.md"
    items = group(bob, "friend")["items"]
    assert [i["ref"] for i in items] == ["projects/alice.md"]
    claim = items[0]["relations"][0]
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
    assert alice["claims"] == [{"brain": "fixture", **claim}]
    assert alice["backlinks"] == []
    scoped = search([brain], Query(text="bob", target="person:bob"))["items"]
    assert [(i["ref"], i["relations"]) for i in cast(list[dict], scoped)] == [("projects/alice.md#friends", [claim])]
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
    evidence = next(i for i in friends["items"] if i["ref"] == "projects/evidence.md")
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
    records_file(brain, "mail", "undated", [Record(id="c", title="From Carol", links=["person:carol"])])
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
    assert search([brain], Query(text="broken")).get("problems")
    assert not validate(brain)["valid"]
    brain.write("projects/bad.md", b"---\nentity: bf://other/people/bob\n---\n# Wrong authority\n")
    assert not validate(brain)["valid"]
    brain.write("projects/bad.md", b"# Broken\n[Bob](bf://fixture/people/bob#missing)\n")
    assert any("section" in str(p) for p in cast(list, validate(brain)["problems"]))


@pytest.mark.parametrize("record_id", ["a/b#c?d", "https://example.test/a//../b\\name#? "])
def test_record_ids_preserve_delimiters_and_require_no_cache_for_exact_read(brain: Store, record_id: str) -> None:
    records_file(brain, "mail", "undated", [Record(id=record_id, title="Exact")])
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
        b"version: 5\ncases:\n- name: friend\n  read: person:bob\n  expect: [projects/alice.md]\n"
        b"- name: stranger\n  read: person:nobody\n  empty: true\n",
    )
    assert evaluate(brain)["passed"]


def test_explanations_preserve_origin_and_report_truncation(brain: Store) -> None:
    people(brain)
    body = "---\nentity: bf://fixture/people/alice\n---\n# Alice\n" + "\n".join(
        f"## Event {i}\nMet at event {i}: [Bob](bf://fixture/people/bob?rel=friend)" for i in range(51)
    )
    brain.write("projects/alice.md", body.encode())
    result = group(read([brain], "person:bob"), "friend")["items"][0]
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
    item = group(reply)["items"][0]
    assert item["ref"] == "projects/offline.md"
    assert item["relations"][0]["target"] == "meetings:decision-1"
    scoped = search([brain], Query(text="decided", target="bf://fixture/meetings:decision-1"))["items"]
    assert [i["ref"] for i in cast(list[dict], scoped)] == ["projects/offline.md"]


def test_repeated_links_in_one_section_are_one_claim(brain: Store) -> None:
    people(brain)
    brain.write(
        "projects/carol.md",
        b"# Carol\n## Friends\n[Bob](bf://fixture/people/bob?rel=friend) and again [Bob](bf://fixture/people/bob?rel=friend)\n",
    )
    item = next(i for i in group(read([brain], "person:bob"), "friend")["items"] if i["ref"] == "projects/carol.md")
    assert len(item["relations"]) == 1
