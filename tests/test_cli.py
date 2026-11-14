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
        timeout=120,
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
        b"version: 11\nname: fixture\njobs:\n  review:\n    run: [routines/review.sh]\n    on: { hook: pre-push }\n",
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


def test_routine_arguments_that_are_not_utf8_reach_the_routine_as_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the program name must be text: refusing other bytes, such as a hook's Git arguments, could block a push.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    (tmp_path / "brain").mkdir()
    store = Store(tmp_path / "brain")
    received = tmp_path / "received"
    store.write("bf.yaml", b"version: 11\nname: fixture\njobs:\n  review:\n    run: [routines/review.sh]\n")
    store.write("routines/review.sh", f"#!/bin/sh\nprintf %s \"$1\" > '{received}'\n".encode())
    (store.root / "routines/review.sh").chmod(0o700)
    # Python passes an undecodable argument byte, such as a Latin-1 é, as a lone surrogate.
    result = CliRunner().invoke(app, ["run", "review", "caf\udce9", "--brain", str(store.root)])
    assert result.exit_code == 0, result.output
    assert received.read_bytes() == b"caf\xe9"


@pytest.mark.parametrize(
    ("arguments", "option"),
    [
        (["search", "word", "--limit", "0"], "--limit"),
        (["search", "word", "--scope", "soon"], "--scope"),
        # Search results carry section refs; as a scope, one would silently match nothing.
        (["search", "word", "--scope", "projects/plan.md#next"], "--scope"),
        # Before 20 a section's address scoped only the links naming it, and a source's fragment named no source.
        (["search", "word", "--scope", "bf://fixture/projects/plan.md#next"], "--scope"),
        (["search", "word", "--scope", "memories/mail#next"], "--scope"),
        # Before 20 an empty fragment read, and scoped, the whole note under another spelling.
        (["search", "word", "--scope", "bf://fixture/projects/plan.md#"], "--scope"),
        (["read", "bf://fixture/projects/plan.md#"], "REF"),
        (["collect", "source", "--since", "soon"], "--since"),
        (["collect", "source", "--since", "2026-09-02", "--until", "2026-09-01"], "--since"),
        (["watch", "--notify", "unknown"], "--notify"),
        # A NaN compares false with both bounds of a range.
        (["watch", "--poll-interval", "nan"], "--poll-interval"),
        (["init", "fresh", "--name", "INVALID"], "--name"),
        # Before 20 an empty name, as an unset variable gives, silently derived one from the directory.
        (["init", "fresh", "--name", ""], "--name"),
        (["init", "fresh", "--name", " "], "--name"),
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
        (["unregister", ""], "TARGET"),
        (["guide", ""], "TOPIC"),
        (["skill", "install", "--brain", ""], "--brain"),
        # Before 18.1.4 these rebuilt the whole cache, collected the default window or named an unknown program "".
        (["build", "--reproject", ""], "--reproject"),
        (["collect", "calendar", "--since", ""], "--since"),
        (["post", "hi", "--to", ""], "--to: give a send job name; an empty value names none"),
        (["wake", "--job", ""], "--job: give a job name; an empty value names none"),
        (["wake", "--in", ""], "--in: give a delay such as 10m or 600; an empty value names none"),
        (["wake", "--at", ""], "--at: give a timestamp; an empty value names none"),
        (["run", "--hook", ""], "--hook: give an event name; an empty value would run the first argument as a job"),
        (["collect", ""], "JOB"),
        (["ingest", ""], "SOURCE"),
        # An empty --input, as an unset variable gives, used to run the collector and save records instead of checking.
        (["collect", "calendar", "--input", ""], "--input"),
        (["action", "status", "rotate", "unknown"], "STATUS: 'unknown' is not one of 'draft', 'stable', 'deprecated'"),
        (["run", ""], "JOB"),
        (["update", "--job", "calendar", "--job", ""], "--job"),
        (["update", "--job", " "], "--job"),
        # Checked before a brain is selected, and quoted like Click's choices: an empty kind used to read as a blank.
        (["export", "--kind", ""], "--kind: '' is not one of 'edges', 'identities', 'items'"),
        # A suite path is relative to the brain, never the working directory: it keeps its own reason.
        (["eval", "--path", ""], "--path: expected a normalized brain-relative path"),
        # Python passes undecodable argument bytes, such as a Latin-1 é, as lone surrogates. Before 18 the read
        # failed binding them into the cache and blamed a brain file; the same bytes as a QUERY were invalid input.
        (["read", "caf\udce9"], "REF: expected UTF-8 text"),
        # Run's variadic arguments used to skip that check: the name failed later as an unknown job, exit 1.
        (["run", "caf\udce9"], "JOB: expected UTF-8 text"),
        (["forget", "caf\udce9"], "REF: expected UTF-8 text"),
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
    if arguments[:2] == ["skill", "install"]:
        help_target = "skill install"
    elif arguments[:2] == ["action", "status"]:
        help_target = "action status"
    else:
        help_target = arguments[0]
    assert err.endswith(f" (see bf {help_target} -h)\n")
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
    monkeypatch.setattr("bf.health.report", cancelled)
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
    # --job selects several jobs by repetition; an unknown one is a failure, not a usage error.
    result = CliRunner().invoke(app, ["update", "--brain", brain, "--job", "a", "--job", "b"])
    assert result.exit_code == 1, result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["init", "", "-h"],
        ["ingest", "", "--help"],
        ["guide", "", "--help"],
        ["unregister", "", "--help"],
        ["skill", "install", "--help"],
        ["update", "--job", "", "-h"],
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
        ["skill", "install", "--brain", "~bf-no-such-user/brain"],
    ],
)
def test_an_unknown_home_directory_fails_without_a_traceback(arguments: list[str]) -> None:
    result = bf(*arguments)
    assert result.returncode == 1, result.stderr
    assert not result.stdout
    assert result.stderr == "bf: a path's ~ or ~user home directory cannot be resolved; check the user name\n"


@pytest.mark.parametrize("command", ["build", "eval", "validate"])
def test_single_root_commands_describe_their_selection(command: str) -> None:
    # Single-brain commands select one brain explicitly, by cwd/env, or from the default registered brain.
    help_text = " ".join(plain(CliRunner().invoke(app, [command, "--help"]).stdout).replace("│", " ").split())
    assert "BF_BRAIN, cwd, or default" in help_text
    assert "no default" not in help_text


def test_root_help_explains_first_use_and_output() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    help_text = plain(result.stdout)
    assert "bf init ~/brain" in help_text
    assert "JSON" in help_text
    assert "bf COMMAND -h" in help_text
    assert {"guide", "post", "register", "run", "skill", "unregister", "wake"} <= set(COMMANDS)


@pytest.mark.parametrize(
    ("command", "argument"),
    [
        ("search", "QUERY"),
        ("read", "REF"),
        ("collect", "JOB"),
        ("ingest", "SOURCE"),
        ("init", "PATH"),
        ("register", "PATH"),
        ("unregister", "TARGET"),
        ("guide", "TOPIC"),
    ],
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
        timeout=120,
        check=False,
    )
    assert (result.returncode, result.stderr) == (1, message)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "argv", ["bf", "build", "--brain", str(tmp_path / "brain")])
    with pytest.raises(SystemExit) as exited:
        main()
    assert (exited.value.code, capsys.readouterr().err) == (1, message)
    assert not (tmp_path / "brain/.bf/cache.sqlite3").exists()


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
        timeout=120,
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
    monkeypatch.setattr("bf.retrieve.read", failing)
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
            timeout=120,
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
        assert json.loads(process.stdout.read()) == {"ok": True, "dry_run": False, "hook": "pre-push", "jobs": []}


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


def test_arguments_that_are_not_utf8_are_invalid_before_anything_is_written(tmp_path: Path) -> None:
    # Bad UTF-8 arguments fail validation before init/register/unregister/guide touch disk.
    parent = tmp_path / "parent"
    parent.mkdir()
    target = os.fsencode(parent) + b"/bad\xff"
    cases = [
        ([b"init", target, b"--name", b"bad"], b"PATH"),
        ([b"register", target], b"PATH"),
        ([b"unregister", target], b"TARGET"),
        ([b"guide", b"bad\xff"], b"[TOPIC]"),
    ]
    for arguments, hint in cases:
        result = subprocess.run(  # noqa: S603 - fixed interpreter and synthetic CLI arguments
            [sys.executable, "-m", "bf", *arguments], capture_output=True, timeout=120, check=False
        )
        assert (result.returncode, result.stdout) == (2, b"")
        assert result.stderr == b"bf: invalid input: %s: expected UTF-8 text (see python -m bf %s -h)\n" % (
            hint,
            arguments[0],
        )
    assert not list(parent.iterdir())


def test_action_commands_create_and_settle_actions(brain: Store) -> None:
    runner = CliRunner()
    created = runner.invoke(
        app,
        [
            "action",
            "new",
            "rotate-keys",
            "--title",
            "Rotate API keys",
            "--summary",
            "Quarterly key rotation",
            "--brain",
            str(brain.root),
        ],
    )
    assert created.exit_code == 0, created.output
    first_reply = json.loads(created.stdout)
    first_path = str(first_reply["action"])
    assert first_reply["status"] == "draft"
    assert "summary: Quarterly key rotation" in brain.read(first_path).decode()

    # Same slug on the same day allocates a suffixed folder instead of overwriting.
    second = runner.invoke(
        app, ["action", "new", "rotate-keys", "--title", "Second rotation", "--brain", str(brain.root)]
    )
    assert second.exit_code == 0, second.output
    second_path = str(json.loads(second.stdout)["action"])
    assert second_path != first_path

    # Validation errors on bad slug or multi-line title.
    assert runner.invoke(app, ["action", "new", "Bad_Slug", "--title", "Ok", "--brain", str(brain.root)]).exit_code == 2
    assert (
        runner.invoke(
            app, ["action", "new", "clean-slug", "--title", "Line1\nLine2", "--brain", str(brain.root)]
        ).exit_code
        == 2
    )

    # Bare slug is ambiguous when two folders match; exact folder or path resolves cleanly.
    ambiguous = runner.invoke(app, ["action", "status", "rotate-keys", "stable", "--brain", str(brain.root)])
    assert ambiguous.exit_code == 1
    assert "multiple actions match rotate-keys" in str(ambiguous.exception)

    folder_name = Path(first_path).parent.name
    settled = runner.invoke(app, ["action", "status", folder_name, "stable", "--brain", str(brain.root)])
    assert settled.exit_code == 0, settled.output
    assert json.loads(settled.stdout)["status"] == "stable"

    # Home shows only open draft actions; settled actions leave Home while remaining readable.
    home = json.loads(runner.invoke(app, ["read", "--brain", str(brain.root)]).stdout)
    assert [item["ref"] for item in home["actions"]] == [second_path]

    # Unique slug resolves directly; missing and malformed targets fail cleanly.
    unique = runner.invoke(app, ["action", "new", "ship-notes", "--title", "Ship notes", "--brain", str(brain.root)])
    assert unique.exit_code == 0
    deprecated = runner.invoke(app, ["action", "status", "ship-notes", "deprecated", "--brain", str(brain.root)])
    assert deprecated.exit_code == 0
    assert json.loads(deprecated.stdout)["status"] == "deprecated"
    assert runner.invoke(app, ["action", "status", "missing-slug", "stable", "--brain", str(brain.root)]).exit_code == 1
    assert (
        runner.invoke(app, ["action", "status", "2020-01-01_missing", "stable", "--brain", str(brain.root)]).exit_code
        == 1
    )
    assert runner.invoke(app, ["action", "status", second_path, "stable", "--brain", str(brain.root)]).exit_code == 0
    assert (
        runner.invoke(
            app, ["action", "status", "actions/nope/ACTION.md", "stable", "--brain", str(brain.root)]
        ).exit_code
        == 1
    )
    assert (
        runner.invoke(
            app, ["action", "status", "actions/a/b/ACTION.md", "stable", "--brain", str(brain.root)]
        ).exit_code
        == 2
    )
    assert (
        runner.invoke(app, ["action", "status", "actions/../bad", "stable", "--brain", str(brain.root)]).exit_code == 2
    )
    assert (
        runner.invoke(app, ["action", "status", "projects/offline.md", "stable", "--brain", str(brain.root)]).exit_code
        == 2
    )


def test_export_items_emits_relations_attributes_and_observed_since(brain: Store) -> None:
    from pydantic import JsonValue

    from bf.models import Record, encode
    from bf.records import path as record_path

    brain.write(
        "bf.yaml",
        b'version: 11\nname: fixture\nfields:\n  topic:\n    description: Topics\n    targets: ["topic:"]\n'
        b"jobs:\n  feed:\n    run: [echo]\n    emit: records\n",
    )
    brain.write(
        "projects/tagged.md",
        b"---\ntype: project\nstatus: stable\nupdated: 2026-09-25\n"
        b"relations:\n  topic: [topic:alpha, topic:beta]\nattributes:\n  priority: high\n---\n\n# Tagged\n",
    )
    cases: tuple[tuple[str, str, list[JsonValue]], ...] = (
        ("old", "2026-09-01T00:00:00Z", ["legacy"]),
        ("recent", "2026-09-25T10:00:00Z", ["gamma", "delta"]),
    )
    for rec_id, obs, topics in cases:
        rec = Record(
            id=rec_id,
            type="Note",
            title=f"Item {rec_id}",
            time=obs,
            relations={"topic": [f"topic:{t}" for t in topics]},
            attributes={"observed": obs, "topics": topics, "priority": "high"},
        )
        brain.write(record_path("feed", rec.id), encode(rec.model_dump(by_alias=True, exclude_defaults=True)))

    runner = CliRunner()
    wrong_kind = runner.invoke(app, ["export", "--kind", "edges", "--observed-since", "7d", "--brain", str(brain.root)])
    assert wrong_kind.exit_code == 2

    all_exported = runner.invoke(app, ["export", "--kind", "items", "--brain", str(brain.root)])
    assert all_exported.exit_code == 0, all_exported.output
    all_rows = {row["ref"]: row for row in (json.loads(line) for line in all_exported.stdout.splitlines())}
    assert all_rows["projects/tagged.md"]["relations"] == {"topic": ["topic:alpha", "topic:beta"]}
    assert all_rows["projects/tagged.md"]["attributes"] == {"priority": "high"}
    assert "feed:old" in all_rows

    exported = runner.invoke(
        app,
        ["export", "--kind", "items", "--observed-since", "2026-09-20T00:00:00Z", "--brain", str(brain.root)],
    )
    assert exported.exit_code == 0, exported.output
    rows = {row["ref"]: row for row in (json.loads(line) for line in exported.stdout.splitlines())}
    assert "feed:old" not in rows
    recent_row = rows["feed:recent"]
    assert recent_row["@type"] == "Note"
    assert recent_row["relations"] == {"topic": ["topic:gamma", "topic:delta"]}
    assert recent_row["attributes"]["priority"] == "high"


def test_action_status_preserves_rich_frontmatter_and_resolves_unique_slugs(brain: Store) -> None:
    import yaml

    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\n",
    )
    runner = CliRunner()
    created = runner.invoke(app, ["action", "new", "audit-logs", "--unique", "--brain", str(brain.root)])
    assert created.exit_code == 0, created.output
    action_path = str(json.loads(created.stdout)["action"])
    assert "# Audit logs\n" in brain.read(action_path).decode()

    brain.write(
        action_path,
        b"---\ntype: action\nstatus: draft\nupdated: 2026-09-20\nsummary: Keep keys\ntags: [ops, audit]\n"
        b"attributes:\n  priority: high\n---\n# Audit logs\n\n## Outcome\n\nSettled.\n",
    )
    updated = runner.invoke(app, ["action", "status", "audit-logs", "stable", "--brain", str(brain.root)])
    assert updated.exit_code == 0, updated.output
    raw_text = brain.read(action_path).decode()
    parts = raw_text.split("---\n")
    assert len(parts) == 3
    meta = yaml.safe_load(parts[1])
    assert (meta["type"], meta["status"], meta["summary"], meta["tags"], meta["attributes"]) == (
        "action",
        "stable",
        "Keep keys",
        ["ops", "audit"],
        {"priority": "high"},
    )
    assert "# Audit logs\n\n## Outcome\n\nSettled.\n" in parts[2]


def test_cli_run_wake_post_action_watch_and_input_validation(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n  feed:\n    run: [routines/noop.sh]\n    emit: records\n"
        b"  brief:\n    prompt: prompts/brief.md\n    harness: [routines/noop.sh]\n"
        b"  noop:\n    run: [routines/noop.sh]\n",
    )
    brain.write("prompts/brief.md", b"Summarize.\n")
    brain.write("routines/noop.sh", b"#!/bin/sh\nexit 0\n")
    (brain.root / "routines/noop.sh").chmod(0o700)

    runner = CliRunner()
    assert runner.invoke(app, ["run", "--hook", "pre-push", "--since", "1d", "--brain", str(brain.root)]).exit_code == 2
    assert runner.invoke(app, ["run", "feed", "extra", "--brain", str(brain.root)]).exit_code == 2
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "argv", ["bf", "run", "noop", "--stdin", "--brain", str(brain.root)])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 0
    monkeypatch.undo()

    # wake validation and integer/duration/--at paths
    for bad_wake in (
        ["wake", "--job", "brief", "--in", "5m", "--at", "2026-09-25T10:00:00Z"],
        ["wake", "--job", "brief", "--in", "bad"],
        ["wake", "--job", "brief", "--in", "0"],
        ["wake", "--in", "5m"],
    ):
        assert runner.invoke(app, [*bad_wake, "--brain", str(brain.root)]).exit_code == 2

    woke_secs = runner.invoke(
        app, ["wake", "--job", "brief", "--in", "60", "--reason", "follow-up", "--brain", str(brain.root)]
    )
    assert woke_secs.exit_code == 0, woke_secs.output
    assert json.loads(woke_secs.stdout)["job"] == "brief"

    woke_at = runner.invoke(app, ["wake", "--job", "brief", "--at", "2026-09-25T10:00:00Z", "--brain", str(brain.root)])
    assert woke_at.exit_code == 0, woke_at.output
    assert json.loads(woke_at.stdout)["due"].startswith("2026-09-25T10:00:00")

    # post argument/UTF-8 validation
    assert runner.invoke(app, ["post", "--brain", str(brain.root)]).exit_code == 2
    assert runner.invoke(app, ["post", "hi", "--stdin", "--brain", str(brain.root)]).exit_code == 2
    bad_utf8 = runner.invoke(app, ["post", "--stdin", "--brain", str(brain.root)], input=b"\xff\xfe")
    assert bad_utf8.exit_code == 1

    # action title control char rejection and `actions/<folder>` resolution
    bad_title = runner.invoke(app, ["action", "new", "ctrl", "--title", "bad\x1btitle", "--brain", str(brain.root)])
    assert bad_title.exit_code == 2
    created = json.loads(runner.invoke(app, ["action", "new", "by-folder", "--brain", str(brain.root)]).stdout)[
        "action"
    ]
    folder_ref = created.removesuffix("/ACTION.md")
    updated = runner.invoke(app, ["action", "status", folder_ref, "stable", "--brain", str(brain.root)])
    assert updated.exit_code == 0, updated.output
    assert json.loads(updated.stdout)["status"] == "stable"

    # watch non-finite poll interval
    assert runner.invoke(app, ["watch", "--poll-interval", "nan", "--brain", str(brain.root)]).exit_code == 2


def test_cli_mcp_signal_main_errors_and_closed_stdin(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    import signal
    from types import ModuleType

    from pydantic import BaseModel, ValidationError

    import bf.cli as cli_mod

    brain.write("bf.yaml", b"version: 11\nname: fixture\n")
    called = False

    class _FakeTools:
        def run(self) -> None:
            nonlocal called
            called = True

    class _FakeMCP(ModuleType):
        def server(self, getter: object) -> _FakeTools:
            assert callable(getter)
            assert cast(Store, getter()).root == brain.root
            return _FakeTools()

    monkeypatch.setitem(sys.modules, "bf.mcp", _FakeMCP("bf.mcp"))

    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        res = CliRunner().invoke(app, ["mcp", "--brain", str(brain.root)])
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        assert CliRunner().invoke(app, ["mcp", "--brain", str(brain.root)]).exit_code == 0
    finally:
        for sig, handler in saved.items():
            if handler is not None:
                signal.signal(sig, handler)
    assert res.exit_code == 0, res.output
    assert called is True

    # main() signal handling (SIGHUP ignored) + KeyboardInterrupt (130) + ValidationError (2)
    class _Item(BaseModel):
        count: int

    try:
        _Item.model_validate({"count": "not-an-int"})
    except ValidationError as exc:
        val_err = exc

    prev_hup = signal.getsignal(signal.SIGHUP)
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        monkeypatch.setattr(cli_mod, "app", lambda: None)
        main()
        for raised, code in ((KeyboardInterrupt(), 130), (val_err, 2)):
            monkeypatch.setattr(cli_mod, "app", lambda err=raised: (_ for _ in ()).throw(err))
            with pytest.raises(SystemExit) as exited:
                main()
            assert exited.value.code == code
    finally:
        if prev_hup is not None:
            signal.signal(signal.SIGHUP, prev_hup)


def test_cli_ingest_reads_input_stdin_snapshot_dry_run_and_validation(brain: Store, tmp_path: Path) -> None:
    brain.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n"
        b"  mapped:\n"
        b"    run: [echo]\n"
        b"    emit: records\n"
        b"    map:\n"
        b"      items: /items\n"
        b"      id: /key\n"
        b"      title: /subject\n"
        b"      text: /body\n",
    )
    runner = CliRunner()
    payload = tmp_path / "mapped.json"
    payload.write_text('{"items":[{"key":"m-1","subject":"Mapped title","body":"Mapped evidence."}]}')

    # --dry-run validates and previews without writing memories/, .bf/wakeup, or .bf/runtime.sqlite3.
    dry = runner.invoke(app, ["ingest", "mapped", "--input", str(payload), "--dry-run", "--brain", str(brain.root)])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.stdout)["records"] == 1
    assert not (brain.root / "memories/mapped").exists()
    assert not (brain.root / ".bf/wakeup").exists()
    assert not (brain.root / ".bf/runtime.sqlite3").exists()

    # --input commits mapped records, pings .bf/wakeup, and keeps .bf/runtime.sqlite3 untouched.
    saved = runner.invoke(app, ["ingest", "mapped", "--input", str(payload), "--brain", str(brain.root)])
    assert saved.exit_code == 0, saved.output
    assert json.loads(saved.stdout)["added"] == 1
    assert len(list((brain.root / "memories/mapped").glob("*.json"))) == 1
    assert (brain.root / ".bf/wakeup").exists()
    assert not (brain.root / ".bf/runtime.sqlite3").exists()

    # Omitting --input reads pre-normalized JSONL from stdin.
    jsonl = (
        '{"id":"s-1","title":"Stdin one","text":"First streamed record."}\n'
        '{"id":"s-2","title":"Stdin two","text":"Second streamed record."}\n'
    )
    streamed = runner.invoke(app, ["ingest", "feed", "--brain", str(brain.root)], input=jsonl)
    assert streamed.exit_code == 0, streamed.output
    assert json.loads(streamed.stdout)["added"] == 2
    assert len(list((brain.root / "memories/feed").glob("*.json"))) == 2
    assert not (brain.root / ".bf/runtime.sqlite3").exists()

    # --snapshot trips removal guard when emptying a catalog; --allow-removal accepts it.
    blocked = runner.invoke(app, ["ingest", "feed", "--snapshot", "--brain", str(brain.root)], input="[]")
    assert blocked.exit_code == 1
    assert "allow-removal" in str(blocked.exception)
    assert len(list((brain.root / "memories/feed").glob("*.json"))) == 2

    accepted = runner.invoke(
        app, ["ingest", "feed", "--snapshot", "--allow-removal", "--brain", str(brain.root)], input="[]"
    )
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["removed"] == 2
    assert list((brain.root / "memories/feed").glob("*.json")) == []

    # Invalid source, reserved source, missing file, and malformed JSON fail cleanly.
    assert runner.invoke(app, ["ingest", "BadName", "--input", str(payload), "--brain", str(brain.root)]).exit_code == 2
    assert runner.invoke(app, ["ingest", "bf", "--brain", str(brain.root)], input="[]").exit_code == 2
    missing = runner.invoke(
        app, ["ingest", "feed", "--input", str(tmp_path / "missing.json"), "--brain", str(brain.root)]
    )
    assert missing.exit_code == 2
    assert runner.invoke(app, ["ingest", "feed", "--brain", str(brain.root)], input="{broken").exit_code == 1
