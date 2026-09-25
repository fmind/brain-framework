"""Schema contracts preserve roles and make the derived graph recoverable from records."""

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


CONFIG = b"""version: 4
name: fixture
schema:
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


def ingest(brain: Store, values: list[dict]) -> dict:
    return collect(
        brain, "mail", start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z", runner=lambda *_: encode(values)
    )


def test_roles_replacement_aliases_and_cache_rebuild(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    brain.write("projects/alice.md", b"---\naliases: [person:alice, account:alice]\n---\n# Alice\n")
    first = {"id": "one", "title": "Planning", "attributes": {"from": "person:alice", "to": ["person:bob"]}}
    second = {"id": "two", "title": "Reply", "attributes": {"from": "person:bob", "to": ["person:alice"]}}
    assert ingest(brain, [first, second])["added"] == 2
    query = Query(relation="sender", target="person:alice")
    assert [i["ref"] for i in search([brain], query)["items"]] == ["mail:one"]
    assert [i["ref"] for i in search([brain], Query(relation="sender", target="account:alice"))["items"]] == [
        "mail:one"
    ]
    assert search([brain], Query(text="message"))["items"]
    assert cast(dict, read([brain], "mail:one")["record"])["fields"] == {
        "sender": "person:alice",
        "recipient": ["person:bob"],
        "kind": "message",
    }
    assert validate(brain)["valid"]
    with writer(brain):
        brain.delete(index.CACHE)
    assert search([brain], query)["items"][0]["ref"] == "mail:one"
    first["attributes"]["from"] = "person:carol"
    ingest(brain, [first])
    assert search([brain], query)["items"] == []
    assert search([brain], Query(relation="recipient", target="person:alice"))["items"][0]["ref"] == "mail:two"
    with writer(brain):
        records.upsert(brain, "mail", [], snapshot=True)
    assert search([brain], Query(relation="recipient", target="person:alice"))["items"] == []


def test_invalid_batch_writes_nothing_and_does_not_leak(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    good = {"id": "one", "title": "Good", "attributes": {"from": "person:alice"}}
    bad = {"id": "two", "title": "Bad", "attributes": {"from": "PRIVATE-SECRET"}}
    with pytest.raises(Error, match="schema field sender") as caught:
        ingest(brain, [good, bad])
    assert "PRIVATE-SECRET" not in str(caught.value)
    assert records.partitions(brain, "mail") == []
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
        with pytest.raises(ValueError, match=r"expected|requires|exceeds|nonempty"):
            field.normalize(value)


def test_config_contracts_and_pointer_escaping() -> None:
    with pytest.raises(ValidationError, match="relation fields"):
        SchemaField(description="Wrong", type="string", relation=True)
    with pytest.raises(ValidationError):
        SchemaField(description="Wrong", type="integer", examples=[True])
    field = SchemaField(description="Many", type="string", cardinality="many")
    assert field.normalize(["a", "a", "b"]) == ["a", "b"]
    for invalid in ["a", ["a"] * 1001, [1]]:
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
    with pytest.raises(ValidationError, match="undeclared"):
        Config.model_validate(
            {"name": "test", "sensors": {"x": {"command": ["fake"], "fields": {"missing": {"value": "x"}}}}}
        )
    with pytest.raises(ValidationError):
        Config.model_validate({"version": 3, "name": "old"})


def test_schema_change_invalidates_cache_and_bad_partition_is_reported(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    ingest(brain, [{"id": "one", "title": "Mail", "attributes": {"from": "person:alice"}}])
    query = Query(relation="sender", target="person:alice")
    assert search([brain], query)["items"]
    brain.write("bf.yaml", CONFIG.replace(b"relation: true", b"relation: false"))
    assert search([brain], query)["items"] == []
    record = Record(id="bad", title="Invalid", fields={"undeclared": "x"})
    brain.write("memories/bad/undated.jsonl", records.line(record))
    reply = search([brain], Query(text="offline"))
    assert reply["items"]
    assert reply["problems"]
    assert not validate(brain)["valid"]
    with pytest.raises(Error, match="schema field sender"):
        ontology.validate(Record(id="bad", title="Bad", fields={"sender": 1}), load(brain))


def test_cli_mcp_and_evaluation_share_relationship_filters(brain: Store) -> None:
    brain.write("bf.yaml", CONFIG)
    ingest(brain, [{"id": "one", "title": "Mail", "attributes": {"from": "person:alice"}}])
    cli = invoke("search", "--relation", "sender", "--target", "person:alice")

    async def call() -> dict:
        result = await server([brain]).call_tool("search", {"relation": "sender", "target": "person:alice"})
        assert isinstance(result, CallToolResult)
        return cast(dict, result.structured_content)

    assert asyncio.run(call())["items"] == cli["items"]
    brain.write(
        "evals/roles.yaml",
        b"version: 4\ncases:\n- name: sender\n  relation: sender\n  target: person:alice\n  expect: [mail:one]\n",
    )
    brain.write(
        "evals/nested/empty.yml",
        b"version: 4\ncases:\n- name: absent\n  relation: sender\n  target: person:unknown\n  empty: true\n",
    )
    assert evaluate(brain)["score"] == "2/2"
    assert evaluate(brain, "evals/roles.yaml")["score"] == "1/1"
    for params in [{"relation": "sender"}, {"relation": "sender", "target": "Alice"}]:
        with pytest.raises(ValidationError):
            Query.model_validate(params)
    brain.write(
        "evals/duplicate.yaml", b"version: 4\ncases:\n- name: twice\n  empty: true\n- name: twice\n  empty: true\n"
    )
    with pytest.raises(Error, match="duplicate"):
        evaluate(brain)
