"""Declarative JSON Pointer extraction for `map:` and `extract:` job outputs."""

from __future__ import annotations

import re

from bf import links
from bf.models import Error, MapSpec, TransformSpec, decode

# RFC 6901 array indices forbid leading zeros except "0".
_INDEX = re.compile(r"^(?:0|[1-9][0-9]*)$")
_ESCAPE = re.compile(r"~(?![01])")


def _load(raw: bytes | object) -> object:
    """Decode JSON or newline-delimited JSON objects, returning an empty list for blank bytes."""
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
        for index, line in enumerate(raw_lines, 1):
            try:
                obj = decode(line)
            except Error as line_error:
                raise Error(f"line {index}: {line_error}") from line_error
            if not isinstance(obj, dict):
                raise Error(f"line {index}: collector JSONL line must be a JSON object") from None
            items.append(obj)
        return items


def _tokens(pointer: str, *, allow_root: bool = False) -> tuple[tuple[str, ...], bool]:
    """Parse an RFC 6901 JSON Pointer into unescaped segments and report `/*` wildcard usage."""
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
        # Unescape ~1 before ~0 so "~01" decodes to literal "~1" rather than "/".
        parts.append(raw.replace("~1", "/").replace("~0", "~"))
    return tuple(parts), "*" in parts


def _walk(node: object, tokens: tuple[str, ...]) -> list[object]:
    """Traverse objects and arrays iteratively, fanning out across `/*` array wildcards."""
    current: list[object] = [node]
    for segment in tokens:
        nxt: list[object] = []
        for item in current:
            if segment == "*":
                if isinstance(item, list):
                    nxt.extend(item)
                elif isinstance(item, dict) and "*" in item:
                    nxt.append(item["*"])
            elif isinstance(item, dict) and segment in item:
                nxt.append(item[segment])
            elif isinstance(item, list) and _INDEX.match(segment) and (index := int(segment)) < len(item):
                nxt.append(item[index])
        if not nxt:
            return []
        current = nxt
    return current


def _apply(value: object, expr: str | TransformSpec, context: str) -> str | None:
    """Coerce a non-empty scalar value to string and apply optional TransformSpec."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise Error(f"{context} must resolve to a string or number")
    text = value if isinstance(value, str) else str(value)
    if isinstance(expr, TransformSpec):
        if expr.lower:
            text = text.lower()
        return f"{expr.prefix}{text}"
    return text


def _single(node: object, expr: str | TransformSpec, context: str) -> str | None:
    """Resolve one non-wildcard JSON Pointer or TransformSpec into an optional scalar string."""
    if isinstance(expr, str) and not expr:
        return None
    tokens, wildcard = _tokens(expr.path if isinstance(expr, TransformSpec) else expr)
    if wildcard:
        raise Error(f"{context}: wildcard '*' is not valid here")
    hits = _walk(node, tokens)
    return _apply(hits[0], expr, context) if hits else None


def _flatten(hits: list[object]) -> list[object]:
    return [elem for hit in hits if hit is not None for elem in (hit if isinstance(hit, list) else (hit,))]


def _extract_items(payload: object, pointer: str) -> list[dict[str, object]]:
    """Select record objects from decoded payload via an optional `items` pointer."""
    if not pointer:
        if isinstance(payload, list):
            raw_items = payload
        elif isinstance(payload, dict):
            raw_items = [payload]
        else:
            raise Error("payload must be a JSON object or array")
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
    rows: list[dict[str, object]] = []
    for pos, row in enumerate(raw_items):
        if not isinstance(row, dict):
            raise Error(f"record {pos}: item must be a JSON object")
        rows.append(row)
    return rows


def _extract_relations(
    row: dict[str, object], spec: dict[str, str | TransformSpec | list[str | TransformSpec]], pos: int
) -> dict[str, list[str]]:
    """Extract, transform, deduplicate, and identity-validate record relations."""
    relations: dict[str, list[str]] = {}
    for name, expr in spec.items():
        targets: dict[str, None] = {}
        context = f"record {pos}: relations.{name}"
        for entry in expr if isinstance(expr, list) else (expr,):
            tokens, _ = _tokens(entry.path if isinstance(entry, TransformSpec) else entry)
            for val in _flatten(_walk(row, tokens)):
                if (candidate := _apply(val, entry, context)) is None:
                    continue
                try:
                    targets[links.identity(candidate)] = None
                except Error as error:
                    raise Error(f"{context}: {error}") from error
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
    """Extract structured JSON attributes, applying TransformSpec when declared."""
    attributes: dict[str, object] = {}
    for name, expr in spec.items():
        tokens, wildcard = _tokens(expr.path if isinstance(expr, TransformSpec) else expr)
        context = f"record {pos}: attributes.{name}"
        if vals := [val for hit in _walk(row, tokens) if (val := _attr_value(hit, expr, context)) is not None]:
            attributes[name] = vals if wildcard else vals[0]
    return attributes


def extract(raw: bytes | object, spec: MapSpec) -> tuple[list[dict[str, object]], str | None]:
    """Extract Record-shaped dicts and an optional next cursor from JSON/JSONL output."""
    payload = _load(raw)
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
        for field, pointer in (("title", spec.title), ("text", spec.text), ("url", spec.url)):
            if pointer and (val := _single(row, pointer, f"record {pos}: {field}")) is not None:
                record[field] = val
        created = _single(row, spec.created, f"record {pos}: created") if spec.created else None
        updated = _single(row, spec.updated, f"record {pos}: updated") if spec.updated else None
        if event_time := (created or updated):
            record["time"] = event_time
        attributes = _extract_attributes(row, spec.attributes, pos)
        if updated is not None and "updated" not in attributes:
            attributes["updated"] = updated
        if attributes:
            record["attributes"] = attributes
        if relations := _extract_relations(row, spec.relations, pos):
            record["relations"] = relations
        records.append(record)
    return records, cursor


def extract_pointer(data: bytes | object, pointer: str) -> str:
    """Extract one non-empty scalar string from decoded JSON output using an RFC 6901 pointer."""
    payload = decode(data) if isinstance(data, bytes) else data
    tokens, wildcard = _tokens(pointer, allow_root=True)
    if wildcard:
        raise Error(f"extract pointer {pointer!r} must not contain wildcard '*'")
    context = f"extract pointer {pointer!r}"
    if not (hits := _walk(payload, tokens)) or (value := _apply(hits[0], pointer, context)) is None:
        raise Error(f"{context} resolved empty or missing")
    return value
