"""Schema contracts preserve relations and make the derived graph recoverable from records."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from mcp.types import CallToolResult
from pydantic import JsonValue, ValidationError

from bf import index, ontology, records
from bf.collect import collect
from bf.config import load
from bf.evaluate import evaluate
from bf.mcp import server
from bf.models import Config, Error, Mapping, Query, Record, SchemaField, encode
from bf.retrieve import read
from bf.retrieve import search as retrieve_search
from bf.storage import Store, writer
from bf.validate import validate
from test_interfaces import invoke


def search(stores: list[Store], query: Query) -> dict:
    return cast(dict, retrieve_search(stores, query))


CONFIG = b"""version: 7
name: fixture
fields:
  sender:
    description: Account that sent the message.
    type: identity
    cardinality: one
    relation: true
    examples: [person:alice]
  recipient:
    description: Explicit recipients of this message.
    type: identity
    cardinality: many
    relation: true
    examples: [[person:bob, person:carol]]
  label:
    description: Reviewed category.
    type: string
  kind:
    description: Common record kind.
    type: string
    cardinality: one
sensors:
  mail:
    command: [fake-provider]
    fields:
      sender: {path: /attributes/from}
      recipient: {path: /attributes/to}
      label: {path: /attributes/category}
      kind: {value: message}
"""


def linking(brain: Store, identity: str, relation: str) -> list[str]:
    """Refs linking to an identity through one explicit relation, from its read."""
    groups = cast(list[dict], read([brain], identity)["backlinks"])
    return [i["ref"] for g in groups if g.get("relation") == relation for i in g["items"]]


def ingest(brain: Store, values: list[dict]) -> dict:
    return collect(
        brain, "mail", start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z", runner=lambda *_: encode(values)
    )


def test_relations_replacement_aliases_and_cache_rebuild(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    brain.write("projects/alice.md", b"---\ntype: person\naliases: [person:alice, account:alice]\n---\n# Alice\n")
    first = {"id": "one", "title": "Planning", "attributes": {"from": "person:alice", "to": ["person:bob"]}}
    second = {"id": "two", "title": "Reply", "attributes": {"from": "person:bob", "to": ["person:alice"]}}
    assert ingest(brain, [first, second])["added"] == 2
    assert linking(brain, "person:alice", "sender") == ["mail:one"]
    assert linking(brain, "account:alice", "sender") == ["mail:one"]
    assert linking(brain, "person:alice", "recipient") == ["mail:two"]
    assert search([brain], Query(text="message"))["items"]
    assert cast(dict, read([brain], "mail:one")["record"])["fields"] == {
        "sender": "person:alice",
        "recipient": ["person:bob"],
        "kind": "message",
    }
    assert validate(brain)["valid"]
    with writer(brain):
        brain.delete(index.CACHE)
    assert linking(brain, "person:alice", "sender") == ["mail:one"]
    first["attributes"]["from"] = "person:carol"
    ingest(brain, [first])
    assert linking(brain, "person:alice", "sender") == []
    assert linking(brain, "person:alice", "recipient") == ["mail:two"]
    with writer(brain):
        records.upsert(brain, "mail", [], snapshot=True)
    assert read([brain], "person:alice")["backlinks"] == []


def test_invalid_batch_writes_nothing_and_does_not_leak(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    good = {"id": "one", "title": "Good", "attributes": {"from": "person:alice"}}
    bad = {"id": "two", "title": "Bad", "attributes": {"from": "PRIVATE-SECRET"}}
    with pytest.raises(Error, match="field sender") as caught:
        ingest(brain, [good, bad])
    assert "PRIVATE-SECRET" not in str(caught.value)
    assert records.files(brain, "mail") == []
    with pytest.raises(Error, match="required mapped value"):
        ingest(brain, [{"id": "one", "title": "Missing"}])
    with pytest.raises(Error, match="not precomputed"):
        ingest(brain, [{**good, "fields": {"sender": "person:mallory"}}])


@pytest.mark.parametrize(
    ("kind", "value", "valid"),
    [
        ("integer", 2, True),
        ("integer", True, False),
        ("number", 1.5, True),
        ("number", "2", False),
        ("boolean", False, True),
        ("boolean", 1, False),
        ("timestamp", "2026-01-01T01:00:00+01:00", True),
        ("timestamp", "2026-01-01", False),
        ("identity", "person:alice", True),
        ("identity", "Alice", False),
        ("identity", "person:ali\u200bce", False),
        ("identity", "person:alice\ufeff", False),
        ("string", "", False),
        ("string", "a" * 8193, False),
        ("string", {}, False),
    ],
)
def test_scalar_contracts(kind: str, value: JsonValue, valid: bool) -> None:
    field = SchemaField.model_validate({"description": "Example", "type": kind})
    if valid:
        assert field.normalize(value) is not None
    else:
        with pytest.raises(ValueError, match=r"expected|requires|exceeds|nonempty|invisible"):
            field.normalize(value)


def test_config_contracts_and_pointer_escaping() -> None:
    with pytest.raises(ValidationError, match="relation fields"):
        SchemaField(description="Wrong", type="string", relation=True)
    with pytest.raises(ValidationError):
        SchemaField(description="Wrong", type="integer", examples=[True])
    field = SchemaField(description="Many", type="string", cardinality="many")
    assert field.normalize(["a", "a", "b"]) == ["a", "b"]
    invalid_values: list[JsonValue] = ["a", ["a"] * 1001, [1]]
    for invalid in invalid_values:
        with pytest.raises(ValueError, match=r"expected|requires|exceeds|nonempty"):
            field.normalize(invalid)
    for invalid in [{}, {"path": None}, {"path": "/x", "value": 1}, {"path": "x"}, {"path": "/~2"}, {"value": None}]:
        with pytest.raises(ValidationError):
            Mapping.model_validate(invalid)
    assert ontology.pointer({"a/b": {"~": ["ok"]}}, "/a~1b/~0/0") == "ok"
    assert ontology.pointer({}, "/missing") is None
    assert ontology.pointer([], "/1") is None
    for document, path in [(1, "/x"), ([], "/01"), ([], "/x")]:
        with pytest.raises(Error, match="cannot traverse"):
            ontology.pointer(document, path)
    with pytest.raises(ValidationError, match=r"sensors\.x\.fields\.missing: not declared in fields"):
        Config.model_validate(
            {
                "version": 7,
                "name": "test",
                "sensors": {"x": {"command": ["fake"], "fields": {"missing": {"value": "x"}}}},
            }
        )
    with pytest.raises(ValidationError):
        Config.model_validate({"version": 3, "name": "old"})


def test_schema_change_invalidates_cache_and_bad_record_is_reported(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    ingest(brain, [{"id": "one", "title": "Mail", "attributes": {"from": "person:alice"}}])
    assert linking(brain, "person:alice", "sender") == ["mail:one"]
    brain.write("bf.yaml", CONFIG.replace(b"relation: true", b"relation: false"))
    with pytest.raises(Error, match="not found"):
        read([brain], "person:alice")
    record = Record(id="bad", title="Invalid", fields={"undeclared": "x"})
    brain.write(
        "memories/bad/2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881.json", records.serialize(record)
    )
    reply = search([brain], Query(text="offline"))
    assert reply["items"]
    assert reply["problems"]
    assert not validate(brain)["valid"]
    with pytest.raises(Error, match="field sender"):
        ontology.validate(Record(id="bad", title="Bad", fields={"sender": 1}), load(brain))


def test_schema_edits_keep_every_stored_identity_edge(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    values = {"from": "person:alice", "to": ["person:bob"], "category": "urgent"}
    ingest(brain, [{"id": "one", "title": "Planning", "attributes": values}])
    assert linking(brain, "person:bob", "recipient") == ["mail:one"]
    # A declared relation keeps its stored identities whatever cardinality it now has.
    many = b"    cardinality: many\n    relation: true\n    examples: [[person:bob, person:carol]]\n"
    retyped = CONFIG.replace(many, b"    cardinality: optional\n    relation: true\n")
    brain.write("bf.yaml", retyped)
    assert linking(brain, "person:bob", "recipient") == ["mail:one"]
    assert "field recipient" in str(validate(brain)["problems"])
    # A value stored before its field became an identity names none, so it claims nothing.
    retyped = retyped.replace(b"    type: string\n  kind:", b"    type: identity\n    relation: true\n  kind:")
    brain.write("bf.yaml", retyped)
    reply = search([brain], Query(text="planning urgent", target="person:bob"))
    assert [item["ref"] for item in reply["items"]] == ["mail:one"]
    assert "problems" not in reply
    assert "field label" in str(validate(brain)["problems"])


def test_cli_mcp_and_evaluation_share_relationship_pages(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    ingest(brain, [{"id": "one", "title": "Mail", "attributes": {"from": "person:alice"}}])
    cli = invoke("read", "person:alice")
    assert cli["page"] == "person:alice"

    async def call() -> dict:
        result = await server([brain]).call_tool("read", {"ref": "person:alice"})
        assert isinstance(result, CallToolResult)
        return cast(dict, result.structured_content)

    assert asyncio.run(call())["backlinks"] == cli["backlinks"]
    assert invoke("search", "mail", "--scope", "person:alice")["items"][0]["ref"] == "mail:one"
    brain.write(
        "evals/relations.yaml",
        b"version: 7\ncases:\n- name: sender\n  read: person:alice\n  expect: [mail:one]\n  text: [sender]\n",
    )
    brain.write(
        "evals/nested/empty.yml",
        b"version: 7\ncases:\n- name: absent\n  read: person:unknown\n  empty: true\n",
    )
    assert evaluate(brain)["score"] == "2/2"
    assert evaluate(brain, "evals/relations.yaml")["score"] == "1/1"
    for params in [{"text": "x", "relation": "sender"}, {"text": "x", "target": "Alice"}]:
        with pytest.raises(ValidationError):
            Query.model_validate(params)
    brain.write(
        "evals/duplicate.yaml", b"version: 7\ncases:\n- name: twice\n  empty: true\n- name: twice\n  empty: true\n"
    )
    with pytest.raises(Error, match="duplicate"):
        evaluate(brain)


def test_field_values_are_searchable_words_without_their_keys(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    assert ingest(
        brain, [{"id": "one", "title": "Planning", "attributes": {"from": "person:alice", "category": "urgent"}}]
    )
    found = {str(i["ref"]) for i in search([brain], Query(text="urgent"))["items"]}
    assert "mail:one" in found
    # Field names and JSON punctuation are not words of the record: "label" and "kind" match nothing here.
    assert search([brain], Query(text="label kind"))["items"] == []


def test_loaded_configurations_are_shared_frozen_and_follow_edits(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    first = load(brain)
    assert load(brain) is first
    # One command shares this object, so neither the configuration nor its programs accept changes.
    with pytest.raises(ValidationError):
        setattr(first, "name", "other")  # noqa: B010 - exercise the frozen model at runtime
    with pytest.raises(ValidationError):
        setattr(first.sensors["mail"], "enabled", False)  # noqa: B010 - exercise the frozen model at runtime
    brain.write("bf.yaml", CONFIG.replace(b"  mail:\n", b"  post:\n"))
    assert set(load(brain).sensors) == {"post"}


def field(**values: object) -> dict[str, object]:
    return {"description": "A relation.", "type": "identity", "cardinality": "many", "relation": True, **values}


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"cites": field()}, "reserved for OKF sources"),
        ({"owner": field(broader="owner")}, r"fields\.owner\.broader"),
        ({"owner": field(broader="absent")}, r"fields\.owner\.broader"),
        ({"owner": field(broader="kind"), "kind": {"description": "Kind.", "type": "string"}}, "broader"),
        ({"a": field(), "b": field(broader="a"), "c": field(broader="b")}, r"fields\.c\.broader"),
        ({"owner": field(targets=["Person:"])}, "targets"),
        ({"owner": field(targets=["person"])}, "targets"),
        ({"owner": field(targets=[])}, "targets"),
        ({"owner": field(targets=["person:"], examples=[["team:core"]])}, "declared targets"),
        ({"owner": field(relation=False, type="identity", targets=["person:"])}, "require relation"),
        ({"owner": field(relation=False, type="identity", broader="kind")}, "require relation"),
    ],
)
def test_broader_and_targets_are_checked_before_anything_runs(schema: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Config.model_validate({"version": 7, "name": "test", "fields": schema})


def test_constant_mappings_respect_their_targets() -> None:
    schema = {"owner": field(targets=["person:"])}
    sensors = {"mail": {"command": ["fake"], "fields": {"owner": {"value": ["team:core"]}}}}
    with pytest.raises(ValidationError, match=r"sensors\.mail\.fields\.owner: value outside the declared targets"):
        Config.model_validate({"version": 7, "name": "test", "fields": schema, "sensors": sensors})
    sensors["mail"]["fields"]["owner"]["value"] = ["person:alice"]
    config = Config.model_validate({"version": 7, "name": "test", "fields": schema, "sensors": sensors})
    assert config.ontology["owner"].allows(["person:alice", "person:bob"])
    assert not config.ontology["owner"].allows(["person:alice", "team:core"])


TARGETED = CONFIG.replace(b"    examples: [person:alice]\n", b'    targets: ["person:", "bf://fixture/people/"]\n')


def test_targets_fail_a_collection_by_position_without_changing_evidence(brain: Store) -> None:
    brain.write("bf.yaml", TARGETED)
    first = {"id": "one", "title": "Planning", "attributes": {"from": "person:alice"}}
    ingest(brain, [first, {"id": "two", "title": "Team", "attributes": {"from": "bf://fixture/people/bob"}}])
    stored = {name: brain.read(name) for name in records.files(brain, "mail")}
    outside = {"id": "three", "title": "Team", "attributes": {"from": "team:PRIVATE-TEAM"}}
    with pytest.raises(Error, match=r"record 1: field sender: identity outside the declared targets") as caught:
        ingest(brain, [{**first, "title": "Changed"}, outside])
    assert "PRIVATE-TEAM" not in str(caught.value)
    # A typed link names a relation too: its target must fit the same declared targets.
    linked = {**first, "links": ["bf://fixture/teams/PRIVATE?rel=sender"]}
    with pytest.raises(Error, match=r"record 0: link relation sender: identity outside the declared targets") as caught:
        ingest(brain, [linked])
    assert "PRIVATE" not in str(caught.value)
    assert {name: brain.read(name) for name in records.files(brain, "mail")} == stored


def test_validate_reports_targets_that_stored_records_and_note_links_miss(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    ingest(brain, [{"id": "one", "title": "Planning", "attributes": {"from": "team:core"}}])
    brain.write("concepts/core.md", b"---\ntype: team\nentity: bf://fixture/teams/core\n---\n# Core\n")
    brain.write("projects/plan.md", b"---\ntype: plan\n---\n# Plan\n\n[Core](bf://fixture/teams/core?rel=recipient)\n")
    assert validate(brain)["valid"]
    brain.write(
        "bf.yaml",
        TARGETED.replace(b"    examples: [[person:bob, person:carol]]\n", b'    targets: ["person:"]\n'),
    )
    problems = cast(list[dict], validate(brain)["problems"])
    assert problems == [
        {"file": records.path("mail", "one"), "error": "field sender: identity outside the declared targets"},
        {"file": "projects/plan.md", "error": "recipient link outside the declared targets: bf://fixture/teams/core"},
    ]
    # Evidence stays readable: the claims remain until their files change.
    assert linking(brain, "team:core", "sender") == ["mail:one"]
    assert linking(brain, "bf://fixture/teams/core", "recipient") == ["projects/plan.md"]


def test_reprojection_applies_current_mappings_to_stored_records(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write("bf.yaml", CONFIG)
    first = {"id": "one", "title": "Planning", "attributes": {"from": "person:alice", "to": ["person:bob"]}}
    second = {"id": "two", "title": "Reply", "attributes": {"from": "person:bob"}}
    ingest(brain, [first, second, {**second, "id": "three"}])
    before = {name: brain.read(name) for name in records.files(brain, "mail")}
    # Rename a relation: stored records keep the old field, which no longer builds an edge, until reprojected.
    renamed = CONFIG.replace(b"  sender:\n", b"  author:\n").replace(b"      sender: {", b"      author: {")
    brain.write("bf.yaml", renamed)
    assert "field sender is not declared in bf.yaml fields" in str(validate(brain)["problems"])
    preview = invoke("build", "--brain", str(brain.root), "--reproject", "mail", "--dry-run")
    assert preview == {"sensor": "mail", "dry_run": True, "records": 3, "changed": 3, "unchanged": 0, "failed": 0}
    assert {name: brain.read(name) for name in records.files(brain, "mail")} == before
    recovered: list[Store] = []
    monkeypatch.setattr(records, "recover", lambda store: recovered.append(store))
    # Two records per transaction: each commit leaves every record either reprojected or as it was.
    monkeypatch.setattr("bf.collect.REPROJECT", 2)
    done = invoke("build", "--brain", str(brain.root), "--reproject", "mail")
    assert recovered
    assert (done["changed"], done["unchanged"], done["failed"]) == (3, 0, 0)
    assert cast(dict, done["index"])["skipped"] == 0
    assert validate(brain)["valid"]
    assert linking(brain, "person:alice", "author") == ["mail:one"]
    stored = records.load(brain, records.path("mail", "one"))
    assert stored.fields == {"author": "person:alice", "recipient": ["person:bob"], "kind": "message"}
    # Collection's own time and every other stored value stay as they were.
    previous = Record.model_validate_json(before[records.path("mail", "one")])
    assert stored.model_dump(exclude={"fields"}) == previous.model_dump(exclude={"fields"})
    assert invoke("build", "--brain", str(brain.root), "--reproject", "mail")["unchanged"] == 3


def test_reprojection_keeps_records_the_current_mappings_reject(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    ingest(brain, [{"id": "one", "title": "Planning", "attributes": {"from": "person:alice", "category": "x"}}])
    ingest(brain, [{"id": "two", "title": "Reply", "attributes": {"from": "person:bob"}}])
    # `label` becomes required: a record whose sensor never printed a category cannot gain one now.
    brain.write(
        "bf.yaml", CONFIG.replace(b"    type: string\n  kind:", b"    type: string\n    cardinality: one\n  kind:")
    )
    before = brain.read(records.path("mail", "two"))
    reply = invoke("build", "--brain", str(brain.root), "--reproject", "mail", code=1)
    assert {key: reply[key] for key in ("records", "changed", "unchanged", "failed")} == {
        "records": 2,
        "changed": 0,
        "unchanged": 1,
        "failed": 1,
    }
    assert reply["problems"] == [
        {"file": records.path("mail", "two"), "error": "field label: required mapped value is missing"}
    ]
    assert brain.read(records.path("mail", "two")) == before
    for args, code in ((["--dry-run"], 2), (["--reproject", "Mail"], 2), (["--reproject", "absent"], 1)):
        assert invoke("build", "--brain", str(brain.root), *args, code=code) == {}


def test_broader_and_targets_keep_the_cache_like_other_read_time_settings(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    index.refresh(brain)
    # Before 17 either setting rebuilt the cache, though relation pages read broader at query time and only
    # collection and validation check targets: a large brain spent minutes rebuilding identical rows.
    for changed in (
        TARGETED,
        CONFIG.replace(b"    cardinality: many\n", b"    cardinality: many\n    broader: sender\n"),
    ):
        brain.write("bf.yaml", changed)
        assert index.refresh(brain)["changed"] == 0
    # A structural change, such as a field's type, still rebuilds every row.
    brain.write(
        "bf.yaml", CONFIG.replace(b"    type: string\n  kind:", b"    type: string\n    cardinality: many\n  kind:")
    )
    assert index.refresh(brain)["changed"] == 4


def test_identity_search_lists_a_typed_link_once(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    # Like git-history, the sensor emits the sender both as a link and as a mapped relation.
    ingest(
        brain, [{"id": "one", "title": "Planning", "links": ["person:alice"], "attributes": {"from": "person:alice"}}]
    )
    items = cast(list[dict], search([brain], Query(text="person:alice"))["items"])
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
    assert linking(brain, "person:alice", "sender") == ["mail:one"]
