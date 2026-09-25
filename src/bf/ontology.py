"""Explicit, deterministic schema projection; no expressions, inference or provider access."""

from __future__ import annotations

from pydantic import JsonValue

from bf import links
from bf.markdown import Note, authored, reference, split_ref
from bf.models import Config, Error, Knowledge, Record, Sensor


def pointer(document: JsonValue, path: str) -> JsonValue:
    """Read a JSON Pointer; a missing member is absent, an invalid traversal is an error."""
    value = document
    for encoded in path[1:].split("/"):
        key = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if key not in value:
                return None
            value = value[key]
        elif isinstance(value, list) and key.isascii() and key.isdecimal() and (key == "0" or not key.startswith("0")):
            if int(key) >= len(value):
                return None
            value = value[int(key)]
        else:
            raise Error("schema mapping cannot traverse the sensor value")
    return value


def project(record: Record, sensor: Sensor, config: Config) -> Record:
    """Map reviewed sensor output into common fields before any evidence is committed."""
    if record.fields:
        raise Error("sensors must supply mapped output, not precomputed fields")
    document = record.model_dump(mode="json")
    fields: dict[str, JsonValue] = {}
    for name, mapping in sensor.fields.items():
        definition = config.ontology[name]
        value = pointer(document, mapping.path) if mapping.path is not None else mapping.value
        if value is None:
            if definition.cardinality == "one":
                raise Error(f"schema field {name}: required mapped value is missing")
            continue
        try:
            fields[name] = definition.normalize(value)
        except ValueError as error:
            raise Error(f"schema field {name}: {error}") from error
    result = record.model_copy(update={"fields": fields})
    validate(result, config)
    return result


def validate(record: Record | Knowledge, config: Config) -> None:
    """Validate persisted fields, including historical sources with no active sensor."""
    for name, value in record.fields.items():
        if name not in config.ontology:
            raise Error(f"undeclared schema field {name}")
        try:
            config.ontology[name].normalize(value)
        except ValueError as error:
            raise Error(f"schema field {name}: {error}") from error
        if config.ontology[name].relation:
            for target in value if isinstance(value, list) else [value]:
                links.identity(str(target))
    for alias in record.aliases:
        if parsed := links.parse(alias):
            links.identity(alias)
            if parsed.brain != config.name:
                raise Error("BF aliases must belong to their own brain namespace")
    for target in record.links:
        links.claim(target, config, links.address(config.name, "item"), links.address(config.name, "item"))
    if isinstance(record, Record) and record.url:
        links.claim(record.url, config, links.address(config.name, "item"), links.address(config.name, "item"))


def relations(record: Record | Knowledge, config: Config) -> list[tuple[str, str]]:
    """Each edge is supported by this record; replacement removes its obsolete edges."""
    validate(record, config)
    return [
        (name, links.identity(str(target)))
        for name, value in record.fields.items()
        if config.ontology[name].relation
        for target in (value if isinstance(value, list) else [value])
    ]


def qualify(config: Config, ref: str, fragment: str = "") -> str:
    return links.address(config.name, ref, fragment)


def note_claims(note: Note, config: Config) -> list[links.Claim]:
    origin = qualify(config, note.path)
    subject = links.identity(note.knowledge.entity) if note.knowledge.entity else origin
    if (entity := links.parse(subject)) and entity.brain != config.name:
        raise Error("a note entity must belong to its own brain namespace")
    result = [links.Claim(subject, role, target, origin) for role, target in relations(note.knowledge, config)]
    for value, fragment in note.contexts:
        evidence = qualify(config, note.path, fragment)
        claim = links.claim(value, config, subject, evidence)
        ref = reference(note.path, value)
        if not links.parse(value) and ":" not in ref.split("/")[0]:
            # reference() has already resolved a Markdown-relative path.
            path, section = split_ref(ref)
            if authored(path):
                ref = qualify(config, path, section)
        result.append(claim or links.Claim(subject, "", links.target(ref), evidence))
    return result


def record_claims(record: Record, config: Config, ref: str) -> list[links.Claim]:
    origin = qualify(config, ref)
    result = [links.Claim(origin, role, target, origin) for role, target in relations(record, config)]
    result.extend(
        links.claim(value, config, origin, origin) or links.Claim(origin, "", links.target(value), origin)
        for value in [*record.links, *([record.url] if record.url else [])]
    )
    return result
