"""Declarative JSON Pointer extraction for `map:` and `extract:` job outputs."""

from __future__ import annotations

import re
from typing import cast

from bf import links
from bf.models import Error, MapSpec, TransformSpec, decode

# RFC 6901 array indices forbid leading zeros except "0".
_INDEX = re.compile(r"^(?:0|[1-9][0-9]*)$")
_ESCAPE = re.compile(r"~(?![01])")


def load_json(raw: bytes | object) -> object:
    """Decode JSON or NDJSON objects, returning [] for blank bytes."""
    if not isinstance(raw, bytes):
        return raw
    if not (stripped := raw.strip()):
        return []
    try:
        return decode(raw)
    except Error:
        raw_lines = [line for line in raw.splitlines() if line.strip()]
        if stripped.startswith(b"[") or len(raw_lines) < 2:
            raise
        items: list[object] = []
        for idx, line in enumerate(raw_lines, 1):
            try:
                obj = decode(line)
            except Error as err:
                raise Error(f"line {idx}: {err}") from err
            if not isinstance(obj, dict):
                raise Error(f"line {idx}: collector JSONL line must be a JSON object") from None
            items.append(obj)
        return items


def _tokens(pointer: str, *, allow_root: bool = False) -> tuple[tuple[str, ...], bool]:
    if not pointer:
        if allow_root:
            return (), False
        raise Error("invalid JSON pointer '': must start with '/'")
    if not pointer.startswith("/"):
        raise Error(f"invalid JSON pointer {pointer!r}: must be empty or start with '/'")
    parts: list[str] = []
    for raw in pointer.split("/")[1:]:
        if _ESCAPE.search(raw):
            raise Error(f"invalid JSON pointer {pointer!r}: '~' must precede 0 or 1")
        parts.append(raw.replace("~1", "/").replace("~0", "~"))
    return tuple(parts), "*" in parts


def _walk(node: object, tokens: tuple[str, ...]) -> list[object]:
    current: list[object] = [node]
    for seg in tokens:
        nxt: list[object] = []
        for item in current:
            if seg == "*":
                nxt.extend(
                    item if isinstance(item, list) else [item["*"]] if isinstance(item, dict) and "*" in item else ()
                )
            elif isinstance(item, dict) and seg in item:
                nxt.append(item[seg])
            elif isinstance(item, list) and _INDEX.match(seg) and (idx := int(seg)) < len(item):
                nxt.append(item[idx])
        if not nxt:
            return []
        current = nxt
    return current


def _apply(value: object, expr: str | TransformSpec, context: str) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise Error(f"{context} must resolve to a string or number")
    text = value if isinstance(value, str) else str(value)
    if isinstance(expr, TransformSpec):
        return f"{expr.prefix}{text.lower() if expr.lower else text}"
    return text


def _single(node: object, expr: str | TransformSpec, context: str) -> str | None:
    if isinstance(expr, str) and not expr:
        return None
    tokens, wildcard = _tokens(expr.path if isinstance(expr, TransformSpec) else expr)
    if wildcard:
        raise Error(f"{context}: wildcard '*' is not valid here")
    return _apply(hits[0], expr, context) if (hits := _walk(node, tokens)) else None


def _flatten(hits: list[object]) -> list[object]:
    return [elem for hit in hits if hit is not None for elem in (hit if isinstance(hit, list) else (hit,))]


def _extract_items(payload: object, pointer: str) -> list[dict[str, object]]:
    if not pointer:
        if not isinstance(payload, (list, dict)):
            raise Error("payload must be a JSON object or array")
        raw_items = payload if isinstance(payload, list) else [payload]
    else:
        tokens, wildcard = _tokens(pointer)
        hits = _walk(payload, tokens)
        if wildcard:
            raw_items = _flatten(hits)
        elif not hits or hits[0] is None:
            return []
        elif not isinstance(hits[0], list):
            raise Error("items pointer must resolve to a JSON array")
        else:
            raw_items = hits[0]
    for pos, row in enumerate(raw_items):
        if not isinstance(row, dict):
            raise Error(f"record {pos}: item must be a JSON object")
    return cast("list[dict[str, object]]", raw_items)


def _extract_relations(
    row: dict[str, object], spec: dict[str, str | TransformSpec | list[str | TransformSpec]], pos: int
) -> dict[str, list[str]]:
    relations: dict[str, list[str]] = {}
    for name, expr in spec.items():
        targets, ctx = dict[str, None](), f"record {pos}: relations.{name}"
        for entry in expr if isinstance(expr, list) else (expr,):
            tokens, _ = _tokens(entry.path if isinstance(entry, TransformSpec) else entry)
            for val in _flatten(_walk(row, tokens)):
                if (cand := _apply(val, entry, ctx)) is not None:
                    try:
                        targets[links.identity(cand)] = None
                    except Error as err:
                        raise Error(f"{ctx}: {err}") from err
        if targets:
            relations[name] = list(targets)
    return relations


def _attr_value(value: object, expr: str | TransformSpec, context: str) -> object | None:
    if value is None or value == "":
        return None
    if not isinstance(expr, TransformSpec):
        return value
    if isinstance(value, list):
        return [out for elem in value if (out := _apply(elem, expr, context)) is not None] or None
    return _apply(value, expr, context)


def _extract_attributes(row: dict[str, object], spec: dict[str, str | TransformSpec], pos: int) -> dict[str, object]:
    attributes: dict[str, object] = {}
    for name, expr in spec.items():
        tokens, wildcard = _tokens(expr.path if isinstance(expr, TransformSpec) else expr)
        ctx = f"record {pos}: attributes.{name}"
        if vals := [v for hit in _walk(row, tokens) if (v := _attr_value(hit, expr, ctx)) is not None]:
            attributes[name] = vals if wildcard else vals[0]
    return attributes


def extract(raw: bytes | object, spec: MapSpec) -> tuple[list[dict[str, object]], str | None]:
    """Extract Record-shaped dicts and optional cursor from JSON/NDJSON."""
    payload = load_json(raw)
    cursor = _single(payload, spec.cursor, "cursor") if spec.cursor else None
    records: list[dict[str, object]] = []
    for pos, row in enumerate(_extract_items(payload, spec.items)):
        if (record_id := _single(row, spec.id, f"record {pos}: id")) is None:
            raise Error(f"record {pos}: id resolved empty or missing")
        record: dict[str, object] = {"id": record_id}
        if spec.type and (
            rtype := _single(row, spec.type, f"record {pos}: @type") if spec.type.startswith("/") else spec.type
        ):
            record["@type"] = rtype
        for field, ptr in (("title", spec.title), ("text", spec.text), ("url", spec.url)):
            if ptr and (val := _single(row, ptr, f"record {pos}: {field}")) is not None:
                record[field] = val
        created = _single(row, spec.created, f"record {pos}: created") if spec.created else None
        updated = _single(row, spec.updated, f"record {pos}: updated") if spec.updated else None
        if event_time := (created or updated):
            record["time"] = event_time
        attrs = _extract_attributes(row, spec.attributes, pos)
        if updated is not None and "updated" not in attrs:
            attrs["updated"] = updated
        if attrs:
            record["attributes"] = attrs
        if rels := _extract_relations(row, spec.relations, pos):
            record["relations"] = rels
        records.append(record)
    return records, cursor


def extract_pointer(data: bytes | object, pointer: str) -> str:
    """Extract one non-empty scalar string from JSON using an RFC 6901 pointer."""
    payload = decode(data) if isinstance(data, bytes) else data
    tokens, wildcard = _tokens(pointer, allow_root=True)
    if wildcard:
        raise Error(f"extract pointer {pointer!r} must not contain wildcard '*'")
    ctx = f"extract pointer {pointer!r}"
    if not (hits := _walk(payload, tokens)) or (val := _apply(hits[0], pointer, ctx)) is None:
        raise Error(f"{ctx} resolved empty or missing")
    return val
