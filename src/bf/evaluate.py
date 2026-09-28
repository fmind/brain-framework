"""Owner-written retrieval cases: the questions a brain must keep answering."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Annotated, Literal, cast

from pydantic import ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from bf import links
from bf.config import yaml_object
from bf.markdown import authored, split_ref
from bf.models import Error, Model, NotFoundError, Query, check_version, clean, digest, explain
from bf.pages import readable, scope
from bf.retrieve import read, search
from bf.storage import Store

Nonblank = Annotated[str, Field(pattern=r"\S")]


def _query(text: str, value: str = "", limit: int = 10) -> Query:
    """A case's search, by the search's own rules: words to match, a nonempty window and a bounded prefix."""
    try:
        return Query.model_validate({"text": text, "limit": limit, **scope(value)})
    except ValidationError as error:
        raise Error("; ".join(item["msg"].removeprefix("Value error, ") for item in error.errors())) from None


def _bounds(value: str) -> None:
    """A scope checked on its own, so its failure names the scope field."""
    _query("scope", value)


class Case(Model):
    """A search (`query`, optional `scope`) or a read (`read`: a page, note, record or identity)."""

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "oneOf": [
                        {"required": ["query"], "properties": {"query": {"pattern": r"\S"}, "read": {"type": "null"}}},
                        {
                            "required": ["read"],
                            "properties": {
                                "read": {"type": "string"},
                                "query": {"not": {"pattern": r"\S"}},
                                "scope": {"const": ""},
                            },
                            "not": {"required": ["limit"]},
                        },
                    ]
                },
                {
                    "oneOf": [
                        {
                            "required": ["empty"],
                            "properties": {
                                "empty": {"const": True},
                                "expect": {"maxItems": 0},
                                "text": {"maxItems": 0},
                            },
                        },
                        {
                            "properties": {"empty": {"const": False}},
                            "anyOf": [
                                {"required": ["expect"], "properties": {"expect": {"minItems": 1}}},
                                {"required": ["text"], "properties": {"text": {"minItems": 1}}},
                            ],
                        },
                    ]
                },
            ]
        }
    )

    name: Nonblank = Field(description="Nonblank case label, unique within this suite.")
    query: Annotated[str, Field(max_length=4096)] = Field(
        default="", description="Search words; use either query or read."
    )
    scope: str = Field(default="", description="Optional search scope; unavailable for read cases.")
    limit: Annotated[int, Field(ge=1, le=50)] = Field(
        default=10, description="Maximum search hits; unavailable for read cases."
    )
    read: str | None = Field(
        default=None, description="Exact ref or page; an empty string reads home. Use instead of query."
    )
    # A note path without #fragment matches any of its sections.
    expect: list[Nonblank] = Field(
        default_factory=list, description="Refs that must appear; whole-note refs match any section."
    )
    forbid: list[Nonblank] = Field(default_factory=list, description="Refs that must not appear.")
    # Each text must appear in the reply: search titles and excerpts, or any text of a read.
    text: list[Nonblank] = Field(
        default_factory=list, description="Nonblank answer fragments; case-insensitive literal matching."
    )
    empty: bool = Field(default=False, description="Require no returned refs; excludes expect and text assertions.")

    _name = field_validator("name")(clean)

    # Syntax is checked while loading, so a malformed case names its suite and field before any retrieval.
    @field_validator("scope", "read", "expect", "forbid")
    @classmethod
    def syntax(cls, value: str | list[str] | None, info: ValidationInfo) -> str | list[str] | None:
        check = {"scope": _bounds, "read": readable}.get(info.field_name or "", links.parse)
        try:
            for item in value if isinstance(value, list) else [] if value is None else [value]:
                check(item)
        except Error as error:
            raise ValueError(str(error)) from None
        return value

    @model_validator(mode="after")
    def consistent(self) -> Case:
        if self.empty == bool(self.expect or self.text):
            raise ValueError(f"case {self.name}: use expect/text, or empty: true")
        if (self.read is None) == (not self.query.strip()):
            raise ValueError(f"case {self.name}: use either query or read")
        if self.read is not None and (self.scope or "limit" in self.model_fields_set):
            raise ValueError(f"case {self.name}: scope and limit apply to query cases")
        if self.read is None:
            try:
                _query(self.query, self.scope, self.limit)
            except Error as error:
                raise ValueError(f"case {self.name}: {error}") from None
        return self


class Suite(Model):
    """A bounded collection of retrieval assertions; evaluated offline without executing programs."""

    version: Literal[5] = Field(description="Evaluation format, independent of the brain and package versions.")
    cases: Annotated[list[Case], Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def unique(self) -> Suite:
        if len({case.name for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate evaluation case names")
        return self


def _strings(value: object, key: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for name, child in value.items():
            yield from _strings(child, str(name))
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child, key)
    elif isinstance(value, str):
        yield key, value


def _whole(store: Store, ref: str) -> dict[str, object]:
    """Assemble a chunked exact read like a client would: chunks share one digest, verified before parsing."""
    reply = read([store], ref, counted=False)
    if "chunk" not in reply:
        return reply
    expected, parts = reply["sha256"], []
    while True:
        parts.append(str(reply["chunk"]))
        if "next_offset" not in reply:
            break
        reply = read([store], ref, offset=cast("int", reply["next_offset"]), counted=False)
        if reply.get("sha256") != expected:
            raise Error("exact reply changed during evaluation; rerun bf eval")
    text = "".join(parts)
    if digest(text.encode()) != expected:
        raise Error("assembled exact reply does not match its sha256; rerun bf eval")
    return cast("dict[str, object]", json.loads(text))


def _answer(store: Store, case: Case) -> tuple[dict[str, object], list[str], list[str], str]:
    """The reply, its refs, its portable addresses and the text it delivers."""
    if case.read is not None:
        try:
            reply = _whole(store, case.read)
        except NotFoundError:
            # Nothing to read answers "is anything there?"; every other failure fails the case.
            reply = {}
        pairs = [(key, value) for key, value in _strings(reply) if key != "notice"]
        return (
            reply,
            [value for key, value in pairs if key == "ref"],
            [value for key, value in pairs if key == "uri"],
            "\n".join(value for _, value in pairs),
        )
    reply = search([store], _query(case.query, case.scope, case.limit), counted=False)
    items = cast("list[dict[str, object]]", reply["items"])
    return (
        reply,
        [str(item["ref"]) for item in items],
        [str(item["uri"]) for item in items],
        "\n".join(f"{item.get('title', '')}\n{item.get('excerpt', '')}" for item in items),
    )


def _matches(expected: str, refs: list[str]) -> bool:
    parsed = links.parse(expected)
    whole_note = authored(parsed.path if parsed else expected) and not (parsed.fragment if parsed else "#" in expected)
    return any(ref == expected or (whole_note and split_ref(ref)[0] == expected) for ref in refs)


def evaluate(store: Store, path: str = "evals") -> dict[str, object]:
    paths = (
        [path]
        if path.endswith((".yaml", ".yml"))
        else sorted(name for name in store.files(path) if name.endswith((".yaml", ".yml")))
    )
    if not paths:
        raise Error(f"{path} has no suites; add evals/retrieval.yaml before running bf eval")
    if len(paths) > 100:
        raise Error("evaluation exceeds 100 suites")
    cases: list[tuple[str, Case]] = []
    for name in paths:
        try:
            value = yaml_object(store.read(name, 1 << 20), name)
            check_version(value, 5, name)
            suite = Suite.model_validate(value)
        except FileNotFoundError:
            raise Error(f"{name} does not exist; add retrieval cases before running bf eval") from None
        except ValidationError as error:
            raise Error(f"invalid {name}: " + explain(error)) from error
        cases.extend((name, case) for case in suite.cases)
    results = []
    for path, case in cases:
        try:
            reply, refs, uris, delivered = _answer(store, case)
        except Error as error:
            results.append({"suite": path, "name": case.name, "passed": False, "error": str(error)})
            continue
        matches = [*refs, *uris]
        missing = [ref for ref in case.expect if not _matches(ref, matches)]
        forbidden = [ref for ref in case.forbid if _matches(ref, matches)]
        absent = [text for text in case.text if text.casefold() not in delivered.casefold()]
        passed = not (missing or forbidden or absent or reply.get("problems") or reply.get("stale")) and (
            not case.empty or not refs
        )
        result: dict[str, object] = {"suite": path, "name": case.name, "passed": passed}
        if not passed:
            result.update(missing=missing, forbidden=forbidden, absent=absent, returned=refs)
            for field in ("problems", "stale"):
                if field in reply:
                    result[field] = reply[field]
        results.append(result)
    passed = sum(bool(r["passed"]) for r in results)
    return {"passed": passed == len(results), "score": f"{passed}/{len(results)}", "cases": results}
