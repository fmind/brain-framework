"""The documented example runs offline through the same CLI users invoke."""

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from bf.cli import app


def test_example_collects_searches_and_resumes(tmp_path: Path) -> None:
    brain = tmp_path / "example"
    shutil.copytree(Path(__file__).parents[1] / "examples/brain", brain)
    runner = CliRunner()

    def invoke(*args: str, code: int = 0) -> dict:
        result = runner.invoke(app, [*args, "--brain", str(brain)])
        assert result.exit_code == code, result.output
        return json.loads(result.stdout) if result.stdout else {}

    assert invoke("update", "--dry-run")["brains"][0]["skipped"]
    assert runner.invoke(app, ["register", str(brain), "--collect"]).exit_code == 0
    assert invoke("update", "--dry-run")["brains"][0]["sensors"][0]["status"] == "due"
    assert not (brain / "memories").exists()
    assert invoke("update")["brains"][0]["sensors"][0]["status"] == "collected"
    assert invoke("validate")["valid"]
    assert invoke("eval")["passed"]
    evidence = invoke("read", "demo:retention")
    assert "upstream content can disappear" in evidence["record"]["text"]
    action = brain / "actions/2026-09-19_retention"
    (action / "outputs/answer.md").write_text(f"# Answer\n\nKeep originals. Evidence: [record]({evidence['ref']}).\n")
    found = invoke("search", "keep originals", "--type", "action")
    assert found["items"][0]["ref"] == "actions/2026-09-19_retention/outputs/answer.md"
    assert invoke("validate")["valid"]
    partitions = sorted((brain / "memories/demo").glob("*.jsonl"))
    assert invoke("update")["brains"][0]["sensors"] == []
    assert sorted((brain / "memories/demo").glob("*.jsonl")) == partitions
    assert (action / "inputs/request.txt").is_file()
