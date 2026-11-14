"""Tests for declarative JSON Pointer extraction (`bf.map`)."""

from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from bf import map as bf_map
from bf.models import Error, MapSpec, Record, TransformSpec


def test_extract_single_object_with_created_updated_and_constant_type() -> None:
    spec = MapSpec.model_validate(
        {
            "id": "/id",
            "@type": "SocialMediaPosting",
            "title": "/subject",
            "text": "/body",
            "url": "/permalink",
            "created": "/created_at",
            "updated": "/updated_at",
            "attributes": {"score": "/votes", "pinned": "/pinned", "empty": "/blank", "missing": "/nope"},
        }
    )
    payload = json.dumps(
        {
            "id": 42,
            "subject": "Hello",
            "body": "Body text",
            "permalink": "https://example.test/p/42",
            "created_at": "2026-09-25T10:00:00Z",
            "updated_at": "2026-09-26T12:00:00Z",
            "votes": 0,
            "pinned": False,
            "blank": "",
        }
    ).encode()

    items, cursor = bf_map.extract(payload, spec)

    assert cursor is None
    assert items == [
        {
            "id": "42",
            "@type": "SocialMediaPosting",
            "title": "Hello",
            "text": "Body text",
            "url": "https://example.test/p/42",
            "time": "2026-09-25T10:00:00Z",
            "attributes": {"score": 0, "pinned": False, "updated": "2026-09-26T12:00:00Z"},
        }
    ]
    records = TypeAdapter(list[Record]).validate_python(items)
    assert records[0].id == "42"
    assert records[0].type == "SocialMediaPosting"
    assert records[0].updated == "2026-09-26T12:00:00.000000Z"


def test_extract_updated_only_sets_time_and_explicit_updated_attribute_wins() -> None:
    spec = MapSpec(id="/id", updated="/updated_at", attributes={"updated": "/explicit_updated"})
    items, _ = bf_map.extract(
        {"id": "1", "updated_at": "2026-09-26T12:00:00Z", "explicit_updated": "2026-09-27T00:00:00Z"},
        spec,
    )
    assert items == [
        {
            "id": "1",
            "time": "2026-09-26T12:00:00Z",
            "attributes": {"updated": "2026-09-27T00:00:00Z"},
        }
    ]


def test_extract_array_items_cursor_and_jsonl() -> None:
    spec = MapSpec.model_validate(
        {"items": "/data/nodes", "cursor": "/pageInfo/endCursor", "id": "/id", "@type": "/kind", "title": "/name"}
    )
    items, cursor = bf_map.extract(
        {
            "pageInfo": {"endCursor": "cur-2"},
            "data": {"nodes": [{"id": "a", "kind": "Note", "name": "Alpha"}, {"id": "b", "kind": "", "name": "Beta"}]},
        },
        spec,
    )
    assert cursor == "cur-2"
    assert items == [{"id": "a", "@type": "Note", "title": "Alpha"}, {"id": "b", "title": "Beta"}]

    jsonl = b'{"id": "1", "title": "One"}\n\n{"id": "2", "title": "Two"}\n'
    line_items, line_cursor = bf_map.extract(jsonl, MapSpec(id="/id", title="/title"))
    assert line_cursor is None
    assert line_items == [{"id": "1", "title": "One"}, {"id": "2", "title": "Two"}]
    assert bf_map.extract(b"   \n", spec) == ([], None)
    assert bf_map.extract({"data": {"nodes": None}, "pageInfo": {"endCursor": ""}}, spec) == ([], None)


def test_extract_rfc6901_escapes_and_array_indices() -> None:
    spec = MapSpec(
        id="/a~1b/0/c~0d",
        title="/a~1b/0/~01",
        attributes={"meta": "/meta", "leading_zero_miss": "/a~1b/01/c~0d"},
    )
    payload = {"a/b": [{"c~d": "id-1", "~1": "TildeOne"}, {"c~d": "ignored"}], "meta": {"tags": ["x", "y"]}}
    items, _ = bf_map.extract(payload, spec)
    assert items == [{"id": "id-1", "title": "TildeOne", "attributes": {"meta": {"tags": ["x", "y"]}}}]


def test_extract_wildcards_transforms_and_relation_dedup() -> None:
    spec = MapSpec(
        items="/pages/*/items",
        id=TransformSpec(path="/num", prefix="gh:"),
        relations={
            "assigned": [
                TransformSpec(path="/author/login", prefix="github:", lower=True),
                TransformSpec(path="/assignees/*/login", prefix="github:", lower=True),
            ],
            "tagged": TransformSpec(path="/sub/*/labels", prefix="tag:", lower=True),
            "labels-flat": TransformSpec(path="/sub/*/labels/*", prefix="lbl:"),
        },
        attributes={
            "logins": TransformSpec(path="/assignees/*/login", prefix="@", lower=True),
            "nested_labels": "/sub/*/labels",
            "literal_star": "/stars/*",
            "empty_wild": "/missing/*",
        },
    )
    payload = {
        "pages": [
            {
                "items": [
                    {
                        "num": 7,
                        "author": {"login": "Alice"},
                        "assignees": [{"login": "Bob"}, {"login": ""}, {"login": "alice"}, {"login": None}],
                        "sub": [{"labels": ["Bug", "P1"]}, {"labels": ["bug"]}],
                        "stars": {"*": "ok"},
                    }
                ]
            },
            {"items": None},
        ]
    }
    items, _ = bf_map.extract(payload, spec)
    assert items == [
        {
            "id": "gh:7",
            "attributes": {
                "logins": ["@bob", "@alice"],
                "nested_labels": [["Bug", "P1"], ["bug"]],
                "literal_star": ["ok"],
            },
            "relations": {
                "assigned": ["github:alice", "github:bob"],
                "tagged": ["tag:bug", "tag:p1"],
                "labels-flat": ["lbl:Bug", "lbl:P1", "lbl:bug"],
            },
        }
    ]


@pytest.mark.parametrize(
    ("raw", "spec", "match"),
    [
        (b"{bad", MapSpec(id="/id"), "JSON"),
        (b'{"id": "1"}\n[1, 2]\n', MapSpec(id="/id"), "line 2: collector JSONL line must be a JSON object"),
        (b'{"id": "1"}\n{bad\n', MapSpec(id="/id"), "line 2:"),
        (b'"scalar"', MapSpec(id="/id"), "JSON object or array"),
        ({"items": "not-a-list"}, MapSpec(items="/items", id="/id"), "items pointer must resolve to a JSON array"),
        ([123], MapSpec(id="/id"), "record 0: item must be a JSON object"),
        ({"id": ""}, MapSpec(id="/id"), "record 0: id resolved empty or missing"),
        ({"id": {"nested": 1}}, MapSpec(id="/id"), "record 0: id must resolve to a string or number"),
        ({"id": True}, MapSpec(id="/id"), "record 0: id must resolve to a string or number"),
        ({"a": [{"id": "1"}]}, MapSpec(id="/a/*/id"), "wildcard '\\*' is not valid"),
        ({"id": "1"}, MapSpec.model_construct(id="id"), "invalid JSON pointer 'id'"),
        ({"id": "1"}, MapSpec.model_construct(id="/a~2b"), "'~' must precede 0 or 1"),
        ({"id": "1"}, MapSpec.model_construct(id="/trailing~"), "'~' must precede 0 or 1"),
        ({"id": "1", "rel": "bad space"}, MapSpec(id="/id", relations={"mentions": "/rel"}), "relations.mentions"),
        (
            {"id": "1", "rel": "bf://b/n?rel=cites"},
            MapSpec(id="/id", relations={"mentions": "/rel"}),
            "relations.mentions",
        ),
        ({"id": "1", "rel": "ok\u200b"}, MapSpec(id="/id", relations={"mentions": "/rel"}), "relations.mentions"),
    ],
)
def test_extract_errors_without_leaking_values(raw: bytes | object, spec: MapSpec, match: str) -> None:
    with pytest.raises(Error, match=match):
        bf_map.extract(raw, spec)
    if isinstance(raw, dict) and "rel" in raw and isinstance(raw["rel"], str):
        with pytest.raises(Error) as info:
            bf_map.extract(raw, spec)
        assert raw["rel"] not in str(info.value)


def test_extract_pointer_normal_and_errors() -> None:
    assert bf_map.extract_pointer(b'"root-token"', "") == "root-token"
    assert bf_map.extract_pointer(b'{"a/b": [{"url": "https://example.test/1"}]}', "/a~1b/0/url") == (
        "https://example.test/1"
    )
    assert bf_map.extract_pointer({"count": 7}, "/count") == "7"

    for bad_data, pointer in (
        (b"", ""),
        (b"{}", "/missing"),
        (b'{"v": ""}', "/v"),
        (b'{"v": null}', "/v"),
        (b'{"v": true}', "/v"),
        (b'{"v": {"k": 1}}', "/v"),
        (b'{"items": ["a"]}', "/items/*"),
        (b"{}", "no-slash"),
    ):
        with pytest.raises(Error):
            bf_map.extract_pointer(bad_data, pointer)


def test_map_spec_rejects_invalid_pointer_at_validation() -> None:
    with pytest.raises(ValidationError):
        MapSpec(id="no-leading-slash")
