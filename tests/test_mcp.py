"""Tests for the read-only MCP server (`bf mcp`) tools, path redaction, and error handling."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.types import CallToolResult, TextContent

from bf import mcp
from bf.models import InputError
from bf.storage import Store


def test_mcp_server_exposes_read_only_tools_and_redacts_brain_root(brain: Store) -> None:
    srv = mcp.server(brain)

    async def exercise() -> None:
        tools = await srv.list_tools()
        names = {t.name for t in tools}
        assert names == {"bf_review", "bf_read", "bf_query", "bf_validate"}

        def call_json(res: object) -> dict:
            assert isinstance(res, CallToolResult)
            assert not res.is_error
            blk = res.content[0]
            assert isinstance(blk, TextContent)
            assert str(brain.root) not in blk.text
            return json.loads(blk.text)

        rv = call_json(await srv.call_tool("bf_review", {}))
        assert rv["tasks_open"] == 1

        val = call_json(await srv.call_tool("bf_validate", {}))
        assert val["valid"] is True

        rd = call_json(await srv.call_tool("bf_read", {"target": "bf:///projects/offline.md#decision"}))
        assert "Decision" in rd["text"]

        rg = call_json(await srv.call_tool("bf_query", {"q": "durable", "mode": "rg", "limit": 5.0}))
        assert rg["count"] >= 1
        assert rg["items"][0]["path"].startswith("memories/")

        jq_res = call_json(await srv.call_tool("bf_query", {"q": 'select(.kind=="project") | .ref', "mode": "jq"}))
        assert jq_res["items"] == ["projects/offline.md"]

        sql_res = call_json(await srv.call_tool("bf_query", {"q": "SELECT count(*) AS n FROM records", "mode": "sql"}))
        assert sql_res["items"][0]["n"] >= 3

        # Errors return `is_error=True` with sanitized text without leaking local filesystem root!
        err_res = await srv.call_tool("bf_query", {"q": ".shell id", "mode": "sql"})
        assert isinstance(err_res, CallToolResult)
        assert err_res.is_error is True
        err_blk = err_res.content[0]
        assert isinstance(err_blk, TextContent)
        assert "dot-commands" in err_blk.text
        assert str(brain.root) not in err_blk.text

    asyncio.run(exercise())

    with pytest.raises(InputError, match="between 1 and 65535"):
        mcp.serve(str(brain.root), port=0)


def test_mcp_serve_transports(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr("mcp.server.mcpserver.MCPServer.run", lambda _self, *, transport="stdio": ran.append(transport))
    mcp.serve(str(brain.root), transport="stdio")
    assert ran == ["stdio"]

    http_calls: list[tuple[str, int]] = []
    monkeypatch.setattr("uvicorn.run", lambda _app, *, host, port, **_kw: http_calls.append((host, port)))
    mcp.serve(str(brain.root), transport="http", port=8799)
    assert http_calls == [("127.0.0.1", 8799)]
