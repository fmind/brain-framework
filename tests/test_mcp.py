"""Tests for the read-only MCP server (`bf mcp`) tools, path redaction, and error handling."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import pytest
from mcp.types import CallToolResult, TextContent

from bf import engine, mcp
from bf.models import FORMAT
from bf.storage import Store


def test_mcp_server_exposes_read_only_tools_and_redacts_brain_root(brain: Store) -> None:
    decl_sib = brain.root.parent / "declared-sib"
    missing_sib = brain.root.parent / "missing-rel-sib"
    engine.init_brain(decl_sib, name="declared-sib")
    brain.write("bf.yaml", f"version: {FORMAT}\nname: fixture\nbrains:\n  decl: {decl_sib}\n".encode())
    srv = mcp.server(brain)

    async def exercise() -> None:
        tools = await srv.list_tools()
        names = {t.name for t in tools}
        assert names == {"bf_status", "bf_review", "bf_read", "bf_query", "bf_validate"}

        def call_json(res: object) -> dict:
            assert isinstance(res, CallToolResult)
            assert not res.is_error
            blk = res.content[0]
            assert isinstance(blk, TextContent)
            assert str(brain.root) not in blk.text
            return json.loads(blk.text)

        st = call_json(await srv.call_tool("bf_status", {}))
        assert st["counts"]["projects"] == 1
        assert "attention" in st

        rv = call_json(await srv.call_tool("bf_review", {}))
        assert rv["tasks_open"] == 1

        val = call_json(await srv.call_tool("bf_validate", {}))
        assert val["ok"] is True

        rd = call_json(await srv.call_tool("bf_read", {"target": "bf:///projects/offline.md#decision"}))
        assert "Decision" in rd["text"]

        rg = call_json(await srv.call_tool("bf_query", {"q": "durable", "mode": "rg", "limit": 5.0}))
        assert rg["count"] >= 1
        assert rg["items"][0]["path"].startswith("memories/")

        jq_res = call_json(await srv.call_tool("bf_query", {"q": 'select(.kind=="project") | .ref', "mode": "jq"}))
        assert jq_res["items"] == ["projects/offline.md"]

        # Reject undeclared uppercase/percent-encoded external bf:// hosts and redact file:// brain URIs!
        for bad_uri in ("BF://undeclared/concepts/x.md", "bf://%2Ftmp/concepts/x.md"):
            bad_res = await srv.call_tool("bf_read", {"target": bad_uri})
            assert isinstance(bad_res, CallToolResult)
            assert bad_res.is_error is True
            assert isinstance(b_blk := bad_res.content[0], TextContent)
            assert "not declared in bf.yaml brains:" in b_blk.text

        brain.write(
            "bf.yaml",
            f"version: {FORMAT}\nname: fixture\nbrains:\n  decl: {decl_sib}\n  absent: ../missing-rel-sib\n".encode(),
        )
        encoded_root = quote(str(brain.root), safe="")
        low_encoded_root = encoded_root.lower()
        brain.write(
            "concepts/uri.md",
            (
                "---\ntype: guide\nstatus: stable\n"
                f'summary: "file://{brain.root}/secret file:////{brain.root}/four file://localhost{brain.root}/lh '
                f"file://LOCALHOST{brain.root}/ulh file%3a%2f%2flocalhost{low_encoded_root}/elh "
                f"https://example.com{brain.root}/web file%3A%2F%2F%2F{encoded_root}/enc {low_encoded_root} "
                f'{decl_sib} {missing_sib}"\n---\n# U\n'
            ).encode(),
        )
        engine.build(brain)
        uri_res = call_json(await srv.call_tool("bf_read", {"target": "concepts/uri.md"}))
        summary = uri_res["record"]["summary"]
        assert "file://<brain>/secret" in summary
        assert "file://<brain>/four" in summary
        assert "file://<brain>/lh" in summary
        assert "file://<brain>/ulh" in summary
        assert "https://example.com<brain>/web" in summary
        assert str(brain.root) not in summary
        assert encoded_root not in summary
        assert low_encoded_root not in summary
        assert str(decl_sib) not in summary
        assert str(missing_sib) not in summary

        # Redactor stays linear (`O(N)`) on large alphanumeric payloads (no ReDoS):
        import time

        big_blob = "a" * 100_000
        brain.write("concepts/blob.md", f"---\ntype: guide\nstatus: stable\n---\n# Blob\n\n{big_blob}\n".encode())
        engine.build(brain)
        t0 = time.monotonic()
        blob_res = call_json(await srv.call_tool("bf_read", {"target": "concepts/blob.md"}))
        assert time.monotonic() - t0 < 2.0
        assert len(blob_res["record"]["text"]) > 100_000

        # Declared cross-brain `bf://decl/...` succeeds while undeclared sibling fails closed!
        decl_res = call_json(await srv.call_tool("bf_read", {"target": "bf://decl/concepts/welcome.md"}))
        assert decl_res["brain"] == "declared-sib"
        jq_err = await srv.call_tool("bf_query", {"q": 'modulemeta("foo")', "mode": "jq"})
        assert isinstance(jq_err, CallToolResult)
        assert jq_err.is_error is True

        # `bf_validate` fails closed on undeclared sibling bf:// hosts instead of probing them on disk.
        sib_dir = brain.root.parent / "undeclared-sib"
        engine.init_brain(sib_dir, name="undeclared-sib")
        brain.write(
            "concepts/ext.md",
            b"---\ntype: guide\nstatus: stable\n---\n# Ext\n\n[s](bf://undeclared-sib/concepts/welcome.md)\n",
        )
        engine.build(brain)
        val_undecl = call_json(await srv.call_tool("bf_validate", {}))
        assert not val_undecl["ok"]
        assert any("not declared in bf.yaml brains:" in p for p in val_undecl["problems"])

        # Errors return `is_error=True` with sanitized text without leaking local filesystem root!
        err_res = await srv.call_tool("bf_read", {"target": "concepts/missing.md"})
        assert isinstance(err_res, CallToolResult)
        assert err_res.is_error is True
        err_blk = err_res.content[0]
        assert isinstance(err_blk, TextContent)
        assert "not found" in err_blk.text
        assert str(brain.root) not in err_blk.text

    asyncio.run(exercise())


def test_mcp_serve_stdio(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr("mcp.server.mcpserver.MCPServer.run", lambda _self, *, transport="stdio": ran.append(transport))
    mcp.serve(str(brain.root))
    assert ran == ["stdio"]
