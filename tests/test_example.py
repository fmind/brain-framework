"""The documented example runs offline through the same CLI users invoke."""

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from fkf.cli import app


def test_example_collects_retrieves_and_resumes(tmp_path: Path) -> None:
    base = tmp_path / "example"
    shutil.copytree(Path(__file__).parents[1] / "examples/base", base)
    runner = CliRunner()

    def invoke(*args: str) -> dict:
        result = runner.invoke(app, [*args, "--base", str(base)])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    assert invoke("update", "--dry-run")["sources"][0]["status"] == "due"
    assert not (base / "records").exists()
    assert invoke("update")["sources"][0]["status"] == "collected"
    assert invoke("validate")["valid"]
    assert invoke("eval")["passed"]
    evidence = invoke("read", "demo:retention")
    assert "upstream content can disappear" in evidence["record"]["text"]
    assert invoke("read", evidence["ref"]) == evidence
    task = base / "tasks/2026-09-19_retention"
    (task / "outputs/answer.md").write_text(f"Keep originals. Evidence: {evidence['ref']}\n")
    result = runner.invoke(app, ["context", "retention", "--base", str(base)])
    assert result.exit_code != 0
    assert "stale; run fkf build" in str(result.exception)
    assert invoke("build")["changed"]
    assert invoke("eval")["passed"]
    receipts = list((base / "records/demo").glob("*.json"))
    assert invoke("update")["sources"] == [{"source": "demo", "status": "not-due"}]
    assert list((base / "records/demo").glob("*.json")) == receipts
    assert (task / "inputs/request.txt").is_file()
