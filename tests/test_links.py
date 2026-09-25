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

CONFIG = b"""version: 4
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
  since:
    description: Stated start of a relationship.
    type: timestamp
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
[Bob](bf://fixture/people/bob?rel=friend&weight=1)
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
    with pytest.raises(Error, match="attributes"):
        links.identity("bf://team/people/marc?rel=friend")


def test_link_claims_backlinks_subjects_and_file_evidence(brain: Store) -> None:
    people(brain)
    result = search([brain], Query(target="bf://fixture/people/bob", relation="friend"))
    items = cast(list[dict], result["items"])
    assert [i["ref"] for i in items] == ["projects/alice.md"]
    claim = items[0]["relations"][0]
    assert claim == {
        "subject": "bf://fixture/people/alice",
        "relation": "friend",
        "target": "bf://fixture/people/bob",
        "evidence": "bf://fixture/projects/alice.md#friends",
        "attributes": {"weight": 1},
        "origin": "bf://fixture/projects/alice.md#friends",
    }
    assert "Bob" in str(read([brain], claim["evidence"])["text"])
    assert search([brain], Query(target="person:bob"))["items"] == items
    assert search([brain], Query(subject="person:alice", relation="friend"))["items"] == items
    assert validate(brain)["valid"]
    assert read([brain], "bf://fixture/people/bob#about")["text"] == "## About Bob {#about}\nA reviewed profile.\n"
    assert read([brain], "bf://fixture/people/bob?rel=friend#about")["ref"] == "projects/bob.md#about"


def test_explicit_subject_author_and_independent_support(brain: Store) -> None:
    people(brain)
    brain.write(
        "projects/evidence.md",
        b"""---
fields:
  author: [person:alice]
---
# Evidence
## Meeting
[Bob](bf://fixture/people/bob?rel=friend&subject=person:alice&asserted-by=person:alice&since=2026-01-01T00%3A00%3A00Z)
""",
    )
    result = search([brain], Query(target="person:bob", relation="friend"))
    assert len(cast(list, result["items"])) == 2
    assert (
        cast(list[dict], search([brain], Query(relation="author", target="person:alice"))["items"])[0]["ref"]
        == "projects/evidence.md"
    )
    brain.delete("projects/evidence.md")
    assert len(cast(list, search([brain], Query(target="person:bob"))["items"])) == 1
    with writer(brain):
        brain.delete(index.CACHE)
    assert len(cast(list, search([brain], Query(target="person:bob"))["items"])) == 1
    brain.write("projects/alice.md", b"# Alice\nNo assertion remains.\n")
    assert search([brain], Query(target="person:bob"))["items"] == []


def test_federation_and_no_implicit_brain_access(brain: Store, tmp_path: Path) -> None:
    people(brain)
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", CONFIG.replace(b"name: fixture", b"name: team"))
    team.write("projects/shared.md", b"# Shared\n[Bob](bf://fixture/people/bob?rel=friend)\n")
    assert search([team], Query(target="person:bob"))["items"] == []
    reply = search([brain, team], Query(target="person:bob", relation="friend"))
    assert {(i["brain"], i["ref"]) for i in cast(list[dict], reply["items"])} == {
        ("fixture", "projects/alice.md"),
        ("team", "projects/shared.md"),
    }
    with pytest.raises(Error, match="unknown brain"):
        read([team], "bf://fixture/people/bob")
    assert read([brain, team], "bf://fixture/people/bob")["brain"] == "fixture"
    assert validate(team)["unresolved"] == ["bf://fixture/people/bob"]
    team.write("projects/collision.md", b"---\naliases: [person:bob]\n---\n# A different Bob\n")
    ambiguous = search([brain, team], Query(target="person:bob"))
    assert ambiguous.get("problems")
    assert not any(i["brain"] == "team" for i in cast(list[dict], ambiguous["items"]))
    assert search([brain, team], Query(target="bf://fixture/people/bob"))["items"]


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
    with pytest.raises(Error, match="link attribute"):
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
    cli = invoke("search", "--subject", "person:alice", "--relation", "friend")

    async def call() -> dict:
        result = await server([brain]).call_tool("search", {"subject": "person:alice", "relation": "friend"})
        assert isinstance(result, CallToolResult)
        return cast(dict, result.structured_content)

    assert asyncio.run(call())["items"] == cli["items"]
    brain.write(
        "evals/links.yaml",
        b"version: 4\ncases:\n- name: friend\n  subject: person:alice\n  relation: friend\n  expect: [projects/alice.md]\n",
    )
    assert evaluate(brain)["passed"]
    assert invoke("read", "bf://fixture/people/bob")["ref"] == "projects/bob.md"


def test_explanations_preserve_origin_and_report_truncation(brain: Store) -> None:
    people(brain)
    body = "---\nentity: bf://fixture/people/alice\n---\n# Alice\n" + "\n".join(
        f"## Event {i}\n[Bob](bf://fixture/people/bob?rel=friend&evidence=https%3A%2F%2Fexample.test%2Fproof)"
        for i in range(51)
    )
    brain.write("projects/alice.md", body.encode())
    result = cast(list[dict], search([brain], Query(target="person:bob"))["items"])[0]
    assert result["relations_truncated"]
    assert len(result["relations"]) == 50
    claim = result["relations"][0]
    assert claim["evidence"] == "https://example.test/proof"
    assert claim["origin"].startswith("bf://fixture/projects/alice.md#event-")
    assert "example.test" in str(read([brain], claim["origin"])["text"])


def test_same_brain_ambiguity_does_not_expand_and_qualified_refs_still_work(brain: Store) -> None:
    people(brain)
    brain.write("projects/other.md", b"---\naliases: [person:bob]\n---\n# Another Bob\n")
    result = search([brain], Query(target="person:bob"))
    assert result["problems"]
    assert result["items"] == []
    assert search([brain], Query(target="bf://fixture/people/bob"))["items"]
    assert not validate(brain)["valid"]


def test_canonical_record_backlinks_keep_evidence(brain: Store) -> None:
    reply = search([brain], Query(target="bf://fixture/meetings:decision-1"))
    item = cast(list[dict], reply["items"])[0]
    assert item["ref"] == "projects/offline.md"
    assert item["relations"][0]["target"] == "meetings:decision-1"
