"""Tests for native+optional JSON Schemas, JSONL ingestion, disposable FTS5 index, .forgotten, snapshots, and `.bf/state/events.jsonl`."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bf import engine, index, models, records, schemas, state
from bf.models import FORMAT, MAX_BATCH, NOTES_FILE, Error, InputError, decode
from bf.storage import Store, private_dir


def test_schemas_native_validator_external_ref_and_optional_domain_schemas(brain: Store) -> None:
    for kind in ("bf", "record"):
        doc = schemas.document(kind)
        assert doc["$schema"].endswith("2020-12/schema")

    assert schemas.BF_SCHEMA["properties"]["version"]["const"] == FORMAT
    assert schemas.get_schema("bf") == schemas.document("bf")
    assert schemas.get_schema("record.schema.json") == schemas.document("record")
    assert set(schemas.export_schemas()) == {"bf", "record"}
    for bad in ("unknown", "evals", "concepts", "usages"):
        with pytest.raises(InputError, match="unknown schema"):
            schemas.get_schema(bad)

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
    assert schemas.validate_core_record(compact_note, "note") == []
    assert schemas.validate_instance(rec_val, compact_note, "note") == []
    assert any(
        "missing required 'id'" in e for e in schemas.validate_core_record({**compact_note, "kind": "memory"}, "mem")
    )
    assert any("must be one of" in e for e in schemas.validate_core_record({**compact_note, "kind": "usage"}, "mem"))

    # Native core validator enforces real calendar dates (rejecting 2026-02-29), strict URLs, and trailing-newline checks!
    for bad_dt in ("2026-02-29", "2026-02-31", "2026-04-31", "2026-13-01", "2026-00-10"):
        assert schemas.validate_core_record({**compact_note, "created": bad_dt}, "note")
        assert schemas.validate_core_record({**compact_note, "updated": bad_dt}, "note")
        assert schemas.validate_core_record({**compact_note, "stale_after": bad_dt}, "note")
    for bad_tm in ("2026-02-31T10:00:00Z", "2026-04-31T00:00:00Z", "2026-02-29T10:00:00Z"):
        assert schemas.validate_core_record({**compact_note, "created": bad_tm}, "note")
    for good_dt in ("2026-04-30", "2024-02-29", "2026-04-30T12:00:00Z"):
        assert schemas.validate_core_record({**compact_note, "created": good_dt}, "note") == []
        assert schemas.validate_core_record({**compact_note, "updated": good_dt}, "note") == []
    assert schemas.validate_core_record({**compact_note, "url": "file:///etc/passwd"}, "note")
    assert schemas.validate_core_record({**compact_note, "source": "bad\n"}, "note")

    with pytest.raises(Error, match="must be a JSON object"):
        schemas.compile_schema(["not", "an", "object"], "bad.json")
    with pytest.raises(Error, match="invalid JSON Schema"):
        schemas.compile_schema({"type": "not-a-json-schema-type"}, "bad.json")

    # External unresolvable $ref in user schema surfaces cleanly as a schema error instead of crashing!
    ext_ref_val = schemas.compile_schema({"$ref": "https://example.invalid/missing.json"}, "ext.json")
    ext_errs = schemas.validate_instance(ext_ref_val, {"a": 1}, "rec")
    assert any("unresolvable external $ref" in e for e in ext_errs)

    assert schemas.load_validator(brain, "schemas/nonexistent.json") is None

    # Non-HTTP record IDs reject `?` and `//` prefixes, while HTTP URL record IDs preserve `?`.
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


def test_ingest_optional_schema_explicit_schema_ingested_at_forgotten_snapshot_and_events(brain: Store) -> None:
    t1 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)

    # Optional schemas: ingesting without `schemas/freeform.json` succeeds via native core validation!
    free = records.ingest(brain, "freeform", b'[{"id":"f1","title":"Freeform"}]', now=t1)
    assert (free["ok"], free["added"]) == (True, 1)
    assert _load_source(brain, "freeform")[0]["ingested_at"] == "2026-11-15T09:00:00Z"
    assert "updated" not in _load_source(brain, "freeform")[0]

    # Explicit `--schema` fails closed if missing, and validates against shared schema when present!
    with pytest.raises(Error, match="explicitly requested schema not found"):
        records.ingest(brain, "jira-2026", b'[{"id":"J-1","title":"Bug"}]', schema_name="jira", now=t1)

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
    # Year-partitioned source sharing `schemas/jira.json` via `schema_name="jira"`:
    assert (
        records.ingest(
            brain, "jira-2026", b'[{"id":"J-9","title":"Bug 2026","priority":"P1"}]', schema_name="jira", now=t1
        )["added"]
        == 1
    )

    batch1 = json.dumps(
        [
            {
                "id": "J-1",
                "title": "Fix login",
                "text": "Details",
                "priority": "P1",
                "url": "",
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
    assert saved1[0]["ingested_at"] == "2026-11-15T09:00:00Z"
    assert "updated" not in saved1[0]
    assert "url" not in saved1[0]
    assert any(lnk["to"] == "meetings:decision-1" and lnk["meta"] == {"rel": "follows"} for lnk in saved1[0]["links"])
    assert any(lnk["to"] == "bf://other/meetings:decision-1" and lnk["kind"] == "brain" for lnk in saved1[0]["links"])
    # Disposable SQLite FTS5 index was updated incrementally on ingest!
    assert not index.is_stale(brain, records.valid_memory_sources(brain))

    # Ingesting new records emits `records.ingested` in `.bf/state/events.jsonl`, and `dedupe=True` returns `(ev, deduped)`!
    ev1, dup1 = state.append_event(brain, "loop.message", "chat", {"id": "msg-42", "text": "hi"}, dedupe=True, now=t1)
    ev2, dup2 = state.append_event(brain, "loop.message", "chat", {"id": "msg-42", "text": "hi"}, dedupe=True, now=t2)
    assert (dup1, dup2) == (False, True)
    assert ev1 == ev2
    ev_items, latest_seq = state.read_events(brain)
    assert latest_seq == state.latest_event_seq(brain)
    assert sum(1 for e in ev_items if e["data"].get("id") == "msg-42") == 1
    bf_dir = private_dir(brain, ".bf", "state")
    ev_lines = [decode(ln, ".bf/state/events.jsonl") for ln in bf_dir.read("events.jsonl").splitlines() if ln.strip()]
    assert any(isinstance(ln, dict) and ln.get("kind") == "loop.message" for ln in ev_lines)

    # Re-ingesting identical payload preserves original `ingested_at` timestamp!
    res2 = records.ingest(brain, "jira", batch1, now=t2)
    assert (res2["added"], res2["updated"], res2["unchanged"]) == (0, 0, 1)
    assert _load_source(brain, "jira")[0]["ingested_at"] == "2026-11-15T09:00:00Z"

    # Modifying title updates record and advances `ingested_at` to t2.
    batch2 = json.dumps([{"id": "J-1", "title": "Fix SSO login", "priority": "P0"}]).encode()
    res3 = records.ingest(brain, "jira", batch2, now=t2)
    assert (res3["added"], res3["updated"], res3["unchanged"]) == (0, 1, 0)
    assert _load_source(brain, "jira")[0]["ingested_at"] == "2026-11-15T10:00:00Z"

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
        ("valid", "upsert", b'[{"id":"bad#id","title":"t"}]', Error, "#"),
        ("valid", "upsert", b'[{"id":"1","title":"t","source":"other"}]', Error, "source must match"),
        ("valid", "upsert", b'[{"id":"1","title":"t","ref":"valid:2"}]', Error, "ref must match"),
        ("valid", "upsert", b'[{"id":"1","title":"t","kind":"concept"}]', Error, "kind must equal 'memory'"),
        ("valid", "upsert", b'[{"id":"1","title":"t","text":123}]', Error, "text must be a string"),
        ("valid", "upsert", b'[{"id":"1","title":"t","url":123}]', Error, "url must be a string"),
        ("valid", "upsert", b'[{"id":"1","title":"t","url":"file:///etc/passwd"}]', Error, "https://"),
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
            "configured note directories without dotfiles",
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

    # Oversized events fail on `.bf/state/events.jsonl` bounds BEFORE advancing sequence.
    state.append_event(brain, "test.ok", "t", {"id": "e0"})
    before_seq = state.latest_event_seq(brain)
    bf_dir = private_dir(brain, ".bf", "state")
    before_bytes = bf_dir.read("events.jsonl")
    with pytest.raises(Error, match="exceeds"):
        state.append_event(brain, "test.huge", "t", {"id": "e1", "blob": "x" * ((1 << 20) + 64)})
    assert state.latest_event_seq(brain) == before_seq
    assert bf_dir.read("events.jsonl") == before_bytes

    healed, latest = state.read_events(brain, after_seq=999_999)
    assert len(healed) >= 1
    assert latest == before_seq

    state.append_event(brain, "test.pad", "t", {"id": "pad", "blob": "y" * 70_000})
    assert state.latest_event_seq(brain) == before_seq + 1
    state.append_event(brain, "test.tail", "t", {"id": "tail"})
    assert state.latest_event_seq(brain) == before_seq + 2

    monkeypatch.setattr(state, "MAX_EVENTS", 5)
    seed_lines = b"".join(
        (
            json.dumps(
                {"seq": s, "id": f"e-{s}", "time": "2026-11-15T00:00:00Z", "kind": "k", "source": "s", "data": {}}
            )
            + "\n"
        ).encode()
        for s in range(190, 200)
    )
    bf_dir.write("events.jsonl", seed_lines, mode=0o600, durable=False)
    ev200, _ = state.append_event(brain, "test.compact", "t", {"id": "c200"})
    assert ev200["seq"] == 200
    compacted, comp_latest = state.read_events(brain)
    assert (comp_latest, len(compacted)) == (200, 5)

    monkeypatch.setattr(state, "MAX_EVENTS", 1000)
    monkeypatch.setattr(state, "COMPACT_BYTES", 600)
    for idx in range(5):
        state.append_event(brain, "test.bytes", "t", {"id": f"b{idx}", "pad": "z" * 90})
    assert len(bf_dir.read("events.jsonl")) <= 300
    state.append_event(brain, "test.bytes", "t", {"id": "b5", "pad": "z" * 90})
    assert 300 < len(bf_dir.read("events.jsonl")) < 600

    bf_dir.write("events.jsonl", b"", mode=0o600, durable=False)
    assert state.latest_event_seq(brain) == 0
    bf_dir.write("events.jsonl", b"\n\n", mode=0o600, durable=False)
    assert state.latest_event_seq(brain) == 0
    bf_dir.delete("events.jsonl")
    assert state.read_events(brain) == ([], 0)
    first_dedup, d_first = state.append_event(brain, "test.dedup", "t", {"id": "d1"}, dedupe=True)
    second_dedup, d_second = state.append_event(brain, "test.dedup", "t", {"id": "d1"}, dedupe=True)
    assert (d_first, d_second) == (False, True)
    assert second_dedup["id"] == first_dedup["id"]

    sec_ev, _ = state.append_event(
        brain, "test.sec", "t", {"id": "s-1", "OPENAI_API_KEY": "sk-secretvalue1234", "msg": "Bearer tok99999999"}
    )
    assert sec_ev["data"]["OPENAI_API_KEY"] == "[REDACTED]"
    assert "tok99999999" not in sec_ev["data"]["msg"]
    assert b"sk-secretvalue1234" not in bf_dir.read("events.jsonl")
    with pytest.raises(Error, match="event kind"):
        state.append_event(brain, "BAD KIND", "t", {})
    with pytest.raises(Error, match="event source"):
        state.append_event(brain, "ok.ev", "BAD_SRC", {})

    (bf_dir.root / "events.jsonl").unlink()
    (bf_dir.root / "events.jsonl").symlink_to(brain.root / "bf.yaml")
    with pytest.raises(Error, match="symlinks are not followed"):
        state.append_event(brain, "test.sym", "t", {"id": "s1"})
    (bf_dir.root / "events.jsonl").unlink()
    os.mkfifo(bf_dir.root / "events.jsonl", 0o600)
    with pytest.raises(Error, match="expected a regular file"):
        state.latest_event_seq(brain)
    with pytest.raises(Error, match="expected a regular file"):
        state.append_event(brain, "test.fifo", "t", {"id": "f1"})
    (bf_dir.root / "events.jsonl").unlink()


def test_ingest_atomic_jsonl_write_url_records_and_stderr_logger_edges(brain: Store) -> None:
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

    # `make_stderr_logger` handles blank lines, malformed JSON, JSON lists, single-line & multi-line PEM blocks, and exact secrets.
    cb = state.make_stderr_logger(brain, "demo", "run-1", secrets=("exact_forwarded_secret_42",))
    dash = b"-" * 5
    pem_hdr = b"".join((dash, b"BEGIN ", b"PRIVATE KEY", dash))
    pem_ftr = b"".join((dash, b"END ", b"PRIVATE KEY", dash))
    pem_rsa_hdr = b"".join((dash, b"BEGIN ", b"RSA PRIVATE KEY", dash))
    pem_rsa_ftr = b"".join((dash, b"END ", b"RSA PRIVATE KEY", dash))
    for raw in (
        b"   \n",
        b"{not-json\n",
        b"[1,2]\n",
        b'{"message":"safe exact_forwarded_secret_42","time_iso":"2026-11-20T00:00:00Z","loop":"spoof","run":"spoof"}\n',
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
    for hidden in ("SECRET1", "LINE2_SECRET", "CHAINED_SECRET", "exact_forwarded_secret_42"):
        assert hidden not in logged
    assert '"loop":"spoof"' not in logged
    assert '"message":"safe [REDACTED]"' in logged
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

    # `append_event` rolls back `.bf/state/events.jsonl` via `ftruncate` if writing fails mid-line!
    state.append_event(brain, "pre.ok", "t", {"id": "p1"})
    bf_dir = private_dir(brain, ".bf", "state")
    before_ev = bf_dir.read("events.jsonl")
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
            state.append_event(brain, "fail.disk", "t", {"id": "p2"})
    assert bf_dir.read("events.jsonl") == before_ev

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
            state.append_event(brain, "fail.zero", "t", {"id": "p3"})
    assert bf_dir.read("events.jsonl") == before_ev


def test_events_jsonl_tolerates_crash_tail_and_self_heals_durably(brain: Store) -> None:
    bf_dir = private_dir(brain, ".bf", "state")
    bf_dir.write("events.jsonl", b"", mode=0o600, durable=False)
    e1, _ = state.append_event(brain, "test.one", "t", {"id": "c1"})
    valid_bytes = bf_dir.read("events.jsonl")

    # Simulate an unclean host shutdown leaving trailing NUL bytes (no newline) at EOF.
    corrupt_bytes = valid_bytes + (b"\x00" * 512)
    bf_dir.write("events.jsonl", corrupt_bytes, mode=0o600, durable=False)

    # Read-only readers recover the last valid event without mutating disk.
    assert state.latest_event_seq(brain) == e1["seq"]
    evs, latest = state.read_events(brain)
    assert latest == e1["seq"]
    assert [e["id"] for e in evs] == [e1["id"]]
    assert bf_dir.read("events.jsonl") == corrupt_bytes

    # Appending a new event self-heals the corrupt tail atomically and fsyncs the append.
    fsync_calls: list[int] = []
    real_fsync = os.fsync
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "fsync", lambda fd: (fsync_calls.append(fd), real_fsync(fd))[1])
        e2, _ = state.append_event(brain, "test.two", "t", {"id": "c2"})
    assert e2["seq"] == e1["seq"] + 1
    assert fsync_calls
    repaired = bf_dir.read("events.jsonl")
    assert b"\x00" not in repaired
    assert [json.loads(ln)["seq"] for ln in repaired.splitlines()] == [e1["seq"], e2["seq"]]

    # A valid line missing a trailing newline is also repaired before appending, and all-NUL files reset cleanly.
    bf_dir.write("events.jsonl", repaired.rstrip(b"\n"), mode=0o600, durable=False)
    e3, _ = state.append_event(brain, "test.three", "t", {"id": "c3"})
    assert e3["seq"] == e2["seq"] + 1
    assert [json.loads(ln)["seq"] for ln in bf_dir.read("events.jsonl").splitlines()] == [
        e1["seq"],
        e2["seq"],
        e3["seq"],
    ]

    bf_dir.write("events.jsonl", b"\x00" * 64 + b"\n", mode=0o600, durable=False)
    assert state.latest_event_seq(brain) == 0
    e_reset, _ = state.append_event(brain, "test.reset", "t", {"id": "c4"})
    assert e_reset["seq"] == 1
    assert b"\x00" not in bf_dir.read("events.jsonl")

    # Non-positive seq entries are rejected as invalid and self-healed on append; fresh creation fsyncs file + dir.
    bf_dir.write("events.jsonl", b'{"seq":0,"id":"bad"}\n{"seq":-2,"id":"neg"}\n', mode=0o600, durable=False)
    assert state.latest_event_seq(brain) == 0
    assert state.read_events(brain) == ([], 0)
    bf_dir.delete("events.jsonl")
    create_fsyncs: list[int] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "fsync", lambda fd: (create_fsyncs.append(fd), real_fsync(fd))[1])
        e_fresh, _ = state.append_event(brain, "test.fresh", "t", {"id": "c5"})
    assert e_fresh["seq"] == 1
    assert len(create_fsyncs) >= 2


def test_corrupt_mark_stderr_time_iso_batch_emit_and_bool_ingest(brain: Store) -> None:
    # mark_unit_state recovers corrupt .bf/state/<loop>.json instead of crashing.
    private_dir(brain, ".bf", "state").write("broken.json", b"{corrupt", mode=0o600)
    state.mark_unit_state(brain, "broken", status="running")
    assert state.load_unit_state(brain, "broken")["status"] == "running"

    # Structured stderr containing both time and time_iso does not raise TypeError.
    state.append_loop_stderr(
        brain,
        "stderr-ts",
        "r1",
        b'{"time":"2026-11-23T10:00:00Z","time_iso":"2026-11-23T09:00:00Z","severity":"INFO","level":"WARN","message":"hello","event":"alt"}\n',
    )
    logged = json.loads(brain.read("logs/stderr-ts.jsonl").splitlines()[-1])
    assert logged["message"] == "hello"
    assert logged["time"] == "2026-11-23T10:00:00Z"

    # emit_output_events batches deduplicated lines, strips `kind` from `data`, and returns actual written count (2, not 3!).
    payload = b'{"kind":"chat.msg","id":"m1","v":1}\n{"kind":"chat.msg","id":"m1","v":1}\n{"id":"m2","v":2}\n'
    assert state.emit_output_events(brain, "chat", "loop.message", payload) == 2
    evs, _ = state.read_events(brain)
    chat_evs = [e for e in evs if e["source"] == "chat"]
    assert [(e["kind"], e["data"]) for e in chat_evs] == [
        ("chat.msg", {"id": "m1", "v": 1}),
        ("loop.message", {"id": "m2", "v": 2}),
    ]

    # ingest detects bool <-> int changes via canonical JSON byte comparison.
    r1 = records.ingest(brain, "bools", b'[{"id":"1","title":"One","flag":1}]')
    assert r1["added"] == 1
    r2 = records.ingest(brain, "bools", b'[{"id":"1","title":"One","flag":true}]')
    assert r2["updated"] == 1
    assert r2["unchanged"] == 0

    # Oversized emit_output_events batch compacts in-place across the 200-stride boundary without exceeding MAX_EVENTS_FILE.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(state, "MAX_EVENTS", 5)
        mp.setattr(state, "COMPACT_BYTES", 2048)
        big_batch = b"\n".join(f'{{"n":{i}}}'.encode() for i in range(12)) + b"\n"
        assert state.emit_output_events(brain, "burst", "loop.burst", big_batch) == 12
        evs_after, latest = state.read_events(brain)
        assert len(evs_after) <= 5
        assert evs_after[-1]["seq"] == latest
        # Externally oversized events.jsonl still self-heals on next append via read_tail fallback.
        bf_dir = private_dir(brain, ".bf", "state")
        raw_cur = bf_dir.read("events.jsonl")
        mp.setattr(state, "MAX_EVENTS_FILE", max(len(raw_cur) - 1, 1))
        e_ok, _ = state.append_event(brain, "test.recover", "t", {"x": 1})
        assert e_ok["seq"] == latest + 1


def test_validate_core_record_exhaustive_and_index_edges(tmp_path: Path, brain: Store) -> None:
    import sqlite3

    from bf import index

    base = {"kind": "note", "ref": "concepts/a.md", "source": "concepts", "title": "A"}
    assert schemas.validate_core_record("not-a-dict", "x") == ["x: expected type object"]
    assert schemas.validate_core_record(base, "ok") == []
    assert schemas.validate_instance(schemas.compile_schema(schemas.RECORD_SCHEMA, "record.json"), base, "ok") == []

    # String length & type checks, URL, dates, stale_after, tags, aliases, attributes, sha256, headings, links, tasks.
    bad_cases = [
        {**base, "title": 123},
        {**base, "title": ""},
        {**base, "title": "x" * 4097},
        {**base, "source": "BAD"},
        {**base, "url": 123},
        {**base, "created": 123},
        {**base, "stale_after": 30},
        {**base, "stale_after": "30d\n"},
        {**base, "stale_after": "0s"},
        {**base, "tags": "nope"},
        {**base, "tags": [f"t{i}" for i in range(201)]},
        {**base, "tags": ["bad/tag"]},
        {**base, "tags": ["dup", "dup"]},
        {**base, "aliases": "nope"},
        {**base, "aliases": [f"a{i}" for i in range(201)]},
        {**base, "aliases": [""]},
        {**base, "attributes": "nope"},
        {**base, "sha256": "short"},
        {**base, "headings": "nope"},
        {**base, "headings": ["not-dict"]},
        {**base, "headings": [{"id": "h", "title": "H", "level": 1, "extra": 1}]},
        {**base, "headings": [{"id": "", "title": "H", "level": 1}]},
        {**base, "headings": [{"id": "h", "title": 1, "level": 1}]},
        {**base, "headings": [{"id": "h", "title": "H", "level": 7}]},
        {**base, "headings": [{"id": "h", "title": "H", "level": 1, "meta": "bad"}]},
        {**base, "headings": [{"id": "h", "title": "H", "level": 1, "meta": {"1bad": "v"}}]},
        {**base, "headings": [{"id": "h", "title": "H", "level": 1, "meta": {"k": 1}}]},
        {**base, "links": "nope"},
        {**base, "links": [{"to": "https://example.com", "kind": "url"}] * 2001},
        {**base, "links": ["not-dict"]},
        {**base, "links": [{"to": "https://example.com", "kind": "url", "extra": 1}]},
        {**base, "links": [{"to": "", "kind": "url"}]},
        {**base, "links": [{"to": "https://example.com", "kind": "bad"}]},
        {**base, "links": [{"to": "https://example.com", "kind": "url", "anchor": 1}]},
        {**base, "links": [{"to": "https://example.com", "kind": "url", "section": 1}]},
        {**base, "links": [{"to": "https://example.com", "kind": "url", "meta": {"k": 1}}]},
        {**base, "tasks": "nope"},
        {**base, "tasks": ["not-dict"]},
        {**base, "tasks": [{"text": "Do", "done": False, "extra": 1}]},
        {**base, "tasks": [{"text": "", "done": False}]},
        {**base, "tasks": [{"text": "Do", "done": "no"}]},
        {**base, "tasks": [{"text": "Do", "done": False, "section": 1}]},
        {**base, "tasks": [{"text": "Do", "done": False, "meta": {"k": 1}}]},
    ]
    for case in bad_cases:
        assert schemas.validate_core_record(case, "rec"), case

    # > 5 errors cap on validate_core_record and schema=False / bad regex on validate_instance!
    many_bad = {"title": "", "source": "BAD", "url": 1, "created": 1, "tags": "x", "aliases": "x", "sha256": "x"}
    many_errs = schemas.validate_core_record(many_bad, "many")
    assert any("more schema errors" in e for e in many_errs)

    forb = schemas.compile_schema({"type": "object", "properties": {"x": False}}, "forb.json")
    assert any("property is forbidden" in e for e in schemas.validate_instance(forb, {"x": 1}, "r"))
    with pytest.raises(Error, match="invalid JSON Schema"):
        schemas.compile_schema({"type": "string", "pattern": "[unclosed"}, "re.json")
    from jsonschema import Draft202012Validator

    raw_bad_re = Draft202012Validator({"type": "string", "pattern": "[unclosed"})
    assert any("invalid regular expression" in e for e in schemas.validate_instance(raw_bad_re, "abc", "r"))

    # `index` edge cases: FTS query syntax, delta fallback on stale pre_stat, schema version rebuild, loose perms & symlink guard.
    assert index.build_fts_query('"exact phrase" ret* plain') == '"exact phrase" AND "ret"* AND "plain"'
    assert index.build_fts_query("!!!") == ""

    # Delta sync falls back cleanly when pre_stat doesn't match and deletes bucket when file absent.
    from contextlib import closing

    db_path = brain.root / ".bf" / "cache" / index.INDEX_NAME
    # Mutate SQLite directly to verify stale `pre_stat=(0, 0, 0)` triggers full rebuild from canonical JSONL!
    with closing(sqlite3.connect(str(db_path))) as con, con:
        con.execute("DELETE FROM records WHERE source = 'meetings'")
    index.sync_source_delta(
        brain,
        "fixture",
        "meetings",
        "memories/meetings.jsonl",
        pre_stat=(0, 0, 0),
        changed_rows=[],
        row_offsets={},
        removed_refs=[],
        total_count=2,
    )
    con_ro = index.open_readonly(brain)
    assert con_ro is not None
    try:
        assert index.lookup_ref(con_ro, brain, "meetings:decision-1") is not None
    finally:
        con_ro.close()

    # Delta update, mid-file offset shift, & snapshot removal evict stale FTS/aliases/links while keeping sibling rows hydrated!
    records.ingest(
        brain,
        "delta-src",
        b'[{"id":"1","title":"OldAlpha","text":"unique_old_term","aliases":["old-al"],"links":["concepts/architecture.md"]},'
        b'{"id":"2","title":"SiblingKeep","text":"keep_sibling_term"}]',
    )
    records.ingest(
        brain,
        "delta-src",
        b'[{"id":"0","title":"InsertedFirst","text":"shift_before_sibling"},'
        b'{"id":"1","title":"NewBeta","text":"unique_new_term","aliases":["new-al"]}]',
    )
    con_ro = index.open_readonly(brain)
    assert con_ro is not None
    try:
        assert index.search_fts(con_ro, brain, query_text="unique_old_term")[0] == []
        assert len(index.search_fts(con_ro, brain, query_text="unique_new_term")[0]) == 1
        sib_hits, _ = index.search_fts(con_ro, brain, query_text="keep_sibling_term")
        assert len(sib_hits) == 1
        assert sib_hits[0][0]["ref"] == "delta-src:2"
        assert sib_hits[0][0]["title"] == "SiblingKeep"
        assert index.lookup_alias(con_ro, brain, "old-al") is None
        assert index.lookup_alias(con_ro, brain, "new-al") is not None
        assert con_ro.execute("SELECT 1 FROM links WHERE src_ref = 'delta-src:1'").fetchone() is None
    finally:
        con_ro.close()
    records.ingest(brain, "delta-src", b"[]", mode="snapshot", allow_removal=True)
    con_ro = index.open_readonly(brain)
    assert con_ro is not None
    try:
        assert index.search_fts(con_ro, brain, query_text="unique_new_term")[0] == []
        assert index.lookup_ref(con_ro, brain, "delta-src:2") is None
    finally:
        con_ro.close()

    # Absent file deletes bucket in `sync_source_delta` and `sync_all`.
    index.sync_source_delta(
        brain,
        "fixture",
        "gone",
        "memories/gone.jsonl",
        pre_stat=None,
        changed_rows=[],
        row_offsets={},
        removed_refs=[],
        total_count=0,
    )
    index.sync_all(brain, "fixture", ["meetings"])

    # Wrong user_version -> open_readonly returns None; _connect_rw drops and rebuilds cleanly!
    with closing(sqlite3.connect(str(db_path))) as con, con:
        con.execute("PRAGMA user_version = 99")
    assert index.open_readonly(brain) is None
    assert index.get_meta(brain, "brain") == ""
    index.sync_all(brain, "fixture", ["meetings"])
    assert index.get_meta(brain, "brain") == "fixture"

    # Corrupt non-SQLite `.bf/cache/index.sqlite3` (with stale `-journal` sidecar) self-heals on write!
    db_path.write_bytes(b"not a sqlite database")
    db_path.with_name(f"{db_path.name}-journal").write_bytes(b"stale journal")
    assert index.open_readonly(brain) is None
    index.sync_all(brain, "fixture", ["meetings"])
    assert index.get_meta(brain, "brain") == "fixture"
    assert not db_path.with_name(f"{db_path.name}-journal").exists()

    # `sync_all` SAVEPOINT evicts a newly corrupted source while preserving valid sibling sources!
    brain.write("memories/corrupt-src.jsonl", b"{not valid json\n")
    index.sync_all(brain, "fixture", ["meetings", "corrupt-src"])
    con_ro = index.open_readonly(brain)
    assert con_ro is not None
    try:
        assert index.lookup_ref(con_ro, brain, "meetings:decision-1") is not None
        assert con_ro.execute("SELECT 1 FROM sources WHERE source = 'corrupt-src'").fetchone() is None
    finally:
        con_ro.close()
    brain.delete("memories/corrupt-src.jsonl")

    # `is_stale()` returns True when all JSONL files are removed from disk while SQLite still holds rows!
    empty_b = tmp_path / "empty-stale"
    engine.init_brain(empty_b, name="empty-stale")
    est = Store(empty_b)
    assert not index.is_stale(est, [])
    est.delete(NOTES_FILE)
    assert index.is_stale(est, []) is True

    # BF-01 & BF-02: invisible Unicode dict-key collision overwrite rejected, and tampered JSONL cannot disclose `.env` via `bf read --format raw`!
    brain.write(".env", b"TOP_SECRET=super_secret_env_value\n", mode=0o600)
    with pytest.raises(Error, match="unsafe control or invisible"):
        records.ingest(
            brain,
            "spoof",
            json.dumps(
                [
                    {
                        "id": "1",
                        "title": "t",
                        "aliases": ["env-target"],
                        "ki\u200bnd": "note",
                        "re\u200bf": ".env",
                    }
                ]
            ).encode(),
        )
    # Even if `memories/tampered.jsonl` is modified directly on disk with `kind: "note"` and `ref: ".env"`,
    # neither indexed nor scan-fallback `bf read --format raw` ever reads `.env`!
    from bf import query as q_mod

    brain.write(
        "memories/tampered.jsonl",
        b'{"id":"1","ref":".env","source":"tampered","kind":"note","title":"T","text":"safe-only","aliases":["env-leak"]}\n',
    )
    index.sync_all(brain, "fixture", ["meetings", "tampered"])
    raw_idx = q_mod.read_target(brain, "env-leak", fmt="raw")
    assert "TOP_SECRET" not in raw_idx
    assert raw_idx == "safe-only"
    brain.delete(f".bf/cache/{index.INDEX_NAME}")
    raw_scan = q_mod.read_target(brain, "env-leak", fmt="raw")
    assert "TOP_SECRET" not in raw_scan
    assert raw_scan == "safe-only"
    brain.delete("memories/tampered.jsonl")

    # F1: nested `"attributes": {"id": ...}` sorting before top-level `"id"` never collides or drops records,
    # escaped IDs round-trip, and corrupt existing JSONL lines fail closed!
    records.ingest(
        brain,
        "nested-id",
        json.dumps(
            [
                {"id": "rec-1", "title": "First", "attributes": {"id": "shared-org", "k": 1}},
                {"id": "rec-2", "title": "Second", "attributes": {"id": "shared-org", "k": 2}},
                {"id": 'esc\\"id', "title": "Escaped"},
            ]
        ).encode(),
    )
    upd_nest = records.ingest(
        brain,
        "nested-id",
        json.dumps(
            [
                {"id": "rec-1", "title": "First Updated", "attributes": {"id": "shared-org"}},
                {"id": 'esc\\"id', "title": "Escaped"},
            ]
        ).encode(),
    )
    assert (upd_nest["records"], upd_nest["updated"], upd_nest["added"], upd_nest["unchanged"]) == (3, 1, 0, 1)
    assert engine.validate(brain)["ok"] is True
    brain.write("memories/nested-id.jsonl", b'{"id":"ok", broken}\n')
    with pytest.raises(Error, match="invalid JSON"):
        records.ingest(brain, "nested-id", b'[{"id":"2","title":"T"}]')
    brain.delete("memories/nested-id.jsonl")

    # Mid-sync sqlite3.DatabaseError (corruption) recovery vs OperationalError (busy -> NEVER unlinks DB)!
    index.sync_all(brain, "fixture", ["meetings"])
    with closing(sqlite3.connect(str(db_path))) as con_seed, con_seed:
        con_seed.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('pre_corrupt', '1')")
        con_seed.execute("UPDATE sources SET mtime_ns = 0 WHERE source = 'meetings'")
    db_path.with_name(f"{db_path.name}-journal").write_bytes(b"stale journal")
    fail_once = [True]
    real_ins = index._insert_records  # noqa: SLF001

    def _flaky_ins(*args: Any, **kwargs: Any) -> int:
        if fail_once[0]:
            fail_once[0] = False
            raise sqlite3.DatabaseError("simulated disk image malformed")
        return real_ins(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp_db:
        mp_db.setattr(index, "_insert_records", _flaky_ins)
        index.sync_all(brain, "fixture", ["meetings"])
    assert index.get_meta(brain, "brain") == "fixture"
    assert index.get_meta(brain, "pre_corrupt") == ""
    assert not db_path.with_name(f"{db_path.name}-journal").exists()
    con_post = index.open_readonly(brain)
    assert con_post is not None
    try:
        assert index.lookup_ref(con_post, brain, "meetings:decision-1") is not None
    finally:
        con_post.close()
    # Non-corruption OperationalError (SQLITE_BUSY) MUST NOT unlink existing DB!
    with closing(sqlite3.connect(str(db_path))) as con_s2, con_s2:
        con_s2.execute("UPDATE sources SET mtime_ns = 0 WHERE source = 'meetings'")
    ino_before_busy = db_path.stat().st_ino
    with pytest.MonkeyPatch.context() as mp_busy:
        mp_busy.setattr(
            index,
            "_insert_records",
            lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
        )
        with pytest.raises(Error, match="SQLite index error"):
            index.sync_all(brain, "fixture", ["meetings"])
    assert db_path.is_file()
    assert db_path.stat().st_ino == ino_before_busy

    with pytest.MonkeyPatch.context() as mp_db2:
        mp_db2.setattr(
            index,
            "_insert_records",
            lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.DatabaseError("persistent malformed corruption")),
        )
        with pytest.raises(Error, match="SQLite index error"):
            index.sync_all(brain, "fixture", ["meetings"])
    index.sync_all(brain, "fixture", ["meetings"])

    # `is_source_fresh` for absent file (True if also absent in DB, False if still indexed) + blank line in JSONL!
    con_fr = index.open_readonly(brain)
    assert con_fr is not None
    try:
        assert index.is_source_fresh(con_fr, brain, "never-existed", "memories/never-existed.jsonl") is True
        assert index.is_source_fresh(con_fr, brain, "meetings", "memories/missing-file.jsonl") is False
    finally:
        con_fr.close()
    brain.write(
        "memories/blankln.jsonl", b'\n{"id":"1","ref":"blankln:1","source":"blankln","kind":"memory","title":"B"}\n\n'
    )
    index.sync_all(brain, "fixture", ["meetings", "blankln"])
    brain.delete("memories/blankln.jsonl")

    # Missing `meta` / `sources` tables make `get_meta` return `""` and `is_stale` return `True` without raising!
    with closing(sqlite3.connect(str(db_path))) as con_drop, con_drop:
        con_drop.execute("DROP TABLE meta")
        con_drop.execute("DROP TABLE sources")
    assert index.get_meta(brain, "brain") == ""
    assert index.is_stale(brain, ["meetings"]) is True

    # Loose permissions (0644) rejected by open_readonly; symlink rejected by _connect_rw!
    index.sync_all(brain, "fixture", ["meetings"])
    db_path.chmod(0o644)
    assert index.open_readonly(brain) is None
    db_path.chmod(0o600)
    db_path.unlink()
    db_path.symlink_to(tmp_path / "external.db")
    with pytest.raises(Error, match="symlinks"):
        index.sync_all(brain, "fixture", ["meetings"])
    db_path.unlink()


def test_core_record_validator_schema_parity_and_sqlite_hardening(tmp_path: Path, brain: Store) -> None:
    """Suggestion `b`: lock `validate_core_record` <-> `RECORD_SCHEMA` parity and SQLite cache security/durability."""
    import sqlite3
    from contextlib import closing

    rec_val = schemas.compile_schema(schemas.RECORD_SCHEMA, "record.json")
    valid_note = {"kind": "note", "ref": "concepts/x.md", "source": "concepts", "title": "X"}
    valid_mem = {"kind": "memory", "id": "1", "ref": "src:1", "source": "src", "title": "M"}
    parity_cases: list[tuple[dict[str, Any], bool]] = [
        (valid_note, True),
        (valid_mem, True),
        ({**valid_note, "url": "https://example.com/a?b=1"}, True),
        ({**valid_note, "url": "mailto:alice@example.com"}, True),
        ({**valid_note, "url": "urn:ietf:rfc:3986"}, True),
        ({**valid_note, "created": "2026-11-15T10:00:00+02:00", "stale_after": "30d"}, True),
        ({**valid_note, "kind": None}, False),
        ({**valid_note, "kind": ["note"]}, False),
        ({**valid_note, "kind": 123}, False),
        ({**valid_note, "links": [{"to": "https://example.com", "kind": None}]}, False),
        ({**valid_note, "links": [{"to": "https://example.com", "kind": 123}]}, False),
        ({**valid_note, "url": "https://"}, False),
        ({**valid_note, "url": "HTTPS://example.com"}, False),
        ({**valid_note, "url": "mailto:"}, False),
        ({**valid_note, "url": "urn:"}, False),
        ({**valid_note, "created": "2026-11-15 10:00:00Z"}, False),
        ({**valid_note, "stale_after": "1d" * 20}, False),
    ]
    for doc, expected_ok in parity_cases:
        native_ok = schemas.validate_core_record(doc, "rec") == []
        schema_ok = schemas.validate_instance(rec_val, doc, "rec") == []
        assert (native_ok, schema_ok) == (expected_ok, expected_ok), doc

    # SQLite hardening: WAL headers (`\x02\x02` AND `\x01\x02`) refused by `open_readonly` without creating `-shm` on disk!
    index.sync_all(brain, "fixture", ["meetings"])
    db_path = brain.root / ".bf" / "cache" / index.INDEX_NAME
    with closing(sqlite3.connect(str(db_path))) as con_wal:
        con_wal.execute("PRAGMA journal_mode = WAL")
        con_wal.execute("CREATE TABLE IF NOT EXISTS _t (x INT)")
    db_path.with_name(f"{db_path.name}-wal").unlink(missing_ok=True)
    db_path.with_name(f"{db_path.name}-shm").unlink(missing_ok=True)
    assert index.open_readonly(brain) is None
    assert not db_path.with_name(f"{db_path.name}-shm").exists()
    # Specifically verify header bytes `18:20 == b"\x01\x02"` (read-version WAL) also fails closed without `-shm`!
    raw_hdr = bytearray(db_path.read_bytes())
    raw_hdr[18:20] = b"\x01\x02"
    db_path.write_bytes(bytes(raw_hdr))
    assert index.open_readonly(brain) is None
    assert not db_path.with_name(f"{db_path.name}-shm").exists()

    # Extended SQLite error codes (e.g. `SQLITE_CORRUPT_VTAB = 267`) recognized via `& 0xFF`!
    ext_err = sqlite3.DatabaseError("vtab")
    ext_err.sqlite_errorcode = 267
    assert index._is_corrupt(ext_err) is True  # noqa: SLF001

    # Sidecar symlink (`index.sqlite3-wal`) rejected by both `open_readonly` and `_connect_rw`!
    index.sync_all(brain, "fixture", ["meetings"])
    wal_sym = db_path.with_name(f"{db_path.name}-wal")
    wal_sym.symlink_to(tmp_path / "outside-wal")
    assert index.open_readonly(brain) is None
    with pytest.raises(Error, match="without symlinks"):
        index.sync_all(brain, "fixture", ["meetings"])
    wal_sym.unlink()

    # Untrusted SQLite `TRIGGER` or `VIEW` is rejected by `open_readonly` and self-healed on write!
    for ddl in (
        "CREATE VIEW evil_v AS SELECT 1",
        "CREATE TRIGGER evil_t AFTER INSERT ON meta BEGIN SELECT 1; END",
    ):
        index.sync_all(brain, "fixture", ["meetings"])
        with closing(sqlite3.connect(str(db_path))) as con_tr, con_tr:
            con_tr.execute(ddl)
        assert index.open_readonly(brain) is None
        index.sync_all(brain, "fixture", ["meetings"])
        con_ok = index.open_readonly(brain)
        assert con_ok is not None
        con_ok.close()

    # `_hydrate_rows` rejects tampered `sources.path` mismatch, and `sync_source_delta` deletes stray rows!
    with closing(sqlite3.connect(str(db_path))) as con_p, con_p:
        con_p.execute("UPDATE sources SET path = '.env' WHERE source = 'meetings'")
    con_ro_p = index.open_readonly(brain)
    assert con_ro_p is not None
    try:
        with pytest.raises(index.IndexStaleError, match="path mismatch"):
            index._hydrate_rows(con_ro_p, brain, [("meetings:decision-1", "meetings", "memory", 0, 10)])  # noqa: SLF001
    finally:
        con_ro_p.close()
    index.sync_all(brain, "fixture", ["meetings"])
    # Insert a stray row into `records` not present in `row_offsets` and verify `sync_source_delta` removes it!
    st_m = brain.stat("memories/meetings.jsonl")
    with closing(sqlite3.connect(str(db_path))) as con_st, con_st:
        con_st.execute(
            "INSERT INTO records(ref, source, kind, type, status, sort_ts, tags_text, file_offset, byte_len) "
            "VALUES ('meetings:stray', 'meetings', 'memory', '', '', '', '', 0, 10)"
        )
    index.sync_source_delta(
        brain,
        "fixture",
        "meetings",
        "memories/meetings.jsonl",
        pre_stat=(st_m.st_mtime_ns, st_m.st_size, st_m.st_ino),
        changed_rows=[],
        row_offsets={"meetings:decision-1": (0, st_m.st_size)},
        removed_refs=[],
        total_count=1,
    )
    con_st_ro = index.open_readonly(brain)
    assert con_st_ro is not None
    try:
        assert con_st_ro.execute("SELECT 1 FROM records WHERE ref = 'meetings:stray'").fetchone() is None
    finally:
        con_st_ro.close()

    # Best-effort cache: SQLite error during `records.ingest` STILL commits canonical JSONL, emits `records.ingested`, AND reports `index_warning`!
    seq_before = state.latest_event_seq(brain)
    with pytest.MonkeyPatch.context() as mp_be:
        mp_be.setattr(index, "sync_source_delta", lambda *_a, **_kw: (_ for _ in ()).throw(Error("cache offline")))
        be_res = records.ingest(brain, "durable-src", b'[{"id":"d1","title":"Durable"}]')
    assert be_res["ok"] is True
    assert be_res["added"] == 1
    assert "cache offline" in be_res["index_warning"]
    assert brain.exists("memories/durable-src.jsonl")
    assert state.latest_event_seq(brain) > seq_before
    # Per-source staleness ignores unrelated stale/fresh sources!
    assert index.is_stale(brain, ["meetings"], include_notes=False, exact_sources=False) is False
    assert index.is_stale(brain, ["durable-src"], include_notes=False, exact_sources=False) is True
    assert index.is_stale(brain, ["meetings", "durable-src"], ignore_sources=["durable-src"]) is False

    # `state.append_event(dry_run=True)` dedupe hit + oversize guard, and `emit_output_events` empty/bad-kind edges!
    ev_dry_seed, _ = state.append_event(brain, "dry.ev", "src", {"id": "d-dry"}, dedupe=True)
    ev_dry_hit, was_dup = state.append_event(brain, "dry.ev", "src", {"id": "d-dry"}, dedupe=True, dry_run=True)
    assert (was_dup, ev_dry_hit["seq"]) == (True, ev_dry_seed["seq"])
    with pytest.raises(Error, match="exceeds"):
        state.append_event(brain, "dry.big", "src", {"blob": "x" * (state.MAX_EVENT_LINE + 16)}, dry_run=True)
    assert state.emit_output_events(brain, "src", "loop.ev", b"  \n\n ") == 0
    with pytest.raises(Error, match="invalid event kind"):
        state.emit_output_events(brain, "src", "loop.ev", b'{"kind":"BAD KIND"}\n')

    # `records.ingest` guardrails (`--allow-removal` without snapshot, invalid/reserved `--schema`, `stale_after` ISO date vs non-string).
    with pytest.raises(InputError, match="snapshot"):
        records.ingest(brain, "sa", b"[]", mode="upsert", allow_removal=True)
    with pytest.raises(InputError, match="lowercase"):
        records.ingest(brain, "sa", b"[]", schema_name="BAD_SCHEMA")
    with pytest.raises(InputError, match="reserved"):
        records.ingest(brain, "sa", b"[]", schema_name="record")
    with pytest.raises(Error, match="stale_after"):
        records.ingest(brain, "sa", b'[{"id":"1","title":"T","stale_after":123}]')
    assert records.ingest(brain, "sa", b'[{"id":"1","title":"T","stale_after":"2026-12-01"}]')["added"] == 1


def test_records_ingested_refs_delayed_timers_bounds_and_list_events(tmp_path: Path, brain: Store) -> None:
    from datetime import UTC, datetime, timedelta

    # 1. `records.ingested` carries bounded sorted `added_refs`, `updated_refs`, and `refs_truncated`.
    batch = [{"id": f"r{i:03d}", "title": f"Item {i}"} for i in range(70)]
    seq0 = state.latest_event_seq(brain)
    res_add = records.ingest(brain, "many", json.dumps(batch).encode())
    assert res_add["added"] == 70
    evs, _ = state.read_events(brain, after_seq=seq0)
    ing_ev = next(e for e in evs if e["kind"] == "records.ingested" and e["source"] == "many")
    assert len(ing_ev["data"]["added_refs"]) == records.MAX_EVENT_REFS
    assert ing_ev["data"]["added_refs"][0] == "many:r000"
    assert ing_ev["data"]["refs_truncated"] is True
    assert "updated_refs" not in ing_ev["data"]

    seq1 = state.latest_event_seq(brain)
    res_upd = records.ingest(brain, "many", b'[{"id":"r001","title":"Updated 1"}]')
    assert res_upd["updated"] == 1
    evs_u, _ = state.read_events(brain, after_seq=seq1)
    upd_ev = next(e for e in evs_u if e["kind"] == "records.ingested" and e["source"] == "many")
    assert upd_ev["data"]["updated_refs"] == ["many:r001"]
    assert "refs_truncated" not in upd_ev["data"]

    # 2. `event_bounds` returns `(first_seq, latest_seq)` including >64 KiB first-line fallback and empty file.
    first_s, last_s = state.event_bounds(brain)
    assert 1 <= first_s <= last_s
    (tmp_path / "empty-ev").mkdir()
    empty_b = Store(tmp_path / "empty-ev")
    assert state.event_bounds(empty_b) == (0, 0)
    st_e = private_dir(empty_b, ".bf", "state")
    st_e.write(state.EVENTS_FILE, b"", mode=0o600)
    assert state.event_bounds(empty_b) == (0, 0)
    big_first = (
        json.dumps(
            {
                "seq": 42,
                "id": "e42",
                "time": "2026-11-25T00:00:00Z",
                "kind": "k.big",
                "source": "s",
                "data": {"b": "x" * 70_000},
            }
        ).encode()
        + b"\n"
    )
    st_e.write(state.EVENTS_FILE, big_first, mode=0o600)
    assert state.event_bounds(empty_b) == (42, 42)

    # 3. Delayed timers: `schedule_event`, dedupe, dry-run, `next_timer_delay`, `promote_due_timers`, caps, and `list_events`!
    t0 = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    assert state.next_timer_delay(empty_b, t0) is None
    assert state.promote_due_timers(empty_b, now=t0) == []

    tmr_dry, dup_dry = state.schedule_event(
        empty_b,
        "loop.wake",
        "dev",
        {"id": "ci-1", "reason": "check"},
        deliver_at=t0 + timedelta(minutes=10),
        dedupe=True,
        dry_run=True,
        now=t0,
    )
    assert (dup_dry, tmr_dry["kind"]) == (False, "loop.wake")
    assert state.read_timers(empty_b) == []

    tmr1, dup1 = state.schedule_event(
        empty_b,
        "loop.wake",
        "dev",
        {"id": "ci-1", "reason": "check"},
        deliver_at=t0 + timedelta(minutes=10),
        dedupe=True,
        now=t0,
    )
    tmr1_dup, dup2 = state.schedule_event(
        empty_b,
        "loop.wake",
        "dev",
        {"id": "ci-1", "reason": "check"},
        deliver_at=t0 + timedelta(minutes=15),
        dedupe=True,
        now=t0,
    )
    assert (dup1, dup2, tmr1_dup["id"]) == (False, True, tmr1["id"])
    tmr_dry_dup, dup_dry2 = state.schedule_event(
        empty_b,
        "loop.wake",
        "dev",
        {"id": "ci-1"},
        deliver_at=t0 + timedelta(minutes=20),
        dedupe=True,
        dry_run=True,
        now=t0,
    )
    assert (dup_dry2, tmr_dry_dup["id"]) == (True, tmr1["id"])

    tmr2, _ = state.schedule_event(
        empty_b, "loop.wake", "work", {"id": "rem-1"}, deliver_at=t0 + timedelta(minutes=2), now=t0
    )
    assert len(state.read_timers(empty_b)) == 2
    assert state.read_timers(empty_b)[0]["id"] == tmr2["id"]  # sorted by deliver_at!
    assert state.next_timer_delay(empty_b, t0) == pytest.approx(120.0, abs=1.0)
    assert state.promote_due_timers(empty_b, now=t0 + timedelta(minutes=1)) == []

    promoted1 = state.promote_due_timers(empty_b, now=t0 + timedelta(minutes=3))
    assert len(promoted1) == 1
    assert promoted1[0]["source"] == "work"
    assert len(state.read_timers(empty_b)) == 1
    promoted2 = state.promote_due_timers(empty_b, now=t0 + timedelta(minutes=12))
    assert len(promoted2) == 1
    assert promoted2[0]["source"] == "dev"
    assert not private_dir(empty_b, ".bf", "state", create=False).exists(state.TIMERS_FILE)

    # Timer validation & cap guards:
    with pytest.raises(Error, match="event source"):
        state.schedule_event(empty_b, "loop.wake", "BAD_SRC", {}, deliver_at=t0 + timedelta(minutes=1))
    with pytest.raises(Error, match="event kind"):
        state.schedule_event(empty_b, "BAD KIND", "dev", {}, deliver_at=t0 + timedelta(minutes=1))
    with pytest.raises(Error, match="exceeds"):
        state.schedule_event(
            empty_b,
            "loop.wake",
            "dev",
            {"blob": "x" * (state.MAX_EVENT_LINE + 16)},
            deliver_at=t0 + timedelta(minutes=1),
        )
    with pytest.MonkeyPatch.context() as mp_t:
        mp_t.setattr(state, "MAX_TIMERS", 1)
        state.schedule_event(empty_b, "loop.wake", "dev", {"n": 1}, deliver_at=t0 + timedelta(minutes=5), now=t0)
        with pytest.raises(Error, match="pending timers"):
            state.schedule_event(
                empty_b, "loop.wake", "dev", {"n": 2}, deliver_at=t0 + timedelta(minutes=6), now=t0, dry_run=True
            )
        with pytest.raises(Error, match="pending timers"):
            state.schedule_event(empty_b, "loop.wake", "dev", {"n": 2}, deliver_at=t0 + timedelta(minutes=6), now=t0)

    # Read-only `list_events` covers tail vs `--since` (incl. 0 and future cursor), filters, `--timers`, and validation:
    ev_tail = state.list_events(empty_b, "empty-ev", limit=1)
    assert (ev_tail["mode"], ev_tail["count"], ev_tail["truncated"], ev_tail["pending_timers"]) == (
        "events",
        1,
        True,
        1,
    )
    ev_since0 = state.list_events(empty_b, "empty-ev", since=0, limit=1)
    assert ev_since0["items"][0]["seq"] == 42  # forward pagination from 0, NOT newest tail!
    ev_since = state.list_events(empty_b, "empty-ev", since=42, limit=1)
    assert ev_since["items"][0]["seq"] == 43
    ev_future = state.list_events(empty_b, "empty-ev", since=999_999, limit=10)
    assert ev_future["items"] == []
    assert ev_future["count"] == 0
    ev_tmr = state.list_events(empty_b, "empty-ev", timers_only=True, kind="loop.wake", source="dev")
    assert (ev_tmr["mode"], ev_tmr["count"]) == ("timers", 1)
    for bad_call in (
        lambda: state.list_events(empty_b, "empty-ev", since=-1),
        lambda: state.list_events(empty_b, "empty-ev", limit=0),
        lambda: state.list_events(empty_b, "empty-ev", kind="BAD KIND"),
        lambda: state.list_events(empty_b, "empty-ev", source="BAD_SRC"),
    ):
        with pytest.raises(InputError):
            bad_call()

    # Security bounds: JSON nesting depth > 64 (list & dict + direct redact/sanitize), emit count cap, duration cap, byte cap, private mode 0600.
    deep_json = ("[" * 70) + "0" + ("]" * 70)
    with pytest.raises(Error, match="nesting"):
        models.decode(deep_json, "deep")
    deep_obj: dict[str, Any] = {"v": 1}
    for _ in range(66):
        deep_obj = {"a": deep_obj}
    with pytest.raises(Error, match="nesting"):
        models.decode(json.dumps(deep_obj), "deep-dict")
    with pytest.raises(Error, match="nesting"):
        models.sanitize_record_strings(deep_obj)
    with pytest.raises(Error, match="nesting"):
        models.redact_value(deep_obj)
    with pytest.raises(Error, match="duration"):
        models.parse_duration("9" * 5000 + "s", "huge")
    with pytest.raises(Error, match="exceeds"):
        state.emit_output_events(empty_b, "flood", "loop.tick", (b'{"n":1}\n') * (state.MAX_EMIT_EVENTS + 1))

    # Dedupe scan with >16 distinct IDs + >64 KiB backward chunked `read_events_meta(after_seq=...)`!
    many_evs = b"".join(json.dumps({"id": f"id-{i}", "pad": "p" * 4000}).encode("utf-8") + b"\n" for i in range(20))
    assert state.emit_output_events(empty_b, "bulk", "loop.tick", many_evs) == 20
    assert state.emit_output_events(empty_b, "bulk", "loop.tick", many_evs) == 0  # all 20 deduped!
    _, latest_bulk = state.event_bounds(empty_b)
    tail_delta, _, _ = state.read_events_meta(empty_b, after_seq=latest_bulk - 18)
    assert len(tail_delta) == 18
    assert tail_delta[0]["seq"] == latest_bulk - 17

    # Private store enforces mode 0600 on `open_for_reads`, `read_tail`, and `writer` lock!
    st_priv = private_dir(empty_b, ".bf", "state")
    ev_path = empty_b.root / ".bf" / "state" / state.EVENTS_FILE
    ev_path.chmod(0o644)
    try:
        with pytest.raises(Error, match="mode 600"), st_priv.open_for_reads(state.EVENTS_FILE):
            pass
        with pytest.raises(Error, match="mode 600"):
            st_priv.read_tail(state.EVENTS_FILE, 1024)
    finally:
        ev_path.chmod(0o600)
    lk_path = empty_b.root / ".bf" / "write.lock"
    lk_path.chmod(0o644)
    try:
        from bf.storage import writer as bf_writer

        with pytest.raises(Error, match="mode 600"), bf_writer(empty_b):
            pass
    finally:
        lk_path.chmod(0o600)

    long_recs = [{"id": f"r{i:02d}-" + ("x" * 1800), "title": f"Long {i}"} for i in range(30)]
    long_res = records.ingest(brain, "longrefs", json.dumps(long_recs).encode("utf-8"))
    assert long_res["ok"]
    assert long_res["added"] == 30
    evs_l, _ = state.read_events(brain, after_seq=0)
    long_ev = next(e for e in reversed(evs_l) if e["kind"] == "records.ingested" and e["source"] == "longrefs")
    assert long_ev["data"]["refs_truncated"] is True
    assert len(long_ev["data"]["added_refs"]) < 30

    # Pause helpers (`is_paused`, `pause_remaining`):
    assert state.is_paused({"paused": False}, t0) is False
    assert state.is_paused({"paused": True, "paused_until": ""}, t0) is True
    assert state.pause_remaining({"paused": True, "paused_until": ""}, t0) is None
    timed_doc = {"paused": True, "paused_until": "2026-11-25T11:00:00Z"}
    assert state.is_paused(timed_doc, t0) is True
    assert state.pause_remaining(timed_doc, t0) == pytest.approx(3600.0, abs=1.0)
    assert state.is_paused(timed_doc, t0 + timedelta(hours=2)) is False
    assert state.pause_remaining(timed_doc, t0 + timedelta(hours=2)) is None


def test_index_atomicity_tag_parity_rename_security_and_events_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regressions for BF-002 (atomic tx), BF-003 (tag parity), BF-007 (rename rebuild), BF-011 (events window), BF-SEC-02."""
    import sqlite3
    from contextlib import closing

    from bf import engine, index, query

    root = tmp_path / "idx-reg"
    engine.init_brain(root, name="oldname")
    st = Store(root)
    records.ingest(
        st,
        "alpha",
        json.dumps(
            [{"id": "1", "title": "Alpha", "tags": ["ops", "security"], "links": ["bf://newname/concepts/welcome.md"]}]
        ).encode(),
    )
    records.ingest(st, "beta", b'[{"id":"2","title":"Beta"}]')

    # BF-002: `_sync_all_in_tx` opens an explicit outer transaction even when `stale_keys` is empty!
    st.delete(f".bf/cache/{index.INDEX_NAME}")
    tx_states: list[bool] = []
    real_insert = index._insert_records  # noqa: SLF001

    def spy_insert(con: sqlite3.Connection, *args: Any, **kwargs: Any) -> int:
        tx_states.append(con.in_transaction)
        return real_insert(con, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(index, "_insert_records", spy_insert)
        index.sync_all(st, "oldname", ["alpha", "beta"])
    assert tx_states
    assert all(tx_states)

    # BF-003: `--tag "ops security"` with whitespace returns 0 under BOTH fts5 and scan fallback!
    r_fts = query.query(st, tag="ops security")
    assert (r_fts["engine"], r_fts["items"]) == ("fts5", [])
    st.delete(f".bf/cache/{index.INDEX_NAME}")
    r_scan = query.query(st, tag="ops security")
    assert (r_scan["engine"], r_scan["items"]) == ("scan", [])
    index.sync_all(st, "oldname", ["alpha", "beta"])

    # BF-007: Renaming `bf.yaml` `name:` invalidates cached `links.to_ref` and rebuilds!
    con_before = index.open_readonly(st)
    assert con_before is not None
    try:
        links_old = [r[0] for r in con_before.execute("SELECT to_ref FROM links WHERE src_ref = 'alpha:1'")]
        assert links_old == ["bf://newname/concepts/welcome.md"]
    finally:
        con_before.close()
    index.sync_all(st, "newname", ["alpha", "beta"])
    con_after = index.open_readonly(st)
    assert con_after is not None
    try:
        links_new = [r[0] for r in con_after.execute("SELECT to_ref FROM links WHERE src_ref = 'alpha:1'")]
        assert links_new == ["concepts/welcome.md"]
    finally:
        con_after.close()

    # BF-SEC-02 / BF-R2-001: Positive table allowlist rejects untrusted virtual/extra/substituted tables & self-heals!
    db_path = root / ".bf" / "cache" / index.INDEX_NAME
    for bad_ddl in (
        "CREATE /*x*/ VIRTUAL TABLE evil_fts USING fts5(body)",
        "CREATE TABLE extra_tbl (x INT)",
        "DROP TABLE fts; CREATE TABLE fts (body TEXT)",
        "DROP TABLE meta; CREATE VIRTUAL TABLE meta USING fts5(body)",
    ):
        with closing(sqlite3.connect(str(db_path))) as con_vt, con_vt:
            con_vt.executescript(bad_ddl)
        assert index.open_readonly(st) is None
        index.sync_all(st, "newname", ["alpha", "beta"])
        con_healed = index.open_readonly(st)
        assert con_healed is not None
        con_healed.close()

    db_path.chmod(0o644)
    try:
        assert index.open_readonly(st) is None
        with pytest.raises(Error, match="mode 600"):
            index.sync_all(st, "newname", ["alpha", "beta"])
    finally:
        db_path.chmod(0o600)

    # BF-011: `_first_seq_fd` and `read_events_meta` stay aligned when `events.jsonl` exceeds `MAX_EVENTS_FILE`!
    ev_st = private_dir(st, ".bf", "state")
    ev_lines = b"".join(
        json.dumps(
            {
                "seq": i,
                "id": f"e{i}",
                "kind": "loop.tick",
                "source": "s",
                "time": "2026-11-25T10:00:00Z",
                "data": {"p": "x" * 120},
            }
        ).encode()
        + b"\n"
        for i in range(1, 31)
    )
    ev_st.write(state.EVENTS_FILE, ev_lines)
    with monkeypatch.context() as mp:
        mp.setattr(state, "MAX_EVENTS_FILE", 1200)
        first_s, latest_s = state.event_bounds(st)
        evs_after, lat2, fst2 = state.read_events_meta(st, after_seq=1)
        evs_all, lat3, fst3 = state.read_events_meta(st, after_seq=0)
    assert first_s > 1
    assert (first_s, latest_s) == (fst2, lat2) == (fst3, lat3)
    assert [e["seq"] for e in evs_after] == [e["seq"] for e in evs_all]
    assert evs_after[0]["seq"] == first_s
