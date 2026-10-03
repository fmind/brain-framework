"""Documented limits hold at their edges: routine input, skipped files and evaluation suites (docs/docs/limits.md)."""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import cast

import pytest
from typer.testing import CliRunner, Result

from bf import cli
from bf.cli import app
from bf.evaluate import evaluate
from bf.history import log_path
from bf.models import Error, Query
from bf.retrieve import search
from bf.storage import Store

MIB = 1 << 20
ROUTINES = b"""version: 7
name: fixture
routines:
  count:
    command: [routines/count.sh]
  skip:
    command: [routines/skip.sh]
"""


@pytest.fixture
def routines(brain: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """One routine counts the bytes it reads; the other exits without reading any."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    brain.write("bf.yaml", ROUTINES)
    brain.write("routines/count.sh", b"#!/bin/sh\nwc -c\n")
    brain.write("routines/skip.sh", b"#!/bin/sh\nexit 0\n")
    for name in ("count", "skip"):
        (brain.root / f"routines/{name}.sh").chmod(0o700)
    return brain


def run(store: Store, routine: str, size: int) -> Result:
    return CliRunner().invoke(app, ["run", routine, "--stdin", "--brain", str(store.root)], input=b"x" * size)


def test_run_passes_at_most_one_mebibyte_of_input(routines: Store) -> None:
    # More than a pipe buffer holds: the routine reads while bf writes the rest.
    ran = run(routines, "count", MIB)
    assert ran.exit_code == 0, ran.output
    assert str(MIB) in routines.read(log_path("count")).decode()
    refused = run(routines, "skip", MIB + 1)
    assert refused.exit_code == 1
    assert "piped input exceeds 1 MiB" in str(refused.exception)
    assert not (routines.root / log_path("skip")).exists()


def test_a_routine_may_exit_without_reading_its_input(routines: Store) -> None:
    result = run(routines, "skip", MIB)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["routines"] == [{"routine": "skip", "status": "ran"}]


@pytest.mark.parametrize("size", [MIB, MIB + 1])
def test_a_pipe_delivers_at_most_one_mebibyte(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    reader, writer = os.pipe()

    def feed() -> None:
        with os.fdopen(writer, "wb") as stream:
            stream.write(b"x" * size)

    feeder = threading.Thread(target=feed)
    feeder.start()
    with os.fdopen(reader) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        if size > MIB:
            with pytest.raises(Error, match="piped input exceeds 1 MiB"):
                cli._input()  # noqa: SLF001 - the stdin boundary of bf run
        else:
            assert cli._input() == b"x" * size  # noqa: SLF001 - the stdin boundary of bf run
    feeder.join(timeout=30)
    assert not feeder.is_alive()


def test_a_pipe_must_close_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # A shell that holds its input open never ends the pipe; the wait is shortened from its documented 10 seconds.
    monkeypatch.setattr(cli, "_INPUT_WAIT", 0.2)
    reader, writer = os.pipe()
    with os.fdopen(reader) as stdin, os.fdopen(writer, "wb"):
        monkeypatch.setattr(sys, "stdin", stdin)
        with pytest.raises(Error, match="piped input did not end within"):
            cli._input()  # noqa: SLF001 - the stdin boundary of bf run


def test_replies_list_200_skipped_files_then_a_count(brain: Store) -> None:
    for number in range(203):
        (brain.root / f"projects/broken-{number:03}.md").write_bytes(b"---\nstale_after: soon\n---\n# Broken\n")
    problems = cast("list[dict[str, str]]", search([brain], Query(text="offline"))["problems"])
    assert [problem.get("file") for problem in problems[:200]] == [f"projects/broken-{n:03}.md" for n in range(200)]
    assert "file" not in problems[200]
    assert "3 more" in problems[200]["error"]
    assert len(problems) == 201


def test_evaluation_runs_at_most_100_suites(brain: Store) -> None:
    (brain.root / "evals").mkdir()
    for number in range(101):
        (brain.root / f"evals/suite-{number:03}.yaml").write_bytes(b"version: 7\ncases: []\n")
    with pytest.raises(Error, match="evaluation exceeds 100 suites"):
        evaluate(brain)
    # A hundred suites pass the limit, so the run reads them and rejects the first one, which has no case.
    (brain.root / "evals/suite-100.yaml").unlink()
    with pytest.raises(Error, match=r"invalid evals/suite-000\.yaml"):
        evaluate(brain)
