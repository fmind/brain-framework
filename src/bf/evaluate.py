"""Owner-written retrieval cases: the questions a brain must keep answering."""

from __future__ import annotations

from collections.abc import Iterator
from math import inf
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from bf import links
from bf.config import load, yaml_object
from bf.markdown import authored, split_ref
from bf.models import MAX_FILE, Error, Model, NotFoundError, Query, check_version, clean, decode, explain
from bf.pages import address, readable, scope
from bf.retrieve import read, search
from bf.storage import Store

Nonblank = Annotated[str, Field(pattern=r"\S")]
# A search case ranks its expected refs this deep, past its limit, so MRR still sees a drop below the limit.
DEPTH = 50


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

    version: Literal[7] = Field(description="Brain format, shared with bf.yaml; independent of the package version.")
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


def _entries(value: object) -> Iterator[dict[str, object]]:
    """Every object naming a ref, however deeply a page or its backlinks nest it."""
    if isinstance(value, dict):
        if isinstance(value.get("ref"), str):
            yield value
        for child in value.values():
            yield from _entries(child)
    elif isinstance(value, list):
        for child in value:
            yield from _entries(child)


def _text(reply: dict[str, object]) -> str:
    return str(cast("dict[str, object]", reply["record"]).get("text", "") if "record" in reply else reply["text"])


def _whole(store: Store, ref: str) -> dict[str, object]:
    """Assemble a paged exact read like a client would: every page names the same file digest."""
    first = reply = read([store], ref, counted=False)
    if "total_characters" not in reply:
        return reply
    parts = []
    while True:
        parts.append(_text(reply))
        if "next_offset" not in reply:
            break
        reply = read([store], ref, offset=cast("int", reply["next_offset"]), counted=False)
        if reply.get("sha256") != first["sha256"]:
            raise Error("exact reply changed during evaluation; rerun bf eval")
    text = "".join(parts)
    paging = {"offset", "next_offset", "total_characters", "outline", "outline_truncated"}
    whole = {key: value for key, value in first.items() if key not in paging}
    if "record" in whole:
        whole["record"] = {**cast("dict[str, object]", whole["record"]), "text": text}
    else:
        whole["text"] = text
    return whole


def _answer(store: Store, case: Case) -> tuple[dict[str, object], list[str], list[str], str, list[tuple[str, str]]]:
    """The reply, its refs, the refs and addresses expectations match, its text and a search's ranking to DEPTH.

    A plain ref names the evaluated brain's file: an item of a referenced brain, which can hold the same path,
    matches only by its `bf://` address.
    """
    name = load(store).name
    if case.read is not None:
        try:
            reply = _whole(store, case.read)
        except NotFoundError:
            # Nothing to read answers "is anything there?"; every other failure fails the case.
            reply = {}
        pairs = [(key, value) for key, value in _strings(reply) if key != "notice"]
        # An entry without a brain belongs to the evaluated one, the only brain its reply selected.
        entries = [(str(entry.get("brain", name)), entry) for entry in _entries(reply)]
        return (
            reply,
            [str(entry["ref"]) for _, entry in entries],
            [str(entry["ref"]) for brain, entry in entries if brain == name]
            + [value for key, value in pairs if key == "uri"]
            + [address(brain, entry) for brain, entry in entries],
            "\n".join(value for _, value in pairs),
            [],
        )
    # The case's results are the leading items of a deeper search: continuing a search never reorders it.
    reply = search([store], _query(case.query, case.scope, max(case.limit, DEPTH)), counted=False)
    found = cast("list[dict[str, object]]", reply["items"])
    # With one selected brain, items omit their address: every item is the evaluated brain's.
    ranking = [
        (str(item["ref"]) if item.get("brain", name) == name else "", str(item.get("uri") or address(name, item)))
        for item in found
    ]
    items = found[: case.limit]
    return (
        reply,
        [str(item["ref"]) for item in items],
        [ref for pair in ranking[: case.limit] for ref in pair if ref],
        "\n".join(f"{item.get('title', '')}\n{item.get('excerpt', '')}" for item in items),
        ranking,
    )


def _matches(expected: str, refs: list[str]) -> bool:
    parsed = links.parse(expected)
    whole_note = authored(parsed.path if parsed else expected) and not (parsed.fragment if parsed else "#" in expected)
    return any(ref == expected or (whole_note and split_ref(ref)[0] == expected) for ref in refs)


def _ranks(expect: list[str], ranking: list[tuple[str, str]]) -> dict[str, int | None]:
    """Each expected ref's 1-based position among up to DEPTH search items, or None when it is missing."""
    return {ref: next((n for n, item in enumerate(ranking, 1) if _matches(ref, list(item))), None) for ref in expect}


class _Outcome(BaseModel):
    """One case of a previous `bf eval` reply; its other fields are diagnostics."""

    model_config = ConfigDict(strict=True)

    suite: str
    name: str
    passed: bool
    rank: dict[str, Annotated[int, Field(ge=1)] | None] = Field(default_factory=dict)


class _Baseline(BaseModel):
    model_config = ConfigDict(strict=True)

    cases: Annotated[list[_Outcome], Field(max_length=100 * 200)]


def load_baseline(value: str) -> dict[tuple[str, str], _Outcome]:
    """A previous `bf eval` reply, keyed by suite and case name."""
    path = Path(value)
    try:
        # Bounded bytes from a regular file: a plain open would wait on a FIFO.
        data = Store(path.parent).read(path.name)
    except (Error, OSError) as error:
        raise Error(f"baseline must be a readable regular file of at most {MAX_FILE} bytes, not a symlink") from error
    try:
        cases = _Baseline.model_validate(decode(data)).cases
    except (Error, ValidationError) as error:
        raise Error("baseline is not a bf eval reply; save one with bf eval > FILE") from error
    return {(case.suite, case.name): case for case in cases}


def _change(before: _Outcome, after: dict[str, object]) -> int:
    """-1 when a pass became a failure or an expected ref ranks lower, else 1 when a failure passes or one ranks higher."""
    ranks = cast("dict[str, int | None]", after.get("rank", {}))
    # A missing ref ranks below every position.
    moves = [
        (old or inf) - (new or inf)
        for ref, new in ranks.items()
        if ref in before.rank and (old := before.rank[ref]) != new
    ]
    if (before.passed and not after["passed"]) or any(move < 0 for move in moves):
        return -1
    return 1 if (after["passed"] and not before.passed) or moves else 0


def _compare(results: list[dict[str, object]], previous: dict[tuple[str, str], _Outcome]) -> dict[str, object]:
    changes: dict[int, list[dict[str, object]]] = {-1: [], 1: []}
    for result in results:
        before = previous.get((str(result["suite"]), str(result["name"])))
        if before is not None and (change := _change(before, result)):
            changes[change].append(
                {
                    **{key: result[key] for key in ("suite", "name", "passed", "rank") if key in result},
                    "baseline": {"passed": before.passed, **({"rank": before.rank} if before.rank else {})},
                }
            )
    return {"regressions": changes[-1], "improvements": changes[1]}


def evaluate(
    store: Store, path: str = "evals", previous: dict[tuple[str, str], _Outcome] | None = None
) -> dict[str, object]:
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
            check_version(value, name)
            suite = Suite.model_validate(value)
        except FileNotFoundError:
            raise Error(f"{name} does not exist; add retrieval cases before running bf eval") from None
        except ValidationError as error:
            raise Error(f"invalid {name}: " + explain(error)) from error
        cases.extend((name, case) for case in suite.cases)
    results: list[dict[str, object]] = []
    # Each ranked search case's reciprocal rank of its best-placed expected ref within DEPTH results, even below its
    # limit; a miss or an error counts zero.
    reciprocal: list[float] = []
    for path, case in cases:
        ranked = case.read is None and bool(case.expect)
        try:
            reply, refs, matches, delivered, ranking = _answer(store, case)
        except Error as error:
            results.append({"suite": path, "name": case.name, "passed": False, "error": str(error)})
            reciprocal.extend([0.0] if ranked else [])
            continue
        missing = [ref for ref in case.expect if not _matches(ref, matches)]
        forbidden = [ref for ref in case.forbid if _matches(ref, matches)]
        absent = [text for text in case.text if text.casefold() not in delivered.casefold()]
        passed = not (missing or forbidden or absent or reply.get("problems") or reply.get("stale")) and (
            not case.empty or not refs
        )
        result: dict[str, object] = {"suite": path, "name": case.name, "passed": passed}
        if ranked:
            result["rank"] = ranks = _ranks(case.expect, ranking)
            reciprocal.append(max((1 / n for n in ranks.values() if n), default=0.0))
        if not passed:
            result.update(missing=missing, forbidden=forbidden, absent=absent, returned=refs)
            for field in ("problems", "stale"):
                if field in reply:
                    result[field] = reply[field]
        results.append(result)
    passed = sum(bool(r["passed"]) for r in results)
    summary = {
        "passed": passed == len(results),
        "score": f"{passed}/{len(results)}",
        **({"mrr": round(sum(reciprocal) / len(reciprocal), 4)} if reciprocal else {}),
        "cases": results,
    }
    return {**summary, **_compare(results, previous)} if previous is not None else summary
