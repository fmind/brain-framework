"""BF addresses and explicit link claims; external URI semantics are never changed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from bf.models import NAME, Config, Error, clean


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
        path = unquote(uri.path[1:], errors="strict")
        fragment = unquote(uri.fragment, errors="strict")
        clean(path)
        if fragment:
            clean(fragment)
        source, separator, record_id = path.partition(":")
        record = bool(separator and re.fullmatch(NAME, source) and record_id)
        # A source:id is an opaque lookup key, never a filesystem path. Existing ids can contain URLs.
        if not record and ("\\" in path or any(p in {"", ".", ".."} for p in path.split("/"))):
            raise ValueError
        pairs = parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1, errors="strict")
        if pairs and (pairs[0][0] != "rel" or not re.fullmatch(NAME, pairs[0][1])):
            raise ValueError
        return Address(uri.netloc, path, fragment, pairs[0][1] if pairs else "")
    except ValueError, UnicodeError:
        raise Error(
            "invalid BF link; use bf://brain/path?rel=role#section, with rel as the only query, "
            "and without userinfo or traversal"
        ) from None


def identity(value: str) -> str:
    """An identity names no relationship; other schemes remain opaque."""
    if parsed := parse(value):
        if parsed.relation:
            raise Error("identity must not contain a link relationship")
        return parsed.identity
    try:
        clean(value)
        if len(value) > 8192 or not re.fullmatch(r"[a-z][a-z0-9+.-]*:\S+", value):
            raise ValueError
    except ValueError:
        raise Error("expected an explicit namespaced identity") from None
    return value


def target(value: str) -> str:
    parsed = parse(value)
    return parsed.identity if parsed else value


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
    definition = config.ontology.get(parsed.relation)
    if not definition or not definition.relation:
        raise Error(f"link relation {parsed.relation}: declare an identity relationship in schema")
    return Claim(subject, parsed.relation, parsed.identity, origin)
