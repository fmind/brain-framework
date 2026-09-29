"""A brain's layout: initialized folders, authored scope and readable evidence of disabled sensors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bf import records
from bf.cli import app
from bf.config import load, user_path
from bf.markdown import note, reference
from bf.models import Error, Query, Sensor
from bf.retrieve import read, search
from bf.storage import Store, state_store
from bf.validate import validate


def test_initialization_creates_the_layout_and_private_namespaces(tmp_path: Path) -> None:
    target = tmp_path / "new-brain"
    result = CliRunner().invoke(app, ["init", str(target), "--name", "fresh"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["brain"] == "fresh"
    assert {item.name for item in target.iterdir() if item.is_dir()} == {
        "projects",
        "actions",
        "concepts",
        "evals",
    }
    store = Store(target)
    config = load(store)
    assert config.version == 7
    assert config.routines == {}
    assert config.name == "fresh"
    assert set(config.ontology) == {"author", "owner", "depends-on", "related-to"}
    assert config.sensors == {}
    # The instructions are the same for every brain; the starter suite reads through this brain's name.
    assert "bf://NAME/" in store.read("AGENTS.md").decode()
    assert "bf://fresh/concepts/welcome.md" in store.read("evals/retrieval.yaml").decode()
    assert user_path().parts[-2:] == ("bf", "config.yaml")
    assert state_store(store.root).root.parent.name == "bf"
    result = CliRunner().invoke(app, ["search", "welcome", "--brain", str(target)])
    assert result.exit_code == 0, result.output
    assert (target / ".bf/index.sqlite").is_file()
    full = tmp_path / "full-brain"
    result = CliRunner().invoke(app, ["init", str(full), "--name", "full", "--full"])
    assert result.exit_code == 0, result.output
    assert {item.name for item in full.iterdir() if item.is_dir()} == {
        "projects",
        "actions",
        "concepts",
        "memories",
        "assets",
        "sensors",
        "routines",
        "skills",
        "evals",
    }


def test_sensor_commands_reject_unknown_placeholders() -> None:
    with pytest.raises(ValidationError, match="unknown placeholder"):
        Sensor(command=["echo", "{{unknown}}"])


def test_authored_scope_and_types_follow_the_layout(brain: Store) -> None:
    authored = {
        "projects/layout.md": "project",
        "actions/2026-09-24_layout/ACTION.md": "action",
        "actions/2026-09-24_layout/inputs/context.md": "action",
        "actions/2026-09-24_layout/outputs/result.md": "action",
        "concepts/layout.md": "concept",
    }
    for path in authored:
        brain.write(path, b"# Layoutmarker\n")
    for directory in ("sensors", "routines", "settings", "skills", "tests", "inputs", "tasks", "wiki"):
        brain.write(f"{directory}/excluded.md", b"# Layoutmarker\n")
    reply = search([brain], Query(text="layoutmarker"))
    items = cast("list[dict[str, object]]", reply["items"])
    assert {item["ref"]: item["type"] for item in items} == authored
    for path in authored:
        assert read([brain], path)["text"] == "# Layoutmarker\n"
    with pytest.raises(Error, match="not found"):
        read([brain], "inputs/excluded.md")
    assert note("concepts/domain.md", b"---\ntype: decision\n---\n# Domain\n").knowledge.type == "decision"
    assert reference("concepts/nested/note.md", "/layout.md") == "concepts/layout.md"
    assert reference("projects/nested/note.md", "/layout.md") == "projects/layout.md"


def test_attachments_keep_their_own_status_words(brain: Store) -> None:
    # Only projects, concepts and ACTION.md follow OKF statuses; an exported attachment stays searchable.
    brain.write("actions/2026-09-27_import/ACTION.md", b"---\ntype: action\nstatus: draft\n---\n# Import\n")
    brain.write("actions/2026-09-27_import/outputs/export.md", b"---\nstatus: wip\n---\n# Quokka export\n")
    reply = search([brain], Query(text="quokka"))
    assert [item["ref"] for item in cast("list[dict[str, object]]", reply["items"])] == [
        "actions/2026-09-27_import/outputs/export.md"
    ]
    assert "problems" not in reply
    assert validate(brain)["valid"]
    brain.write("projects/finished.md", b"---\ntype: project\nstatus: wip\n---\n# Finished\n")
    assert validate(brain)["problems"] == [
        {"file": "projects/finished.md", "error": "OKF status must be draft, stable or deprecated"}
    ]


def test_disabled_sensor_keeps_source_identity_and_memories_readable(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 7\nname: fixture\nsensors:\n  meetings:\n    command: [unavailable-provider]\n    enabled: false\n",
    )
    reply = read([brain], "meetings:decision-1")
    assert reply["brain"] == "fixture"
    assert reply["ref"] == "meetings:decision-1"
    assert reply["path"] == records.path("meetings", "decision-1")
    assert cast("dict[str, object]", reply["collection"])["state"] == "disabled"
    assert cast("dict[str, object]", reply["record"])["id"] == "decision-1"
    found = search([brain], Query(text="offline", prefix="memories/meetings"))
    assert {item["source"] for item in cast("list[dict[str, object]]", found["items"])} == {"meetings"}
    sources = cast("list[dict[str, object]]", read([brain], "memories")["sources"])
    assert [(entry["source"], entry["state"]) for entry in sources] == [("meetings", "disabled")]
