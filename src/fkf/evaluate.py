"""Owner-written retrieval cases: the questions a base must keep answering."""

from __future__ import annotations

from typing import Annotated, Literal, cast

from pydantic import Field, ValidationError

from fkf.config import yaml_object
from fkf.models import Error, Model, Query, Status, explain, moment
from fkf.retrieve import search
from fkf.storage import Store


class Case(Model):
    name: str
    query: str = ""
    since: str = ""
    until: str = ""
    source: str = ""
    type: str = ""
    status: Status = ""
    recent: bool = False
    changed_since: str = ""
    current: bool = False
    limit: Annotated[int, Field(ge=1, le=50)] = 10
    # A note path without #fragment matches any of its sections.
    expect: list[str] = Field(default_factory=list)
    forbid: list[str] = Field(default_factory=list)
    # Each text must appear in a returned title or excerpt: the answer is delivered, not only its location.
    text: list[str] = Field(default_factory=list)
    empty: bool = False


class Suite(Model):
    version: Literal[2] = 2
    cases: Annotated[list[Case], Field(min_length=1, max_length=200)]


def _matches(expected: str, refs: list[str]) -> bool:
    return any(ref == expected or ("#" not in expected and ref.partition("#")[0] == expected) for ref in refs)


def evaluate(store: Store, path: str = "queries.yaml") -> dict[str, object]:
    try:
        suite = Suite.model_validate(yaml_object(store.read(path, 1 << 20)))
    except ValidationError as error:
        raise Error(f"invalid {path}: " + explain(error)) from error
    results = []
    for case in suite.cases:
        if case.empty == bool(case.expect or case.text):
            raise Error(f"case {case.name}: use expect/text, or empty: true")
        query = Query(
            text=case.query,
            since=moment(case.since) if case.since else "",
            until=moment(case.until) if case.until else "",
            source=case.source,
            type=case.type,
            status=case.status,
            limit=case.limit,
            recent=case.recent,
            changed_since=moment(case.changed_since) if case.changed_since else "",
            current=case.current,
        )
        reply = search([store], query, counted=False)
        items = cast("list[dict[str, object]]", reply["items"])
        refs = [str(item["ref"]) for item in items]
        delivered = "\n".join(f"{item.get('title', '')}\n{item.get('excerpt', '')}" for item in items)
        missing = [ref for ref in case.expect if not _matches(ref, refs)]
        forbidden = [ref for ref in case.forbid if _matches(ref, refs)]
        absent = [text for text in case.text if text.casefold() not in delivered.casefold()]
        passed = not (missing or forbidden or absent or reply.get("problems") or reply.get("stale")) and (
            not case.empty or not items
        )
        result: dict[str, object] = {"name": case.name, "passed": passed}
        if not passed:
            result.update(missing=missing, forbidden=forbidden, absent=absent, returned=refs)
            for field in ("problems", "stale"):
                if field in reply:
                    result[field] = reply[field]
        results.append(result)
    passed = sum(bool(r["passed"]) for r in results)
    return {"passed": passed == len(results), "score": f"{passed}/{len(results)}", "cases": results}
