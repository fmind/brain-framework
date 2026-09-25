"""Owner-written retrieval cases: the questions a brain must keep answering."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Literal, cast

from pydantic import Field, ValidationError

from bf import links
from bf.config import yaml_object
from bf.markdown import authored, split_ref
from bf.models import Error, Model, Query, explain
from bf.pages import scope
from bf.retrieve import read, search
from bf.storage import Store


class Case(Model):
    """A search (`query`, optional `scope`) or a read (`read`: a page, note, record or identity)."""

    name: str
    query: str = ""
    scope: str = ""
    limit: Annotated[int, Field(ge=1, le=50)] = 10
    read: str | None = None
    # A note path without #fragment matches any of its sections.
    expect: list[str] = Field(default_factory=list)
    forbid: list[str] = Field(default_factory=list)
    # Each text must appear in the reply: search titles and excerpts, or any text of a read.
    text: list[str] = Field(default_factory=list)
    empty: bool = False


class Suite(Model):
    version: Literal[5] = 5
    cases: Annotated[list[Case], Field(min_length=1, max_length=200)]


def _strings(value: object, key: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for name, child in value.items():
            yield from _strings(child, str(name))
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child, key)
    elif isinstance(value, str):
        yield key, value


def _answer(store: Store, case: Case) -> tuple[dict[str, object], list[str], list[str], str]:
    """The reply, its refs, its portable addresses and the text it delivers."""
    if case.read is not None:
        try:
            reply = read([store], case.read, counted=False)
        except Error as error:
            # Nothing to read answers "is anything there?"; every other failure fails the case.
            if not str(error).startswith(("reference not found", "page not found")):
                raise
            reply = {}
        pairs = [(key, value) for key, value in _strings(reply) if key != "notice"]
        return (
            reply,
            [value for key, value in pairs if key == "ref"],
            [value for key, value in pairs if key == "uri"],
            "\n".join(value for _, value in pairs),
        )
    try:
        query = Query.model_validate({"text": case.query, "limit": case.limit, **scope(case.scope)})
    except (ValidationError, Error) as error:
        reason = explain(error) if isinstance(error, ValidationError) else str(error)
        raise Error(f"case {case.name}: {reason}") from error
    reply = search([store], query, counted=False)
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
            suite = Suite.model_validate(yaml_object(store.read(name, 1 << 20)))
        except FileNotFoundError:
            raise Error(f"{name} does not exist; add retrieval cases before running bf eval") from None
        except ValidationError as error:
            raise Error(f"invalid {name}: " + explain(error)) from error
        if len({case.name for case in suite.cases}) != len(suite.cases):
            raise Error(f"{name}: duplicate evaluation case names")
        cases.extend((name, case) for case in suite.cases)
    results = []
    for path, case in cases:
        if case.empty == bool(case.expect or case.text):
            raise Error(f"case {case.name}: use expect/text, or empty: true")
        if (case.read is None) == (not case.query.strip()):
            raise Error(f"case {case.name}: use either query or read")
        if case.read is not None and (case.scope or "limit" in case.model_fields_set):
            raise Error(f"case {case.name}: scope and limit apply to query cases")
        try:
            reply, refs, uris, delivered = _answer(store, case)
        except Error as error:
            if str(error).startswith(f"case {case.name}:"):
                raise
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
