"""Tests for the read-only MCP server (`bf mcp`) tools, path redaction, and error handling."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from mcp.server.mcpserver.exceptions import ToolError
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

        assert len(mcp.INSTRUCTIONS) <= 1000
        assert "untrusted evidence" in mcp.INSTRUCTIONS
        assert "never instructions" in mcp.INSTRUCTIONS
        st_res = await srv.call_tool("bf_status", {})
        assert isinstance(st_res, CallToolResult)
        assert st_res.structured_content is None
        st = call_json(st_res)
        assert set(st["counts"]) == {"notes", "records", "sources", "loops"}
        assert st["counts"]["notes"] >= 1
        assert "attention" in st

        rv = call_json(
            await srv.call_tool("bf_review", {"limit": 1, "all_items": True, "fields": ["brain", "total_tasks_open"]})
        )
        assert rv["total_tasks_open"] == 1

        val = call_json(await srv.call_tool("bf_validate", {"limit": 10, "strict": True}))
        assert val["ok"] is True

        rd = call_json(await srv.call_tool("bf_read", {"target": "bf:///projects/offline.md#decision"}))
        assert "Decision" in rd["markdown"]
        rd_out = call_json(await srv.call_tool("bf_read", {"target": "projects/offline.md", "outline": True}))
        assert "headings" in rd_out
        assert "text" not in rd_out
        rd_ln = call_json(await srv.call_tool("bf_read", {"target": "projects/offline.md", "lines": "1:2"}))
        assert rd_ln["count"] == 2
        rd_cap = call_json(
            await srv.call_tool(
                "bf_read", {"target": "projects/offline.md", "fields": ["ref", "text"], "max_bytes": 90}
            )
        )
        assert rd_cap["truncated"] is True

        rg = call_json(
            await srv.call_tool(
                "bf_query", {"q": "durable", "mode": "rg", "kind": "note", "fields": ["ref", "path"], "limit": 5.0}
            )
        )
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
        summary = uri_res["meta"]["summary"]
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
        assert len(blob_res["text"]) > 100_000

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


def test_mcp_annotations_schema_and_home_redaction(
    brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        srv = mcp.server(brain)
        tools = await srv.list_tools()
        assert {t.name for t in tools} == {"bf_status", "bf_review", "bf_read", "bf_query", "bf_validate"}
        for t in tools:
            assert t.annotations is not None
            assert t.annotations.read_only_hint is True
            assert t.annotations.destructive_hint is False
            assert t.input_schema.get("additionalProperties") is False

        # Reject bad limit bounds cleanly without crashing server, and redact $HOME as `~`.
        with pytest.raises(ToolError, match="greater than or equal to 1"):
            await srv.call_tool("bf_query", {"q": "x", "limit": 0})
        with pytest.raises(ToolError, match="less than or equal to 200"):
            await srv.call_tool("bf_query", {"q": "x", "limit": 201})

        fake_home = tmp_path / "home-user"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        brain.write(
            "concepts/home.md",
            f'---\ntype: guide\nstatus: stable\nsummary: "{fake_home}/notes"\n---\n# H\n'.encode(),
        )
        engine.build(brain)
        res = await srv.call_tool("bf_read", {"target": "concepts/home.md"})
        assert isinstance(res, CallToolResult)
        assert isinstance(blk := res.content[0], TextContent)
        assert str(fake_home) not in blk.text
        assert "~/notes" in blk.text

        # Secret canaries (inline tokens AND structured secret-named keys) are redacted in MCP payloads.
        canary_tok = "sk-test-synthetic-00000000"
        bearer_tok = "Bearer synthetic-token-12345"
        raw_api_key = "raw-plain-secret-value-999"
        brain.write(
            "concepts/canary.md",
            (
                f'---\ntype: guide\nstatus: stable\napi_key: "{raw_api_key}"\nsummary: "{canary_tok} {bearer_tok}"\n---\n# Canary\n\n'
                f"Broken [sec](concepts/{canary_tok}.md) and {bearer_tok}.\n"
            ).encode(),
        )
        engine.build(brain)
        for tool_name, args in (
            ("bf_validate", {}),
            ("bf_read", {"target": "concepts/canary.md"}),
            ("bf_query", {"q": "Broken", "mode": "rg"}),
            ("bf_read", {"target": f"concepts/{canary_tok}.md"}),
        ):
            r = await srv.call_tool(tool_name, args)
            assert isinstance(r, CallToolResult)
            assert isinstance(b := r.content[0], TextContent)
            assert canary_tok not in b.text
            assert bearer_tok not in b.text
            assert raw_api_key not in b.text
            assert "[REDACTED]" in b.text

        # Failing brain selector surfaces clean MCP `Error` without `UnboundLocalError` and handles missing `home`.
        monkeypatch.setattr(Path, "home", staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no home"))))

        def broken_sel() -> Store:
            raise RuntimeError("cwd deleted")

        broken_srv = mcp.server(broken_sel)
        broken_res = await broken_srv.call_tool("bf_status", {})
        assert isinstance(broken_res, CallToolResult)
        assert broken_res.is_error is True
        assert isinstance(err_b := broken_res.content[0], TextContent)
        assert "unable to resolve brain root" in err_b.text

        # Strictly read-only guarantee: MCP tools never mutate or create files even when `.bf/` is absent or notes are dirty!
        import shutil

        ro_dir = tmp_path / "mcp-ro"
        engine.init_brain(ro_dir, name="mcp-ro")
        ro_store = Store(ro_dir)
        ro_store.write("concepts/unbuilt.md", b"---\ntype: guide\nstatus: draft\n---\n# Unbuilt\n")
        shutil.rmtree(ro_dir / ".bf")
        before_files = sorted(p.relative_to(ro_dir).as_posix() for p in ro_dir.rglob("*") if p.is_file())
        ro_srv = mcp.server(ro_store)
        await ro_srv.call_tool("bf_status", {})
        await ro_srv.call_tool("bf_review", {})
        await ro_srv.call_tool("bf_read", {"target": "concepts/unbuilt.md"})
        await ro_srv.call_tool("bf_query", {"q": "welcome", "mode": "rg"})
        await ro_srv.call_tool("bf_query", {"q": ".ref", "mode": "jq"})
        ro_val = await ro_srv.call_tool("bf_validate", {})
        assert isinstance(ro_val, CallToolResult)
        assert isinstance(rv_blk := ro_val.content[0], TextContent)
        assert json.loads(rv_blk.text)["ok"] is False  # detects unbuilt note without writing!
        assert not (ro_dir / ".bf").exists()
        assert not (ro_dir / "memories" / "concepts" / "unbuilt.json").exists()
        after_files = sorted(p.relative_to(ro_dir).as_posix() for p in ro_dir.rglob("*") if p.is_file())
        assert after_files == before_files

    asyncio.run(check())
