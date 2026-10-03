"""A newcomer joins evidence from four fictional tools, records a conclusion and sees it flagged when Jira changes."""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml
from typer.testing import CliRunner

from bf.cli import app

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "examples/context-hub"
SOURCES = [
    ("workspace", "workspace:brief", "brief", "approved"),
    ("jira", "jira:review", "issue", "In review"),
    ("github", "github:implementation", "pull-request", "merged"),
    ("gcloud", "gcloud:deployment", "deployment", "healthy"),
]


def test_documented_walkthrough_flags_the_conclusion_when_jira_changes(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    shutil.copytree(EXAMPLE, brain)
    page = (ROOT / "docs/docs/context-hub.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)\n```", page, re.DOTALL)
    # The first block clones the release and the last removes the copy: run everything between on a local copy.
    assert "git clone" in blocks[0]
    assert "rm -rf" in blocks[-1]
    result = subprocess.run(  # noqa: S603 - the reviewed walkthrough on its disposable fixture copy
        ["bash", "--noprofile", "--norc", "-euc", "\n".join(blocks[1:-1])],  # noqa: S607
        cwd=brain,
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    replies = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    collections = [reply for reply in replies if "sensor" in reply]
    assert [(reply["sensor"], reply["records"]) for reply in collections[:4]] == [(s, 1) for s, *_ in SOURCES]
    assert (collections[4]["sensor"], collections[4]["updated"]) == ("jira", 1)

    # The four tools' differently named fields become one relation, with their states side by side.
    project = next(reply for reply in replies if "backlinks" in reply)
    group = next(group for group in project["backlinks"] if group["relation"] == "project")
    assert {item["ref"]: item["fields"]["status"] for item in group["items"]} == {
        ref: status for _, ref, _, status in SOURCES
    }
    assert {"notes": 1, "problems": [], "records": 4, "valid": True}.items() <= next(
        reply for reply in replies if "valid" in reply
    ).items()
    assert next(reply for reply in replies if "score" in reply)["score"] == "10/10"

    # The written conclusion is dated today and needs no review; the Jira change then flags it.
    before, after = [reply for reply in replies if reply.get("page") == ""]
    assert [item["ref"] for item in before["changed"]] == ["projects/new-website.md"]
    assert "review" not in before["projects"][0]
    flagged = after["projects"][0]
    assert {key: flagged[key] for key in ("review", "review_reasons", "new_links", "newer")} == {
        "review": True,
        "review_reasons": ["newer_evidence"],
        "new_links": 1,
        "newer": ["jira:review"],
    }
    # The issue kept its event time; only its upstream change is new.
    record = [reply for reply in replies if reply.get("ref") == "jira:review"][-1]["record"]
    assert (datetime.fromisoformat(record["time"]), record["fields"]["status"]) == (
        datetime(2026, 9, 24, 14, tzinfo=UTC),
        "Done",
    )
    # Updating the conclusion clears the flag.
    note = brain / "projects/new-website.md"
    note.write_text(note.read_text().replace("still blocks launch", "is Done: nothing blocks launch"))
    result = CliRunner().invoke(app, ["read", "--brain", str(brain)])
    assert result.exit_code == 0, result.output
    assert "review" not in json.loads(result.stdout)["projects"][0]
    # The walkthrough changed its own copy only.
    assert json.loads((EXAMPLE / "fixtures/jira.json").read_text())["attributes"]["status"] == "In review"


def test_context_hub_stays_readable_without_sensors(tmp_path: Path) -> None:
    brain = tmp_path / "context"
    shutil.copytree(EXAMPLE, brain)
    runner = CliRunner()

    def invoke(*args: str) -> dict:
        result = runner.invoke(app, [*args, "--brain", str(brain)])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    for source, ref, kind, status in SOURCES:
        assert invoke("collect", source)["records"] == 1
        record = invoke("read", ref)["record"]
        assert record["fields"] == {"project": "project:new-website", "kind": kind, "status": status}
        assert record["url"].startswith("https://example.test/")

    # Any attempt to execute a sensor now fails. Retrieval must keep using the saved evidence.
    shutil.rmtree(brain / "sensors")
    project = invoke("read", "project:new-website")
    related = {
        item["ref"] for group in project["backlinks"] if group["relation"] == "project" for item in group["items"]
    }
    assert related == {ref for _, ref, _, _ in SOURCES}
    assert "Accessibility review is still open" in invoke("read", "jira:review")["record"]["text"]
    assert invoke("search", "keyboard navigation", "--scope", "memories/jira")["items"][0]["ref"] == "jira:review"
    assert invoke("eval")["score"] == "10/10"
    # Validation still names the scripts bf.yaml declares but the brain no longer holds.
    checked = runner.invoke(app, ["validate", "--brain", str(brain)])
    assert checked.exit_code == 1
    assert json.loads(checked.stdout)["problems"] == [
        {"file": "bf.yaml", "error": f"sensors.{source}.command: sensors/demo.py is not an executable regular file"}
        for source in sorted(source for source, *_ in SOURCES)
    ]

    # A broken mapping fails collection without replacing an already collected source.
    shutil.copytree(EXAMPLE / "sensors", brain / "sensors")
    record_files = {path: path.read_bytes() for path in (brain / "memories/jira").glob("*.json")}
    config = yaml.safe_load((brain / "bf.yaml").read_text())
    config["sensors"]["jira"]["fields"]["project"] = {"path": "/attributes/missing"}
    (brain / "bf.yaml").write_text(yaml.safe_dump(config))
    result = runner.invoke(app, ["collect", "jira", "--brain", str(brain)])
    assert result.exit_code == 1
    assert "required mapped value is missing" in str(result.exception)
    assert record_files == {path: path.read_bytes() for path in (brain / "memories/jira").glob("*.json")}
