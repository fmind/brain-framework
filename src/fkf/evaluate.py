"""Owner-authored acceptance cases over delivered context and exact evidence."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, ValidationError

from fkf.config import load, yaml_object
from fkf.index import database
from fkf.models import Error, Model, NoteStatus, NoteType, Query, local_reference
from fkf.retrieve import context, read
from fkf.storage import Store


class Case(Model):
    name: str
    query: str
    expect: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    excerpts: dict[str, list[str]] = Field(default_factory=dict)
    reads: dict[str, list[str]] = Field(default_factory=dict)
    empty: bool = False
    source: str = ""
    order: Literal["relevance", "recent"] = "relevance"
    history: bool = False
    after: str = ""
    before: str = ""
    type: NoteType = ""
    status: NoteStatus = ""
    within: str = ""
    limit: Annotated[int, Field(ge=1, le=100)] = 10
    budget: Annotated[int, Field(ge=128, le=16384)] = 850


class Suite(Model):
    version: Literal[1] = 1
    cases: Annotated[list[Case], Field(min_length=1, max_length=100)]


def evaluate(store: Store, path: str = "queries.yaml") -> dict[str, object]:
    config = load(store)
    try:
        suite = Suite.model_validate(yaml_object(store.read(path, 1 << 20)))
    except ValidationError as error:
        raise Error("invalid evaluation suite") from error
    results = []
    with database(store) as (connection, _state):
        aliases = {
            row["alias"]: row["uri"]
            for row in connection.execute(
                "SELECT a.alias,a.uri FROM aliases a JOIN entries e ON e.uri=a.uri ORDER BY e.captured,e.uri DESC",
                (),
            )
        }
    for case in suite.cases:
        if (not case.empty and not case.expect) or (case.empty and case.expect):
            raise Error("each evaluation case needs expected URIs or empty: true")
        pack = context(
            store,
            Query(
                text=case.query,
                limit=case.limit,
                source=case.source,
                order=case.order,
                history=case.history,
                after=case.after,
                before=case.before,
                type=case.type,
                status=case.status,
                within=case.within,
            ),
            case.budget,
        )
        items = pack["items"]
        if not isinstance(items, list):
            raise Error("invalid context result")
        delivered = {item["uri"]: item for item in items}
        delivered.update({local_reference(config.id, item["ref"]): item for item in items})

        def resolve(uri):
            uri = local_reference(config.id, uri)
            return aliases.get(uri, uri)

        missing = [uri for uri in case.expect if resolve(uri) not in delivered]
        forbidden = [uri for uri in case.forbidden if resolve(uri) in delivered]
        evidence = []
        for uri, fragments in case.excerpts.items():
            item = delivered.get(resolve(uri), {})
            if not fragments or any(part not in item.get("excerpt", "") for part in fragments):
                evidence.append(uri)
        for uri, fragments in case.reads.items():
            try:
                value = read(store, uri)
            except Error, OSError, UnicodeError:
                evidence.append(uri)
                continue
            record = value.get("record", {})
            body = str(value.get("text", "") or (record.get("text", "") if isinstance(record, dict) else ""))
            if not fragments or any(part not in body for part in fragments):
                evidence.append(uri)
        passed = not (missing or forbidden or evidence) and (not case.empty or not delivered)
        results.append(
            {"name": case.name, "passed": passed, "missing": missing, "forbidden": forbidden, "missing_text": evidence}
        )
    return {"passed": all(r["passed"] for r in results), "cases": results}
