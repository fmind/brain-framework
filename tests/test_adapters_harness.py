"""The harness wrapper adds arguments only to runs answering a message, and otherwise runs the harness unchanged."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, Provider

ADAPTER = "harness.py"
FOLDER = "prompts/routines"
BASE = ["--print", "--permission-mode", "dontAsk"]
WIDE = ["--input-arg", "--allowedTools=Edit(projects/**)", "--input-arg", "--add-dir=drafts"]


def wrap(provider: Provider, *arguments: str, trigger: str | None = None, stdin: str | None = None):
    environment = {} if trigger is None else {"BF_TRIGGER": trigger}
    return provider.run(ADAPTER, *arguments, folder=FOLDER, stdin=stdin, environment=environment)


def test_a_run_answering_a_message_gets_the_input_arguments_at_the_end(provider: Provider) -> None:
    provider.install("agent", [{"match": [], "stdout": "answer"}])
    result = wrap(provider, *WIDE, "--", "agent", *BASE, trigger="input")
    assert (result.returncode, result.stdout, result.stderr) == (0, "answer", "")
    assert provider.calls("agent") == [[*BASE, "--allowedTools=Edit(projects/**)", "--add-dir=drafts"]]


@pytest.mark.parametrize("trigger", ["schedule", "wake", "manual", None, "", "INPUT", "input ", "inputs", "junk"])
def test_every_other_run_keeps_the_harness_arguments_unchanged(provider: Provider, trigger: str | None) -> None:
    # Only the exact value BF sets for a message adds anything: a missing or unknown one fails closed.
    provider.install("agent", [{"match": [], "stdout": "answer"}])
    result = wrap(provider, *WIDE, "--", "agent", *BASE, trigger=trigger)
    assert (result.returncode, result.stdout) == (0, "answer"), result.stderr
    assert provider.calls("agent") == [BASE]


def test_arguments_reach_the_harness_verbatim_without_a_shell(provider: Provider, tmp_path: Path) -> None:
    provider.install("agent", [{"match": [], "stdout": ""}])
    hostile = ["$(touch pwned)", "; touch pwned", "`touch pwned`", "*", "--", ""]
    result = subprocess.run(  # noqa: S603 - the wrapper's own process boundary, with fake executables only
        [
            sys.executable,
            str(ROOT / "examples" / FOLDER / ADAPTER),
            "--input-arg",
            "|touch pwned",
            "--",
            "agent",
            *hostile,
        ],
        cwd=tmp_path,
        env={"PATH": f"{provider.bin}:/usr/bin:/bin", "BF_TRIGGER": "input", "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert provider.calls("agent") == [[*hostile, "|touch pwned"]]
    assert not (tmp_path / "pwned").exists()


def test_the_harness_takes_the_wrappers_place_with_its_input_and_exit_code(tmp_path: Path) -> None:
    # One process: BF's timeout and process-group kill reach the harness itself, and the prompt is never consumed.
    folder = tmp_path / "bin"
    folder.mkdir()
    agent = folder / "agent"
    agent.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "print(json.dumps({'pid': os.getpid(), 'stdin': sys.stdin.read(), 'argv': sys.argv[1:]}))\nsys.exit(3)\n"
    )
    agent.chmod(0o700)
    child = subprocess.Popen(  # noqa: S603 - the wrapper's own process boundary, with a fake executable only
        [sys.executable, str(ROOT / "examples" / FOLDER / ADAPTER), "--", "agent", "-p"],
        env={"PATH": f"{folder}:/usr/bin:/bin", "BF_TRIGGER": "schedule", "LANG": "C.UTF-8"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = child.communicate("The expanded prompt.", timeout=60)
    assert (child.returncode, stderr) == (3, "")
    assert json.loads(stdout) == {"pid": child.pid, "stdin": "The expanded prompt.", "argv": ["-p"]}


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["agent"],
        ["--input-arg", "--secret-token=hunter2", "agent"],
        ["--input-arg"],
        ["--"],
        ["--", ""],
        ["--input-arg", "x"],
        ["--unknown=hunter2", "--", "agent"],
        ["--input-arg=hunter2", "--", "agent"],
    ],
)
def test_invalid_arguments_exit_2_without_quoting_them(provider: Provider, arguments: list[str]) -> None:
    provider.install("agent", [{"match": [], "stdout": ""}])
    result = wrap(provider, *arguments, trigger="input")
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("harness: "), result.stderr
    assert result.stderr.count("\n") == 1, result.stderr
    assert "hunter2" not in result.stderr
    assert provider.calls("agent") == []


def test_a_missing_harness_exits_1_without_quoting_its_arguments(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - the wrapper's own process boundary; nothing on PATH is real
        [sys.executable, str(ROOT / "examples" / FOLDER / ADAPTER), "--", "missing-agent", "--token=hunter2"],
        env={"PATH": str(tmp_path), "BF_TRIGGER": "input", "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "harness: cannot start the harness command (No such file or directory); check its name and PATH\n"
    )
