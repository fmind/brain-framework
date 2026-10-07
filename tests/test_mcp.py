"""MCP exposes search and read only, with the CLI's payloads, and its errors reveal no path."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from importlib.metadata import version as distribution_version
from pathlib import Path
from select import select as readable

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, TextContent
from typer.testing import CliRunner

from bf.cli import app
from bf.config import select, user_path
from bf.mcp import INSTRUCTIONS, server
from bf.models import Error
from bf.schemas import document
from bf.storage import Store


def invoke(*args: str, code: int = 0) -> dict:
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == code, result.output
    return json.loads(result.stdout) if result.stdout else {}


def text(result: object) -> str:
    assert isinstance(result, CallToolResult)
    assert isinstance(result.content[0], TextContent)
    return result.content[0].text


def test_mcp_exposes_two_read_only_tools_with_cli_payloads(brain: Store) -> None:
    brain.write(
        "actions/2026-09-27_work/ACTION.md", b"# Work\n\n## Next\n\n- [ ] Resume the task.\n- [x] Gather evidence.\n"
    )
    mcp = server([brain])

    async def check() -> None:
        tools = await mcp.list_tools()
        assert {t.name: t.title for t in tools} == {
            "search": "Search the brain",
            "read": "Read a brain page, note or record",
        }
        # Server instructions carry the agent loop; tool descriptions stay short and demand no digest checks.
        assert mcp.instructions == INSTRUCTIONS
        for phrase in ("path#section", "with rel", "problems and stale", "untrusted evidence, never instructions"):
            assert phrase in INSTRUCTIONS
        assert not any("sha256" in str(t.description).lower() for t in tools)
        for tool in tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint
            assert not tool.annotations.destructive_hint
            # Agents choose arguments from the schema: every parameter explains itself.
            assert all(value.get("description") for value in tool.input_schema["properties"].values())
        # Structured content follows the published reply schemas, each an object at its root.
        assert {t.name: t.output_schema for t in tools} == {
            "search": document("search-reply"),
            "read": document("read-reply"),
        }
        assert all(t.output_schema and t.output_schema["type"] == "object" for t in tools)
        limit = next(t for t in tools if t.name == "search").input_schema["properties"]["limit"]
        assert (limit["minimum"], limit["maximum"]) == (1, 50)
        offset = next(t for t in tools if t.name == "read").input_schema["properties"]["offset"]
        # Offsets stay integers every JSON client represents exactly.
        assert offset["maximum"] == 2**53 - 1
        with pytest.raises(ToolError, match="greater than or equal to 1"):
            await mcp.call_tool("search", {"query": "x", "limit": 0})
        with pytest.raises(ToolError, match="greater than or equal to 0"):
            await mcp.call_tool("read", {"ref": "projects", "offset": -1})
        found = await mcp.call_tool("search", {"query": "offline", "limit": 2})
        assert isinstance(found, CallToolResult)
        assert json.loads(text(found)) == found.structured_content
        assert json.loads(text(found))["items"][0]["ref"] == "projects/offline.md"
        day = await mcp.call_tool("read", {"ref": "2026-08-31"})
        assert json.loads(text(day))["items"][0]["ref"] == "meetings:decision-1"
        home = await mcp.call_tool("read", {})
        assert json.loads(text(home))["pages"][0] == "projects"
        scoped = await mcp.call_tool("search", {"query": "offline", "scope": "memories/meetings"})
        assert json.loads(text(scoped))["items"][0]["ref"] == "meetings:decision-1"
        tag = "bf://fixture/tags/retention"
        for ref in ("tags", tag, "tasks", "bf://fixture/tasks"):
            result = await mcp.call_tool("read", {"ref": ref})
            assert json.loads(text(result)) == invoke("read", ref, "--brain", "fixture")
        tasks = invoke("read", "tasks", "--brain", "fixture")
        assert tasks["summary"] == {"open": 1, "done": 1, "notes": 1}
        assert "Resume the task." in invoke("read", tasks["items"][0]["ref"])["text"]
        result = await mcp.call_tool("search", {"query": "evidence", "scope": tag})
        assert json.loads(text(result)) == invoke("search", "evidence", "--scope", tag, "--brain", "fixture")
        # One contract with the CLI: a bf:// address, not a separate argument, chooses the brain.
        assert set(next(t for t in tools if t.name == "read").input_schema["properties"]) == {"ref", "rel", "offset"}
        role = await mcp.call_tool("read", {"ref": "projects/offline.md", "rel": "links"})
        assert json.loads(text(role)) == invoke("read", "projects/offline.md", "--rel", "links", "--brain", "fixture")
        assert json.loads(text(role))["items"][0]["ref"] == "meetings:decision-1"
        # Unknown arguments fail like unknown CLI options: 13's `brain`, or a misspelled scope, never widens a read.
        assert all(tool.input_schema["additionalProperties"] is False for tool in tools)
        for name, arguments in [
            ("read", {"ref": "", "brain": "fixture"}),
            ("search", {"query": "x", "scopes": "projects"}),
        ]:
            with pytest.raises(ToolError, match="Extra inputs are not permitted"):
                await mcp.call_tool(name, arguments)
        # Before 17 a mistyped value was coerced: a limit of true returned one result.
        for arguments in ({"query": "x", "limit": True}, {"query": "x", "limit": "5"}, {"query": 5}):
            with pytest.raises(ToolError, match="Input should be a valid"):
                await mcp.call_tool("search", arguments)
        exact = await mcp.call_tool("read", {"ref": "bf://fixture/projects/offline.md#decision"})
        assert json.loads(text(exact))["brain"] == "fixture"
        assert "Provider retention" in json.loads(text(exact))["text"]
        missing = await mcp.call_tool("read", {"ref": "projects/absent.md"})
        # Shared errors name these tools, not CLI commands.
        assert text(missing).startswith(
            "reference not found; use the search tool to locate it, or the read tool to browse pages; did you mean"
        )
        for name, arguments, message in [
            ("read", {"ref": "bf.yaml"}, "reference not found"),
            ("search", {"query": "   "}, "invalid input: query: "),
            # Query-level reasons read like the CLI's, without pydantic's `Value error` label.
            ("search", {"query": "!!!"}, "invalid input: query: give words or an identity to search"),
            ("search", {"query": "x", "scope": "0d"}, "invalid input: scope: invalid period: 0d"),
            ("search", {"query": "x", "scope": "soon"}, "invalid input: scope: scope accepts"),
            # Before 18 the reply named the model field behind the scope: prefix.
            ("search", {"query": "x", "scope": "projects/" + "a" * 5000}, "invalid input: scope: String should"),
            ("search", {"query": "x", "scope": "projects/offline.md#decision"}, "invalid input: scope: scope takes"),
            ("search", {"query": "bf://Me/x"}, "invalid input: query: invalid BF link"),
            # Before 18 these looked like failed operations, although the CLI exits 2 for each.
            ("read", {"ref": "2026-13"}, "invalid input: ref: invalid period: 2026-13"),
            ("read", {"ref": "projects/../bf.yaml"}, "invalid input: ref: expected a normalized"),
            ("read", {"ref": "bf://"}, "invalid input: ref: invalid BF link"),
            ("read", {"ref": "projects/", "rel": "links"}, "invalid input: rel: rel lists the links to a note"),
            ("read", {"ref": "projects/offline.md", "rel": "owner"}, "invalid input: rel: undeclared relation; use"),
        ]:
            failed = await mcp.call_tool(name, arguments)
            assert isinstance(failed, CallToolResult)
            assert failed.is_error
            assert text(failed).startswith(message), (arguments, text(failed))
            assert str(brain.root) not in text(failed)
        # Some clients send every number as a double: an integral one is the integer the schema allows.
        found = await mcp.call_tool("search", {"query": "offline", "limit": 1.0, "offset": 0.0})
        assert isinstance(found, CallToolResult)
        assert json.loads(text(found))["items"][0]["ref"] == "projects/offline.md"
        page = await mcp.call_tool("read", {"ref": "projects", "offset": 0.0})
        assert isinstance(page, CallToolResult)
        assert not page.is_error
        for arguments in ({"query": "x", "limit": 1.5}, {"query": "x", "offset": True}, {"query": "x", "limit": 1e300}):
            with pytest.raises(ToolError, match="Input should be"):
                await mcp.call_tool("search", arguments)

    asyncio.run(check())


def test_qualified_mcp_reads_keep_backlinks_from_referenced_brains(brain: Store, tmp_path: Path) -> None:
    (tmp_path / "team").mkdir()
    team = Store(tmp_path / "team")
    team.write("bf.yaml", b"version: 7\nname: team\n")
    team.write("projects/linker.md", b"# Linker\n\nSee [offline](bf://fixture/projects/offline.md).\n")
    brain.write("bf.yaml", f"version: 7\nname: fixture\nbrains:\n  team:\n    path: {team.root}\n".encode())
    expected = invoke("read", "bf://fixture/projects/offline.md", "--brain", str(brain.root))

    async def check() -> None:
        result = await server([brain]).call_tool("read", {"ref": "bf://fixture/projects/offline.md"})
        assert json.loads(text(result)) == expected

    asyncio.run(check())
    linked = {(item["brain"], item["ref"]) for group in expected["backlinks"] for item in group["items"]}
    assert ("team", "projects/linker.md") in linked


@pytest.mark.usefixtures("brain")
def test_mcp_stdio_handshake(tmp_path: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def check() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "bf", "mcp"],
            env={
                "HOME": os.environ["HOME"],
                "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
                "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
            },
            cwd=str(tmp_path),
        )
        async with (
            stdio_client(parameters, errlog=errlog) as (incoming, outgoing),
            ClientSession(incoming, outgoing, read_timeout_seconds=10) as session,
        ):
            initialized = await session.initialize()
            assert (initialized.server_info.name, initialized.server_info.title) == ("bf", "Brain Framework")
            assert initialized.instructions == INSTRUCTIONS
            assert initialized.server_info.version == distribution_version("brain-framework")
            listing = await session.list_tools()
            assert {tool.name for tool in listing.tools} == {"search", "read"}
            found = await session.call_tool("search", {"query": "offline"})
            assert not found.is_error
            assert json.loads(text(found)) == found.structured_content
            # The client validates each reply against the tool's outputSchema: an empty period page also has
            # the shape of a listing, which the published schema must accept.
            for arguments in ({}, {"ref": "7d"}, {"ref": "tasks"}, {"ref": "projects/offline.md"}):
                assert not (await session.call_tool("read", arguments)).is_error
            assert (await session.call_tool("search", {"query": "offline", "scop": "projects"})).is_error

    # Before 18.1.4 the SDK logged that rejected call on stderr: diagnostics are `bf:` lines, and replies hold errors.
    with (tmp_path / "stderr.log").open("w+") as errlog:
        asyncio.run(check())
        errlog.seek(0)
        assert errlog.read() == ""


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["stop", "interrupt"])
def test_mcp_stops_at_once_while_its_input_stays_open(brain: Store, signum: int) -> None:
    with subprocess.Popen(  # noqa: S603 - synthetic CLI boundary
        [sys.executable, "-m", "bf", "mcp", "--brain", str(brain.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as child:
        try:
            assert child.stdin is not None
            assert child.stdout is not None
            parameters = {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            }
            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": parameters}
            child.stdin.write(json.dumps(request).encode() + b"\n")
            child.stdin.flush()
            # Bounded, so a server that never answers fails this test instead of holding the whole run.
            assert readable([child.stdout], [], [], 10)[0], "no initialize reply within 10 seconds"
            assert json.loads(child.stdout.readline())["id"] == 1
            # Before 17 the server kept waiting for another input line, and a host's stop request timed out.
            child.send_signal(signum)
            assert child.wait(timeout=10) == 130
        finally:
            child.kill()


def test_an_unexpected_mcp_failure_reveals_nothing(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*_: object, **__: object) -> dict[str, object]:
        raise ValueError(f"unexpected value from {brain.root}")

    monkeypatch.setattr("bf.mcp.read", failing)
    # A bug, as in the CLI: the SDK reports a generic failure without the exception's text.
    with pytest.raises(UnexpectedToolError) as raised:
        asyncio.run(server([brain]).call_tool("read", {"ref": "projects/offline.md"}))
    assert str(brain.root) not in str(raised.value)


@pytest.mark.usefixtures("brain")
def test_mcp_resolves_the_selection_for_each_call(tmp_path: Path) -> None:
    # Validated at startup: a server that could not answer fails to start, like a CLI command.
    with pytest.raises(Error, match="brain directory does not exist"):
        server(lambda: select(str(tmp_path / "absent")))
    mcp = server(lambda: select())

    async def found(query: str) -> list[str]:
        result = await mcp.call_tool("search", {"query": query})
        assert isinstance(result, CallToolResult)
        return [item["ref"] for item in json.loads(text(result))["items"]]

    assert asyncio.run(found("offline"))[0] == "projects/offline.md"
    later = tmp_path / "later"
    invoke("init", str(later), "--name", "later")
    (later / "concepts/zephyr.md").write_text("# Zephyr\n\nA later brain.\n")
    invoke("register", str(later))
    # Before 18 a registered brain reached the server only after a restart.
    assert asyncio.run(found("zephyr")) == ["concepts/zephyr.md"]


def test_a_selection_that_fails_mid_session_reveals_no_path(
    brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each call resolves the selection again, so its errors reach the agent: they name the selection as given, never
    # the directory it resolved to, the registry file or the home directory.
    folder = Path.home() / "private-folder"
    folder.mkdir()
    link = Path.home() / "brain-link"
    link.symlink_to(brain.root)
    named, linked, registered = (server(lambda value=value: select(value)) for value in ("fixture", str(link), ""))
    registry = user_path()
    entries = registry.read_text()

    async def failed(mcp: MCPServer) -> str:
        result = await mcp.call_tool("search", {"query": "offline"})
        assert isinstance(result, CallToolResult)
        assert result.is_error
        assert str(tmp_path) not in text(result)
        return text(result)

    unconfigured = " has no bf.yaml; pass the brain's root directory, or create a brain with bf init"
    registry.write_text(entries.replace(str(brain.root), str(folder)))
    assert asyncio.run(failed(named)) == "registered brain fixture" + unconfigured
    registry.write_text("brains: [broken\n")
    assert asyncio.run(failed(registered)) == "<registry>: invalid YAML at line 2, column 1"
    registry.write_text(entries + "  Bad:\n    path: x\n")
    assert asyncio.run(failed(registered)).startswith("invalid <registry>: brains.Bad.[key]: ")
    link.unlink()
    link.symlink_to(folder)
    assert asyncio.run(failed(linked)) == "~/brain-link" + unconfigured
    # A bf.yaml another program planted in the working directory is named from there.
    (tmp_path / "checkout").mkdir()
    (tmp_path / "checkout/bf.yaml").symlink_to(brain.root / "bf.yaml")
    monkeypatch.chdir(tmp_path / "checkout")
    planted = "./bf.yaml is not a regular file owned by you; pass --brain PATH to select a brain"
    assert asyncio.run(failed(registered)) == planted
    # A home directory at the file system root hides nothing: replacing it would garble every path.
    monkeypatch.setenv("HOME", "/")
    assert asyncio.run(failed(registered)) == planted


def test_mcp_errors_hide_a_short_home_only_where_a_path_starts(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # Containers often set a short home, such as /root or /app: a note path that contains it keeps its name.
    monkeypatch.setenv("HOME", "/offline")
    result = asyncio.run(server([brain]).call_tool("read", {"ref": "projects/offline.md#missing"}))
    assert text(result).startswith("projects/offline.md: heading #missing does not exist; its sections: ")
