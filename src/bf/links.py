"""BF addresses and explicit link claims; external URI semantics are never changed."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from pydantic import JsonValue

from bf.models import NAME, Config, Error, clean, decode

RESERVED = frozenset({"rel", "subject", "evidence", "asserted-by"})


@dataclass(frozen=True)
class Address:
    brain: str
    path: str
    fragment: str
    attributes: dict[str, str]

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
        pairs = parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True, max_num_fields=32, errors="strict")
        attributes: dict[str, str] = {}
        for key, val in pairs:
            if key in attributes or not re.fullmatch(NAME, key):
                raise ValueError
            attributes[key] = clean(val)
        if attributes and not re.fullmatch(NAME, attributes.get("rel", "")):
            raise ValueError
        return Address(uri.netloc, path, fragment, attributes)
    except ValueError, UnicodeError:
        raise Error("invalid BF link; use bf://brain/path?rel=role#section without userinfo or traversal") from None


def identity(value: str) -> str:
    """An identity contains no edge attributes; other schemes remain opaque."""
    if parsed := parse(value):
        if parsed.attributes:
            raise Error("identity must not contain link attributes")
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
    subject: str
    relation: str
    target: str
    evidence: str
    asserted_by: str = ""
    attributes: dict[str, JsonValue] = field(default_factory=dict)
    origin: str = ""


def claim(value: str, config: Config, subject: str, evidence: str) -> Claim | None:
    parsed = parse(value)
    if not parsed or not parsed.attributes:
        return None
    params = parsed.attributes
    role = params["rel"]
    definition = config.ontology.get(role)
    if not definition or not definition.relation:
        raise Error(f"link relation {role}: declare an identity relationship in schema")
    attributes: dict[str, JsonValue] = {}
    for key, val in params.items():
        if key in RESERVED:
            continue
        schema = config.ontology.get(key)
        if not schema or schema.relation:
            raise Error(f"link attribute {key}: declare a non-relationship schema field")
        try:
            scalar = (
                val
                if schema.cardinality != "many" and schema.type in {"string", "identity", "timestamp"}
                else decode(val)
            )
            # JSON values are validated below; numeric/bool queries use JSON literal spelling.
            attributes[key] = schema.normalize(cast(JsonValue, scalar))
        except (ValueError, Error) as error:
            raise Error(f"invalid link attribute {key}") from error
    return Claim(
        identity(params.get("subject", subject)),
        role,
        parsed.identity,
        identity(params.get("evidence", evidence)),
        identity(params["asserted-by"]) if "asserted-by" in params else "",
        attributes,
        evidence,
    )
