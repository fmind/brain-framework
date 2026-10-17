"""The retrieval example finds the expected evidence through the public CLI."""

import json
import re
import shutil
from pathlib import Path

import yaml
from typer.testing import CliRunner

from bf.cli import app

EXAMPLE = Path(__file__).parents[1] / "examples/retrieval"


def test_retrieval_example_validates_and_answers_its_questions(tmp_path: Path) -> None:
    brain = tmp_path / "retrieval"
    shutil.copytree(EXAMPLE, brain, ignore=shutil.ignore_patterns(".bf"))
    runner = CliRunner()
    baseline = tmp_path / "baseline.json"
    for command, field in [("validate", "valid"), ("eval", "passed")]:
        result = runner.invoke(app, [command, "--brain", str(brain)])
        assert result.exit_code == 0, result.output
        reply = json.loads(result.stdout)
        assert reply[field] is True, reply
        if command == "eval":
            assert (reply["score"], reply["mrr"]) == ("26/26", 1.0), reply
            ranks = {case["name"]: case.get("rank") for case in reply["cases"]}
            # Before 15.0.0 three other projects' Next actions sections mentioning Atlas ranked first.
            assert ranks["project-next-actions-first"] == {"projects/atlas.md#next-actions": 1}
            # Before 16 a glossary definition sharing two of the three words ranked first.
            assert ranks["one-result-per-document-url"] == {"documents:atlas-launch-plan": 1}
            baseline.write_text(result.stdout)
    # The low-priority catalog copy of the launch plan collapses into the full document.
    result = runner.invoke(app, ["search", "Atlas launch plan", "--limit", "3", "--brain", str(brain)])
    items = json.loads(result.stdout)["items"]
    plan = next(item for item in items if item["ref"] == "documents:atlas-launch-plan")
    assert plan["also"] == ["catalog:atlas-launch-plan"]
    compared = json.loads(runner.invoke(app, ["eval", "--brain", str(brain), "--baseline", str(baseline)]).stdout)
    assert (compared["regressions"], compared["improvements"]) == ([], [])
    # The README's feed order, which evaluation `expect` does not check: the observed-only record lists first.
    for period in ("memories/feed/2026-09-22", "2026-09-22"):
        page = json.loads(runner.invoke(app, ["read", period, "--brain", str(brain)]).stdout)
        assert (page["total"], [item["ref"] for item in page["items"]]) == (2, ["feed:observed", "feed:dated"])


def test_retrieval_readme_quotes_its_suite() -> None:
    # The README presents its budget cases as the suite's own: each quoted case matches the suite's case.
    quoted = yaml.safe_load(re.findall(r"```yaml\n(.*?)\n```", (EXAMPLE / "README.md").read_text(), re.DOTALL)[0])
    suite = yaml.safe_load((EXAMPLE / "evals/retrieval.yaml").read_text())
    cases = {case["name"]: case for case in suite["cases"]}
    assert quoted["version"] == suite["version"]
    assert quoted["cases"] == [cases[case["name"]] for case in quoted["cases"]]
