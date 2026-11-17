"""Tests for JSON Schemas, pure JSON per-record ingestion, .forgotten, incomplete envelopes, and events."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from bf import records, schemas
from bf.models import MAX_BATCH, Error, InputError, decode
from bf.storage import Store, private_dir


def test_schemas_compile_and_describe_all_validation_rules(brain: Store) -> None:
    for kind in ("brain", "record", "concepts", "evals"):
        doc = schemas.document(kind)
        assert doc["$schema"].endswith("2020-12/schema")

    with pytest.raises(Error, match="must be a JSON object"):
        schemas.compile_schema(["not", "an", "object"], "bad.json")
    with pytest.raises(Error, match="invalid JSON Schema"):
        schemas.compile_schema({"type": "not-a-json-schema-type"}, "bad.json")

    assert schemas.load_validator(brain, "schemas/nonexistent.json") is None

    strict = schemas.compile_schema(
        {
            "type": "object",
            "required": ["a", "b"],
            "additionalProperties": False,
            "propertyNames": {"pattern": "^[a-z]+$"},
            "properties": {
                "a": {"const": 1},
                "b": {"enum": ["x", "y"]},
                "c": {"type": "integer", "minimum": 5},
                "d": {"type": "string", "pattern": "^ok$"},
                "e": {"type": "array", "uniqueItems": True},
                "f": {"multipleOf": 3},
            },
        },
        "strict.json",
    )
    errs = schemas.validate_instance(
        strict,
        {"a": 2, "c": 1, "d": "no", "e": [1, 1], "f": 4, "BAD": 1},
        "rec",
    )
    assert len(errs) == 6
    assert errs[-1].endswith("more schema errors")

    assert any("expected type" in e for e in schemas.validate_instance(strict, "not-obj", "rec"))
    assert any("must be one of" in e for e in schemas.validate_instance(strict, {"a": 1, "b": "z"}, "rec"))


def test_parse_batch_supports_array_envelope_single_object_and_jsonl() -> None:
    assert records.parse_batch(b"\xef\xbb\xbf  \n ") == []
    arr = records.parse_batch(b'[{"id": "1", "title": "One"}]')
    single = records.parse_batch(b'{"id": "solo", "title": "Solo"}')
    env_items, incomplete = records.parse_batch_envelope(
        b'{"records": [{"id": "e1", "title": "Env"}], "incomplete": {"missed": 2}}'
    )
    jsonl = records.parse_batch(b'\n{"id": "1", "title": "One"}\n\n{"id": "2", "title": "Two"}\n')
    assert len(arr) == 1
    assert single[0]["id"] == "solo"
    assert (len(env_items), incomplete) == (1, True)
    assert [r["id"] for r in jsonl] == ["1", "2"]

    with pytest.raises(Error, match="exceeds"):
        records.parse_batch(b"x" * (MAX_BATCH + 1))
    with pytest.raises(Error, match="not valid UTF-8"):
        records.parse_batch(b"\xff")
    with pytest.raises(Error, match="JSON array of record objects"):
        records.parse_batch(b"[1, 2]")
    with pytest.raises(Error, match="expected a JSON array of record objects"):
        records.parse_batch(b'{"records": "bad"}')
    with pytest.raises(Error, match="line 1: expected a JSON object"):
        records.parse_batch(b'"scalar"\n')


def test_ingest_pure_json_forgotten_incomplete_snapshot_and_events(brain: Store) -> None:
    t1 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)

    brain.write(
        "schemas/jira.json",
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["priority"],
                "properties": {"priority": {"enum": ["P0", "P1", "P2"]}},
            }
        ).encode(),
    )
    with pytest.raises(Error, match="missing required 'priority'"):
        records.ingest(brain, "jira", b'[{"id":"J-1","title":"Bug"}]', now=t1)
    with pytest.raises(Error, match="must be one of"):
        records.ingest(brain, "jira", b'[{"id":"J-1","title":"Bug","priority":"P9"}]', now=t1)

    batch1 = json.dumps(
        [
            {
                "id": "J-1",
                "title": "Fix login",
                "text": "Details",
                "priority": "P1",
                "url": "",
                "time": "",
                "updated": "",
                "tags": ["auth"],
                "links": [
                    "projects/offline.md",
                    {"to": "projects/offline.md", "anchor": "decision", "meta": {"rel": "blocks"}},
                ],
                "relations": [{"target": "meetings:decision-1", "type": "cites"}],
            }
        ]
    ).encode()

    rec_path = records.record_file("jira", "J-1")
    preview = records.ingest(brain, "jira", batch1, dry_run=True, now=t1)
    assert (preview["added"], preview["dry_run"], brain.exists(rec_path)) == (1, True, False)

    res1 = records.ingest(brain, "jira", batch1, now=t1)
    assert (res1["added"], res1["updated"], res1["unchanged"], brain.exists(rec_path)) == (1, 0, 0, True)
    saved1 = records.load_source(brain, "jira")
    assert saved1[0]["updated"] == "2026-11-15T09:00:00Z"
    assert "url" not in saved1[0]
    assert any(lnk["to"] == "meetings:decision-1" for lnk in saved1[0]["links"])

    # Ingesting new records emits `records.ingested` in `.bf/events.json` + `logs/events.log`, and `append_event(..., dedupe=True)` is idempotent!
    ev1 = records.append_event(brain, "channel.message", "chat", {"id": "msg-42", "text": "hi"}, dedupe=True, now=t1)
    ev2 = records.append_event(brain, "channel.message", "chat", {"id": "msg-42", "text": "hi"}, dedupe=True, now=t2)
    assert ev1 == ev2
    ev_raw = private_dir(brain).read("events.json")
    ev_state = decode(ev_raw, ".bf/events.json")
    assert isinstance(ev_state, dict)
    assert sum(1 for e in ev_state["events"] if e["data"].get("id") == "msg-42") == 1
    ev_lines = [decode(ln, "logs/events.log") for ln in brain.read("logs/events.log").splitlines() if ln.strip()]
    assert any(isinstance(ln, dict) and ln.get("message") == "channel.message" for ln in ev_lines)

    # Re-ingesting identical payload without explicit `updated` preserves original `updated` timestamp!
    res2 = records.ingest(brain, "jira", batch1, now=t2)
    assert (res2["added"], res2["updated"], res2["unchanged"]) == (0, 0, 1)
    assert records.load_source(brain, "jira")[0]["updated"] == "2026-11-15T09:00:00Z"

    # Modifying title updates record and advances `updated` to t2.
    batch2 = json.dumps([{"id": "J-1", "title": "Fix SSO login", "priority": "P0"}]).encode()
    res3 = records.ingest(brain, "jira", batch2, now=t2)
    assert (res3["added"], res3["updated"], res3["unchanged"]) == (0, 1, 0)
    assert records.load_source(brain, "jira")[0]["updated"] == "2026-11-15T10:00:00Z"

    # `.forgotten` skips new records and deletes already-saved forgotten records!
    brain.write("memories/jira/.forgotten", b"# comment\nJ-1\nJ-2\n")
    res_forg = records.ingest(
        brain,
        "jira",
        json.dumps([{"id": "J-2", "title": "Forgotten item", "priority": "P2"}]).encode(),
        now=t2,
    )
    assert (res_forg["forgotten"], res_forg["removed"], brain.exists(rec_path)) == (1, 1, False)

    # Removal guard on >= 10 records when > 50% would be removed, plus `incomplete` envelope skipping pruning!
    ten = json.dumps([{"id": f"item-{i:02d}", "title": f"Item {i}"} for i in range(10)]).encode()
    records.ingest(brain, "catalog", ten, mode="snapshot", now=t1)
    inc_env = json.dumps({"records": [{"id": "item-00", "title": "Item 0"}], "incomplete": {"missed": 1}}).encode()
    inc_res = records.ingest(brain, "catalog", inc_env, mode="snapshot", now=t2)
    assert (inc_res["incomplete"], inc_res["removed"], inc_res["records"]) == (True, 0, 10)

    four = json.dumps([{"id": f"item-{i:02d}", "title": f"Item {i}"} for i in range(4)]).encode()
    with pytest.raises(Error, match="snapshot would remove 6 of 10 records"):
        records.ingest(brain, "catalog", four, mode="snapshot", now=t2)

    allowed = records.ingest(brain, "catalog", four, mode="snapshot", allow_removal=True, now=t2)
    assert (allowed["records"], allowed["removed"]) == (4, 6)

    with pytest.raises(Error, match="snapshot would remove 4 of 4 records"):
        records.ingest(brain, "catalog", b"[]", mode="snapshot", now=t2)
    emptied = records.ingest(brain, "catalog", b"[]", mode="snapshot", allow_removal=True, now=t2)
    assert (emptied["records"], emptied["removed"], records.load_source(brain, "catalog")) == (0, 4, [])


@pytest.mark.parametrize(
    ("source", "mode", "payload", "exc", "match"),
    [
        ("BadUpper", "upsert", b"[]", InputError, "lowercase"),
        ("notes", "upsert", b"[]", InputError, "reserved"),
        ("concepts", "upsert", b"[]", InputError, "reserved"),
        ("memories", "upsert", b"[]", InputError, "reserved"),
        ("valid", "upsert", b'[{"id":"1","title":"t","stale_after":"0s"}]', Error, "stale_after"),
        ("valid", "upsert", b'[{"id":"1","title":"t","stale_after":"999y"}]', Error, "stale_after"),
        ("valid", "badmode", b"[]", InputError, "upsert or snapshot"),
        ("valid", "upsert", b'[{"title":"no id"}]', Error, "missing required string 'id'"),
        ("valid", "upsert", b'[{"id":"1"}]', Error, "missing required string 'title'"),
        ("valid", "upsert", b'[{"id":"bad id","title":"t"}]', Error, "no whitespace"),
        ("valid", "upsert", b'[{"id":"1","title":"t","source":"other"}]', Error, "source must match"),
        ("valid", "upsert", b'[{"id":"1","title":"t","ref":"valid:2"}]', Error, "ref must match"),
        ("valid", "upsert", b'[{"id":"1","title":"t","kind":"concept"}]', Error, "kind must equal 'memory'"),
        ("valid", "upsert", b'[{"id":"1","title":"t","text":123}]', Error, "text must be a string"),
        ("valid", "upsert", b'[{"id":"1","title":"t","url":123}]', Error, "url must be a string"),
        ("valid", "upsert", b'[{"id":"1","title":"t","time":123}]', Error, "time must be an ISO"),
        ("valid", "upsert", b'[{"id":"1","title":"t","updated":123}]', Error, "updated must be an ISO"),
        ("valid", "upsert", b'[{"id":"1","title":"t","links":"bad"}]', Error, "links must be a list"),
        ("valid", "upsert", b'[{"id":"1","title":"t","links":[""]}]', Error, "nonempty link targets"),
        ("valid", "upsert", b'[{"id":"1","title":"t","links":[123]}]', Error, "target strings or link objects"),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":[{"to":"concepts/a.md","meta":[]}]}]',
            Error,
            "meta must map",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":[{"to":"concepts/a.md#one","anchor":"two"}]}]',
            Error,
            "conflicting #anchor",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":[{"to":"concepts/a.md","section":123}]}]',
            Error,
            "section must be",
        ),
        ("valid", "upsert", b'[{"id":"1","title":"t","relations":{"sender":123}}]', Error, "list of URI strings"),
        ("valid", "upsert", b'[{"id":"1","title":"t","relations":[{"bad":1}]}]', Error, "objects with target or to"),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":[{"to":"projects/offline.md","meta":{"k":1}}]}]',
            Error,
            "meta must map strings to strings",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"a"},{"id":"1","title":"b"}]',
            Error,
            "duplicate record id",
        ),
    ],
)
def test_ingest_rejects_invalid_records_and_arguments(
    brain: Store, source: str, mode: str, payload: bytes, exc: type[Exception], match: str
) -> None:
    with pytest.raises(exc, match=match):
        records.ingest(brain, source, payload, mode=mode)  # ty: ignore[invalid-argument-type]


def test_load_source_handles_missing_and_corrupt_files(brain: Store) -> None:
    # Dict-form `relations` and `entity` identity on ingested records drop overlapping untyped links:
    ing = records.ingest(
        brain,
        "mail",
        b'[{"id":"m1","title":"Msg","entity":"msg:1",'
        b'"links":["person:email/a@example.com",{"to":"concepts/welcome.md","anchor":"top"}],'
        b'"relations":{"sender":["person:email/a@example.com"]}}]',
    )
    assert ing["records"] == 1
    stored_mail = records.load_source(brain, "mail")[0]
    assert len(stored_mail["links"]) == 2
    assert stored_mail["links"][0]["anchor"] == "top"
    assert stored_mail["links"][1]["meta"] == {"rel": "sender"}

    assert records.load_source(brain, "missing") == []
    brain.write("memories/corrupt/bad.txt", b"text")
    with pytest.raises(Error, match=r"expected a \.json record file"):
        records.load_source(brain, "corrupt")
    brain.write("memories/corrupt2/bad.json", b"\xff\xfe")
    with pytest.raises(Error, match="invalid JSON"):
        records.load_source(brain, "corrupt2")
    brain.write("memories/scalar/s.json", b"123\n")
    with pytest.raises(Error, match="expected a JSON object"):
        records.load_source(brain, "scalar")

    # Oversized events fail on `logs/events.log` bounds BEFORE advancing `.bf/events.json`.
    records.append_event(brain, "test.ok", "t", {"id": "e0"})
    before = decode(private_dir(brain).read("events.json"), ".bf/events.json")
    assert isinstance(before, dict)
    with pytest.raises(Error, match="exceeds"):
        records.append_event(brain, "test.huge", "t", {"id": "e1", "blob": "x" * ((1 << 20) + 64)})
    after = decode(private_dir(brain).read("events.json"), ".bf/events.json")
    assert after == before
