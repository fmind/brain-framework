"""Within a major version, files only gain optional fields: compare every published schema with its release.

`tests/contract/` holds the schemas of the current major release (`bf`, `record`). A change that removes a field,
changes its type, narrows what a file accepts, or requires a new configuration key fails here. Such a change needs the
next major release, whose release checklist refreshes this contract from `docs/*.schema.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast, get_args

import pytest

from bf.schemas import Kind, document

CONTRACT = Path(__file__).parent / "contract"
# The comparison follows these keywords itself; titles, descriptions, defaults and examples accept or reject nothing.
FOLLOWED = {
    "$defs",
    "$ref",
    "additionalProperties",
    "anyOf",
    "const",
    "default",
    "description",
    "enum",
    "examples",
    "items",
    "oneOf",
    "patternProperties",
    "properties",
    "required",
    "title",
    "type",
}
# Other keywords fail closed: only a raised upper bound, a lowered lower bound or a dropped bound, pattern or format
# keeps a file accepting every value. Any other change, such as to `not` or `allOf`, waits for a major release.
UPPER = {"exclusiveMaximum", "maxItems", "maxLength", "maxProperties", "maximum"}
LOWER = {"exclusiveMinimum", "minItems", "minLength", "minProperties", "minimum"}


def _resolve(schema: dict, root: dict) -> dict:
    """Follow local `$ref`s, keeping the keywords beside each; published schemas only reference their own `$defs`."""
    while isinstance(schema, dict) and "$ref" in schema:
        node: object = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]  # type: ignore[index]
        # A keyword beside a `$ref` applies too, such as a bound on a referenced type.
        schema = {**cast(dict, node), **{key: value for key, value in schema.items() if key != "$ref"}}
    return schema


def _values(schema: dict) -> set[str] | None:
    """The literal values a schema accepts, when it names them."""
    if "const" in schema:
        return {json.dumps(schema["const"])}
    if "enum" in schema:
        return {json.dumps(value) for value in schema["enum"]}
    return None


def _breaks(old: dict | bool, new: dict | bool, old_root: dict, new_root: dict, path: str) -> list[str]:
    """Where `new` breaks what `old` promised: a file loses an accepted form."""
    if isinstance(old, bool) or isinstance(new, bool):
        if old == new or new is True:
            return []
        return [f"{path}: boolean schema changed"]
    old, new = _resolve(old, old_root), _resolve(new, new_root)
    # A value that gains alternatives, such as a map made nullable, with only annotations beside them, keeps a file's
    # promises when one alternative keeps them all.
    if (
        new.get("anyOf")
        and not (old.get("anyOf") or old.get("oneOf"))
        and new.keys() - {"anyOf", "default", "description", "examples", "title"} == set()
        and not all(_breaks(old, option, old_root, new_root, path) for option in new["anyOf"])
    ):
        return []
    problems = []
    # A file may drop its type and accept any value.
    if old.get("type") != new.get("type") and new.get("type") is not None:
        problems.append(f"{path}: type {old.get('type')} became {new.get('type')}")
    before, after = _values(old), _values(new)
    # A file value that starts listing its values, with a const or an enum, rejects every other value of its type;
    # one that stops listing them accepts every value of its type.
    if after is not None and (before is None or not before <= after):
        problems.append(f"{path}: accepted values narrowed")
    if gained := set(new.get("required", [])) - set(old.get("required", [])):
        problems.append(f"{path}: newly required {sorted(gained)}")
    # Named fields, and the entries of maps such as `loops`, whose keys match a pattern.
    for keyword in ("properties", "patternProperties"):
        for key, value in old.get(keyword, {}).items():
            if key not in new.get(keyword, {}):
                problems.append(f"{path}.{key}: removed")
                continue
            problems.extend(_breaks(value, new[keyword][key], old_root, new_root, f"{path}.{key}"))
    if added := new.get("patternProperties", {}).keys() - old.get("patternProperties", {}).keys():
        problems.append(f"{path}: new key patterns {sorted(added)}")
    for key in ("items", "additionalProperties"):
        was, now = old.get(key, True), new.get(key, True)
        if isinstance(was, dict) and isinstance(now, dict):
            problems.extend(_breaks(was, now, old_root, new_root, f"{path}[{key}]"))
        elif was is not False and now is not True and was != now:
            # Absent or true accepts any item or extra key and false none: a file's schema may only move toward true.
            problems.append(f"{path}: {key} narrowed")
    alternatives = new.get("oneOf", []) + new.get("anyOf", [])
    originals = old.get("oneOf", []) + old.get("anyOf", [])
    # A file value that gains alternatives, such as one made nullable, must still match one of them.
    if (
        alternatives
        and not originals
        and all(_breaks(old, option, old_root, new_root, path) for option in alternatives)
    ):
        problems.append(f"{path}: accepted values narrowed")
    # Exactly one alternative of a file's `oneOf` may match, so a new one that also matches an old value rejects it.
    if len(new.get("oneOf", [])) > len(old.get("oneOf", [])):
        problems.append(f"{path}: new oneOf alternatives")
    # So does a released one that accepts more: compared backwards, it must still accept all its new form does.
    problems.extend(
        f"{path}.alternative[{index}]: oneOf alternative widened"
        for index, (was, now) in enumerate(zip(old.get("oneOf", []), new.get("oneOf", []), strict=False))
        if _breaks(now, was, new_root, old_root, path)
    )
    # A value that drops its alternatives, as when `Model | None` becomes `Model`, is its own remaining alternative.
    remaining = alternatives or [new]
    for index, option in enumerate(originals):
        title = _resolve(option, old_root).get("title")
        if title is None:
            if not any(not _breaks(option, o, old_root, new_root, path) for o in remaining):
                problems.append(f"{path}.alternative[{index}]: removed")
            continue
        match = next((o for o in remaining if _resolve(o, new_root).get("title") == title), None)
        if match is None:
            problems.append(f"{path}.alternative[{index}] {title}: removed")
        else:
            problems.extend(_breaks(option, match, old_root, new_root, f"{path}.{title}"))
    for key in sorted((old.keys() | new.keys()) - FOLLOWED):
        was, now = old.get(key), new.get(key)
        widened = (now is None and key in UPPER | LOWER | {"format", "pattern"}) or (
            was is not None and now is not None and ((key in UPPER and now > was) or (key in LOWER and now < was))
        )
        if was != now and not widened:
            problems.append(f"{path}: {key} changed")
    return problems


@pytest.mark.parametrize("kind", get_args(Kind))
def test_schemas_keep_the_release_contract(kind: str) -> None:
    released = json.loads((CONTRACT / f"{kind}.schema.json").read_text())
    current = document(cast(Kind, kind))
    assert _breaks(released, current, released, current, kind) == []


def test_the_comparison_catches_breaking_changes() -> None:
    def check(old: dict, new: dict) -> list[str]:
        return _breaks(old, new, old, new, "s")

    old = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}, "b": {"enum": ["x", "y"]}}}
    assert check(old, old) == []
    grown = {**old, "properties": {**old["properties"], "c": {"type": "integer"}}}
    assert check(old, grown) == []
    assert check(old, {**old, "properties": {"b": {"enum": ["x", "y"]}}}) == ["s.a: removed"]
    assert check(old, {**old, "properties": {**old["properties"], "a": {"type": "integer"}}}) == [
        "s.a: type string became integer"
    ]
    assert check(old, {**old, "properties": {**old["properties"], "b": {"enum": ["x"]}}}) == [
        "s.b: accepted values narrowed"
    ]
    assert check(old, {**old, "properties": {**old["properties"], "b": {"enum": ["x", "y", "z"]}}}) == []
    assert check(old, {**old, "properties": {**old["properties"], "a": {}}}) == []
    assert check(old, {**old, "properties": {**old["properties"], "b": {"type": "string", "enum": ["x", "y"]}}}) == [
        "s.b: type None became string"
    ]
    assert check(old, {**grown, "required": ["a", "c"]}) == ["s: newly required ['c']"]
    assert check(old, {**old, "additionalProperties": False}) == ["s: additionalProperties narrowed"]
    nullable = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert check(nullable, nullable) == []
    assert check(nullable, {"anyOf": [{"type": "string"}]}) == ["s.alternative[1]: removed"]
    assert check({"type": "string"}, nullable) == []
    window = {"title": "W", "type": "object", "properties": {"since": {"type": "string"}}}
    assert check({"anyOf": [window, {"type": "null"}]}, window) == [
        "s: type None became object",
        "s.alternative[1]: removed",
    ]


def test_files_keep_every_accepted_bound_pattern_and_condition() -> None:
    def breaks(before: dict, after: dict) -> list[str]:
        old, new = (
            {
                "properties": {"loops": {"patternProperties": {"^[a-z]+$": {"$ref": "#/$defs/Loop"}}}},
                "$defs": {"Loop": {"type": "object", "properties": {"value": value}}},
            }
            for value in (before, after)
        )
        return _breaks(old, new, old, new, "s")

    field = "s.loops.^[a-z]+$.value"
    timeout = {"type": "integer", "minimum": 1, "maximum": 3600}
    assert breaks(timeout, {**timeout, "maximum": 600}) == [f"{field}: maximum changed"]
    assert breaks(timeout, {"type": "integer", "maximum": 600}) == [f"{field}: maximum changed"]
    assert breaks({"type": "integer"}, timeout) == [f"{field}: maximum changed", f"{field}: minimum changed"]
    assert breaks(timeout, {**timeout, "minimum": 0, "maximum": 7200, "description": "Seconds."}) == []
    assert breaks(timeout, {"type": "integer"}) == []
    assert breaks(timeout, {**timeout, "enum": [60, 300]}) == [f"{field}: accepted values narrowed"]
    name = {"type": "string", "pattern": "^[a-z]+$"}
    assert breaks(name, {**name, "pattern": "^[a-z]{1,8}$"}) == [f"{field}: pattern changed"]
    assert breaks(name, {"type": "string"}) == []
    assert breaks(name, {**name, "const": "main"}) == [f"{field}: accepted values narrowed"]
    assert breaks(name, {**name, "not": {"pattern": "--"}}) == [f"{field}: not changed"]
    conditional = {"allOf": [{"if": {"const": "web"}, "then": {"minLength": 4}}]}
    assert breaks(name, {**name, **conditional}) == [f"{field}: allOf changed"]
    assert breaks(name, {"anyOf": [name, {"type": "null"}]}) == []
    assert breaks(name, {"anyOf": [{"type": "integer"}, {"type": "null"}]}) == [f"{field}: accepted values narrowed"]
    entries = {"type": "object", "patternProperties": {"^a$": {"type": "string"}}}
    assert _breaks(entries, {**entries, "additionalProperties": False}, {}, {}, "s") == [
        "s: additionalProperties narrowed"
    ]
    assert _breaks({**entries, "additionalProperties": False}, entries, {}, {}, "s") == []
    assert _breaks(entries, {"type": "object"}, {}, {}, "s") == ["s.^a$: removed"]
    assert _breaks(entries, {"anyOf": [entries, {"type": "null"}], "title": "E"}, {}, {}, "s") == []
    narrower = {"type": "object", "patternProperties": {"^a$": {"type": "string", "maxLength": 3}}}
    assert _breaks(entries, {"anyOf": [narrower, {"type": "null"}]}, {}, {}, "s") != []
    wider = {"type": "object", "patternProperties": {"^a$": {"type": "string"}, "^b$": {"type": "string"}}}
    assert _breaks(entries, wider, {}, {}, "s") == ["s: new key patterns ['^b$']"]
    named = {"properties": {"name": {"$ref": "#/$defs/Name"}}, "$defs": {"Name": {"type": "string"}}}
    bounded = {**named, "properties": {"name": {"$ref": "#/$defs/Name", "maxLength": 8}}}
    assert _breaks(named, bounded, named, bounded, "s") == ["s.name: maxLength changed"]
    either = {"oneOf": [{"required": ["path"]}, {"required": ["value"]}]}
    overlapping = {"oneOf": [*either["oneOf"], {"type": "object"}]}
    assert _breaks(either, overlapping, {}, {}, "s") == ["s: new oneOf alternatives"]
    assert _breaks({"anyOf": either["oneOf"]}, either, {}, {}, "s") == ["s: new oneOf alternatives"]
    assert _breaks(either, {"anyOf": either["oneOf"]}, {}, {}, "s") == []
    short = {"oneOf": [{"type": "string", "maxLength": 3}, {"type": "string", "minLength": 4}]}
    longer = {"oneOf": [{**short["oneOf"][0], "maxLength": 5}, short["oneOf"][1]]}
    assert _breaks(short, longer, {}, {}, "s") == ["s.alternative[0]: oneOf alternative widened"]


@pytest.mark.parametrize(
    ("old", "new", "broken"),
    [
        ({"type": "string", "enum": ["a"]}, {"type": "string"}, False),
        ({"type": "string", "const": "a"}, {"type": "string", "enum": ["a", "b"]}, False),
        ({"type": "string"}, {"type": "string", "enum": ["a"]}, True),
        ({"type": "string", "enum": ["a", "b"]}, {"type": "string", "enum": ["a"]}, True),
    ],
)
def test_listed_values_widen_files(old: dict, new: dict, *, broken: bool) -> None:
    assert bool(_breaks(old, new, old, new, "value")) is broken
    assert _breaks(True, False, {}, {}, "p") == ["p: boolean schema changed"]
    assert _breaks(False, True, {}, {}, "p") == []
