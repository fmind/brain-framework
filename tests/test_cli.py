"""CLI discovery, usage errors and standard streams work before a brain can be selected."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import cast

import pytest
from typer.core import TyperGroup
from typer.main import get_command
from typer.testing import CliRunner

from bf.cli import app, main
from bf.config import user_path
from bf.models import Error
from bf.storage import Store
from conftest import plain

COMMANDS = sorted(cast("TyperGroup", get_command(app)).commands)
# CI forces Typer and Rich colors; Rich disables them for TERM=dumb, so choose the terminal these cases exercise.
FORCED = {"TERM": "xterm-256color", "GITHUB_ACTIONS": "true", "FORCE_COLOR": "1"}


def bf(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and synthetic CLI arguments
        [sys.executable, "-m", "bf", *arguments],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("command", ["", *COMMANDS])
def test_help_needs_no_valid_configuration(command: str, flag: str) -> None:
    path = user_path()
    path.parent.mkdir(parents=True)
    path.write_text("brains: [broken\n")
    result = CliRunner().invoke(app, [*([command] if command else []), flag])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.stdout
    assert not result.stderr
    assert path.read_text() == "brains: [broken\n"


def test_a_bare_bf_and_its_version_need_no_brain() -> None:
    bare = CliRunner().invoke(app, [])
    assert (bare.exit_code, bare.stderr) == (2, "")
    assert "Usage:" in bare.stdout
    version = CliRunner().invoke(app, ["--version"])
    assert (version.exit_code, version.stdout) == (0, distribution_version("brain-framework") + "\n")


@pytest.mark.parametrize("arguments", [["review", "-h"], ["review", "x", "--help"], ["--hook", "pre-push", "-h"]])
def test_routine_help_never_runs_a_routine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    # `run` passes its other arguments to the routine: help must still stop before any routine starts.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    (tmp_path / "brain").mkdir()
    store = Store(tmp_path / "brain")
    marker = tmp_path / "ran"
    store.write(
        "bf.yaml",
        b"version: 7\nname: fixture\nroutines:\n  review:\n    command: [routines/review.sh]\n    hooks: [pre-push]\n",
    )
    store.write("routines/review.sh", f"#!/bin/sh\ntouch '{marker}'\n".encode())
    (store.root / "routines/review.sh").chmod(0o700)
    result = CliRunner().invoke(app, ["run", *arguments, "--brain", str(store.root)])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.stdout
    assert not marker.exists()
    # The same routine without help runs: the marker proves the case above could have failed.
    assert CliRunner().invoke(app, ["run", "review", "--brain", str(store.root)]).exit_code == 0
    assert marker.exists()


@pytest.mark.parametrize(
    ("arguments", "option"),
    [
        (["search", "word", "--limit", "0"], "--limit"),
        (["search", "word", "--scope", "soon"], "--scope"),
        # Search results carry section refs; as a scope, one would silently match nothing.
        (["search", "word", "--scope", "projects/plan.md#next"], "--scope"),
        (["collect", "source", "--since", "soon"], "--since"),
        (["collect", "source", "--since", "2026-09-02", "--until", "2026-09-01"], "--since"),
        (["schedule", "--backend", "unknown"], "--backend"),
        (["schedule", "--every", "7"], "--every"),
        (["schedule", "--name", "INVALID"], "--name"),
        (["watch", "--notify", "unknown"], "--notify"),
        # A NaN compares false with both bounds of a range.
        (["watch", "--poll-interval", "nan"], "--poll-interval"),
        (["init", "fresh", "--name", "INVALID"], "--name"),
        # Before 17 a mistyped option reached the routine as an argument, and the routine ran for real.
        (["run", "review", "--dryrun"], "--dryrun"),
        (["read", "projects", "--rel", "related-to"], "--rel"),
        # Before 18 a trailing slash or a section turned these into failed reads.
        (["read", "projects/", "--rel", "links"], "--rel"),
        (["read", "tags/retention/", "--rel", "links"], "--rel"),
        (["read", "projects/plan.md#next", "--rel", "links"], "--rel"),
        (["read", "--offset"], "--offset"),
        # Before 18 an empty value, as an unset variable gives, silently chose another brain or the working
        # directory.
        (["search", "word", "--brain", ""], "--brain"),
        (["update", "--brain", " "], "--brain"),
        (["mcp", "--brain="], "--brain"),
        (["init", ""], "PATH"),
        (["register", ""], "PATH"),
        (["skills", ""], "DIR"),
        (["schedule", "--output", ""], "--output"),
        # Before 18.1.4 these rebuilt the whole cache, collected the default window or named an unknown program "".
        (["build", "--reproject", ""], "--reproject"),
        (["collect", "calendar", "--since", ""], "--since"),
        (["collect", ""], "SENSOR"),
        (["run", ""], "ROUTINE"),
        (["update", "--sensor", "calendar", "--sensor", ""], "--sensor"),
        (["schedule", "--routine", " "], "--routine"),
        # A suite path is relative to the brain, never the working directory: it keeps its own reason.
        (["eval", "--path", ""], "--path: expected a normalized brain-relative path"),
        # Python passes undecodable argument bytes, such as a Latin-1 é, as lone surrogates. Before 18 the read
        # failed binding them into the cache and blamed a brain file; the same bytes as a QUERY were invalid input.
        (["read", "caf\udce9"], "REF: expected UTF-8 text"),
    ],
)
def test_invalid_options_name_the_option_without_a_selected_brain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arguments: list[str], option: str
) -> None:
    # The console entry point in-process; forced colors in a fresh process are tested below.
    monkeypatch.setattr(sys, "argv", ["bf", *arguments])
    with pytest.raises(SystemExit) as exited:
        main()
    out, err = capsys.readouterr()
    assert (exited.value.code, out) == (2, ""), err
    # One plain line, like every other diagnostic: no usage text or wrapped box.
    assert err.startswith("bf: invalid input: ")
    assert err.endswith(f" (see bf {arguments[0]} -h)\n")
    assert err.count("\n") == 1
    assert option in err


def test_forced_colors_keep_help_panels_and_plain_usage_errors() -> None:
    helped = bf("read", "--help", env=FORCED)
    assert helped.returncode == 0
    assert "\x1b[" in helped.stdout, "the environment no longer forces colors, so this test checks nothing"
    assert "Usage:" in plain(helped.stdout)
    bare = bf(env=FORCED)
    assert (bare.returncode, bare.stderr) == (2, "")
    assert "search" in plain(bare.stdout)
    # Before 18 invalid input printed Typer's usage text and a colored box, wrapped at 80 columns.
    for arguments in (["read", "--offset"], ["search", "word", "--brain", ""], ["read", "projects/", "--rel", "links"]):
        result = bf(*arguments, env=FORCED)
        assert (result.returncode, result.stdout) == (2, "")
        assert result.stderr.startswith("bf: invalid input: ")
        assert result.stderr.count("\n") == 1
        assert "\x1b" not in result.stderr


def test_a_cancelled_command_exits_130_without_a_message(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def cancelled(*_: object) -> dict[str, object]:
        raise KeyboardInterrupt

    # A real brain: the cancellation, not the selection, must decide the exit.
    monkeypatch.setattr("bf.cli.health.report", cancelled)
    result = CliRunner().invoke(app, ["status", "--brain", str(brain.root)])
    assert (result.exit_code, result.output) == (130, "")


@pytest.mark.parametrize(
    ("arguments", "option"),
    [
        (["search", "word", "--scope", "projects", "--scope", "concepts"], "--scope"),
        (["search", "word", "--brain", "a", "--limit", "3", "--brain=b"], "--brain"),
        (["read", "projects/a.md", "--rel", "cites", "--rel", "links"], "--rel"),
        (["run", "--hook", "pre-push", "--hook", "post-merge"], "--hook"),
        (["update", "--brain", "a", "--brain", "b"], "--brain"),
    ],
)
def test_a_repeated_single_value_option_is_invalid_instead_of_keeping_the_last(
    arguments: list[str], option: str
) -> None:
    # Before 17 Click kept the last value: a second scope or brain silently narrowed or redirected the answer.
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 2, result.output
    assert not result.stdout
    assert f"{option}: give it once" in result.stderr


def test_repeated_flags_and_multiple_options_stay_valid(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["init", str(tmp_path / "brain")])
    assert result.exit_code == 0, result.output
    brain = str(tmp_path / "brain")
    assert CliRunner().invoke(app, ["update", "--brain", brain, "--dry-run", "--dry-run"]).exit_code == 0
    # --sensor selects several programs by repetition; an unknown one is a failure, not a usage error.
    result = CliRunner().invoke(app, ["update", "--brain", brain, "--sensor", "a", "--sensor", "b"])
    assert result.exit_code == 1, result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["init", "", "-h"],
        ["skills", "", "--help"],
        ["schedule", "--output", "", "-h"],
        ["search", "x", "--brain", "", "-h"],
        ["search", "-h", "x", "--brain", ""],
        ["search", "x", "--scope", "a", "--scope", "b", "-h"],
    ],
)
def test_help_answers_despite_empty_or_repeated_values(arguments: list[str]) -> None:
    # As it does despite a value Click checks, such as --limit 0: an unset variable on the line never hides help.
    result = CliRunner().invoke(app, arguments)
    assert (result.exit_code, result.stderr) == (0, ""), result.output
    assert "Usage:" in result.stdout


@pytest.mark.parametrize(
    "arguments",
    [
        ["search", "word", "--brain", "~bf-no-such-user/brain"],
        ["init", "~bf-no-such-user/brain"],
        ["register", "~bf-no-such-user/brain"],
        ["skills", "~bf-no-such-user/skills"],
    ],
)
def test_an_unknown_home_directory_fails_without_a_traceback(arguments: list[str]) -> None:
    result = bf(*arguments)
    assert result.returncode == 1, result.stderr
    assert not result.stdout
    assert result.stderr == "bf: a path's ~ or ~user home directory cannot be resolved; check the user name\n"


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
    assert {"run", "skills"} <= set(COMMANDS)


@pytest.mark.parametrize(
    ("command", "argument"),
    [("search", "QUERY"), ("read", "REF"), ("collect", "SENSOR"), ("init", "PATH"), ("skills", "DIR")],
)
def test_help_names_arguments_as_the_reference_does(command: str, argument: str) -> None:
    # The usage line, its errors and the command reference name each argument alike.
    usage = plain(CliRunner().invoke(app, [command, "--help"]).stdout).split("Usage:")[1].split("\n")[0]
    assert argument in usage


def test_a_closed_standard_output_fails_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert CliRunner().invoke(app, ["init", str(tmp_path / "brain")]).exit_code == 0
    message = "bf: standard output is closed; redirect it to a file or /dev/null\n"
    # Python sets sys.stdout to None when descriptor 1 is closed: before 18 the build ran, then a traceback.
    result = subprocess.run(  # noqa: S603 - fixed shell and interpreter with synthetic CLI arguments
        [
            "/bin/sh",
            "-c",
            'exec "$@" >&-',
            "sh",
            sys.executable,
            "-m",
            "bf",
            "build",
            "--brain",
            str(tmp_path / "brain"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (result.returncode, result.stderr) == (1, message)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "argv", ["bf", "build", "--brain", str(tmp_path / "brain")])
    with pytest.raises(SystemExit) as exited:
        main()
    assert (exited.value.code, capsys.readouterr().err) == (1, message)
    assert not (tmp_path / "brain/.bf").exists()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["mcp"], "standard input is closed; an MCP host sends its requests through it"),
        # The dashboards read keys from it.
        (["watch"], "watch needs an interactive terminal; use bf watch --json or bf status"),
        (["status", "--watch"], "status --watch needs an interactive terminal; use bf status for a JSON snapshot"),
    ],
    ids=["mcp", "watch", "status-watch"],
)
def test_a_closed_standard_input_fails_with_one_line(brain: Store, arguments: list[str], message: str) -> None:
    # No request or key could arrive: before 18 each failed with a traceback.
    result = subprocess.run(  # noqa: S603 - fixed shell and interpreter with synthetic CLI arguments
        ["/bin/sh", "-c", 'exec "$@" <&-', "sh", sys.executable, "-m", "bf", *arguments, "--brain", str(brain.root)],
        capture_output=True,
        text=True,
        # The fixture declares no program: none could run.
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        timeout=30,
        check=False,
    )
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == f"bf: {message}\n"


def test_a_terminal_encoding_that_cannot_show_help_names_the_encoding() -> None:
    # Help holds non-ASCII text, such as the 🧠 of the root help: before 18 this failure blamed a brain file.
    result = bf("-h", env={"PYTHONIOENCODING": "ascii"})
    assert result.returncode == 1
    assert result.stderr == (
        "bf: an argument or the terminal encoding is not UTF-8; use UTF-8 arguments and a UTF-8 locale\n"
    )


@pytest.mark.parametrize("arguments", [["schema"], ["-h"], []], ids=["reply", "help", "bare"])
def test_a_reply_that_standard_output_refuses_fails_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    # A descriptor that refuses writes, like a full disk: before 18 Python's exit flush failed again, printed its own
    # warning and exited 120, and the message blamed the brain. Help is written by Typer, not as a reply.
    stdout = io.TextIOWrapper(io.BufferedWriter(io.FileIO(os.open(os.devnull, os.O_RDONLY), "w")))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "argv", ["bf", *arguments])
    with pytest.raises(SystemExit) as exited:
        main()
    # The failure discarded the pending reply: flushing it again cannot fail.
    stdout.close()
    assert exited.value.code == 1
    assert capsys.readouterr().err.startswith("bf: cannot write the reply to standard output (")


def test_a_failure_discards_a_partial_reply_a_closed_pipe_cannot_take(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reader, writer = os.pipe()
    os.close(reader)
    stdout = io.TextIOWrapper(io.BufferedWriter(io.FileIO(writer, "w")))

    def failing(*_: object, **__: object) -> dict[str, object]:
        stdout.write("partial reply")
        raise Error("reference not found")

    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr("bf.cli.read", failing)
    monkeypatch.setattr(sys, "argv", ["bf", "read", "projects/offline.md", "--brain", str(brain.root)])
    with pytest.raises(SystemExit) as exited:
        main()
    stdout.close()
    assert (exited.value.code, capsys.readouterr().err) == (1, "bf: reference not found\n")


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="/dev/full, which fails every write, is Linux-only")
@pytest.mark.parametrize(
    "arguments",
    [
        ["schema"],
        ["--version"],
        ["export", "--brain", "fixture"],
        # Typer writes help, and the watch stream its own lines: before 18 their failures blamed the brain.
        ["-h"],
        [],
        ["watch", "--json", "--brain", "fixture"],
    ],
)
@pytest.mark.usefixtures("brain")
def test_a_full_standard_output_exits_1_without_a_flush_warning(arguments: list[str]) -> None:
    # The same at a real interpreter exit, which flushes standard output once more.
    with Path("/dev/full").open("wb") as full:
        result = subprocess.run(  # noqa: S603 - fixed interpreter and synthetic CLI arguments
            [sys.executable, "-m", "bf", *arguments],
            stdout=full,
            stderr=subprocess.PIPE,
            # The fixture declares no program: watch has none to run.
            env={**os.environ, "PATH": "/usr/bin:/bin"},
            timeout=30,
            check=False,
        )
    assert (result.returncode, result.stderr) == (
        1,
        b"bf: cannot write the reply to standard output (No space left on device)\n",
    )


def test_a_hook_that_no_routine_lists_never_waits_for_input(brain: Store) -> None:
    # An agent's shell can hold its input open; before 18 every hook waited 10 seconds for it, then failed.
    with subprocess.Popen(  # noqa: S603 - fixed interpreter and synthetic CLI arguments
        [sys.executable, "-m", "bf", "run", "--hook", "pre-push", "--brain", str(brain.root), "--", "origin", "x"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    ) as process:
        # Its input stays open until it exits: waiting for it would fail after 10 seconds.
        assert process.wait(timeout=60) == 0
        assert process.stdout is not None
        assert json.loads(process.stdout.read()) == {"ok": True, "dry_run": False, "hook": "pre-push", "routines": []}


@pytest.mark.parametrize(
    ("directory", "name"),
    [
        ("Équipe produit", "equipe-produit"),
        ("cerveau-élève", "cerveau-eleve"),
        ("Été", "ete"),
        ("Team--Brain_", "team-brain"),
        ("ﬁnance²", "finance2"),
    ],
)
def test_default_brain_names_fold_accents(tmp_path: Path, directory: str, name: str) -> None:
    result = CliRunner().invoke(app, ["init", str(tmp_path / directory)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["brain"] == name


@pytest.mark.parametrize("directory", ["Øre", "日本", "2026"])
def test_a_directory_name_without_a_plain_form_asks_for_a_name(tmp_path: Path, directory: str) -> None:
    # Before 18 a letter without a plain form was dropped: "Øre" became "re", a permanent link namespace.
    result = CliRunner().invoke(app, ["init", str(tmp_path / directory)])
    assert result.exit_code == 2
    assert "pass --name NAME" in result.stderr
    assert not (tmp_path / directory).exists()
