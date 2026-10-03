"""The documented example runs offline through the same CLI users invoke."""

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from bf.cli import app
from bf.config import load
from bf.storage import Store


def test_example_collects_searches_and_resumes(tmp_path: Path) -> None:
    brain = tmp_path / "example"
    shutil.copytree(Path(__file__).parents[1] / "examples/brain", brain)
    runner = CliRunner()

    def invoke(*args: str, code: int = 0) -> dict:
        result = runner.invoke(app, [*args, "--brain", str(brain)])
        assert result.exit_code == code, result.output
        return json.loads(result.stdout) if result.stdout else {}

    assert invoke("update", "--dry-run")["sensors"][0]["status"] == "due"
    assert not (brain / "memories").exists()
    assert invoke("update")["sensors"][0]["status"] == "collected"
    assert invoke("validate")["valid"]
    assert invoke("eval")["passed"]
    context = invoke("read", "actions/2026-09-25_retention-review/ACTION.md#context")
    assert len(context["text"].split()) <= 300
    assert len(context["text"].encode()) <= 4096
    assert "backlinks" not in context
    evidence = invoke("read", "demo:retention")
    assert "upstream content can disappear" in evidence["record"]["text"]
    action = brain / "actions/2026-09-19_retention"
    (action / "outputs/answer.md").write_text(f"# Answer\n\nKeep originals. Evidence: [record]({evidence['ref']}).\n")
    found = invoke("search", "keep originals", "--scope", "actions")
    assert found["items"][0]["ref"] == "actions/2026-09-19_retention/outputs/answer.md"
    assert invoke("validate")["valid"]
    record_files = sorted((brain / "memories/demo").glob("*.json"))
    assert invoke("update")["sensors"] == []
    assert sorted((brain / "memories/demo").glob("*.json")) == record_files
    assert (action / "inputs/request.txt").is_file()


def test_example_suites_link_to_the_suite_reference() -> None:
    # Editors open the header URL; it must be the page `bf init` links, which owns the suite fields.
    suites = sorted((Path(__file__).parents[1] / "examples").glob("*/evals/*.yaml"))
    assert len(suites) >= 3
    for suite in suites:
        assert suite.read_text().startswith("# https://fmind.github.io/brain-framework/docs/checks/\n"), suite


def test_prepared_knowledge_transfer_works_without_the_source_brain(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "examples/brain/actions/2026-09-25_retention-review/outputs"
    runner = CliRunner()
    destination = tmp_path / "team"
    assert runner.invoke(app, ["init", str(destination), "--name", "example-team"]).exit_code == 0
    store = Store(destination)
    candidate = (source / "shared-procedure.md").read_bytes()
    manifest = json.loads((source / "share-manifest.json").read_text())
    assert manifest["files"] == ["concepts/selected-evidence.md"]
    assert b"bf://example/" not in candidate
    assert b"archive-policy" not in candidate
    store.write(manifest["files"][0], candidate)
    store.write(
        "evals/retrieval.yaml",
        b"version: 7\ncases:\n  - name: shared-procedure\n    read: concepts/selected-evidence.md\n"
        b"    text: [draft, Partial evidence remains partial, No successful outcome]\n"
        b"  - name: no-private-history\n    query: unverified belief\n    empty: true\n",
    )
    for command in ["validate", "eval"]:
        result = runner.invoke(app, [command, "--brain", str(destination)])
        assert result.exit_code == 0, result.output
    reply = runner.invoke(app, ["read", "concepts/selected-evidence.md", "--brain", str(destination)])
    assert reply.exit_code == 0, reply.output
    assert json.loads(reply.stdout)["brain"] == "example-team"
    assert not load(store).brains


def test_example_broader_relation_page_matches_the_readme(tmp_path: Path) -> None:
    # The README's export lines follow its walkthrough, answer included: tests/test_example_walkthroughs.py runs it.
    brain = tmp_path / "example"
    shutil.copytree(Path(__file__).parents[1] / "examples/brain", brain)
    policy = "bf://example/concepts/archive-policy.md"
    reply = CliRunner().invoke(app, ["read", policy, "--rel", "related-to", "--brain", str(brain)])
    items = json.loads(reply.stdout)["items"]
    assert [(item["ref"], item["relation"]) for item in items] == [
        ("actions/2026-09-25_retention-review/outputs/decision.md", "depends-on")
    ]
