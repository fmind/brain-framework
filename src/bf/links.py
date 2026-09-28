"""BF addresses and explicit link claims; external URI semantics are never changed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from bf.models import AUTHORED, IDENTITY, NAME, TAGGED, Config, Error, clean, tag_name

# The one period syntax, parsed by pages.period(): a day, a month or a trailing window, even an invalid date.
PERIOD = re.compile(r"today|yesterday|[0-9]{4}-[0-9]{2}(?:-[0-9]{2})?|[0-9]{1,5}[hdw]")


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
    try:
        clean(value)
        if len(value) > 8192 or re.search(r"\s|%(?![0-9a-fA-F]{2})", value):
            raise ValueError
        uri = urlsplit(value)
        # No userinfo, ports, credentials, implicit authority or dot-segment rewriting.
        if not re.fullmatch(NAME, uri.netloc) or not uri.path.startswith("/"):
            raise ValueError
        # An empty path, as in bf://brain/, is the home page.
        path = unquote(uri.path[1:], errors="strict")
        fragment = unquote(uri.fragment, errors="strict")
        if path:
            clean(path)
        if fragment:
            clean(fragment)
            if path in {"", "tasks"}:
                raise ValueError
        source, separator, record_id = path.partition(":")
        record = bool(separator and re.fullmatch(NAME, source) and record_id)
        # A source:id is an opaque lookup key, never a filesystem path. Existing ids can contain URLs.
        if path and not record and ("\\" in path or any(p in {"", ".", ".."} for p in path.split("/"))):
            raise ValueError
        pairs = parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1, errors="strict")
        if pairs and (pairs[0][0] != "rel" or not re.fullmatch(NAME, pairs[0][1])):
            raise ValueError
        return Address(uri.netloc, path, fragment, pairs[0][1] if pairs else "")
    except ValueError, UnicodeError:
        raise Error(
            "invalid BF link; use bf://brain/path?rel=role#section, with rel as the only query, "
            "spaces and a literal % percent-encoded as %20 and %25, and without userinfo or traversal"
        ) from None


def identity(value: str) -> str:
    """An identity names no relationship; other schemes remain opaque."""
    if parsed := parse(value):
        if parsed.relation:
            raise Error("identity must not contain a link relationship")
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
        raise Error("use a tag address without a section or relationship: bf://brain/tags/label") from None
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


def computed(value: str) -> bool:
    """A page address cannot be claimed as the identity of a note or record."""
    return (parsed := parse(value)) is not None and reserved(parsed.path)


def record_address(value: str) -> bool:
    """A BF path whose first segment contains ':' names a source:id record, never an entity or alias."""
    return (parsed := parse(value)) is not None and ":" in parsed.path.partition("/")[0]


@dataclass(frozen=True)
class Claim:
    """A directed link from a subject to a target, supported by the section or record that contains it."""

    subject: str
    relation: str
    target: str
    origin: str


def claim(value: str, config: Config, subject: str, origin: str) -> Claim | None:
    """The typed claim of a `?rel=` link; other values are untyped links."""
    parsed = parse(value)
    if not parsed or not parsed.relation:
        return None
    if parsed.relation == TAGGED:
        raise Error(f"link relation {TAGGED} is reserved for tag membership; add the tag to the note instead")
    definition = config.ontology.get(parsed.relation)
    if not definition or not definition.relation:
        # Never quote the relation: a sensor's output controls it, and errors reach run history and status.
        raise Error("link relation is undeclared; declare an identity relationship in schema")
    return Claim(subject, parsed.relation, parsed.identity, origin)
