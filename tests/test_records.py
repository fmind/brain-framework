"""Tests for JSON Schemas, pure JSON per-record ingestion, .forgotten, incomplete envelopes, and events."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bf import records, schemas
from bf.models import MAX_BATCH, Error, InputError, decode
from bf.storage import Store, private_dir


def test_schemas_compile_and_describe_all_validation_rules(brain: Store, tmp_path: Path) -> None:
    for kind in ("brain", "record", "concepts", "evals"):
        doc = schemas.document(kind)
        assert doc["$schema"].endswith("2020-12/schema")

    assert schemas.BRAIN_SCHEMA["properties"]["version"]["const"] == 16
    assert schemas.EVALS_SCHEMA["properties"]["version"]["const"] == 1
    assert schemas.get_schema("bf") == schemas.document("brain")
    assert schemas.get_schema("record.schema.json") == schemas.document("record")
    assert schemas.get_schema("concepts.json") == schemas.document("concepts")
    assert schemas.get_schema("evals") == schemas.document("evals")
    assert set(schemas.export_schemas()) == {"bf", "record", "concepts", "evals"}
    with pytest.raises(InputError, match="unknown schema"):
        schemas.get_schema("unknown")
    base_digests = schemas.schemas_digests(brain)
    assert "__all__" in base_digests
    assert len(base_digests["record"]) == 64
    assert schemas.schema_digest(schemas.RECORD_SCHEMA) == base_digests["record"]
    assert schemas.schema_digest(schemas.CONCEPTS_SCHEMA) == base_digests["concepts"]
    fresh_dir = tmp_path / "fresh-schemas"
    fresh_dir.mkdir()
    assert schemas.schemas_digests(Store(fresh_dir))["record"] == schemas.schema_digest(schemas.RECORD_SCHEMA)
    brain.write("schemas/custom.json", b'{"type":"object"}')
    assert schemas.schemas_digests(brain)["__all__"] != base_digests["__all__"]

    rec_val = schemas.compile_schema(schemas.RECORD_SCHEMA, "record.json")
    compact_note = {
        "kind": "project",
        "ref": "projects/alpha.md",
        "source": "projects",
        "title": "Alpha",
        "entity": "proj:alpha",
        "sources": [{"resource": "jira:PROJ-1"}],
        "headings": [{"id": "top", "title": "Top", "level": 1}],
        "links": [{"to": "jira:PROJ-1", "kind": "record"}],
        "elements": [{"tag": "p"}],
        "tasks": [{"text": "Do thing", "done": False}],
    }
    assert schemas.validate_instance(rec_val, compact_note, "note") == []
    assert any(
        "missing required 'id'" in e
        for e in schemas.validate_instance(rec_val, {**compact_note, "kind": "memory"}, "mem")
    )
    for bad_dt in ("2026-02-31", "2026-04-31", "2026-13-01", "2026-00-10"):
        assert schemas.validate_instance(rec_val, {**compact_note, "updated": bad_dt}, "note")
        assert schemas.validate_instance(rec_val, {**compact_note, "stale_after": bad_dt}, "note")
    for bad_tm in ("2026-04-30", "2026-02-31T10:00:00Z", "2026-04-31T00:00:00Z"):
        assert schemas.validate_instance(rec_val, {**compact_note, "time": bad_tm}, "note")
    for good_dt in ("2026-04-30", "2024-02-29", "2026-04-30T12:00:00Z"):
        assert schemas.validate_instance(rec_val, {**compact_note, "updated": good_dt}, "note") == []
    concept_val = schemas.compile_schema(schemas.CONCEPTS_SCHEMA, "concepts.json")
    assert schemas.validate_instance(concept_val, {"type": "guide", "status": "stable", "updated": "2026-02-31"}, "c")
    assert (
        schemas.validate_instance(concept_val, {"type": "guide", "status": "stable", "updated": "2024-02-29"}, "c")
        == []
    )

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


def _load_source(store: Store, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rel, raw in store.walk(f"memories/{source}", suffix=".json", limit=4 << 20):
        assert raw is not None
        obj = decode(raw, rel)
        assert isinstance(obj, dict)
        out.append(obj)
    return out


def test_parse_batch_supports_array_envelope_single_object_and_jsonl() -> None:
    assert records.parse_batch_envelope(b"\xef\xbb\xbf  \n ") == ([], False)
    arr, _ = records.parse_batch_envelope(b'[{"id": "1", "title": "One"}]')
    single, _ = records.parse_batch_envelope(b'{"id": "solo", "title": "Solo"}')
    pretty_single, pretty_inc = records.parse_batch_envelope(b'{\n  "id": "multi",\n  "title": "Pretty"\n}')
    env_items, incomplete = records.parse_batch_envelope(
        b'{"records": [{"id": "e1", "title": "Env"}], "incomplete": {"missed": 2}}'
    )
    bool_items, bool_inc = records.parse_batch_envelope(
        b'{"records": [{"id": "e2", "title": "Bool"}], "incomplete": true}'
    )
    _, zero_inc = records.parse_batch_envelope(b'{"records": [], "incomplete": {"missed": 0}}')
    jsonl, _ = records.parse_batch_envelope(b'\n{"id": "1", "title": "One"}\n\n{"id": "2", "title": "Two"}\n')
    assert len(arr) == 1
    assert single[0]["id"] == "solo"
    assert (pretty_single[0]["id"], pretty_inc) == ("multi", False)
    assert (len(env_items), incomplete) == (1, True)
    assert (len(bool_items), bool_inc, zero_inc) == (1, True, False)
    assert [r["id"] for r in jsonl] == ["1", "2"]

    for payload, match in (
        (b"x" * (MAX_BATCH + 1), "exceeds"),
        (b"\xff", "not valid UTF-8"),
        (b"[1, 2]", "JSON array of record objects"),
        (b'{"records": "bad"}', "expected a JSON array of record objects"),
        (b'"scalar"\n', "line 1: expected a JSON object"),
        (b'{\n  "id": "1",\n  "id": "2"\n}', "duplicate key"),
        (b'{\n  "id": "1",\n  "score": NaN\n}', "must be finite"),
        (b'{"records": [], "incomplete": "yes"}', "expected a boolean"),
        (b'{"records": [], "incomplete": {}}', "expected a boolean"),
        (b'{"records": [], "incomplete": {"reason": "rate"}}', "expected a boolean"),
        (b'{"records": [], "incomplete": {"missed": -1}}', "expected a boolean"),
        (b'{"records": [], "incomplete": {"missed": true}}', "expected a boolean"),
    ):
        with pytest.raises(Error, match=match):
            records.parse_batch_envelope(payload)


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

    meet_file = records.record_file("meetings", "decision-1")
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
                    "bf:///projects/offline.md",
                    "projects/offline.md",
                    "meetings:decision-1",
                    "bf:///meetings:decision-1",
                    "bf://other/meetings:decision-1",
                    {"to": "bf:///projects/offline.md", "anchor": "decision", "meta": {"rel": "blocks"}},
                    {"to": f"bf:///{meet_file}", "meta": {"rel": "cites"}},
                    {"to": "meetings:decision-1", "meta": {"rel": "follows"}},
                ],
            }
        ]
    ).encode()

    rec_path = records.record_file("jira", "J-1")
    preview = records.ingest(brain, "jira", batch1, dry_run=True, now=t1)
    assert (preview["added"], preview["dry_run"], brain.exists(rec_path)) == (1, True, False)

    res1 = records.ingest(brain, "jira", batch1, now=t1)
    assert (res1["added"], res1["updated"], res1["unchanged"], brain.exists(rec_path)) == (1, 0, 0, True)
    saved1 = _load_source(brain, "jira")
    assert saved1[0]["updated"] == "2026-11-15T09:00:00Z"
    assert "url" not in saved1[0]
    assert any(lnk["to"] == meet_file and lnk["meta"] == {"rel": "cites"} for lnk in saved1[0]["links"])
    assert any(lnk["to"] == meet_file and lnk["meta"] == {"rel": "follows"} for lnk in saved1[0]["links"])
    assert any(lnk["to"] == f"bf://other/{meet_file}" and lnk["kind"] == "brain" for lnk in saved1[0]["links"])

    # Ingesting new records emits `records.ingested` in `.bf/events.jsonl` + `logs/events.log`, and `append_event(..., dedupe=True)` is idempotent!
    ev1 = records.append_event(brain, "loop.message", "chat", {"id": "msg-42", "text": "hi"}, dedupe=True, now=t1)
    ev2 = records.append_event(brain, "loop.message", "chat", {"id": "msg-42", "text": "hi"}, dedupe=True, now=t2)
    assert ev1 == ev2
    ev_items, latest_seq = records.read_events(brain)
    assert latest_seq == records.latest_event_seq(brain)
    assert sum(1 for e in ev_items if e["data"].get("id") == "msg-42") == 1
    ev_lines = [decode(ln, "logs/events.log") for ln in brain.read("logs/events.log").splitlines() if ln.strip()]
    assert any(isinstance(ln, dict) and ln.get("message") == "loop.message" for ln in ev_lines)

    # Re-ingesting identical payload without explicit `updated` preserves original `updated` timestamp!
    res2 = records.ingest(brain, "jira", batch1, now=t2)
    assert (res2["added"], res2["updated"], res2["unchanged"]) == (0, 0, 1)
    assert _load_source(brain, "jira")[0]["updated"] == "2026-11-15T09:00:00Z"

    # Modifying title updates record and advances `updated` to t2.
    batch2 = json.dumps([{"id": "J-1", "title": "Fix SSO login", "priority": "P0"}]).encode()
    res3 = records.ingest(brain, "jira", batch2, now=t2)
    assert (res3["added"], res3["updated"], res3["unchanged"]) == (0, 1, 0)
    assert _load_source(brain, "jira")[0]["updated"] == "2026-11-15T10:00:00Z"

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
    assert (emptied["records"], emptied["removed"], _load_source(brain, "catalog")) == (0, 4, [])


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
        ("valid", "upsert", b'[{"id":"1","title":"t","links":["not-a-link"]}]', Error, "links must use bf://"),
        ("valid", "upsert", b'[{"id":"1","title":"t","links":["concepts:welcome"]}]', Error, "must use memories/"),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":["meetings:decision-1#frag"]}]',
            Error,
            "do not support #anchor fragments",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":["bf:///meetings:decision-1#frag"]}]',
            Error,
            "do not support #anchor fragments",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":["bf://other/meetings:decision-1#frag"]}]',
            Error,
            "do not support #anchor fragments",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":["jira:https://example.com/issue/1#frag"]}]',
            Error,
            "do not support #anchor fragments",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":[{"to":"jira:https://example.com/issue/1","anchor":"frag"}]}]',
            Error,
            "do not support #anchor fragments",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":["jira:http://example.com/issue/1#frag"]}]',
            Error,
            "do not support #anchor fragments",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":[{"to":"jira:PROJ-1","anchor":"frag"}]}]',
            Error,
            "do not support #anchor fragments",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":[{"to":"bf:///jira:PROJ-1","anchor":"frag"}]}]',
            Error,
            "do not support #anchor fragments",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":[{"to":"bf://other/jira:https://example.com/issue/1","anchor":"frag"}]}]',
            Error,
            "do not support #anchor fragments",
        ),
        ("valid", "upsert", b'[{"id":"1","title":"t","links":["bad_src:1"]}]', Error, "links must use bf://"),
        ("valid", "upsert", b'[{"id":"1","title":"t","links":["bad+src:1"]}]', Error, "invalid record reference"),
        ("valid", "upsert", b'[{"id":"1","title":"t","links":["meetings:"]}]', Error, "invalid record reference"),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":["javascript:alert(1)"]}]',
            Error,
            "unsupported link scheme",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":[{"to":"concepts/a.md","anchor":123}]}]',
            Error,
            "anchor must be a string",
        ),
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
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"a","links":["bf:///docs/index.md"]}]',
            Error,
            "must target projects/, concepts/, or actions/",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"a","links":["bf:///memories/concepts/welcome.json"]}]',
            Error,
            "must target projects/, concepts/, or actions/",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"a","links":["bf:///memories/notes/x.json"]}]',
            Error,
            "must target projects/, concepts/, or actions/",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"a","links":["bf:///memories/jira/abcdef.json#frag"]}]',
            Error,
            "do not support #anchor fragments",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"a","links":[{"to":"bf:///projects/offline.md","rel":"cites"}]}]',
            Error,
            "unknown link keys rel",
        ),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"a","links":[{"to":"bf:///projects/offline.md","kind":"url"}]}]',
            Error,
            "does not match target",
        ),
    ],
)
def test_ingest_rejects_invalid_records_and_arguments(
    brain: Store, source: str, mode: str, payload: bytes, exc: type[Exception], match: str
) -> None:
    with pytest.raises(exc, match=match):
        records.ingest(brain, source, payload, mode=mode)  # ty: ignore[invalid-argument-type]


def test_record_links_dedupe_events_jsonl_compaction_and_cursor_healing(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Typed `{to, meta}` links on ingested records drop overlapping untyped links and re-ingest idempotently:
    ing = records.ingest(
        brain,
        "mail",
        b'[{"id":"m1","title":"Msg","entity":"msg:1",'
        b'"links":["https://example.com/u/a",{"to":"https://example.com/u/a","meta":{"rel":"sender"}},'
        b'{"to":"concepts/welcome.md","anchor":"top"}]}]',
    )
    assert ing["records"] == 1
    stored_mail = _load_source(brain, "mail")[0]
    links = stored_mail["links"]
    assert isinstance(links, list)
    assert len(links) == 2
    assert links[0] == {
        "to": "https://example.com/u/a",
        "kind": "url",
        "anchor": "",
        "meta": {"rel": "sender"},
        "section": "",
    }
    assert links[1]["anchor"] == "top"
    # Re-ingesting normalized stored record object (with brain-root `to` in link object) succeeds unchanged:
    reing = records.ingest(brain, "mail", json.dumps([stored_mail]).encode())
    assert reing["unchanged"] == 1

    assert _load_source(brain, "missing") == []

    # Oversized events fail on `logs/events.log` bounds BEFORE advancing `.bf/events.jsonl`.
    records.append_event(brain, "test.ok", "t", {"id": "e0"})
    before_seq = records.latest_event_seq(brain)
    before_bytes = private_dir(brain).read("events.jsonl")
    with pytest.raises(Error, match="exceeds"):
        records.append_event(brain, "test.huge", "t", {"id": "e1", "blob": "x" * ((1 << 20) + 64)})
    assert records.latest_event_seq(brain) == before_seq
    assert private_dir(brain).read("events.jsonl") == before_bytes

    # Cursor self-healing when `after_seq > latest`, multi-chunk `latest_event_seq`, and amortized compaction!
    healed, latest = records.read_events(brain, after_seq=999_999)
    assert len(healed) >= 1
    assert latest == before_seq

    priv = private_dir(brain)
    # Multi-chunk (> 65536 bytes) tail scan in `latest_event_seq` (assert BEFORE appending small tail!):
    records.append_event(brain, "test.pad", "t", {"id": "pad", "blob": "y" * 70_000})
    assert records.latest_event_seq(brain) == before_seq + 1
    records.append_event(brain, "test.tail", "t", {"id": "tail"})
    assert records.latest_event_seq(brain) == before_seq + 2

    # Amortized compaction when `seq > MAX_EVENTS and seq % 200 == 0`:
    monkeypatch.setattr(records, "MAX_EVENTS", 5)
    seed_lines = b"".join(
        (
            json.dumps(
                {"seq": s, "id": f"e-{s}", "time": "2026-11-15T00:00:00Z", "kind": "k", "source": "s", "data": {}}
            )
            + "\n"
        ).encode()
        for s in range(190, 200)
    )
    priv.write("events.jsonl", seed_lines, mode=0o600, durable=False)
    ev200 = records.append_event(brain, "test.compact", "t", {"id": "c200"})
    assert ev200["seq"] == 200
    compacted, comp_latest = records.read_events(brain)
    assert comp_latest == 200
    assert len(compacted) == 5

    # Byte-cap compaction (`size_after > COMPACT_BYTES`) trims to half-budget (`<= 300`) and next append skips rewrite:
    monkeypatch.setattr(records, "MAX_EVENTS", 1000)
    monkeypatch.setattr(records, "COMPACT_BYTES", 600)
    for idx in range(5):
        records.append_event(brain, "test.bytes", "t", {"id": f"b{idx}", "pad": "z" * 90})
    assert len(private_dir(brain).read("events.jsonl")) <= 300
    records.append_event(brain, "test.bytes", "t", {"id": "b5", "pad": "z" * 90})
    assert 300 < len(private_dir(brain).read("events.jsonl")) < 600

    # Empty / blank `.bf/events.jsonl` and missing `.bf/events.jsonl` return `0` cleanly:
    priv.write("events.jsonl", b"", mode=0o600, durable=False)
    assert records.latest_event_seq(brain) == 0
    priv.write("events.jsonl", b"\n\n", mode=0o600, durable=False)
    assert records.latest_event_seq(brain) == 0
    priv.delete("events.jsonl")
    assert records.read_events(brain) == ([], 0)
    first_dedup = records.append_event(brain, "test.dedup", "t", {"id": "d1"}, dedupe=True)
    assert records.append_event(brain, "test.dedup", "t", {"id": "d1"}, dedupe=True)["id"] == first_dedup["id"]

    # Event payloads are redacted before persistence, and invalid event kind/source are rejected.
    sec_ev = records.append_event(
        brain, "test.sec", "t", {"id": "s-1", "OPENAI_API_KEY": "sk-secretvalue1234", "msg": "Bearer tok99999999"}
    )
    assert sec_ev["data"]["OPENAI_API_KEY"] == "[REDACTED]"
    assert "tok99999999" not in sec_ev["data"]["msg"]
    assert b"sk-secretvalue1234" not in priv.read("events.jsonl")
    assert b"sk-secretvalue1234" not in brain.read("logs/events.log")
    with pytest.raises(Error, match="event kind"):
        records.append_event(brain, "BAD KIND", "t", {})
    with pytest.raises(Error, match="event source"):
        records.append_event(brain, "ok.ev", "BAD_SRC", {})

    # Symlinked or FIFO `.bf/events.jsonl` is refused without blocking by `O_NOFOLLOW | O_NONBLOCK`:
    (priv.root / "events.jsonl").unlink()
    (priv.root / "events.jsonl").symlink_to(brain.root / "bf.yaml")
    with pytest.raises(Error, match="symlinks are not followed"):
        records.append_event(brain, "test.sym", "t", {"id": "s1"})
    (priv.root / "events.jsonl").unlink()
    import os

    os.mkfifo(priv.root / "events.jsonl", 0o600)
    with pytest.raises(Error, match="expected a regular file"):
        records.latest_event_seq(brain)
    with pytest.raises(Error, match="expected a regular file"):
        records.append_event(brain, "test.fifo", "t", {"id": "f1"})
    (priv.root / "events.jsonl").unlink()


def test_ingest_batch_fsync(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    durables: list[bool] = []
    synced: list[list[str]] = []
    orig_write, orig_delete, orig_sync = Store.write, Store.delete, Store.sync_dirs

    def spy_write(self: Store, name: str, data: bytes, *, mode: int = 0o644, durable: bool = True) -> None:
        if name.startswith("memories/"):
            durables.append(durable)
        orig_write(self, name, data, mode=mode, durable=durable)

    def spy_delete(self: Store, name: str, *, durable: bool = True) -> None:
        if name.startswith("memories/"):
            durables.append(durable)
        orig_delete(self, name, durable=durable)

    def spy_sync(self: Store, dirs: Any) -> None:
        synced.append(sorted(dirs))
        orig_sync(self, dirs)

    monkeypatch.setattr(Store, "write", spy_write)
    monkeypatch.setattr(Store, "delete", spy_delete)
    monkeypatch.setattr(Store, "sync_dirs", spy_sync)

    batch = json.dumps([{"id": f"c-{i}", "title": f"C {i}"} for i in range(3)]).encode()
    records.ingest(brain, "catalog", batch, mode="snapshot", dry_run=True)
    assert (durables, synced) == ([], [])

    records.ingest(brain, "catalog", batch, mode="snapshot")
    assert durables == [False, False, False]
    assert synced == [["memories/catalog"]]

    durables.clear()
    synced.clear()
    records.ingest(brain, "catalog", batch, mode="snapshot")
    assert (durables, synced) == ([], [])

    records.ingest(
        brain, "catalog", json.dumps([{"id": "c-0", "title": "C 0"}]).encode(), mode="snapshot", allow_removal=True
    )
    assert durables == [False, False]
    assert synced == [["memories/catalog"]]


def test_ingest_allows_url_record_without_fragment_and_external_url_with_fragment(brain: Store) -> None:
    payload = json.dumps(
        [
            {
                "id": "u-1",
                "title": "URL links",
                "links": [
                    "jira:https://example.com/issue/1",
                    "https://example.com/page#frag",
                    {"to": "https://example.com/other", "anchor": "sec"},
                    "bf:///concepts/welcome.md#top",
                ],
            }
        ]
    ).encode()
    res = records.ingest(brain, "ext", payload)
    assert res["added"] == 1
    links = _load_source(brain, "ext")[0]["links"]
    assert {lnk["to"]: lnk["kind"] for lnk in links} == {
        records.record_file("jira", "https://example.com/issue/1"): "record",
        "https://example.com/page#frag": "url",
        "https://example.com/other#sec": "url",
        "concepts/welcome.md": "note",
    }
