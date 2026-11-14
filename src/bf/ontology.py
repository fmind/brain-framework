"""Explicit identity checks, optional relation constraints, and the claims that notes and records assert.

No expressions, inference, attribute scanning, or provider access.
"""

from __future__ import annotations

import re
from contextlib import suppress

from bf import links
from bf.markdown import Note, authored, reference, split_ref
from bf.models import CITES, Config, Error, Record

_PAGES = "home, folder roots, review, tasks, periods, tags and memories are computed page addresses"
# Reads resolve such a path as a record ref, so an entity or alias there would never reach its owner.
_RECORDS = "a BF path whose first segment contains ':' names a source:id record"
# Reads open such a path as that file, so an entity or alias there would never reach its owner either.
_NOTES = "a BF path to a .md file in projects, actions or concepts names that note"
_ACTION_FOLDERS = "a BF path to an action folder in actions/ names its ACTION.md"


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
    """Validate persisted relations, aliases and links against optional bf.yaml constraints.

    Metadata in `attributes` is never scanned as graph edges. Raise the first problem, or with `errors` add each one
    to it. Never quote a value: a sensor's output controls it, and collection errors reach run history and status.
    """
    for name, value in record.relations.items():
        if name in {links.TAGGED, links.LINKS}:
            _reject(errors, f"relation {name} is reserved")
            continue
        targets = value if isinstance(value, list) else [value]
        try:
            checked_targets = [links.identity(target) for target in targets]
        except Error as error:
            _reject(errors, f"relation {name}: {error}")
            continue
        definition = config.ontology.get(name)
        if definition is not None and not definition.allows(checked_targets):
            _reject(errors, f"relation {name}: identity outside the declared targets")
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
        parts = parsed.path.split("/")
        if len(parts) == 2 and parts[0] == "actions" and parts[1]:
            raise Error(f"{_ACTION_FOLDERS} and no other subject; link to the action instead")
        if parsed.fragment:
            raise Error(f"BF {declared} name a whole subject; remove the #section")


def relations(record: Record, config: Config) -> list[tuple[str, str]]:
    """Each explicit relation edge supported by this record (`record.relations`); attributes are never edges."""
    _reachable(record.aliases, config, "aliases")
    result: list[tuple[str, str]] = []
    for name, value in record.relations.items():
        if name in {links.TAGGED, links.LINKS}:
            continue
        for target in value if isinstance(value, list) else [value]:
            with suppress(Error):
                result.append((name, links.identity(target)))
    return result


def qualify(config: Config, ref: str, fragment: str = "") -> str:
    return links.address(config.name, ref, fragment)


def note_claims(
    note: Note, config: Config, *, strict: bool = False, errors: list[str] | None = None
) -> list[links.Claim]:
    """Each link claims a relation from the note's entity, otherwise its file, supported by its section.

    Each OKF source cites its resource from the whole note, unless it is a BF link naming another relation. Any valid
    relation name works without bf.yaml declaration; optional `fields:` target constraints apply when declared.
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
            action = path.rstrip("/")
            if re.fullmatch(r"actions/[^/#]+", action):
                path = action + "/ACTION.md"
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
            # A source cites unless it names another valid relation; a reserved one falls back to cites.
            if found is None or not found.relation:
                found = links.Claim(subject, relation, found.target if found else target(value), file)
            result.append(found)
    for value, fragment, line in note.contexts:
        origin = qualify(config, note.path, fragment)
        result.append(claim(value, origin, f"line {line}") or links.Claim(subject, "", target(value), origin))
    for message, where in rejected.items():
        _reject(errors, f"{note.path}: {where}: {message}")
    for name, value in note.knowledge.relations.items():
        if name in {links.TAGGED, links.LINKS}:
            if checked:
                _reject(errors, f"{note.path}: relations.{name}: relation {name} is reserved")
            continue
        targets = value if isinstance(value, list) else [value]
        try:
            checked_targets = [links.identity(item) for item in targets]
        except Error as error:
            if checked:
                _reject(errors, f"{note.path}: relations.{name}: {error}")
            continue
        definition = config.ontology.get(name)
        if checked and definition is not None and not definition.allows(checked_targets):
            _reject(errors, f"{note.path}: relations.{name}: identity outside the declared targets")
        result.extend(links.Claim(subject, name, item, file) for item in checked_targets)
    return result


def record_claims(record: Record, config: Config, ref: str) -> list[links.Claim]:
    """A record's claims as retrieval keeps them; `validate` checks optional `fields:` target constraints."""
    origin = qualify(config, ref)
    result = [links.Claim(origin, relation, target, origin) for relation, target in relations(record, config)]
    result.extend(
        links.claim(value, config, origin, origin) or links.Claim(origin, "", links.target(value), origin)
        for value in [*record.links, *([record.url] if record.url else [])]
    )
    return result
