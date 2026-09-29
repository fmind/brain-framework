"""Explicit, deterministic schema projection; no expressions, inference or provider access."""

from __future__ import annotations

from contextlib import suppress

from pydantic import JsonValue

from bf import links
from bf.markdown import Note, authored, reference, split_ref
from bf.models import CITES, Config, Error, Record, Sensor

_PAGES = "home, folder roots, tasks, periods, tags and memories are computed page addresses"
# Reads resolve such a path as a record ref, so an entity or alias there would never reach its owner.
_RECORDS = "a BF path whose first segment contains ':' names a source:id record"


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


def outside(config: Config, claim: links.Claim) -> bool:
    """Whether a typed claim names an identity outside its relation's declared `targets`."""
    definition = config.ontology.get(claim.relation)
    return definition is not None and not definition.allows(claim.target)


def validate(record: Record, config: Config, *, fields: bool = True) -> None:
    """Validate persisted fields, including historical sources with no active sensor.

    With `fields`, relation values and typed links must also start with their relation's declared targets. Never
    quote a value: a sensor's output controls it, and collection errors reach run history and status.
    """
    for name, value in record.fields.items() if fields else ():
        if name == links.TAGGED:
            raise Error(f"schema field {name} is reserved for tag membership")
        if name not in config.ontology:
            raise Error(f"undeclared schema field {name}")
        definition = config.ontology[name]
        try:
            definition.normalize(value)
        except ValueError as error:
            raise Error(f"schema field {name}: {error}") from error
        if definition.relation and not definition.allows(
            [links.identity(str(target)) for target in (value if isinstance(value, list) else [value])]
        ):
            raise Error(f"schema field {name}: identity outside the declared targets")
    for alias in record.aliases:
        if links.computed(alias):
            raise Error(f"{_PAGES} and cannot be aliases")
        if links.record_address(alias):
            raise Error(f"{_RECORDS} and cannot be an alias")
        if parsed := links.parse(alias):
            links.identity(alias)
            if parsed.brain != config.name:
                raise Error("BF aliases must belong to their own brain namespace")
    item = links.address(config.name, "item")
    for target in [*record.links, *([record.url] if record.url else [])]:
        claim = links.claim(target, config, item, item)
        if fields and claim and outside(config, claim):
            raise Error(f"link relation {claim.relation}: identity outside the declared targets")


def relations(record: Record, config: Config) -> list[tuple[str, str]]:
    """Each edge is supported by this record; replacement removes its obsolete edges.

    Edges follow the current schema, so a schema edit never hides a stored record: a field no longer declared as a
    relation adds no edge, while a declared relation keeps every stored value that is an identity, whatever
    cardinality or type the field was collected with. `bf validate` names the values the schema now rejects.
    """
    validate(record, config, fields=False)
    result = []
    for name, value in record.fields.items():
        if (definition := config.ontology.get(name)) is None or not definition.relation:
            continue
        for target in value if isinstance(value, list) else [value]:
            with suppress(Error):
                # A value collected before the field became an identity never named one; it claims nothing.
                result.append((name, links.identity(str(target))))
    return result


def reproject(record: Record, sensor: Sensor, config: Config) -> Record:
    """A stored record with the fields its sensor's current mappings give it; its other values stay as stored.

    Mappings read the record as its sensor printed it: without the fields they computed and the `observed` time
    collection added. A value the sensor never printed cannot be mapped now.
    """
    printed = {key: value for key, value in record.attributes.items() if key != "observed"}
    projected = project(record.model_copy(update={"fields": {}, "attributes": printed}), sensor, config)
    return record.model_copy(update={"fields": projected.fields})


def qualify(config: Config, ref: str, fragment: str = "") -> str:
    return links.address(config.name, ref, fragment)


def note_claims(note: Note, config: Config) -> list[links.Claim]:
    """Each link claims a relation from the note's entity, otherwise its file, supported by its section.

    Each OKF source cites its resource from the whole note, unless it is a BF link naming another relationship.
    """
    file = qualify(config, note.path)
    subject = links.identity(note.knowledge.entity) if note.knowledge.entity else file
    if (entity := links.parse(subject)) and entity.brain != config.name:
        raise Error("a note entity must belong to its own brain namespace")
    for alias in note.knowledge.aliases:
        if (parsed := links.parse(alias)) and parsed.brain != config.name:
            raise Error(f"{note.path}: BF aliases must belong to their own brain namespace")
    if any(links.computed(value) for value in [subject, *note.knowledge.aliases]):
        raise Error(f"{_PAGES} and cannot be entities or aliases; link to the page instead")
    if any(links.record_address(value) for value in [subject, *note.knowledge.aliases]):
        raise Error(f"{_RECORDS} and cannot be an entity or alias; link to the record instead")
    result = [links.Claim(subject, links.TAGGED, qualify(config, f"tags/{tag}"), file) for tag in note.knowledge.tags]

    def target(value: str) -> str:
        ref = reference(note.path, value)
        if not links.parse(value) and ":" not in ref.split("/")[0]:
            # reference() has already resolved a Markdown-relative path.
            path, section = split_ref(ref)
            if authored(path):
                ref = qualify(config, path, section)
        return links.target(ref)

    for value, fragment in note.contexts:
        origin = qualify(config, note.path, fragment)
        result.append(links.claim(value, config, subject, origin) or links.Claim(subject, "", target(value), origin))
    result.extend(
        links.claim(value, config, subject, file) or links.Claim(subject, CITES, target(value), file)
        for value in note.sources
    )
    return result


def record_claims(record: Record, config: Config, ref: str) -> list[links.Claim]:
    origin = qualify(config, ref)
    result = [links.Claim(origin, role, target, origin) for role, target in relations(record, config)]
    result.extend(
        links.claim(value, config, origin, origin) or links.Claim(origin, "", links.target(value), origin)
        for value in [*record.links, *([record.url] if record.url else [])]
    )
    return result
