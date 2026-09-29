"""Within a major version, files and replies only gain optional fields: compare every schema with its release.

`tests/contract/` holds the schemas of the current major release. A change that removes a field, changes its type,
narrows accepted values or requires a new configuration key fails here; such a change needs the next major
release, whose release checklist refreshes this contract from `docs/*.schema.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast, get_args

import pytest

from bf.schemas import Kind, document

CONTRACT = Path(__file__).parent / "contract"


def _name(kind: str) -> str:
    return "bf" if kind == "brain" else kind


def _resolve(schema: dict, root: dict) -> dict:
    """Follow a local `$ref` once; published schemas only reference their own `$defs`."""
    while isinstance(schema, dict) and "$ref" in schema:
        node: object = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]  # type: ignore[index]
        schema = node  # type: ignore[assignment]
    return schema


def _values(schema: dict) -> set[str] | None:
    """The literal values a schema accepts, when it names them."""
    if "const" in schema:
        return {json.dumps(schema["const"])}
    if "enum" in schema:
        return {json.dumps(value) for value in schema["enum"]}
    return None


def _breaks(old: dict, new: dict, old_root: dict, new_root: dict, path: str, *, reply: bool) -> list[str]:
    """Where `new` stops describing what `old` promised: a reply loses a field, a file loses an accepted form."""
    old, new = _resolve(old, old_root), _resolve(new, new_root)
    problems = []
    if old.get("type") != new.get("type") and not ({"type"} <= old.keys() and "type" not in new):
        problems.append(f"{path}: type {old.get('type')} became {new.get('type')}")
    before, after = _values(old), _values(new)
    if before is not None and (after is None or not before <= after):
        problems.append(f"{path}: accepted values narrowed")
    if not reply and set(new.get("required", [])) - set(old.get("required", [])):
        problems.append(f"{path}: newly required {sorted(set(new.get('required', [])) - set(old.get('required', [])))}")
    for key, value in old.get("properties", {}).items():
        if key not in new.get("properties", {}):
            problems.append(f"{path}.{key}: removed")
            continue
        problems.extend(_breaks(value, new["properties"][key], old_root, new_root, f"{path}.{key}", reply=reply))
    for key in ("items", "additionalProperties"):
        if isinstance(old.get(key), dict) and isinstance(new.get(key), dict):
            problems.extend(_breaks(old[key], new[key], old_root, new_root, f"{path}[{key}]", reply=reply))
    # An alternative, such as one read page shape, must keep a match in the new schema: a titled one by its title,
    # an untitled one (such as a nullable value) by any compatible option. `anyOf` only relaxes `oneOf`, since open
    # reply shapes may overlap.
    alternatives = new.get("oneOf", []) + new.get("anyOf", [])
    for index, option in enumerate(old.get("oneOf", []) + old.get("anyOf", [])):
        title = _resolve(option, old_root).get("title")
        if title is None:
            if not any(not _breaks(option, o, old_root, new_root, path, reply=reply) for o in alternatives):
                problems.append(f"{path}.alternative[{index}]: removed")
            continue
        match = next((o for o in alternatives if _resolve(o, new_root).get("title") == title), None)
        if match is None:
            problems.append(f"{path}.alternative[{index}] {title}: removed")
        else:
            problems.extend(_breaks(option, match, old_root, new_root, f"{path}.{title}", reply=reply))
    return problems


@pytest.mark.parametrize("kind", get_args(Kind))
def test_schemas_keep_the_release_contract(kind: str) -> None:
    released = json.loads((CONTRACT / f"{_name(kind)}.schema.json").read_text())
    current = document(cast(Kind, kind))
    assert _breaks(released, current, released, current, _name(kind), reply=kind.endswith("-reply")) == []


def test_the_comparison_catches_breaking_changes() -> None:
    old = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}, "b": {"enum": ["x", "y"]}}}
    assert _breaks(old, old, old, old, "s", reply=False) == []
    grown = {**old, "properties": {**old["properties"], "c": {"type": "integer"}}}
    assert _breaks(old, grown, old, grown, "s", reply=False) == []
    removed = {**old, "properties": {"b": {"enum": ["x", "y"]}}}
    assert _breaks(old, removed, old, removed, "s", reply=True) == ["s.a: removed"]
    retyped = {**old, "properties": {**old["properties"], "a": {"type": "integer"}}}
    assert _breaks(old, retyped, old, retyped, "s", reply=True) == ["s.a: type string became integer"]
    narrowed = {**old, "properties": {**old["properties"], "b": {"enum": ["x"]}}}
    assert _breaks(old, narrowed, old, narrowed, "s", reply=False) == ["s.b: accepted values narrowed"]
    nullable = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert _breaks(nullable, nullable, nullable, nullable, "s", reply=False) == []
    assert _breaks(nullable, {"anyOf": [{"type": "string"}]}, {}, {}, "s", reply=False) == ["s.alternative[1]: removed"]
    shapes = {"oneOf": [{"title": "A", "type": "object"}]}
    assert _breaks(shapes, {"anyOf": shapes["oneOf"]}, {}, {}, "s", reply=True) == []
    required = {**grown, "required": ["a", "c"]}
    assert _breaks(old, required, old, required, "s", reply=False) == ["s: newly required ['c']"]
    assert _breaks(old, required, old, required, "s", reply=True) == []
