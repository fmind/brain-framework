"""A newcomer joins evidence from four fictional tools through the public CLI."""

import json
import shutil
from pathlib import Path

import yaml
from typer.testing import CliRunner

from bf.cli import app


def test_context_hub_collects_once_and_remains_readable_without_sensors(tmp_path: Path) -> None:
    brain = tmp_path / "context"
    shutil.copytree(Path(__file__).parents[1] / "examples/context-hub", brain)
    runner = CliRunner()

    def invoke(*args: str) -> dict:
        result = runner.invoke(app, [*args, "--brain", str(brain)])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    sources = [
        ("workspace", "workspace:brief", "brief"),
        ("jira", "jira:review", "issue"),
        ("github", "github:implementation", "pull-request"),
        ("gcloud", "gcloud:deployment", "deployment"),
    ]
    for source, ref, kind in sources:
        assert invoke("collect", source)["records"] == 1
        record = invoke("read", ref)["record"]
        assert record["fields"] == {"project": "project:new-website", "kind": kind}
        assert record["url"].startswith("https://example.test/")

    # Any attempt to execute a sensor now fails. Retrieval must keep using the saved evidence.
    shutil.rmtree(brain / "sensors")
    project = invoke("read", "project:new-website")
    related = {
        item["ref"] for group in project["backlinks"] if group["relation"] == "project" for item in group["items"]
    }
    assert related == {ref for _, ref, _ in sources}
    assert "Accessibility review is still open" in invoke("read", "jira:review")["record"]["text"]
    assert "clear explanation" in invoke("read", "projects/new-website.md#decision")["text"]
    assert invoke("search", "keyboard navigation", "--scope", "memories/jira")["items"][0]["ref"] == "jira:review"
    assert invoke("validate")["valid"]
    assert invoke("eval")["score"] == "10/10"

    # A broken mapping fails collection without replacing an already collected source.
    shutil.copytree(Path(__file__).parents[1] / "examples/context-hub/sensors", brain / "sensors")
    record_files = {path: path.read_bytes() for path in (brain / "memories/jira").glob("*.json")}
    config = yaml.safe_load((brain / "bf.yaml").read_text())
    config["sensors"]["jira"]["fields"]["project"] = {"path": "/attributes/missing"}
    (brain / "bf.yaml").write_text(yaml.safe_dump(config))
    result = runner.invoke(app, ["collect", "jira", "--brain", str(brain)])
    assert result.exit_code == 1
    assert "required mapped value is missing" in str(result.exception)
    assert record_files == {path: path.read_bytes() for path in (brain / "memories/jira").glob("*.json")}
