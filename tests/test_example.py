"""The documented example runs offline through the same CLI users invoke."""

import json
import re
import shlex
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from bf.cli import app
from bf.config import load
from bf.storage import Store

EXAMPLE = Path(__file__).parents[1] / "examples/brain"
SKILLS = sorted((EXAMPLE / "skills").glob("*/SKILL.md"))
# The Agent Skills specification's frontmatter keys: https://agentskills.io/specification
SPEC = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


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
    # The README's three refs, within the bf-action Context budget: 300 words, six evidence refs and 4 KiB.
    assert len(context["text"].split()) <= 300
    assert len(re.findall(r"\]\(", context["text"])) == 3
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
    root = Path(__file__).parents[1]
    suites = [*sorted((root / "examples").glob("*/evals/*.yaml")), root / "src/bf/starter/evals/retrieval.yaml"]
    assert len(suites) >= 4
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
        b"version: 8\ncases:\n  - name: shared-procedure\n    read: concepts/selected-evidence.md\n"
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


def test_example_instructions_are_the_generated_starter_plus_its_own_rules(tmp_path: Path) -> None:
    # The skill evaluation copies this brain: its agents load what a new brain's agents load, plus one local rule.
    assert CliRunner().invoke(app, ["init", str(tmp_path / "new")]).exit_code == 0
    title, starter = (tmp_path / "new/AGENTS.md").read_text().split("\n", 1)
    example_title, example = (EXAMPLE / "AGENTS.md").read_text().split("\n", 1)
    assert (title, example_title) == ("# Brain", "# Example brain")
    # A refresh replaces the text above this heading and keeps the owner's rules below it.
    assert "\n## Your rules\n" in starter
    assert example.startswith(starter)
    assert "fictional evidence" in example.removeprefix(starter)


@pytest.mark.parametrize("entry", SKILLS, ids=lambda path: path.parent.name)
def test_example_skills_follow_the_specification_and_their_reads_run(tmp_path: Path, entry: Path) -> None:
    text = entry.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---\n")[1])
    assert set(frontmatter) <= SPEC
    assert frontmatter["name"] == entry.parent.name
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", frontmatter["name"])
    assert len(frontmatter["name"]) <= 64
    assert 1 <= len(frontmatter["description"].strip()) <= 1024
    assert 1 <= len(frontmatter.get("compatibility", "unset").strip()) <= 500
    # A brain-local procedure retrieves through the installed bf, never a brain's pinned runtime.
    assert not re.search(r"\buv\s+run\b", text)
    brain = tmp_path / "example"
    shutil.copytree(EXAMPLE, brain)
    reads = re.findall(r"`(bf (?:read|search)\b[^`]*)`", text)
    assert reads
    for command in reads:
        result = CliRunner().invoke(app, [*shlex.split(command)[1:], "--brain", str(brain)])
        assert result.exit_code == 0, (command, result.output)
