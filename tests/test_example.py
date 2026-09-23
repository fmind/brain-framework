"""The documented example runs offline through the same CLI users invoke."""

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from fkf.cli import app


def test_example_collects_searches_and_resumes(tmp_path: Path) -> None:
    base = tmp_path / "example"
    shutil.copytree(Path(__file__).parents[1] / "examples/base", base)
    runner = CliRunner()

    def invoke(*args: str, code: int = 0) -> dict:
        result = runner.invoke(app, [*args, "--base", str(base)])
        assert result.exit_code == code, result.output
        return json.loads(result.stdout) if result.stdout else {}

    assert invoke("update", "--dry-run")["bases"][0]["skipped"]
    assert runner.invoke(app, ["register", str(base), "--collect"]).exit_code == 0
    assert invoke("update", "--dry-run")["bases"][0]["sources"][0]["status"] == "due"
    assert not (base / "records").exists()
    assert invoke("update")["bases"][0]["sources"][0]["status"] == "collected"
    assert invoke("validate")["valid"]
    assert invoke("eval")["passed"]
    evidence = invoke("read", "demo:retention")
    assert "upstream content can disappear" in evidence["record"]["text"]
    task = base / "tasks/2026-09-19_retention"
    (task / "outputs/answer.md").write_text(f"# Answer\n\nKeep originals. Evidence: [record]({evidence['ref']}).\n")
    found = invoke("search", "keep originals", "--type", "task")
    assert found["items"][0]["ref"] == "tasks/2026-09-19_retention/outputs/answer.md"
    assert invoke("validate")["valid"]
    partitions = sorted((base / "records/demo").glob("*.jsonl"))
    assert invoke("update")["bases"][0]["sources"] == []
    assert sorted((base / "records/demo").glob("*.jsonl")) == partitions
    assert (task / "inputs/request.txt").is_file()
