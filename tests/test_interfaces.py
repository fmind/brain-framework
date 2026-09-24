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

from bf.cli import app
from bf.config import user_config
from bf.mcp import server
from bf.storage import Store, writer


def invoke(*args: str, code: int = 0) -> dict:
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == code, result.output
    return json.loads(result.stdout) if result.stdout else {}


def test_cli_lifecycle(tmp_path: Path, brain: Store) -> None:
    assert CliRunner().invoke(app, ["--help"]).exit_code == 0
    target = tmp_path / "new"
    created = invoke("init", str(target), "--name", "fresh")
    assert created["collect"] is True
    assert user_config().brains["fresh"].path == str(target)
    assert CliRunner().invoke(app, ["init", str(target)]).exit_code != 0
    assert invoke("validate", "--brain", "fresh")["valid"]
    assert (target / "AGENTS.md").read_text().startswith("# Brain")
    found = invoke("search", "offline", "--brain", "fixture")
    assert found["items"][0]["ref"] == "projects/offline.md"
    everywhere = invoke("search", "welcome")
    assert {i["brain"] for i in everywhere["items"]} == {"fresh"}
    window = invoke("search", "--since", "2026-08-31", "--until", "2026-09-02", "--brain", "fixture")
    assert [i["ref"] for i in window["items"]] == ["projects/offline.md", "meetings:decision-1"]
    exact = invoke("read", "meetings:decision-1")
    assert exact["record"]["title"] == "Preserve durable evidence"
    assert invoke("build", "--brain", "fixture")["changed"] == 3
    status = invoke("status", "--brain", "fixture", "--check")
    assert status["healthy"]
    assert status["brains"][0]["sources"]["meetings"] == {
        "records": 2,
        "latest": "2026-08-31T12:00:00.000000Z",
        "configured": False,
        "state": "historical",
        "freshness": "unknown",
    }
    brain.write(
        "queries.yaml",
        b"version: 3\ncases:\n  - name: decision\n    query: retention decision\n    expect: [projects/offline.md]\n",
    )
    assert invoke("eval", "--brain", "fixture")["passed"]
    brain.write("queries.yaml", b"version: 3\ncases:\n  - name: missing\n    query: lunch\n    empty: true\n")
    failed = invoke("eval", "--brain", "fixture", code=1)
    assert failed["cases"][0]["returned"] == ["meetings:lunch"]
    other = tmp_path / "clone"
    other.mkdir()
    (other / "bf.yaml").write_text("version: 3\nname: clone\n")
    assert invoke("register", str(other))["collect"] is False
    assert invoke("update", "--brain", "clone", "--dry-run")["brains"][0]["skipped"]
    brain.write("projects/bad.md", b"# Bad [x](missing.md)\n")
    assert not invoke("validate", "--brain", "fixture", code=1)["valid"]
    assert invoke("schema")["title"] == "Config"


def test_status_check_fails_on_stale_trusted_sources(brain: Store) -> None:
    brain.write("bf.yaml", b"version: 3\nname: fixture\nsensors:\n  mail:\n    command: [echo]\n    refresh: 3600\n")
    report = invoke("status", "--brain", "fixture", "--check", code=1)
    assert report["brains"][0]["sources"]["mail"]["stale"] is True
    brain.write("bf.yaml", b"version: 3\nname: fixture\nsensors:\n  mail:\n    command: [sh, -c, exit 3]\n")
    assert invoke("collect", "mail", "--brain", "fixture", "--since", "2d", code=1) == {}
    entry = invoke("status", "--brain", "fixture", code=0)["brains"][0]["sources"]["mail"]
    assert "status 3" in entry["error"]
    assert entry["log"].endswith("mail.log")


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [("source_fish", "complete --command bf"), ("complete_fish", "search")],
)
def test_fish_completion_in_fresh_process(instruction: str, expected: str, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from bf.cli import app; app(prog_name='bf')"],
        cwd=tmp_path,
        env={
            **os.environ,
            "_BF_COMPLETE": instruction,
            "_TYPER_COMPLETE_ARGS": "bf sea",
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


def test_console_errors_are_private_and_on_stderr(brain: Store) -> None:
    def bf(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - synthetic CLI boundary
            [sys.executable, "-m", "bf", *args], capture_output=True, text=True, check=False
        )

    for args in [
        ["read", "bf.yaml", "--brain", "fixture"],
        ["search", "x", "--limit", "0"],
        ["search", "--since", "soon"],
    ]:
        result = bf(*args)
        assert result.returncode
        assert not result.stdout
        assert result.stderr.startswith("bf:")
        assert str(brain.root) not in result.stderr
    assert "limit" in bf("search", "x", "--limit", "0").stderr
    assert "a filter" in bf("search").stderr
    version = bf("--version")
    assert version.stdout.strip() == distribution_version("brain-framework")


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


def test_mcp_exposes_two_read_only_tools_with_cli_payloads(brain: Store) -> None:
    mcp = server([brain])

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
        exact = await mcp.call_tool("read", {"ref": "projects/offline.md#decision", "brain": "fixture"})
        assert json.loads(text(exact))["brain"] == "fixture"
        assert "Provider retention" in json.loads(text(exact))["text"]
        for name, arguments, message in [
            ("read", {"ref": "bf.yaml"}, "not found"),
            ("search", {"query": "x", "limit": 0}, "invalid"),
            ("search", {"since": "soon"}, "times accept"),
        ]:
            failed = await mcp.call_tool(name, arguments)
            assert isinstance(failed, CallToolResult)
            assert failed.is_error
            assert message in text(failed)
            assert str(brain.root) not in text(failed)

    asyncio.run(check())


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
            stdio_client(parameters) as (incoming, outgoing),
            ClientSession(incoming, outgoing, read_timeout_seconds=10) as session,
        ):
            initialized = await session.initialize()
            assert initialized.server_info.name == "bf"
            assert initialized.server_info.version == distribution_version("brain-framework")
            listing = await session.list_tools()
            assert {tool.name for tool in listing.tools} == {"search", "read"}
            found = await session.call_tool("search", {"query": "offline"})
            assert not found.is_error
            assert json.loads(text(found)) == found.structured_content

    asyncio.run(check())
