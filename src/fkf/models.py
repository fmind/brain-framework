"""Small public contracts shared by collection, storage, CLI and MCP."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator, model_validator

MAX_FILE = 16 << 20
MAX_REPLY = 4 << 20
MAX_FILES = 20_000
MAX_CORPUS = 512 << 20
NAME = r"^[a-z][a-z0-9-]{0,63}$"
BASE_ID = r"^[0-9a-f]{32}$"
AUTHORED = ("projects", "tasks", "wiki")
NoteType = Annotated[str, Field(pattern=r"^(?:[a-z][a-z0-9-]{0,63})?$")]
NoteStatus = Literal[
    "", "draft", "proposed", "accepted", "current", "active", "paused", "blocked", "done", "superseded", "archived"
]


class Error(Exception):
    """A safe, actionable error; never includes provider output."""


class Model(BaseModel):
    """Reject accidental fields and coercions at every external boundary."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def encode(value: object) -> bytes:
    """One compact UTF-8 output representation, including its final newline."""
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Error("JSON contains a duplicate key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise Error("JSON numbers must be finite")


def decode(data: bytes) -> object:
    """Decode exactly one bounded JSON document without duplicate keys or NaN."""
    if len(data) > MAX_FILE:
        raise Error("JSON exceeds the 16 MiB document limit")
    try:
        return json.loads(data, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError, UnicodeError) as error:
        raise Error("invalid JSON document") from error


def explain(error: ValidationError) -> str:
    """Name each invalid field with pydantic's reason, never the rejected value."""
    parts = {".".join(map(str, item["loc"])) or "input": item["msg"] for item in error.errors(include_input=False)}
    return "; ".join(f"{location}: {reason}" for location, reason in sorted(parts.items()))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def timestamp(value: str) -> str:
    """Canonical UTC seconds/microseconds; reject ambiguous naive timestamps."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("expected an ISO 8601 timestamp with timezone") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def clean(value: str) -> str:
    if not value.strip() or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value):
        raise ValueError("value must be nonempty and contain no control characters")
    return value


class Record(Model):
    """A source-normalized record; attributes preserve additional selected data."""

    id: Annotated[str, Field(min_length=1, max_length=4096)]
    title: Annotated[str, Field(min_length=1, max_length=4096)]
    text: Annotated[str, Field(max_length=4 << 20)] = ""
    time: str = ""
    url: str = ""
    links: Annotated[list[str], Field(max_length=1000)] = Field(default_factory=list)
    aliases: Annotated[list[str], Field(max_length=1000)] = Field(default_factory=list)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    kind: Literal["record", "container"] = "record"
    parents: Annotated[list[str], Field(max_length=1000)] = Field(default_factory=list)

    _clean = field_validator("id", "title")(clean)

    @field_validator("time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return timestamp(value) if value else ""

    @field_validator("links", "aliases", "parents")
    @classmethod
    def references(cls, values: list[str]) -> list[str]:
        if any(len(v) > 8192 for v in values):
            raise ValueError("reference exceeds 8192 characters")
        return sorted({clean(v) for v in values})


class Collection(Model):
    """An immutable capture; runtime plans and credentials never enter evidence."""

    version: Literal[1] = 1
    source: Annotated[str, Field(pattern=NAME)]
    captured: str
    start: str = ""
    end: str = ""
    origin: str = ""
    mode: Literal["window", "snapshot"] = "window"
    records: Annotated[list[Record], Field(max_length=100_000)]

    _captured = field_validator("captured")(timestamp)

    @field_validator("start", "end")
    @classmethod
    def boundary(cls, value: str) -> str:
        return timestamp(value) if value else ""

    @model_validator(mode="after")
    def unique_ids(self) -> Collection:
        if bool(self.start) != bool(self.end) or (
            self.start and datetime.fromisoformat(self.start) >= datetime.fromisoformat(self.end)
        ):
            raise ValueError("capture window requires start before end")
        if len({r.id for r in self.records}) != len(self.records):
            raise ValueError("record ids must be unique within a capture")
        return self


def record_uri(capture_hash: str, record_id: str) -> str:
    """Bind an exact original capture and id, independent of future model defaults."""
    return "record:" + digest(encode([capture_hash, record_id]))


def identity_uri(source: str, record_id: str) -> str:
    """Navigate a source item across observations; this is not an evidence citation."""
    return f"source:{source}:{quote(record_id, safe='')}"


def qualify(base_id: str, uri: str) -> str:
    return f"fkf://{base_id}/{uri}"


def local_reference(base_id: str, uri: str) -> str:
    """Never open another base in response to a reference supplied as evidence."""
    if not uri.startswith("fkf://"):
        return uri
    owner, separator, target = uri[6:].partition("/")
    if owner != base_id or not separator or not target:
        raise Error("reference belongs to another base or is malformed; select its base explicitly")
    return target


class Knowledge(Model):
    """Only the metadata that changes retrieval is typed; other authored fields remain data."""

    model_config = ConfigDict(extra="ignore", strict=True)
    type: NoteType = ""
    status: NoteStatus = ""
    reviewed: str = ""
    effective: str = ""
    supersedes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)

    @field_validator("reviewed", "effective")
    @classmethod
    def dated(cls, value: str) -> str:
        try:
            if value and (len(value) != 10 or date.fromisoformat(value).isoformat() != value):
                raise ValueError
        except ValueError:
            raise ValueError("expected YYYY-MM-DD") from None
        return value

    @field_validator("supersedes", "sources")
    @classmethod
    def references(cls, values: list[str]) -> list[str]:
        return Record.references(values)


class Passage(Model):
    fragment: str = ""
    title: str
    text: str
    status: NoteStatus = ""


class Source(Model):
    """Only execution configuration: adapters own provider-specific projection."""

    command: Annotated[list[str], Field(min_length=1, max_length=128)]
    enabled: bool = True
    mode: Literal["window", "snapshot"] = "window"
    timeout: Annotated[int, Field(ge=1, le=3600)] = 120
    max_bytes: Annotated[int, Field(ge=1, le=MAX_FILE)] = MAX_FILE

    @field_validator("command")
    @classmethod
    def valid_command(cls, values: list[str]) -> list[str]:
        for value in values:
            if "\x00" in value or len(value) > 16_384:
                raise ValueError("invalid command argument")
            stripped = re.sub(r"\{\{(?:base|home|start|end)\}\}", "", value)
            if "{{" in stripped or "}}" in stripped:
                raise ValueError("unknown placeholder; use base, home, start or end")
        if not values[0] or "{{" in values[0]:
            raise ValueError("executable must be a literal command name or sources/ path")
        return values


class Config(Model):
    """One base configuration with an optional machine-local source overlay."""

    version: Literal[1] = 1
    id: Annotated[str, Field(pattern=BASE_ID)]
    name: Annotated[str, Field(pattern=NAME)]
    sources: dict[Annotated[str, Field(pattern=NAME)], Source] = Field(default_factory=dict)


class Item(Model):
    """A searchable projection, always resolvable back to durable bytes."""

    uri: str
    kind: Literal["note", "record", "container"]
    title: str
    text: str
    source: str = ""
    time: str = ""
    captured: str = ""
    path: str
    key: str = ""
    aliases: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    parents: list[str] = Field(default_factory=list)
    knowledge: Knowledge = Field(default_factory=Knowledge)
    passages: list[Passage] = Field(default_factory=list)


class Query(Model):
    text: Annotated[str, Field(min_length=1, max_length=4096)]
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    source: str = ""
    order: Literal["relevance", "recent"] = "relevance"
    history: bool = False
    after: str = ""
    before: str = ""
    type: NoteType = ""
    status: NoteStatus = ""
    within: Annotated[str, Field(max_length=8192)] = ""

    @field_validator("after", "before")
    @classmethod
    def valid_after(cls, value: str) -> str:
        return timestamp(value) if value else ""

    @model_validator(mode="after")
    def ordered(self) -> Query:
        if self.text == "*" and not self.within:
            raise ValueError("wildcard browsing requires within")
        if self.after and self.before and datetime.fromisoformat(self.after) >= datetime.fromisoformat(self.before):
            raise ValueError("query after must be earlier than before")
        return self
