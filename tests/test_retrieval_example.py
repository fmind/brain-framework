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
    baseline = tmp_path / "baseline.json"
    for command, field in [("validate", "valid"), ("eval", "passed")]:
        result = runner.invoke(app, [command, "--brain", str(brain)])
        assert result.exit_code == 0, result.output
        reply = json.loads(result.stdout)
        assert reply[field] is True, reply
        if command == "eval":
            assert (reply["score"], reply["mrr"]) == ("17/17", 0.95), reply
            ranks = {case["name"]: case.get("rank") for case in reply["cases"]}
            # Before 15.0.0 three other projects' Next actions sections mentioning Atlas ranked first.
            assert ranks["project-next-actions-first"] == {"projects/atlas.md#next-actions": 1}
            baseline.write_text(result.stdout)
    # The low-priority catalog copy of the launch plan collapses into the full document.
    result = runner.invoke(app, ["search", "Atlas launch plan", "--limit", "3", "--brain", str(brain)])
    items = json.loads(result.stdout)["items"]
    plan = next(item for item in items if item["ref"] == "documents:atlas-launch-plan")
    assert plan["also"] == ["catalog:atlas-launch-plan"]
    compared = json.loads(runner.invoke(app, ["eval", "--brain", str(brain), "--baseline", str(baseline)]).stdout)
    assert (compared["regressions"], compared["improvements"]) == ([], [])
