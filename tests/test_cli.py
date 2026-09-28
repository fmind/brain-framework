"""CLI discovery and usage errors work before a brain can be selected."""

from __future__ import annotations

import subprocess
import sys

import pytest
from typer.testing import CliRunner

from bf.cli import app
from bf.config import user_path
from conftest import plain


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize(
    "command",
    [
        "",
        "build",
        "collect",
        "eval",
        "init",
        "mcp",
        "read",
        "register",
        "schedule",
        "schema",
        "search",
        "status",
        "update",
        "validate",
        "watch",
    ],
)
def test_help_needs_no_valid_configuration(command: str, flag: str) -> None:
    path = user_path()
    path.parent.mkdir(parents=True)
    path.write_text("brains: [broken\n")
    result = CliRunner().invoke(app, [*([command] if command else []), flag])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.stdout
    assert not result.stderr
    assert path.read_text() == "brains: [broken\n"


@pytest.mark.parametrize(
    ("arguments", "option"),
    [
        (["search", "word", "--limit", "0"], "--limit"),
        (["search", "word", "--scope", "soon"], "--scope"),
        (["collect", "source", "--since", "soon"], "--since"),
        (["collect", "source", "--since", "2026-09-02", "--until", "2026-09-01"], "--since"),
        (["schedule", "--backend", "unknown"], "--backend"),
        (["schedule", "--every", "7"], "--every"),
        (["schedule", "--name", "INVALID"], "--name"),
        (["watch", "--notify", "unknown"], "--notify"),
        (["init", "fresh", "--name", "INVALID"], "--name"),
    ],
)
def test_invalid_options_name_the_option_without_a_selected_brain(arguments: list[str], option: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and parametrized synthetic CLI arguments
        [sys.executable, "-m", "bf", *arguments], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 2, result.stderr
    assert not result.stdout
    assert option in plain(result.stderr)
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("command", ["build", "eval", "validate"])
def test_single_root_commands_describe_their_selection(command: str) -> None:
    # These commands check one root: their help never promises every registered brain.
    help_text = " ".join(plain(CliRunner().invoke(app, [command, "--help"]).stdout).replace("│", " ").split())
    assert "the only registered brain present" in help_text
    assert "every registered brain" not in help_text


def test_root_help_explains_first_use_and_output() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    help_text = plain(result.stdout)
    assert "bf init ~/brain" in help_text
    assert "JSON" in help_text
    assert "bf COMMAND --help" in help_text
