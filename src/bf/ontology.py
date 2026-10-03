"""Explicit, deterministic field projection; no expressions, inference or provider access."""

from __future__ import annotations

from contextlib import suppress

from pydantic import JsonValue, ValidationError

from bf import links
from bf.markdown import Note, authored, reference, split_ref
from bf.models import CITES, RECORD_KEYS, Config, Error, Record, Sensor, explain

_PAGES = "home, folder roots, tasks, periods, tags and memories are computed page addresses"
# Reads resolve such a path as a record ref, so an entity or alias there would never reach its owner.
_RECORDS = "a BF path whose first segment contains ':' names a source:id record"
# Reads open such a path as that file, so an entity or alias there would never reach its owner either.
_NOTES = "a BF path to a .md file in projects, actions or concepts names that note"


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
            raise Error("field mapping cannot traverse the sensor value")
    return value


def project(record: Record, sensor: Sensor, config: Config) -> Record:
    """Map reviewed sensor output into common fields before any evidence is committed.

    The record holds no fields: collection rejects printed ones and reprojection clears the stored ones.
    """
    document = record.model_dump(mode="json")
    fields: dict[str, JsonValue] = {}
    for name, mapping in sensor.fields.items():
        definition = config.ontology[name]
        value = pointer(document, mapping.path) if mapping.path is not None else mapping.value
        if value is None:
            if definition.cardinality == "one":
                raise Error(f"field {name}: required mapped value is missing")
            continue
        try:
            fields[name] = definition.normalize(value)
        except ValueError as error:
            raise Error(f"field {name}: {error}") from error
    result = _with(record, fields)
    validate(result, config)
    return result


def _with(record: Record, fields: dict[str, JsonValue]) -> Record:
    """The record with these fields, checked as `records.load` reads it: mapped values can exceed its bounds."""
    if fields == record.fields:
        return record
    try:
        return Record.model_validate({**record.model_dump(), "fields": fields})
    except ValidationError as error:
        raise Error(explain(error, RECORD_KEYS)) from error


def outside(config: Config, claim: links.Claim) -> bool:
    """Whether a typed claim names an identity outside its relation's declared `targets`."""
    definition = config.ontology.get(claim.relation)
    return definition is not None and not definition.allows(claim.target)


def _reject(errors: list[str] | None, message: str) -> None:
    """A check raises its first problem, or adds each one to `errors`."""
    if errors is None:
        raise Error(message)
    errors.append(message)


def validate(record: Record, config: Config, *, errors: list[str] | None = None) -> None:
    """Validate persisted fields, aliases and links, including historical sources with no active sensor.

    Relation values and typed links must also start with their relation's declared targets. Raise the first problem,
    or with `errors` add each one to it. Never quote a value: a sensor's output controls it, and collection errors
    reach run history and status.
    """
    for name, value in record.fields.items():
        definition = config.ontology.get(name)
        if name == links.TAGGED:
            _reject(errors, f"field {name} is reserved for tag membership")
        elif definition is None:
            _reject(errors, f"field {name} is not declared in bf.yaml fields")
        else:
            try:
                definition.normalize(value)
                if definition.relation and not definition.allows(
                    [links.identity(str(target)) for target in (value if isinstance(value, list) else [value])]
                ):
                    raise ValueError("identity outside the declared targets")
            except (Error, ValueError) as error:
                _reject(errors, f"field {name}: {error}")
    try:
        _reachable(record.aliases, config, "aliases")
    except Error as error:
        _reject(errors, str(error))
    item = links.address(config.name, "item")
    for target in [*record.links, *([record.url] if record.url else [])]:
        try:
            claim = links.claim(target, config, item, item, strict=True)
        except Error as error:
            _reject(errors, str(error))
            continue
        if claim and outside(config, claim):
            _reject(errors, f"link relation {claim.relation}: identity outside the declared targets")


def _reachable(values: list[str], config: Config, declared: str) -> None:
    """Each BF identity naming a note or record reaches it only as reads resolve one: in its own brain's namespace,
    never at a page, record or note file address, and without a section.

    `declared` names these identities, such as "aliases"; no value is quoted, as a sensor's output controls a record's.
    """
    for value in values:
        if (parsed := links.parse(value)) is None:
            continue
        links.identity(value)
        if parsed.brain != config.name:
            raise Error(f"BF {declared} must belong to their own brain namespace")
        if links.reserved(parsed.path):
            raise Error(f"{_PAGES} and name no note or record; link to the page instead")
        if ":" in parsed.path.partition("/")[0]:
            raise Error(f"{_RECORDS} and no other subject; link to the record instead")
        if authored(parsed.path):
            raise Error(f"{_NOTES} and no other subject; link to the note instead")
        if parsed.fragment:
            raise Error(f"BF {declared} name a whole subject; remove the #section")


def relations(record: Record, config: Config) -> list[tuple[str, str]]:
    """Each edge is supported by this record; replacement removes its obsolete edges.

    Edges follow the current schema, so a schema edit never hides a stored record: a field no longer declared as a
    relation adds no edge, while a declared relation keeps every stored value that is an identity, whatever
    cardinality or type the field was collected with. `bf validate` names the values the schema now rejects.
    """
    _reachable(record.aliases, config, "aliases")
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
    # The `observed` time returns beside the new fields: the stored record must still load.
    return _with(record, projected.fields)


def qualify(config: Config, ref: str, fragment: str = "") -> str:
    return links.address(config.name, ref, fragment)


def note_claims(
    note: Note, config: Config, *, strict: bool = False, errors: list[str] | None = None
) -> list[links.Claim]:
    """Each link claims a relation from the note's entity, otherwise its file, supported by its section.

    Each OKF source cites its resource from the whole note, unless it is a BF link naming another relation. A path,
    entity, alias or resource that could never name the note raises: retrieval drops the note. Otherwise a link
    naming an undeclared relation stays an untyped link and an invalid field claims nothing, as retrieval keeps
    them; a `strict` check raises the first such problem, or with `errors` adds each one to it once, where it first
    occurs: a relation renamed in bf.yaml can leave hundreds of links naming it.
    """
    file = qualify(config, note.path)
    try:
        links.parse(file)
    except Error:
        # Claims, backlinks and identities name a note by its address.
        raise Error(
            f"{note.path}: path holds a control character or backslash, which no BF address can; rename it"
        ) from None
    entity = note.knowledge.entity
    # `names` holds each identity-shaped alias and resource: a resource describing a population stays data.
    _reachable([entity, *note.knowledge.names], config, "entities, aliases and resources")
    subject = links.identity(entity) if entity else file
    checked = strict or errors is not None
    result = [links.Claim(subject, links.TAGGED, qualify(config, f"tags/{tag}"), file) for tag in note.knowledge.tags]
    # Each rejected relation's message, with the frontmatter key or line of the first link naming it.
    rejected: dict[str, str] = {}

    def target(value: str) -> str:
        ref = reference(note.path, value)
        if not links.parse(value) and ":" not in ref.split("/")[0]:
            # reference() has already resolved a Markdown-relative path. One holding a backslash or control character,
            # as a copied Windows path, has no BF address: it stays a plain path, and `bf validate` reports the link.
            path, section = split_ref(ref)
            if authored(path):
                with suppress(Error):
                    return links.target(qualify(config, path, section))
        return links.target(ref)

    def claim(value: str, origin: str, where: str) -> links.Claim | None:
        """A link's typed claim; a relation the check rejects stays untyped, as retrieval keeps it."""
        try:
            return links.claim(value, config, subject, origin, strict=checked, authored=True)
        except Error as error:
            rejected.setdefault(str(error), where)
            return links.claim(value, config, subject, origin, authored=True)

    # Frontmatter first, as the note reads.
    for key, values, relation in (("links", note.knowledge.links, ""), ("sources", note.sources, CITES)):
        for value in values:
            found = claim(value, file, key)
            # A source cites unless it names another relation: an undeclared one names none, so it still cites.
            if found is None or not found.relation:
                found = links.Claim(subject, relation, found.target if found else target(value), file)
            result.append(found)
    for value, fragment, line in note.contexts:
        origin = qualify(config, note.path, fragment)
        result.append(claim(value, origin, f"line {line}") or links.Claim(subject, "", target(value), origin))
    for message, where in rejected.items():
        _reject(errors, f"{note.path}: {where}: {message}")
    for name, value in note.knowledge.fields.items():
        # Like a record's mapped fields: declared, typed and within their targets. The whole note supports them.
        definition = config.ontology.get(name)
        try:
            if definition is None:
                raise ValueError("not declared in bf.yaml fields")
            normalized = definition.normalize(value)
            targets = normalized if isinstance(normalized, list) else [normalized]
            claims = (
                [links.Claim(subject, name, links.identity(str(t)), file) for t in targets]
                if definition.relation
                else []
            )
        except (Error, ValueError) as error:
            if checked:
                _reject(errors, f"{note.path}: fields.{name}: {error}")
            continue
        result.extend(claims)
    return result


def record_claims(record: Record, config: Config, ref: str) -> list[links.Claim]:
    """A record's claims as retrieval keeps them; `validate` names the relations and values the schema rejects."""
    origin = qualify(config, ref)
    result = [links.Claim(origin, relation, target, origin) for relation, target in relations(record, config)]
    result.extend(
        links.claim(value, config, origin, origin) or links.Claim(origin, "", links.target(value), origin)
        for value in [*record.links, *([record.url] if record.url else [])]
    )
    return result
