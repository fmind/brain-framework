"""The retrieval example finds the expected evidence through the public CLI."""

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from bf.cli import app


def test_retrieval_example_validates_and_answers_its_questions(tmp_path: Path) -> None:
    brain = tmp_path / "retrieval"
    shutil.copytree(Path(__file__).parents[1] / "examples/retrieval", brain, ignore=shutil.ignore_patterns(".bf"))
    runner = CliRunner()

    for command, field in [("validate", "valid"), ("eval", "passed")]:
        result = runner.invoke(app, [command, "--brain", str(brain)])
        assert result.exit_code == 0, result.output
        reply = json.loads(result.stdout)
        assert reply[field] is True, reply
        if command == "eval":
            assert reply["score"] == "14/14", reply
