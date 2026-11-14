"""Schema-less relations, optional bf.yaml targets/broader rules, @type, and reprojection."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import cast

import pytest
from mcp.types import CallToolResult
from pydantic import ValidationError
from typer.testing import CliRunner

from bf import index, ontology, records
from bf.cli import app
from bf.collect import collect
from bf.config import load
from bf.evaluate import evaluate
from bf.markdown import note
from bf.mcp import server
from bf.models import Config, Error, Knowledge, Query, Record, SchemaField, encode
from bf.retrieve import read, search
from bf.storage import Store, writer
from bf.validate import validate
from test_interfaces import invoke

CONFIG = b"""version: 10
name: fixture
fields:
  sender:
    description: Account that sent the message.
  recipient:
    description: Explicit recipients of this message.
jobs:
  mail:
    run: [fake-provider]
    emit: records
"""


def linking(brain: Store, identity: str, relation: str) -> list[str]:
    """Refs linking to an identity through one explicit relation, from its read."""
    groups = cast("list[dict]", read(brain, identity)["backlinks"])
    return [i["ref"] for g in groups if g.get("relation") == relation for i in g["items"]]


def ingest(brain: Store, values: list[dict], sensor: str = "mail") -> dict:
    return collect(
        brain, sensor, start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z", runner=lambda *_: encode(values)
    )


def test_undeclared_camel_and_kebab_relations_work_and_rebuild_cleanly(brain: Store) -> None:
    brain.write("bf.yaml", b"version: 10\nname: fixture\njobs:\n  mail:\n    run: [fake-provider]\n    emit: records\n")
    brain.write("projects/alice.md", b"---\ntype: person\naliases: [person:alice, account:alice]\n---\n# Alice\n")
    brain.write(
        "projects/note.md",
        b"---\ntype: project\nrelations:\n  depends-on: bf://fixture/projects/alice.md\n---\n"
        b"# Note\n\n[Assignee](person:bob?rel=assignedTo) invalid-inline\n",
    )
    brain.write(
        "projects/inline.md",
        b"---\ntype: project\n---\n# Inline\n\n[Owner](bf://fixture/projects/alice.md?rel=worksWith)\n",
    )
    first = {"id": "one", "title": "Planning", "relations": {"assignedTo": "person:alice", "blocked-by": "person:bob"}}
    second = {"id": "two", "title": "Reply", "relations": {"worksWith": "account:alice"}}
    assert ingest(brain, [first, second])["added"] == 2
    assert linking(brain, "person:alice", "assignedTo") == ["mail:one"]
    assert set(linking(brain, "account:alice", "worksWith")) == {"mail:two", "projects/inline.md"}
    assert linking(brain, "person:alice", "depends-on") == ["projects/note.md"]
    assert linking(brain, "person:bob", "blocked-by") == ["mail:one"]
    # Rebuild .bf/cache.sqlite3 cleanly from authoritative files alone.
    with writer(brain):
        brain.delete(index.CACHE)
    assert linking(brain, "person:alice", "assignedTo") == ["mail:one"]
    first["relations"] = {"assignedTo": "person:carol"}
    ingest(brain, [first])
    assert linking(brain, "person:alice", "assignedTo") == []
    assert set(linking(brain, "person:alice", "worksWith")) == {"mail:two", "projects/inline.md"}
    with writer(brain):
        records.upsert(brain, "mail", [], snapshot=True)
    assert linking(brain, "person:alice", "worksWith") == ["projects/inline.md"]


def test_relation_naming_boundaries_and_invisible_characters_are_enforced() -> None:
    for valid in ("owner", "assignedTo", "depends-on", "a1-b2"):
        Record(id="x", title="Ok", relations={valid: "person:alice"})
        Knowledge(type="concept", relations={valid: "person:alice"})
    for invalid in ("Assigned", "bad--name", "bad_name", "-lead", "tail-", "links", "tagged-with"):
        with pytest.raises(ValidationError):
            Record(id="x", title="Bad", relations={invalid: "person:alice"})
    for identity, ok in [
        ("person:alice", True),
        ("person:ali\u200bce", False),
        ("person:alice\ufeff", False),
        ("bf://fixture/people/ali%E2%80%8Bce", False),
        ("https://example.test/ali%E2%80%8Bce", True),
    ]:
        if ok:
            Record(id="x", title="Ok", links=[identity])
        else:
            with pytest.raises(ValidationError, match=r"invisible|format|invalid BF link"):
                Record(id="x", title="Bad", relations={"owner": identity})
    Record(id="x", title="Ok", links=["https://example.test/ali%E2%80%8Bce"])


def test_attributes_and_item_type_stay_distinct_from_graph_relations(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    brain.write(
        "concepts/plan.md",
        b"---\ntype: concept\nattributes:\n  note_ref: person:ghost\n  bf_ref: bf://fixture/projects/ghost.md\n  tag_word: sapphire\n---\n# Plan\n",
    )
    ingest(
        brain,
        [
            {
                "id": "one",
                "title": "Planning",
                "@type": "Message",
                "relations": {"sender": "person:alice"},
                "attributes": {
                    "category": "urgent",
                    "ghost": "person:ghost",
                    "bf_ref": "bf://fixture/projects/ghost.md",
                },
            },
            {"id": "two", "title": "Plain", "attributes": {"category": "urgent"}},
        ],
    )
    # Attributes are indexed without their keys; @type appears only when present; no "type": "record" leaks.
    found = {
        item["ref"]: (item.get("@type"), "type" in item)
        for item in cast("list[dict]", search(brain, Query(text="urgent"))["items"])
    }
    assert found == {
        "mail:one": ("Message", False),
        "mail:two": (None, False),
    }
    assert search(brain, Query(text="category tag_word"))["items"] == []
    assert search(brain, Query(text="sapphire"))["items"]
    exact_one = cast("dict", read(brain, "mail:one")["record"])
    exact_two = cast("dict", read(brain, "mail:two")["record"])
    assert exact_one["@type"] == "Message"
    assert exact_one["relations"] == {"sender": "person:alice"}
    assert exact_one["attributes"]["category"] == "urgent"
    assert "@type" not in exact_two
    assert "relations" not in exact_two
    exported_run = CliRunner().invoke(app, ["export", "--brain", str(brain.root), "--kind", "items"])
    assert exported_run.exit_code == 0, exported_run.output
    exported = {
        item["ref"]: item for item in (json.loads(line) for line in exported_run.stdout.splitlines() if line.strip())
    }
    assert exported["mail:one"]["@type"] == "Message"
    assert "@type" not in exported["mail:two"]
    assert "type" not in exported["mail:one"]
    # Attribute values shaped like identities or BF URIs never create graph edges.
    with pytest.raises(Error, match="not found"):
        read(brain, "person:ghost")
    assert "claims" not in read(brain, "concepts/plan.md")


def test_invalid_batch_writes_nothing_and_rejects_legacy_fields(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    good = {"id": "one", "title": "Good", "relations": {"sender": "person:alice"}}
    bad = {"id": "two", "title": "Bad", "relations": {"sender": "PRIVATE-SECRET"}}
    with pytest.raises(Error, match="relations") as caught:
        ingest(brain, [good, bad])
    assert "PRIVATE-SECRET" not in str(caught.value)
    assert records.files(brain, "mail") == []
    with pytest.raises(Error, match="not fields"):
        ingest(brain, [{**good, "fields": {"sender": "person:mallory"}}])


def test_optional_field_rules_validate_targets_and_broader_without_legacy_keys() -> None:
    for rejected in (
        {"description": "X", "type": "identity"},
        {"description": "X", "cardinality": "one"},
        {"description": "X", "relation": True},
        {"description": "X", "examples": ["person:a"]},
    ):
        with pytest.raises(ValidationError):
            SchemaField.model_validate(rejected)
    for schema, message in [
        ({"cites": {"description": "R."}}, "reserved for OKF sources"),
        ({"links": {"description": "R."}}, "reserved for untyped"),
        ({"tagged-with": {"description": "R."}}, "reserved for tag"),
        ({"owner": {"broader": "owner"}}, r"fields\.owner\.broader"),
        ({"owner": {"broader": "absent"}}, r"fields\.owner\.broader"),
        ({"a": {}, "b": {"broader": "a"}, "c": {"broader": "b"}}, r"fields\.c\.broader"),
        ({"owner": {"targets": ["Person:"]}}, "targets"),
        ({"owner": {"targets": ["person"]}}, "targets"),
        ({"owner": {"targets": []}}, "targets"),
    ]:
        with pytest.raises(ValidationError, match=message):
            Config.model_validate({"version": 10, "name": "test", "fields": schema})
    with pytest.raises(ValidationError):
        Config.model_validate({"version": 10, "name": "test", "jobs": {"x": {"run": ["fake"], "fields": {}}}})
    with pytest.raises(ValidationError):
        Config.model_validate({"version": 9, "name": "old"})


TARGETED = CONFIG.replace(
    b"    description: Account that sent the message.\n",
    b'    description: Account that sent the message.\n    targets: ["person:", "bf://fixture/people/"]\n',
)


def test_targets_fail_collection_atomically_and_validate_existing_evidence(brain: Store) -> None:
    brain.write("bf.yaml", TARGETED)
    first = {"id": "one", "title": "Planning", "relations": {"sender": "person:alice"}}
    ingest(brain, [first, {"id": "two", "title": "Team", "relations": {"sender": "bf://fixture/people/bob"}}])
    stored = {name: brain.read(name) for name in records.files(brain, "mail")}
    outside = {"id": "three", "title": "Team", "relations": {"sender": "team:PRIVATE-TEAM"}}
    with pytest.raises(Error, match=r"relation sender: identity outside the declared targets") as caught:
        ingest(brain, [{**first, "title": "Changed"}, outside])
    assert "PRIVATE-TEAM" not in str(caught.value)
    linked = {**first, "links": ["bf://fixture/teams/PRIVATE?rel=sender"]}
    with pytest.raises(Error, match=r"link relation sender: identity outside the declared targets") as caught:
        ingest(brain, [linked])
    assert "PRIVATE" not in str(caught.value)
    assert {name: brain.read(name) for name in records.files(brain, "mail")} == stored
    # Widening or narrowing targets in bf.yaml reports existing violations while preserving evidence and zero rebuild.
    brain.write("bf.yaml", CONFIG)
    ingest(brain, [{"id": "four", "title": "Team mail", "relations": {"recipient": "team:core"}}])
    brain.write("concepts/bob.md", b"---\ntype: person\naliases: [bf://fixture/people/bob]\n---\n# Bob\n")
    brain.write("concepts/core.md", b"---\ntype: team\nentity: bf://fixture/teams/core\n---\n# Core\n")
    brain.write(
        "projects/plan.md", b"---\ntype: project\n---\n# Plan\n\n[Core](bf://fixture/teams/core?rel=recipient)\n"
    )
    assert validate(brain)["valid"]
    index.refresh(brain)
    brain.write(
        "bf.yaml",
        TARGETED.replace(
            b"    description: Explicit recipients of this message.\n",
            b'    description: Explicit recipients of this message.\n    targets: ["person:"]\n    broader: sender\n',
        ),
    )
    assert index.refresh(brain)["changed"] == 0
    problems = cast("list[dict]", validate(brain)["problems"])
    assert problems == [
        {"file": records.path("mail", "four"), "error": "relation recipient: identity outside the declared targets"},
        {"file": "projects/plan.md", "error": "recipient link outside the declared targets: bf://fixture/teams/core"},
    ]
    # Evidence stays readable under its stored relation, and relation pages roll up narrower relations at read time.
    assert linking(brain, "team:core", "recipient") == ["mail:four"]
    assert [i["ref"] for i in cast("list[dict]", read(brain, "team:core", rel="sender")["items"])] == ["mail:four"]
    assert [i["ref"] for i in cast("list[dict]", read(brain, "bf://fixture/teams/core", rel="sender")["items"])] == [
        "projects/plan.md"
    ]


def test_cli_mcp_and_evaluation_share_relation_pages(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    ingest(brain, [{"id": "one", "title": "Mail", "relations": {"sender": "person:alice"}}])
    cli = invoke("read", "person:alice", "--brain", str(brain.root))
    assert cli["page"] == "person:alice"

    async def call() -> dict:
        result = await server(brain).call_tool("read", {"ref": "person:alice"})
        assert isinstance(result, CallToolResult)
        return cast("dict", result.structured_content)

    assert asyncio.run(call())["backlinks"] == cli["backlinks"]
    assert (
        invoke("search", "mail", "--scope", "person:alice", "--brain", str(brain.root))["items"][0]["ref"] == "mail:one"
    )
    brain.write(
        "evals/relations.yaml",
        b"version: 10\ncases:\n- name: sender\n  read: person:alice\n  expect: [mail:one]\n  text: [sender]\n",
    )
    brain.write(
        "evals/nested/empty.yml",
        b"version: 10\ncases:\n- name: absent\n  read: person:unknown\n  empty: true\n",
    )
    assert evaluate(brain)["score"] == "2/2"
    assert evaluate(brain, "evals/relations.yaml")["score"] == "1/1"
    for params in [{"text": "x", "relation": "sender"}, {"text": "x", "target": "Alice"}]:
        with pytest.raises(ValidationError):
            Query.model_validate(params)
    brain.write(
        "evals/duplicate.yaml", b"version: 10\ncases:\n- name: twice\n  empty: true\n- name: twice\n  empty: true\n"
    )
    with pytest.raises(Error, match="duplicate"):
        evaluate(brain)


def test_reproject_revalidates_records_and_keeps_target_failures_unchanged(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain.write("bf.yaml", CONFIG)
    for item in ("one", "two", "three"):
        brain.write(
            records.path("mail", item),
            json.dumps(
                {
                    "id": item,
                    "title": f"Item {item}",
                    "time": "2026-01-01T00:00:00Z",
                    "relations": {"sender": "person:alice"},
                }
            ).encode(),
        )
    before = {name: brain.read(name) for name in records.files(brain, "mail")}
    preview = invoke("build", "--brain", str(brain.root), "--reproject", "mail", "--dry-run")
    assert preview == {
        "job": "mail",
        "source": "mail",
        "dry_run": True,
        "records": 3,
        "changed": 0,
        "unchanged": 3,
        "failed": 0,
    }
    assert {name: brain.read(name) for name in records.files(brain, "mail")} == before
    recovered: list[Store] = []
    monkeypatch.setattr(records, "recover", lambda store: recovered.append(store))
    done = invoke("build", "--brain", str(brain.root), "--reproject", "mail")
    assert recovered
    assert (done["changed"], done["unchanged"], done["failed"]) == (0, 3, 0)
    assert cast("dict", done["index"])["skipped"] == 0
    stored = records.load(brain, records.path("mail", "one"))
    assert stored.relations == {"sender": "person:alice"}
    # A record violating newly added targets stays untouched on disk and exits with code 1.
    brain.write(
        records.path("mail", "two"), encode({"id": "two", "title": "Bad", "relations": {"sender": "team:nope"}})
    )
    bad_before = brain.read(records.path("mail", "two"))
    brain.write("bf.yaml", TARGETED)
    reply = invoke("build", "--brain", str(brain.root), "--reproject", "mail", code=1)
    assert (reply["records"], reply["changed"], reply["unchanged"], reply["failed"]) == (3, 0, 2, 1)
    assert brain.read(records.path("mail", "two")) == bad_before
    for args, code in ((["--dry-run"], 2), (["--reproject", "Mail"], 2), (["--reproject", "absent"], 1)):
        assert invoke("build", "--brain", str(brain.root), *args, code=code) == {}


def test_identity_search_deduplicates_links_and_config_follows_edits(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    first = load(brain)
    assert load(brain) is first
    with pytest.raises(ValidationError):
        setattr(first, "name", "other")  # noqa: B010 - exercise the frozen model at runtime
    ingest(
        brain, [{"id": "one", "title": "Planning", "links": ["person:alice"], "relations": {"sender": "person:alice"}}]
    )
    items = cast("list[dict]", search(brain, Query(text="person:alice"))["items"])
    assert [(item["ref"], item["relations"]) for item in items] == [
        (
            "mail:one",
            [
                {
                    "subject": "bf://fixture/mail:one",
                    "relation": "sender",
                    "target": "person:alice",
                    "origin": "bf://fixture/mail:one",
                }
            ],
        )
    ]
    brain.write("bf.yaml", CONFIG.replace(b"  mail:\n", b"  post:\n"))
    assert set(load(brain).jobs) == {"post"}


def test_ontology_direct_checks_reject_reserved_and_invalid_relations(brain: Store) -> None:
    config = load(brain)
    bad_record = Record.model_construct(id="r1", title="T", relations={"tagged-with": "person:a", "owner": "bad-id"})
    record_errors: list[str] = []
    ontology.validate(bad_record, config, errors=record_errors)
    assert record_errors[0] == "relation tagged-with is reserved"
    assert record_errors[1].startswith("relation owner:")

    clean_record = Record.model_construct(
        id="r2", title="T2", relations={"tagged-with": "person:a", "owner": "person:a"}, aliases=[]
    )
    assert ontology.relations(clean_record, config) == [("owner", "person:a")]

    parsed = replace(
        note("concepts/n.md", b"---\ntype: concept\n---\n# N\n"),
        knowledge=Knowledge.model_construct(
            type="concept",
            relations={"tagged-with": "person:a", "owner": "bad-id"},
        ),
    )
    assert ontology.note_claims(parsed, config, strict=False) == []
    note_errors: list[str] = []
    assert ontology.note_claims(parsed, config, strict=True, errors=note_errors) == []
    assert note_errors[0] == "concepts/n.md: relations.tagged-with: relation tagged-with is reserved"
    assert note_errors[1].startswith("concepts/n.md: relations.owner:")
