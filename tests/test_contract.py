"""Within a major version, files and replies only gain optional fields: compare every schema with its release.

`tests/contract/` holds the schemas of the current major release. A change that removes a field, changes its type,
narrows what a file accepts, widens what a reply returns or requires a new configuration key fails here: clients
validate replies with the schema they saved. Such a change needs the next major release, whose release checklist
refreshes this contract from `docs/*.schema.json`.
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
# keeps a file accepting every value, and only the reverse keeps a reply within what its readers accept. Any other
# change, such as to `not` or `allOf`, waits for a major release.
UPPER = {"exclusiveMaximum", "maxItems", "maxLength", "maxProperties", "maximum"}
LOWER = {"exclusiveMinimum", "minItems", "minLength", "minProperties", "minimum"}


def _name(kind: str) -> str:
    return "bf" if kind == "brain" else kind


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


def _breaks(old: dict, new: dict, old_root: dict, new_root: dict, path: str, *, reply: bool) -> list[str]:
    """Where `new` breaks what `old` promised: a file loses an accepted form, a reply loses a field or returns a value
    that its readers, validating with `old`, reject."""
    old, new = _resolve(old, old_root), _resolve(new, new_root)
    # A value that gains alternatives, such as a map made nullable, with only annotations beside them, keeps a file's
    # promises when one alternative keeps them all, and a reply's only when every alternative does.
    if (
        new.get("anyOf")
        and not (old.get("anyOf") or old.get("oneOf"))
        and new.keys() - {"anyOf", "default", "description", "examples", "title"} == set()
    ):
        verdicts = [_breaks(old, option, old_root, new_root, path, reply=reply) for option in new["anyOf"]]
        if reply:
            return [problem for verdict in verdicts for problem in verdict]
        if not all(verdicts):
            return []
    problems = []
    # A file may drop its type and accept any value; a reply may add one and return fewer.
    if old.get("type") != new.get("type") and (old if reply else new).get("type") is not None:
        problems.append(f"{path}: type {old.get('type')} became {new.get('type')}")
    before, after = _values(old), _values(new)
    # A file value that starts listing its values, with a const or an enum, rejects every other value of its type.
    if not reply and (
        (before is not None and (after is None or not before <= after)) or (before is None and after is not None)
    ):
        problems.append(f"{path}: accepted values narrowed")
    # A reply value may drop listed values, but not add one or stop listing them.
    if reply and before is not None and (after is None or not after <= before):
        problems.append(f"{path}: returned values widened")
    gained = set(new.get("required", [])) - set(old.get("required", []))
    lost = set(old.get("required", [])) - set(new.get("required", []))
    if not reply and gained:
        problems.append(f"{path}: newly required {sorted(gained)}")
    if reply and lost:
        problems.append(f"{path}: no longer required {sorted(lost)}")
    # Named fields, and the entries of maps such as `sensors`, whose keys match a pattern.
    for keyword in ("properties", "patternProperties"):
        for key, value in old.get(keyword, {}).items():
            if key not in new.get(keyword, {}):
                problems.append(f"{path}.{key}: removed")
                continue
            problems.extend(_breaks(value, new[keyword][key], old_root, new_root, f"{path}.{key}", reply=reply))
    if not reply and (added := new.get("patternProperties", {}).keys() - old.get("patternProperties", {}).keys()):
        problems.append(f"{path}: new key patterns {sorted(added)}")
    for key in ("items", "additionalProperties"):
        was, now = old.get(key, True), new.get(key, True)
        if isinstance(was, dict) and isinstance(now, dict):
            problems.extend(_breaks(was, now, old_root, new_root, f"{path}[{key}]", reply=reply))
        elif not reply and was is not False and now is not True and was != now:
            # Absent or true accepts any item or extra key and false none: a file's schema may only move toward true,
            # a reply's toward false.
            problems.append(f"{path}: {key} narrowed")
        elif reply and was is not True and now is not False and was != now:
            problems.append(f"{path}: {key} widened")
    # An alternative, such as one read page shape, must keep a match in the new schema: a titled one by its title, a
    # file's untitled one (such as a nullable value) by any compatible option. `anyOf` only relaxes `oneOf`, since open
    # reply shapes may overlap.
    alternatives = new.get("oneOf", []) + new.get("anyOf", [])
    originals = old.get("oneOf", []) + old.get("anyOf", [])
    # A file value that gains alternatives, such as one made nullable, must still match one of them.
    if (
        not reply
        and alternatives
        and not originals
        and all(_breaks(old, option, old_root, new_root, path, reply=reply) for option in alternatives)
    ):
        problems.append(f"{path}: accepted values narrowed")
    # Exactly one alternative of a file's `oneOf` may match, so a new one that also matches an old value rejects it.
    if not reply and len(new.get("oneOf", [])) > len(old.get("oneOf", [])):
        problems.append(f"{path}: new oneOf alternatives")
    # Readers accept a reply that fits one released alternative, so each new one, or the value itself once it has none,
    # must fit one; a titled one is compared with its namesake below. Alternatives a value gains beside other keywords
    # only narrow it.
    if reply and originals:
        titles = {_resolve(option, old_root).get("title") for option in originals} - {None}
        for index, option in enumerate(alternatives or [new]):
            title = _resolve(option, new_root).get("title")
            if title in titles or not all(_breaks(o, option, old_root, new_root, path, reply=reply) for o in originals):
                continue
            label = (f"{path}.alternative[{index}]" + (f" {title}" if title else "")) if alternatives else path
            problems.append(f"{label}: fits no released alternative")
    # A value that drops its alternatives, as when `Model | None` becomes `Model`, is its own remaining alternative.
    remaining = alternatives or [new]
    for index, option in enumerate(originals):
        title = _resolve(option, old_root).get("title")
        if title is None:
            # A reply may stop returning one, such as null: its readers accept the others.
            if not reply and not any(not _breaks(option, o, old_root, new_root, path, reply=reply) for o in remaining):
                problems.append(f"{path}.alternative[{index}]: removed")
            continue
        match = next((o for o in remaining if _resolve(o, new_root).get("title") == title), None)
        if match is None:
            problems.append(f"{path}.alternative[{index}] {title}: removed")
        else:
            problems.extend(_breaks(option, match, old_root, new_root, f"{path}.{title}", reply=reply))
    for key in sorted((old.keys() | new.keys()) - FOLLOWED):
        # Read backwards, a reply that narrows is a file that widens.
        was, now = (new.get(key), old.get(key)) if reply else (old.get(key), new.get(key))
        widened = (now is None and key in UPPER | LOWER | {"format", "pattern"}) or (
            was is not None and now is not None and ((key in UPPER and now > was) or (key in LOWER and now < was))
        )
        if was != now and not widened:
            problems.append(f"{path}: {key} changed")
    return problems


@pytest.mark.parametrize("kind", get_args(Kind))
def test_schemas_keep_the_release_contract(kind: str) -> None:
    released = json.loads((CONTRACT / f"{_name(kind)}.schema.json").read_text())
    current = document(cast(Kind, kind))
    assert _breaks(released, current, released, current, _name(kind), reply=kind.endswith("-reply")) == []


def test_the_comparison_catches_breaking_changes() -> None:
    def both(old: dict, new: dict) -> tuple[list[str], list[str]]:
        """What breaks when `new` is a file schema, then when it is a reply schema."""
        return _breaks(old, new, old, new, "s", reply=False), _breaks(old, new, old, new, "s", reply=True)

    old = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}, "b": {"enum": ["x", "y"]}}}
    assert both(old, old) == ([], [])
    grown = {**old, "properties": {**old["properties"], "c": {"type": "integer"}}}
    assert both(old, grown) == ([], [])
    removed = {**old, "properties": {"b": {"enum": ["x", "y"]}}}
    assert both(old, removed) == (["s.a: removed"], ["s.a: removed"])
    retyped = {**old, "properties": {**old["properties"], "a": {"type": "integer"}}}
    assert both(old, retyped) == (["s.a: type string became integer"], ["s.a: type string became integer"])
    # A file must keep accepting every value, and a reply must return only values its readers accept.
    narrowed = {**old, "properties": {**old["properties"], "b": {"enum": ["x"]}}}
    assert both(old, narrowed) == (["s.b: accepted values narrowed"], [])
    widened = {**old, "properties": {**old["properties"], "b": {"enum": ["x", "y", "z"]}}}
    assert both(old, widened) == ([], ["s.b: returned values widened"])
    untyped = {**old, "properties": {**old["properties"], "a": {}}}
    assert both(old, untyped) == ([], ["s.a: type string became None"])
    typed = {**old, "properties": {**old["properties"], "b": {"type": "string", "enum": ["x", "y"]}}}
    assert both(old, typed) == (["s.b: type None became string"], [])
    required = {**grown, "required": ["a", "c"]}
    assert both(old, required) == (["s: newly required ['c']"], [])
    assert both(old, {**old, "required": []}) == ([], ["s: no longer required ['a']"])
    closed = {**old, "additionalProperties": False}
    assert both(old, closed) == (["s: additionalProperties narrowed"], [])
    assert both(closed, old) == ([], ["s: additionalProperties widened"])
    nullable = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert both(nullable, nullable) == ([], [])
    assert both(nullable, {"anyOf": [{"type": "string"}]}) == (["s.alternative[1]: removed"], [])
    assert both({"type": "string"}, nullable) == ([], ["s: type string became null"])
    # A reply value without alternatives must fit a released one.
    assert _breaks(nullable, {"type": "string"}, {}, {}, "s", reply=True) == []
    assert _breaks(nullable, {"type": "integer"}, {}, {}, "s", reply=True) == ["s: fits no released alternative"]
    # A reply alternative that returns a new value fits none of the released ones.
    listed = {"anyOf": [{"type": "string", "enum": ["x"]}, {"type": "null"}]}
    grew = {"anyOf": [{"type": "string", "enum": ["x", "y"]}, {"type": "null"}]}
    assert both(listed, grew) == ([], ["s.alternative[0]: fits no released alternative"])
    shapes = {"oneOf": [{"title": "A", "type": "object"}]}
    assert _breaks(shapes, {"anyOf": shapes["oneOf"]}, {}, {}, "s", reply=True) == []
    # A reply shape that fits no released one fails its readers, unless only its title is new.
    more = {"anyOf": [*shapes["oneOf"], {"title": "B", "type": "array"}]}
    assert both(shapes, more) == ([], ["s.alternative[1] B: fits no released alternative"])
    assert both(shapes, {"anyOf": [*shapes["oneOf"], {"title": "C", "type": "object"}]}) == ([], [])
    # A model that is no longer nullable keeps its shape: a reply narrows, a file stops accepting null.
    window = {"title": "W", "type": "object", "properties": {"since": {"type": "string"}}}
    assert both({"anyOf": [window, {"type": "null"}]}, window) == (
        ["s: type None became object", "s.alternative[1]: removed"],
        [],
    )


def test_reply_readers_keep_accepting_every_reply() -> None:
    # A client validating with its saved schema rejects a new freshness value, a reply without items or a null notice.
    saved = document("search-reply")
    minor = json.loads(json.dumps(saved))
    minor["$defs"]["Coverage"]["properties"]["freshness"]["enum"].append("paused")
    minor["required"].remove("items")
    minor["properties"]["notice"] = {"anyOf": [minor["properties"]["notice"], {"type": "null"}]}
    assert _breaks(saved, minor, saved, minor, "search-reply", reply=True) == [
        "search-reply: no longer required ['items']",
        "search-reply.notice: type string became null",
        "search-reply.sources[items].freshness: returned values widened",
    ]
    # Fewer values, a new required field or a tighter bound still validate.
    minor = json.loads(json.dumps(saved))
    minor["$defs"]["Coverage"]["properties"]["freshness"]["enum"].remove("unknown")
    minor["required"].append("sources")
    minor["$defs"]["Item"]["properties"]["newer"]["maxItems"] = 3
    assert _breaks(saved, minor, saved, minor, "search-reply", reply=True) == []


def test_files_keep_every_accepted_bound_pattern_and_condition() -> None:
    def breaks(before: dict, after: dict, *, reply: bool = False) -> list[str]:
        """Compare a field of map entries that, like bf.yaml's sensors, are reached through a key pattern and a $ref."""
        old, new = (
            {
                "properties": {"sensors": {"patternProperties": {"^[a-z]+$": {"$ref": "#/$defs/Sensor"}}}},
                "$defs": {"Sensor": {"type": "object", "properties": {"value": value}}},
            }
            for value in (before, after)
        )
        return _breaks(old, new, old, new, "s", reply=reply)

    field = "s.sensors.^[a-z]+$.value"
    timeout = {"type": "integer", "minimum": 1, "maximum": 3600}
    assert breaks(timeout, {**timeout, "maximum": 600}) == [f"{field}: maximum changed"]
    assert breaks(timeout, {"type": "integer", "maximum": 600}) == [f"{field}: maximum changed"]
    assert breaks({"type": "integer"}, timeout) == [f"{field}: maximum changed", f"{field}: minimum changed"]
    assert breaks(timeout, {**timeout, "minimum": 0, "maximum": 7200, "description": "Seconds."}) == []
    assert breaks(timeout, {"type": "integer"}) == []
    assert breaks(timeout, {**timeout, "enum": [60, 300]}) == [f"{field}: accepted values narrowed"]
    # A reply may narrow: its readers still accept every value it returns. It may not widen.
    assert breaks(timeout, {**timeout, "maximum": 600}, reply=True) == []
    assert breaks(timeout, {**timeout, "enum": [60, 300]}, reply=True) == []
    assert breaks({"type": "integer"}, timeout, reply=True) == []
    assert breaks(timeout, {**timeout, "maximum": 7200}, reply=True) == [f"{field}: maximum changed"]
    assert breaks(timeout, {**timeout, "minimum": 0}, reply=True) == [f"{field}: minimum changed"]
    assert breaks(timeout, {"type": "integer"}, reply=True) == [
        f"{field}: maximum changed",
        f"{field}: minimum changed",
    ]
    name = {"type": "string", "pattern": "^[a-z]+$"}
    assert breaks(name, {**name, "pattern": "^[a-z]{1,8}$"}) == [f"{field}: pattern changed"]
    assert breaks(name, {"type": "string"}) == []
    assert breaks(name, {"type": "string"}, reply=True) == [f"{field}: pattern changed"]
    assert breaks({"type": "string"}, {**name, "format": "email"}, reply=True) == []
    assert breaks({**name, "format": "email"}, name, reply=True) == [f"{field}: format changed"]
    assert breaks(name, {**name, "not": {"pattern": "--"}}, reply=True) == [f"{field}: not changed"]
    assert breaks(name, {**name, "const": "main"}) == [f"{field}: accepted values narrowed"]
    assert breaks(name, {**name, "not": {"pattern": "--"}}) == [f"{field}: not changed"]
    conditional = {"allOf": [{"if": {"const": "web"}, "then": {"minLength": 4}}]}
    assert breaks(name, {**name, **conditional}) == [f"{field}: allOf changed"]
    assert breaks(name, {"anyOf": [name, {"type": "null"}]}) == []
    assert breaks(name, {"anyOf": [{"type": "integer"}, {"type": "null"}]}) == [f"{field}: accepted values narrowed"]
    entries = {"type": "object", "patternProperties": {"^a$": {"type": "string"}}}
    assert _breaks(entries, {**entries, "additionalProperties": False}, {}, {}, "s", reply=False) == [
        "s: additionalProperties narrowed"
    ]
    assert _breaks({**entries, "additionalProperties": False}, entries, {}, {}, "s", reply=False) == []
    assert _breaks(entries, {"type": "object"}, {}, {}, "s", reply=False) == ["s.^a$: removed"]
    # A map made nullable still accepts every map, as when an emptied `sensors:` key became null-tolerant.
    assert _breaks(entries, {"anyOf": [entries, {"type": "null"}], "title": "E"}, {}, {}, "s", reply=False) == []
    narrower = {"type": "object", "patternProperties": {"^a$": {"type": "string", "maxLength": 3}}}
    assert _breaks(entries, {"anyOf": [narrower, {"type": "null"}]}, {}, {}, "s", reply=False) != []
    wider = {"type": "object", "patternProperties": {"^a$": {"type": "string"}, "^b$": {"type": "string"}}}
    assert _breaks(entries, wider, {}, {}, "s", reply=False) == ["s: new key patterns ['^b$']"]
    named = {"properties": {"name": {"$ref": "#/$defs/Name"}}, "$defs": {"Name": {"type": "string"}}}
    bounded = {**named, "properties": {"name": {"$ref": "#/$defs/Name", "maxLength": 8}}}
    assert _breaks(named, bounded, named, bounded, "s", reply=False) == ["s.name: maxLength changed"]
    # A value matching an old alternative also matches the new one: a oneOf then rejects it, an anyOf accepts it.
    either = {"oneOf": [{"required": ["path"]}, {"required": ["value"]}]}
    overlapping = {"oneOf": [*either["oneOf"], {"type": "object"}]}
    assert _breaks(either, overlapping, {}, {}, "s", reply=False) == ["s: new oneOf alternatives"]
    assert _breaks({"anyOf": either["oneOf"]}, either, {}, {}, "s", reply=False) == ["s: new oneOf alternatives"]
    assert _breaks(either, {"anyOf": either["oneOf"]}, {}, {}, "s", reply=False) == []
