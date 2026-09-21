"""The installed-facing command and MCP contracts share behavior and limits."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mcp.types import CallToolResult, TextContent
from typer.testing import CliRunner

from fkf.cli import app
from fkf.index import CACHE, build
from fkf.mcp import server
from fkf.models import Collection, encode
from fkf.storage import Store, writer


def test_cli_lifecycle(tmp_path: Path, base: Store) -> None:
    runner = CliRunner()
    build(base)
    assert runner.invoke(app, ["--help"]).exit_code == 0
    target = tmp_path / "new"
    assert runner.invoke(app, ["init", str(target)]).exit_code == 0
    assert runner.invoke(app, ["init", str(target)]).exit_code != 0
    schema = runner.invoke(app, ["schema"])
    assert schema.exit_code == 0
    assert json.loads(schema.stdout)["properties"]["version"]
    for command in [
        ["validate"],
        ["status"],
        ["find", "offline"],
        ["context", "offline"],
        ["read", "wiki/project.md"],
        ["build"],
        ["build", "--check"],
    ]:
        result = runner.invoke(app, [*command, "--base", str(base.root)])
        assert result.exit_code == 0, result.output
        assert isinstance(json.loads(result.stdout), dict)
    status = runner.invoke(app, ["status", "--base", str(base.root)])
    assert json.loads(status.stdout) == {
        "name": "fixture",
        "base": {"id": "aabbccddeeff00112233445566778899", "name": "fixture"},
        "index": "ready",
        "sources": {
            "meetings": {"enabled": False, "captures": 1, "records": 2, "latest": "2026-09-01T00:00:00.000000Z"}
        },
    }
    base.write(
        "fkf.yaml",
        b"version: 1\nid: aabbccddeeff00112233445566778899\nname: fixture\nsources:\n  fresh: {command: [echo, '[]']}\n",
    )
    base.write("wiki/change.md", b"# New")
    assert runner.invoke(app, ["build", "--check", "--base", str(base.root)]).exit_code == 1
    status = runner.invoke(app, ["status", "--base", str(base.root)])
    assert json.loads(status.stdout)["index"] == "stale"
    assert json.loads(status.stdout)["sources"]["fresh"] == {"enabled": True, "captures": 0, "records": 0, "latest": ""}
    # A quiet day yields an empty capture, which is still a fresh observation of that source.
    empty = Collection(source="fresh", captured="2026-09-03T00:00:00Z", records=[])
    base.write("records/fresh/empty.json", encode(empty.model_dump()))
    status = runner.invoke(app, ["status", "--base", str(base.root)])
    assert json.loads(status.stdout)["sources"]["fresh"] == {
        "enabled": True,
        "captures": 1,
        "records": 0,
        "latest": "2026-09-03T00:00:00.000000Z",
    }


def test_mcp_read_only_tools_and_payload_parity(base: Store) -> None:
    build(base)
    app = server(base)

    async def check() -> None:
        tools = await app.list_tools()
        assert {t.name for t in tools} == {"find", "context", "read"}
        for tool in tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint
            assert not tool.annotations.destructive_hint
        for name, arguments in [
            ("find", {"query": "offline"}),
            ("context", {"query": "offline", "budget": 128}),
            ("read", {"uri": "wiki/project.md"}),
        ]:
            result = await app.call_tool(name, arguments)
            assert isinstance(result, CallToolResult)
            assert isinstance(result.content[0], TextContent)
            assert not result.is_error
            assert json.loads(result.content[0].text) == result.structured_content
        failed = await app.call_tool("read", {"uri": "fkf.yaml"})
        assert isinstance(failed, CallToolResult)
        assert isinstance(failed.content[0], TextContent)
        assert failed.is_error
        assert str(base.root) not in failed.content[0].text
        invalid = await app.call_tool("context", {"query": "x", "budget": 0})
        assert isinstance(invalid, CallToolResult)
        assert invalid.is_error

    asyncio.run(check())


def test_console_errors_are_private_and_on_stderr(base: Store) -> None:
    for args in [["find", "offline"], ["read", "fkf.yaml"], ["find", "query", "--limit", "0"]]:
        result = subprocess.run(  # noqa: S603 - synthetic CLI boundary
            [sys.executable, "-m", "fkf", *args, "--base", str(base.root)], capture_output=True, text=True, check=False
        )
        assert result.returncode
        assert not result.stdout
        assert "fkf:" in result.stderr
        assert str(base.root) not in result.stderr
        if args == ["find", "offline"]:
            assert "missing; run fkf build" in result.stderr
    assert "limit: " in result.stderr
    assert "greater than or equal to 1" in result.stderr
    dated = subprocess.run(  # noqa: S603 - synthetic CLI boundary
        [sys.executable, "-m", "fkf", "find", "x", "--after", "2026-01-01", "--base", str(base.root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dated.returncode == 2
    assert "after: " in dated.stderr
    assert "timezone" in dated.stderr
    version = subprocess.run([sys.executable, "-m", "fkf", "--version"], capture_output=True, text=True, check=False)
    assert version.returncode == 0
    assert version.stdout.strip() == "7.0.0"


def test_initialization_obeys_existing_physical_writer_lock(tmp_path: Path) -> None:
    target = tmp_path / "empty"
    target.mkdir()
    with writer(Store(target)):
        result = CliRunner().invoke(app, ["init", str(target)])
    assert result.exit_code != 0
    assert not list(target.iterdir())


def test_mcp_stdio_handshake_and_read_only_roundtrip(base: Store, tmp_path: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    build(base)

    async def check() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "fkf", "mcp", "--base", str(base.root)],
            env={"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")},
        )
        async with (
            stdio_client(parameters) as (incoming, outgoing),
            ClientSession(incoming, outgoing, read_timeout_seconds=10) as session,
        ):
            await session.initialize()
            listing = await session.list_tools()
            assert {tool.name for tool in listing.tools} == {"find", "context", "read"}
            found = await session.call_tool("context", {"query": "offline", "budget": 850})
            assert isinstance(found, CallToolResult)
            assert not found.is_error
            assert isinstance(found.content[0], TextContent)
            assert json.loads(found.content[0].text) == found.structured_content
            exact = await session.call_tool("read", {"uri": "wiki/project.md"})
            assert isinstance(exact, CallToolResult)
            assert not exact.is_error
            denied = await session.call_tool("read", {"uri": "fkf.yaml"})
            assert isinstance(denied, CallToolResult)
            assert denied.is_error

    asyncio.run(check())


def test_mcp_limit_counts_text_and_structured_representations(base: Store) -> None:
    base.write("wiki/large.md", b"# Large\n\n" + b'"' * 1_000_000)

    build(base)

    async def check() -> None:
        result = await server(base).call_tool("read", {"uri": "wiki/large.md"})
        assert isinstance(result, CallToolResult)
        assert result.is_error
        assert isinstance(result.content[0], TextContent)
        assert "byte limit" in result.content[0].text

    asyncio.run(check())


def test_mcp_context_budget_covers_complete_tool_result(base: Store) -> None:
    from fkf.models import encode

    base.write("wiki/budget.md", ("# Budget\n\n" + "Budget evidence 日本語. " * 100).encode())

    build(base)

    async def check() -> None:
        for budget in (128, 256, 850):
            result = await server(base).call_tool("context", {"query": "budget", "budget": budget})
            assert isinstance(result, CallToolResult)
            assert not result.is_error
            assert len(encode(result.model_dump(mode="json", by_alias=True, exclude_none=True))) <= budget * 4
            if budget >= 256:
                assert result.structured_content is not None
                assert result.structured_content["items"]

    asyncio.run(check())


def test_cli_and_mcp_explain_index_repair_without_writing(base: Store) -> None:
    for state in ("missing", "stale", "corrupt"):
        if state != "missing":
            build(base)
        if state == "stale":
            base.write("wiki/new.md", b"# New evidence")
        if state == "corrupt":
            base.write(CACHE, b"broken")
        before = {path: base.read(path) for path in base.files(".fkf")}
        result = CliRunner().invoke(app, ["find", "offline", "--base", str(base.root)])
        assert result.exit_code == 1
        assert not result.stdout
        assert f"index is {state}; run fkf build" in str(result.exception)
        assert str(base.root) not in str(result.exception)

        async def check(expected: str = state) -> None:
            result = await server(base).call_tool("context", {"query": "offline"})
            assert isinstance(result, CallToolResult)
            assert result.is_error
            assert isinstance(result.content[0], TextContent)
            assert f"index is {expected}; run fkf build" in result.content[0].text

        asyncio.run(check())
        assert {path: base.read(path) for path in base.files(".fkf")} == before
