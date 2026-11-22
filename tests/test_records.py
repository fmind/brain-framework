"""Tests for JSON Schemas, required per-source schemas, JSONL ingestion, .forgotten, snapshots, and events."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import pytest

from bf import records, schemas
from bf.models import MAX_BATCH, Error, InputError, decode
from bf.storage import Store, private_dir

OBJ_SCHEMA = b'{"type":"object"}\n'


def test_schemas_compile_and_describe_all_validation_rules(brain: Store) -> None:
    for kind in ("bf", "record"):
        doc = schemas.document(kind)
        assert doc["$schema"].endswith("2020-12/schema")

    assert schemas.BF_SCHEMA["properties"]["version"]["const"] == 18
    assert schemas.get_schema("bf") == schemas.document("bf")
    assert schemas.get_schema("record.schema.json") == schemas.document("record")
    assert set(schemas.export_schemas()) == {"bf", "record"}
    for bad in ("unknown", "evals", "concepts"):
        with pytest.raises(InputError, match="unknown schema"):
            schemas.get_schema(bad)

    usages_doc = schemas.starter_usages_schema()
    assert usages_doc["title"] == "UsageRecord"
    assert "cost_usd" in usages_doc["properties"]
    assert schemas.compile_schema(usages_doc, "usages.json") is not None

    rec_val = schemas.compile_schema(schemas.RECORD_SCHEMA, "record.json")
    compact_note = {
        "kind": "note",
        "ref": "projects/alpha.md",
        "source": "projects",
        "title": "Alpha",
        "entity": "proj:alpha",
        "sources": [{"resource": "jira:PROJ-1"}],
        "headings": [{"id": "top", "title": "Top", "level": 1}],
        "links": [{"to": "jira:PROJ-1", "kind": "record"}],
        "tasks": [{"text": "Do thing", "done": False}],
    }
    assert schemas.validate_instance(rec_val, compact_note, "note") == []
    assert any(
        "missing required 'id'" in e
        for e in schemas.validate_instance(rec_val, {**compact_note, "kind": "memory"}, "mem")
    )
    for bad_dt in ("2026-02-31", "2026-04-31", "2026-13-01", "2026-00-10"):
        assert schemas.validate_instance(rec_val, {**compact_note, "created": bad_dt}, "note")
        assert schemas.validate_instance(rec_val, {**compact_note, "updated": bad_dt}, "note")
        assert schemas.validate_instance(rec_val, {**compact_note, "stale_after": bad_dt}, "note")
    for bad_tm in ("2026-02-31T10:00:00Z", "2026-04-31T00:00:00Z"):
        assert schemas.validate_instance(rec_val, {**compact_note, "created": bad_tm}, "note")
    for good_dt in ("2026-04-30", "2024-02-29", "2026-04-30T12:00:00Z"):
        assert schemas.validate_instance(rec_val, {**compact_note, "created": good_dt}, "note") == []
        assert schemas.validate_instance(rec_val, {**compact_note, "updated": good_dt}, "note") == []

    with pytest.raises(Error, match="must be a JSON object"):
        schemas.compile_schema(["not", "an", "object"], "bad.json")
    with pytest.raises(Error, match="invalid JSON Schema"):
        schemas.compile_schema({"type": "not-a-json-schema-type"}, "bad.json")

    assert schemas.load_validator(brain, "schemas/nonexistent.json") is None
    with pytest.raises(Error, match="missing required schema"):
        schemas.require_validator(brain, "nonexistent")

    # Non-HTTP record IDs reject `?` and `//` prefixes, while HTTP URL record IDs preserve `?`.
    brain.write("schemas/web.json", b'{"type":"object"}\n')
    with pytest.raises(Error, match="non-URL"):
        records.ingest(brain, "web", b'[{"id":"WEB-1?v=2","title":"Bad"}]')
    with pytest.raises(Error, match="'//' prefix"):
        records.ingest(brain, "web", b'[{"id":"//foo","title":"Bad"}]')
    assert records.ingest(brain, "web", b'[{"id":"https://example.com/p?a=1","title":"OK"}]')["added"] == 1

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
    path = records.memory_file(source)
    if not store.exists(path):
        return []
    return [obj for _, obj in records.iter_jsonl(store, path)]


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


def test_ingest_jsonl_forgotten_incomplete_snapshot_and_events(brain: Store) -> None:
    t1 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)

    # Missing schemas/<source>.json fails closed!
    with pytest.raises(Error, match="missing required schema"):
        records.ingest(brain, "jira", b'[{"id":"J-1","title":"Bug"}]', now=t1)

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
                    "bf:///projects/offline.md",
                    "projects/offline.md",
                    "meetings:decision-1",
                    "bf:///meetings:decision-1",
                    "bf://other/meetings:decision-1",
                    {"to": "bf:///projects/offline.md", "anchor": "decision", "meta": {"rel": "blocks"}},
                    {"to": "meetings:decision-1", "meta": {"rel": "follows"}},
                ],
            }
        ]
    ).encode()

    mem_path = records.memory_file("jira")
    preview = records.ingest(brain, "jira", batch1, dry_run=True, now=t1)
    assert (preview["ok"], preview["added"], preview["dry_run"], brain.exists(mem_path)) == (True, 1, True, False)

    res1 = records.ingest(brain, "jira", batch1, now=t1)
    assert (res1["ok"], res1["added"], res1["updated"], res1["unchanged"], brain.exists(mem_path)) == (
        True,
        1,
        0,
        0,
        True,
    )
    saved1 = _load_source(brain, "jira")
    assert saved1[0]["updated"] == "2026-11-15T09:00:00Z"
    assert "url" not in saved1[0]
    assert any(lnk["to"] == "meetings:decision-1" and lnk["meta"] == {"rel": "follows"} for lnk in saved1[0]["links"])
    assert any(lnk["to"] == "bf://other/meetings:decision-1" and lnk["kind"] == "brain" for lnk in saved1[0]["links"])

    # Ingesting new records emits `records.ingested` in `logs/events.jsonl`, and `dedupe=True` is idempotent!
    ev1 = records.append_event(brain, "loop.message", "chat", {"id": "msg-42", "text": "hi"}, dedupe=True, now=t1)
    ev2 = records.append_event(brain, "loop.message", "chat", {"id": "msg-42", "text": "hi"}, dedupe=True, now=t2)
    assert ev1 == ev2
    ev_items, latest_seq = records.read_events(brain)
    assert latest_seq == records.latest_event_seq(brain)
    assert sum(1 for e in ev_items if e["data"].get("id") == "msg-42") == 1
    ev_lines = [decode(ln, "logs/events.jsonl") for ln in brain.read("logs/events.jsonl").splitlines() if ln.strip()]
    assert any(isinstance(ln, dict) and ln.get("kind") == "loop.message" for ln in ev_lines)

    # Re-ingesting identical payload without explicit `updated` preserves original `updated` timestamp!
    res2 = records.ingest(brain, "jira", batch1, now=t2)
    assert (res2["added"], res2["updated"], res2["unchanged"]) == (0, 0, 1)
    assert _load_source(brain, "jira")[0]["updated"] == "2026-11-15T09:00:00Z"

    # Modifying title updates record and advances `updated` to t2.
    batch2 = json.dumps([{"id": "J-1", "title": "Fix SSO login", "priority": "P0"}]).encode()
    res3 = records.ingest(brain, "jira", batch2, now=t2)
    assert (res3["added"], res3["updated"], res3["unchanged"]) == (0, 1, 0)
    assert _load_source(brain, "jira")[0]["updated"] == "2026-11-15T10:00:00Z"

    # `memories/<source>.forgotten` skips new records and deletes already-saved forgotten records (deleting empty jsonl)!
    brain.write("memories/jira.forgotten", b"# comment\nJ-1\nJ-2\n")
    res_forg = records.ingest(
        brain,
        "jira",
        json.dumps([{"id": "J-2", "title": "Forgotten item", "priority": "P2"}]).encode(),
        now=t2,
    )
    assert (res_forg["forgotten"], res_forg["removed"], brain.exists(mem_path)) == (1, 1, False)

    # Removal guard on >= 10 records when > 50% would be removed, plus `incomplete` envelope skipping pruning!
    brain.write("schemas/catalog.json", OBJ_SCHEMA)
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
    assert (emptied["records"], emptied["removed"], brain.exists("memories/catalog.jsonl")) == (0, 4, False)


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
        ("valid", "upsert", b'[{"id":"1","title":"t","created":123}]', Error, "created must be an ISO"),
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
        ("valid", "upsert", b'[{"id":"1","title":"t","links":["not-a-link"]}]', Error, "links must use"),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"t","links":["concepts:welcome"]}]',
            Error,
            "invalid record reference",
        ),
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
            b'[{"id":"1","title":"t","links":[{"to":"jira:PROJ-1","anchor":"frag"}]}]',
            Error,
            "do not support #anchor fragments",
        ),
        ("valid", "upsert", b'[{"id":"1","title":"t","links":["bad_src:1"]}]', Error, "links must use"),
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
        ("valid", "upsert", b'[{"id":"1","title":"a"},{"id":"1","title":"b"}]', Error, "duplicate record id"),
        (
            "valid",
            "upsert",
            b'[{"id":"1","title":"a","links":["bf:///docs/index.md"]}]',
            Error,
            "must target a configured note directory",
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
    brain.write("schemas/valid.json", OBJ_SCHEMA)
    with pytest.raises(exc, match=match):
        records.ingest(brain, source, payload, mode=mode)  # ty: ignore[invalid-argument-type]


def test_record_links_dedupe_events_jsonl_compaction_and_cursor_healing(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain.write("schemas/mail.json", OBJ_SCHEMA)
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
    reing = records.ingest(brain, "mail", json.dumps([stored_mail]).encode())
    assert reing["unchanged"] == 1
    assert _load_source(brain, "missing") == []

    # Oversized events fail on `logs/events.jsonl` bounds BEFORE advancing sequence.
    records.append_event(brain, "test.ok", "t", {"id": "e0"})
    before_seq = records.latest_event_seq(brain)
    logs_dir = private_dir(brain, "logs")
    before_bytes = logs_dir.read("events.jsonl")
    with pytest.raises(Error, match="exceeds"):
        records.append_event(brain, "test.huge", "t", {"id": "e1", "blob": "x" * ((1 << 20) + 64)})
    assert records.latest_event_seq(brain) == before_seq
    assert logs_dir.read("events.jsonl") == before_bytes

    healed, latest = records.read_events(brain, after_seq=999_999)
    assert len(healed) >= 1
    assert latest == before_seq

    records.append_event(brain, "test.pad", "t", {"id": "pad", "blob": "y" * 70_000})
    assert records.latest_event_seq(brain) == before_seq + 1
    records.append_event(brain, "test.tail", "t", {"id": "tail"})
    assert records.latest_event_seq(brain) == before_seq + 2

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
    logs_dir.write("events.jsonl", seed_lines, mode=0o600, durable=False)
    ev200 = records.append_event(brain, "test.compact", "t", {"id": "c200"})
    assert ev200["seq"] == 200
    compacted, comp_latest = records.read_events(brain)
    assert (comp_latest, len(compacted)) == (200, 5)

    monkeypatch.setattr(records, "MAX_EVENTS", 1000)
    monkeypatch.setattr(records, "COMPACT_BYTES", 600)
    for idx in range(5):
        records.append_event(brain, "test.bytes", "t", {"id": f"b{idx}", "pad": "z" * 90})
    assert len(logs_dir.read("events.jsonl")) <= 300
    records.append_event(brain, "test.bytes", "t", {"id": "b5", "pad": "z" * 90})
    assert 300 < len(logs_dir.read("events.jsonl")) < 600

    logs_dir.write("events.jsonl", b"", mode=0o600, durable=False)
    assert records.latest_event_seq(brain) == 0
    logs_dir.write("events.jsonl", b"\n\n", mode=0o600, durable=False)
    assert records.latest_event_seq(brain) == 0
    logs_dir.delete("events.jsonl")
    assert records.read_events(brain) == ([], 0)
    first_dedup = records.append_event(brain, "test.dedup", "t", {"id": "d1"}, dedupe=True)
    assert records.append_event(brain, "test.dedup", "t", {"id": "d1"}, dedupe=True)["id"] == first_dedup["id"]

    sec_ev = records.append_event(
        brain, "test.sec", "t", {"id": "s-1", "OPENAI_API_KEY": "sk-secretvalue1234", "msg": "Bearer tok99999999"}
    )
    assert sec_ev["data"]["OPENAI_API_KEY"] == "[REDACTED]"
    assert "tok99999999" not in sec_ev["data"]["msg"]
    assert b"sk-secretvalue1234" not in logs_dir.read("events.jsonl")
    assert b"sk-secretvalue1234" not in brain.read("logs/events.jsonl")
    with pytest.raises(Error, match="event kind"):
        records.append_event(brain, "BAD KIND", "t", {})
    with pytest.raises(Error, match="event source"):
        records.append_event(brain, "ok.ev", "BAD_SRC", {})

    (logs_dir.root / "events.jsonl").unlink()
    (logs_dir.root / "events.jsonl").symlink_to(brain.root / "bf.yaml")
    with pytest.raises(Error, match="symlinks are not followed"):
        records.append_event(brain, "test.sym", "t", {"id": "s1"})
    (logs_dir.root / "events.jsonl").unlink()
    os.mkfifo(logs_dir.root / "events.jsonl", 0o600)
    with pytest.raises(Error, match="expected a regular file"):
        records.latest_event_seq(brain)
    with pytest.raises(Error, match="expected a regular file"):
        records.append_event(brain, "test.fifo", "t", {"id": "f1"})
    (logs_dir.root / "events.jsonl").unlink()


def test_ingest_atomic_jsonl_write_and_url_records(brain: Store) -> None:
    brain.write("schemas/ext.json", OBJ_SCHEMA)
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
        "jira:https://example.com/issue/1": "record",
        "https://example.com/page#frag": "url",
        "https://example.com/other#sec": "url",
        "concepts/welcome.md": "note",
    }


def test_v32_usages_hash_id_and_stderr_logger_edges(brain: Store) -> None:
    # `#` is rejected in record IDs for both memories and usages.
    brain.write("schemas/hashsrc.json", OBJ_SCHEMA)
    with pytest.raises(Error, match="#"):
        records.ingest(brain, "hashsrc", b'[{"id":"bad#id","title":"Bad"}]')

    # Missing `schemas/usages.json` fails closed on `target="usages"`.
    brain.delete("schemas/usages.json")
    with pytest.raises(Error, match=r"schemas/usages\.json"):
        records.ingest(brain, "llm", b'[{"id":"u1","title":"U1"}]', target="usages")
    brain.write("schemas/usages.json", OBJ_SCHEMA)
    assert (
        records.ingest(brain, "llm", b'[{"id":"u1","title":"U1","aliases":["tok:1"]}]', target="usages")["added"] == 1
    )

    # `make_stderr_logger` handles blank lines, malformed JSON, JSON lists, single-line & multi-line PEM blocks.
    cb = records.make_stderr_logger(brain, "demo", "run-1")
    dash = b"-" * 5
    pem_hdr = b"".join((dash, b"BEGIN ", b"PRIVATE KEY", dash))
    pem_ftr = b"".join((dash, b"END ", b"PRIVATE KEY", dash))
    pem_rsa_hdr = b"".join((dash, b"BEGIN ", b"RSA PRIVATE KEY", dash))
    pem_rsa_ftr = b"".join((dash, b"END ", b"RSA PRIVATE KEY", dash))
    for raw in (
        b"   \n",
        b"{not-json\n",
        b"[1,2]\n",
        b'{"message":"safe","time_iso":"2026-11-20T00:00:00Z","loop":"spoof","run":"spoof"}\n',
        pem_hdr + b"SECRET1" + pem_ftr + b"\n",
        pem_rsa_hdr + b"\n",
        b"LINE2_SECRET\n",
        pem_rsa_ftr + b" " + pem_hdr + b"\n",
        b"CHAINED_SECRET\n",
        pem_ftr + b"\n",
        pem_hdr + b"\n",
        b"X" * 17000 + b"\n",
        b"after pem ok\n",
    ):
        cb(raw)
    logged = brain.read("logs/demo.jsonl").decode()
    assert "SECRET1" not in logged
    assert "LINE2_SECRET" not in logged
    assert "CHAINED_SECRET" not in logged
    assert '"loop":"spoof"' not in logged
    assert '"message":"safe"' in logged
    assert "after pem ok" in logged

    # `Store.iter_lines` guards missing files, symlinks, FIFOs, and oversized files.
    with pytest.raises(FileNotFoundError):
        list(brain.iter_lines("memories/no-such.jsonl"))
    (brain.root / "memories" / "sym.jsonl").symlink_to(brain.root / "bf.yaml")
    with pytest.raises(Error, match="symlinks"):
        list(brain.iter_lines("memories/sym.jsonl"))
    (brain.root / "memories" / "sym.jsonl").unlink()
    os.mkfifo(brain.root / "memories" / "pipe.jsonl", 0o600)
    with pytest.raises(Error, match="expected a regular file"):
        list(brain.iter_lines("memories/pipe.jsonl"))
    (brain.root / "memories" / "pipe.jsonl").unlink()
    brain.write("memories/tiny.jsonl", b"x" * 64)
    with pytest.raises(Error, match="exceeds"):
        list(brain.iter_lines("memories/tiny.jsonl", limit=16))
    brain.delete("memories/tiny.jsonl")

    # `append_event` rolls back `logs/events.jsonl` via `ftruncate` if writing fails mid-line!
    records.append_event(brain, "pre.ok", "t", {"id": "p1"})
    before_ev = brain.read("logs/events.jsonl")
    orig_fdopen = os.fdopen

    class _ShortFile:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def fileno(self) -> int:
            return self._inner.fileno()

        def write(self, _view: Any) -> int:
            self._inner.write(b"{partial")
            raise OSError("disk full")

        def __enter__(self) -> _ShortFile:
            return self

        def __exit__(self, *a: Any) -> None:
            self._inner.close()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            os,
            "fdopen",
            lambda fd, mode="r", **kw: (
                _ShortFile(orig_fdopen(fd, mode, **kw)) if mode == "wb" else orig_fdopen(fd, mode, **kw)
            ),
        )
        with pytest.raises(OSError, match="disk full"):
            records.append_event(brain, "fail.disk", "t", {"id": "p2"})
    assert brain.read("logs/events.jsonl") == before_ev

    class _ZeroFile(_ShortFile):
        def write(self, _view: Any) -> int:
            return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            os,
            "fdopen",
            lambda fd, mode="r", **kw: (
                _ZeroFile(orig_fdopen(fd, mode, **kw)) if mode == "wb" else orig_fdopen(fd, mode, **kw)
            ),
        )
        with pytest.raises(Error, match="short write"):
            records.append_event(brain, "fail.zero", "t", {"id": "p3"})
    assert brain.read("logs/events.jsonl") == before_ev
