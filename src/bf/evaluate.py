"""Owner-written retrieval cases: the questions a brain must keep answering."""

from __future__ import annotations

from typing import Annotated, Literal, cast

from pydantic import Field, ValidationError

from bf import links
from bf.config import yaml_object
from bf.markdown import authored, split_ref
from bf.models import Error, Model, Query, Status, explain
from bf.retrieve import search
from bf.storage import Store

# Case fields passed unchanged to Query; `query` becomes its text.
_SEARCH = {
    "since",
    "until",
    "source",
    "type",
    "status",
    "recent",
    "changed_since",
    "current",
    "limit",
    "relation",
    "target",
    "subject",
}


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
    relation: str = ""
    target: str = ""
    subject: str = ""
    limit: Annotated[int, Field(ge=1, le=50)] = 10
    # A note path without #fragment matches any of its sections.
    expect: list[str] = Field(default_factory=list)
    forbid: list[str] = Field(default_factory=list)
    # Each text must appear in a returned title or excerpt: the answer is delivered, not only its location.
    text: list[str] = Field(default_factory=list)
    empty: bool = False


class Suite(Model):
    version: Literal[4] = 4
    cases: Annotated[list[Case], Field(min_length=1, max_length=200)]


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
        try:
            query = Query.model_validate({"text": case.query, **case.model_dump(include=_SEARCH)})
        except ValidationError as error:
            raise Error(f"case {case.name}: " + explain(error)) from error
        reply = search([store], query, counted=False)
        items = cast("list[dict[str, object]]", reply["items"])
        refs = [str(item["ref"]) for item in items]
        matches = [*refs, *(str(item["uri"]) for item in items)]
        delivered = "\n".join(f"{item.get('title', '')}\n{item.get('excerpt', '')}" for item in items)
        missing = [ref for ref in case.expect if not _matches(ref, matches)]
        forbidden = [ref for ref in case.forbid if _matches(ref, matches)]
        absent = [text for text in case.text if text.casefold() not in delivered.casefold()]
        passed = not (missing or forbidden or absent or reply.get("problems") or reply.get("stale")) and (
            not case.empty or not items
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
