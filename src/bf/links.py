"""BF addresses and explicit link claims; external URI semantics are never changed."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from bf.models import AUTHORED, CITES, IDENTITY, LINKS, NAME, TAGGED, Config, Error, clean, tag_name

# The one period syntax, parsed by pages.period(): a day, a range of days, a month or a trailing window, even an
# invalid date.
PERIOD = re.compile(
    r"today|yesterday|[0-9]{4}-[0-9]{2}(?:-[0-9]{2}(?:\.\.[0-9]{4}-[0-9]{2}-[0-9]{2})?)?|[0-9]{1,5}[hdw]"
)


@dataclass(frozen=True)
class Address:
    brain: str
    path: str
    fragment: str
    # The declared relationship of a `?rel=` link; the only query a BF link accepts.
    relation: str = ""

    @property
    def identity(self) -> str:
        return address(self.brain, self.path, self.fragment)


def address(brain: str, path: str, fragment: str = "") -> str:
    """Encode components separately, preserving literal delimiters inside record ids and filenames."""
    return f"bf://{brain}/{quote(path, safe='/@:')}" + ("#" + quote(fragment, safe="") if fragment else "")


def parse(value: str) -> Address | None:
    if not value.lower().startswith("bf:"):
        return None
    # The whole rule, unless a failed check names its exact correction.
    hint = (
        "use bf://brain/path?rel=relation#section, with rel as the only query, "
        "spaces and a literal % percent-encoded as %20 and %25, and without userinfo or traversal"
    )
    try:
        clean(value)
        if len(value) > 8192 or re.search(r"\s|%(?![0-9a-fA-F]{2})", value):
            raise ValueError
        uri = urlsplit(value)
        # No userinfo, ports, credentials, implicit authority or dot-segment rewriting.
        if not re.fullmatch(NAME, uri.netloc):
            raise ValueError
        if not uri.path.startswith("/"):
            hint = "end a brain's address with /, as in bf://brain/ for its home"
            raise ValueError
        # An empty path, as in bf://brain/, is the home page. A page's fragment selects nothing: `bf validate` names
        # such a link, like one to a missing section, instead of failing the note that holds it.
        path = unquote(uri.path[1:], errors="strict")
        fragment = unquote(uri.fragment, errors="strict")
        if path:
            clean(path)
        if fragment:
            clean(fragment)
        source, separator, record_id = path.partition(":")
        record = bool(separator and re.fullmatch(NAME, source) and record_id)
        # A source:id is an opaque lookup key, never a filesystem path. Existing ids can contain URLs.
        segments = path.split("/")
        if path and not record and ("\\" in path or any(p in {".", ".."} for p in segments)):
            raise ValueError
        if path and not record and "" in segments:
            hint = "remove the trailing / or empty path segment, as in bf://brain/projects"
            raise ValueError
        pairs = parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1, errors="strict")
        if pairs and (pairs[0][0] != "rel" or not re.fullmatch(NAME, pairs[0][1])):
            raise ValueError
        return Address(uri.netloc, path, fragment, pairs[0][1] if pairs else "")
    except ValueError:
        # Including the UnicodeError of strict percent-decoding.
        raise Error(f"invalid BF link; {hint}") from None


def identity(value: str) -> str:
    """An identity names no relation; other schemes remain opaque."""
    if parsed := parse(value):
        if parsed.relation:
            raise Error("identity must not contain a link relation")
        return parsed.identity
    try:
        clean(value)
        if len(value) > 8192 or not re.fullmatch(IDENTITY, value):
            raise ValueError
    except ValueError:
        raise Error("expected an explicit namespaced identity") from None
    return value


def target(value: str) -> str:
    parsed = parse(value)
    return parsed.identity if parsed else value


def tag(value: str) -> str | None:
    """A brain-qualified tag is an exact membership scope, never an alias or word query."""
    parsed = parse(value)
    if parsed is None or not parsed.path.startswith("tags/"):
        return None
    try:
        name = tag_name(parsed.path.removeprefix("tags/"))
        if parsed.fragment or parsed.relation:
            raise ValueError
    except ValueError:
        raise Error("use a tag address without a section or relation: bf://brain/tags/label") from None
    return name


def reserved(path: str) -> bool:
    """Whether a brain path belongs to computed pages: home, folder roots, tasks, periods, tags and memories.

    Reserving the whole page namespace keeps an entity from being shadowed by a page, now or in a later release.
    """
    return (
        path in {"", "tasks", *AUTHORED}
        or path.partition("/")[0] in {"tags", "memories"}
        or PERIOD.fullmatch(path) is not None
    )


@dataclass(frozen=True)
class Claim:
    """A directed link from a subject to a target, supported by the section or record that contains it."""

    subject: str
    relation: str
    target: str
    origin: str


def claim(
    value: str, config: Config, subject: str, origin: str, *, strict: bool = False, authored: bool = False
) -> Claim | None:
    """The typed claim of a `?rel=` link: a declared relation or the built-in `cites`.

    Retrieval keeps a link naming an undeclared or reserved relation as an untyped link, so one wrong word never
    hides its note or record; `strict` checks, such as validation and collection, reject it instead. Only an
    `authored` link's relation is quoted: a sensor's output controls a record's, and errors reach run history.
    """
    parsed = parse(value)
    if not parsed or not parsed.relation:
        return None
    if parsed.relation == CITES:
        return Claim(subject, CITES, parsed.identity, origin)
    # bf.yaml cannot declare tagged-with or links, so neither is ever a declared relation here.
    definition = config.ontology.get(parsed.relation)
    if definition is not None and definition.relation:
        return Claim(subject, parsed.relation, parsed.identity, origin)
    if not strict:
        return Claim(subject, "", parsed.identity, origin)
    raise Error(undeclared(parsed.relation, config.ontology, named=authored))


def undeclared(relation: str, fields: Collection[str], *, named: bool = True) -> str:
    """How to correct a `?rel=` naming no declared relation, the one wording of reads, validation and collection.

    A reserved word or a declared field that is no relation is quoted: each is built in or declared in bf.yaml. Any
    other relation is quoted only when `named`: a sensor's output controls a record's.
    """
    if relation == TAGGED:
        return f"link relation {TAGGED} is reserved for tag membership; add the tag to the note instead"
    if relation == LINKS:
        return f"untyped links need no ?rel=; remove ?rel={LINKS}"
    if relation in fields:
        return f"field {relation} is not a relation; relations need type: identity and relation: true"
    quoted = f" {relation}" if named else ""
    return f"link relation{quoted} is undeclared; declare it in bf.yaml fields with type: identity and relation: true"
