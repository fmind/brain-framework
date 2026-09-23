"""The installed-facing command and MCP contracts share one service layer."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent
from typer.testing import CliRunner

from fkf.cli import app
from fkf.config import user_config
from fkf.mcp import server
from fkf.storage import Store, writer


def invoke(*args: str, code: int = 0) -> dict:
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == code, result.output
    return json.loads(result.stdout) if result.stdout else {}


def test_cli_lifecycle(tmp_path: Path, base: Store) -> None:
    assert CliRunner().invoke(app, ["--help"]).exit_code == 0
    target = tmp_path / "new"
    created = invoke("init", str(target), "--name", "fresh")
    assert created["collect"] is True
    assert user_config().bases["fresh"].path == str(target)
    assert CliRunner().invoke(app, ["init", str(target)]).exit_code != 0
    assert invoke("validate", "--base", "fresh")["valid"]
    assert (target / "AGENTS.md").read_text().startswith("# Knowledge base")
    found = invoke("search", "offline", "--base", "fixture")
    assert found["items"][0]["ref"] == "projects/offline.md"
    everywhere = invoke("search", "welcome")
    assert {i["base"] for i in everywhere["items"]} == {"fresh"}
    window = invoke("search", "--since", "2026-08-31", "--until", "2026-09-02", "--base", "fixture")
    assert [i["ref"] for i in window["items"]] == ["projects/offline.md", "meetings:decision-1"]
    exact = invoke("read", "meetings:decision-1")
    assert exact["record"]["title"] == "Preserve durable evidence"
    assert invoke("build", "--base", "fixture")["changed"] == 3
    status = invoke("status", "--base", "fixture", "--check")
    assert status["healthy"]
    assert status["bases"][0]["sources"]["meetings"] == {
        "records": 2,
        "latest": "2026-08-31T12:00:00.000000Z",
        "configured": False,
    }
    base.write(
        "queries.yaml",
        b"version: 2\ncases:\n  - name: decision\n    query: retention decision\n    expect: [projects/offline.md]\n",
    )
    assert invoke("eval", "--base", "fixture")["passed"]
    base.write("queries.yaml", b"version: 2\ncases:\n  - name: missing\n    query: lunch\n    empty: true\n")
    failed = invoke("eval", "--base", "fixture", code=1)
    assert failed["cases"][0]["returned"] == ["meetings:lunch"]
    other = tmp_path / "clone"
    other.mkdir()
    (other / "fkf.yaml").write_text("version: 2\nname: clone\n")
    assert invoke("register", str(other))["collect"] is False
    assert invoke("update", "--base", "clone", "--dry-run")["bases"][0]["skipped"]
    base.write("projects/bad.md", b"# Bad [x](missing.md)\n")
    assert not invoke("validate", "--base", "fixture", code=1)["valid"]
    assert invoke("schema")["title"] == "Config"


def test_status_check_fails_on_stale_trusted_sources(base: Store) -> None:
    base.write("fkf.yaml", b"version: 2\nname: fixture\nsources:\n  mail:\n    command: [echo]\n    refresh: 3600\n")
    report = invoke("status", "--base", "fixture", "--check", code=1)
    assert report["bases"][0]["sources"]["mail"]["stale"] is True
    base.write("fkf.yaml", b"version: 2\nname: fixture\nsources:\n  mail:\n    command: [sh, -c, exit 3]\n")
    assert invoke("collect", "mail", "--base", "fixture", "--since", "2d", code=1) == {}
    entry = invoke("status", "--base", "fixture", code=0)["bases"][0]["sources"]["mail"]
    assert "status 3" in entry["error"]
    assert entry["log"].endswith("mail.log")


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [("source_fish", "complete --command fkf"), ("complete_fish", "search")],
)
def test_fish_completion_in_fresh_process(instruction: str, expected: str, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from fkf.cli import app; app(prog_name='fkf')"],
        cwd=tmp_path,
        env={
            **os.environ,
            "_FKF_COMPLETE": instruction,
            "_TYPER_COMPLETE_ARGS": "fkf sea",
            "_TYPER_COMPLETE_FISH_ACTION": "get-args",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert not result.stderr


def test_console_errors_are_private_and_on_stderr(base: Store) -> None:
    def fkf(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - synthetic CLI boundary
            [sys.executable, "-m", "fkf", *args], capture_output=True, text=True, check=False
        )

    for args in [
        ["read", "fkf.yaml", "--base", "fixture"],
        ["search", "x", "--limit", "0"],
        ["search", "--since", "soon"],
    ]:
        result = fkf(*args)
        assert result.returncode
        assert not result.stdout
        assert result.stderr.startswith("fkf:")
        assert str(base.root) not in result.stderr
    assert "limit" in fkf("search", "x", "--limit", "0").stderr
    assert "a filter" in fkf("search").stderr
    version = fkf("--version")
    assert version.stdout.strip() == distribution_version("fkf")


def test_initialization_obeys_the_physical_writer_lock(tmp_path: Path) -> None:
    target = tmp_path / "empty"
    target.mkdir()
    with writer(Store(target)):
        result = CliRunner().invoke(app, ["init", str(target)])
    assert result.exit_code != 0
    assert not list(target.iterdir())


def text(result: object) -> str:
    assert isinstance(result, CallToolResult)
    assert isinstance(result.content[0], TextContent)
    return result.content[0].text


def test_mcp_exposes_two_read_only_tools_with_cli_payloads(base: Store) -> None:
    mcp = server([base])

    async def check() -> None:
        tools = await mcp.list_tools()
        assert {t.name for t in tools} == {"search", "read"}
        for tool in tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint
            assert not tool.annotations.destructive_hint
        found = await mcp.call_tool("search", {"query": "offline", "limit": 2})
        assert isinstance(found, CallToolResult)
        assert json.loads(text(found)) == found.structured_content
        assert json.loads(text(found))["items"][0]["ref"] == "projects/offline.md"
        window = await mcp.call_tool("search", {"since": "2026-08-31", "until": "2026-09-01"})
        assert json.loads(text(window))["items"][0]["ref"] == "meetings:decision-1"
        exact = await mcp.call_tool("read", {"ref": "projects/offline.md#decision"})
        assert "Provider retention" in json.loads(text(exact))["text"]
        for name, arguments, message in [
            ("read", {"ref": "fkf.yaml"}, "not found"),
            ("search", {"query": "x", "limit": 0}, "invalid"),
            ("search", {"since": "soon"}, "times accept"),
        ]:
            failed = await mcp.call_tool(name, arguments)
            assert isinstance(failed, CallToolResult)
            assert failed.is_error
            assert message in text(failed)
            assert str(base.root) not in text(failed)

    asyncio.run(check())


@pytest.mark.usefixtures("base")
def test_mcp_stdio_handshake(tmp_path: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def check() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "fkf", "mcp"],
            env={
                "HOME": os.environ["HOME"],
                "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
                "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
            },
            cwd=str(tmp_path),
        )
        async with (
            stdio_client(parameters) as (incoming, outgoing),
            ClientSession(incoming, outgoing, read_timeout_seconds=10) as session,
        ):
            initialized = await session.initialize()
            assert initialized.server_info.version == distribution_version("fkf")
            listing = await session.list_tools()
            assert {tool.name for tool in listing.tools} == {"search", "read"}
            found = await session.call_tool("search", {"query": "offline"})
            assert not found.is_error
            assert json.loads(text(found)) == found.structured_content

    asyncio.run(check())
