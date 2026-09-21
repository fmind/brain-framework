"""Current observations cannot be displaced by better-matching historical captures."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.types import CallToolResult
from typer.testing import CliRunner

from fkf.cli import app
from fkf.evaluate import evaluate
from fkf.index import build
from fkf.mcp import server
from fkf.models import Collection, Query, Record, digest, encode, record_uri
from fkf.retrieve import context, find, read
from fkf.storage import Store


def observations(base: Store) -> tuple[str, str]:
    uris = []
    for day, title, text in [
        (1, "Retention purge obsolete", "Purge originals. Retention purge obsolete."),
        (2, "Retention", "Keep originals because provider retention is limited."),
    ]:
        value = Collection(
            source="decisions",
            captured=f"2026-09-0{day}T00:00:00Z",
            records=[
                Record(
                    id="retention",
                    title=title,
                    text=text,
                    time=f"2026-08-0{day}T00:00:00Z",
                    aliases=["decision:retention"],
                )
            ],
        )
        data = encode(value.model_dump())
        base.write(f"records/decisions/{day}.json", data)
        uris.append(record_uri(digest(data), "retention"))
    return uris[0], uris[1]


def test_latest_precedes_relevance_and_time_filtering(base: Store) -> None:
    old, latest = observations(base)
    build(base)
    result = json.loads(encode(find(base, Query(text="retention purge", source="decisions"))))
    assert result["index"] == "ready"
    assert [(item["uri"], item["snapshot"]) for item in result["items"]] == [(latest, "latest")]
    assert "Keep originals" in result["items"][0]["excerpt"]
    assert not find(base, Query(text="obsolete", source="decisions"))["items"]
    assert not find(base, Query(text="retention", source="decisions", before="2026-08-02T00:00:00Z"))["items"]
    historical = json.loads(encode(context(base, Query(text="obsolete", history=True))))
    assert [(item["uri"], item["snapshot"]) for item in historical["items"]] == [(old, "historical")]
    assert read(base, old)["snapshot"] == "historical"
    assert read(base, "decision:retention")["uri"] == latest
    # An exact captured URI remains an explicit request for those durable bytes.
    exact = json.loads(encode(find(base, Query(text=old))))
    assert exact["items"][0]["uri"] == old
    assert exact["items"][0]["snapshot"] == "historical"
    history = json.loads(encode(find(base, Query(text="decision:retention", history=True))))
    assert {item["uri"] for item in history["items"]} == {old, latest}


@pytest.mark.parametrize("operation", ["find", "context"])
def test_history_is_available_through_cli_and_mcp(base: Store, operation: str) -> None:
    old, _latest = observations(base)
    build(base)
    result = CliRunner().invoke(app, [operation, "obsolete", "--history", "--base", str(base.root)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["items"][0]["uri"] == old

    async def check() -> None:
        result = await server(base).call_tool(operation, {"query": "obsolete", "history": True})
        assert isinstance(result, CallToolResult)
        assert not result.is_error
        assert result.structured_content is not None
        data = json.loads(encode(result.structured_content))
        assert data["items"][0]["uri"] == old
        assert data["items"][0]["snapshot"] == "historical"
        if operation == "context":
            assert len(encode(result.model_dump(mode="json", by_alias=True, exclude_none=True))) <= 850 * 4

    asyncio.run(check())


def test_evaluation_can_require_old_evidence_without_promoting_it(base: Store) -> None:
    old, latest = observations(base)
    # JSON is also valid YAML; no provider or private evidence is involved.
    base.write(
        "queries.yaml",
        encode(
            {
                "version": 1,
                "cases": [
                    {
                        "name": "current",
                        "query": "retention purge",
                        "source": "decisions",
                        "expect": [latest],
                        "forbidden": [old],
                        "excerpts": {latest: ["Keep originals"]},
                    },
                    {
                        "name": "historical",
                        "query": "retention",
                        "source": "decisions",
                        "history": True,
                        "before": "2026-08-02T00:00:00Z",
                        "expect": [old],
                        "reads": {old: ["Purge originals"]},
                    },
                ],
            }
        ),
    )
    build(base)
    assert evaluate(base)["passed"]
