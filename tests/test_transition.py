"""Brain Framework's single format preserves searchable evidence and rejects legacy interfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bf.cli import app
from bf.config import load, select, user_path
from bf.markdown import note, reference
from bf.models import Config, Error, Query, Sensor, UserConfig
from bf.retrieve import read, search
from bf.storage import Store, state_store


def test_initialization_uses_only_the_final_layout_and_private_namespaces(tmp_path: Path) -> None:
    target = tmp_path / "new-brain"
    result = CliRunner().invoke(app, ["init", str(target), "--name", "fresh"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["brain"] == "fresh"
    assert {item.name for item in target.iterdir() if item.is_dir()} == {"projects", "actions", "concepts"}
    store = Store(target)
    config = load(store)
    assert config.version == 5
    assert config.routines == {}
    assert config.name == "fresh"
    assert set(config.ontology) == {"author", "owner", "depends-on", "related-to"}
    assert config.sensors == {}
    assert "bf://fresh/" in store.read("AGENTS.md").decode()
    assert user_path().parts[-2:] == ("bf", "config.yaml")
    assert state_store(store.root).root.parent.name == "bf"
    result = CliRunner().invoke(app, ["search", "welcome", "--brain", str(target)])
    assert result.exit_code == 0, result.output
    assert (target / ".bf/index.sqlite").is_file()
    assert ".fkf/" not in store.read(".gitignore").decode()
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
        "settings",
        "skills",
        "tests",
        "evals",
    }


def test_prior_schema_registry_and_placeholder_are_rejected() -> None:
    for value in ({"version": 2, "name": "old"}, {"version": 3, "name": "old", "sources": {}}):
        with pytest.raises(ValidationError):
            Config.model_validate(value)
    with pytest.raises(ValidationError):
        UserConfig.model_validate({"bases": {}})
    with pytest.raises(ValidationError, match="unknown placeholder"):
        Sensor(command=["echo", "{{base}}"])
    result = CliRunner().invoke(app, ["search", "evidence", "--base", "old"])
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_old_selection_environment_does_not_override_registered_brains(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FKF_BASE", "missing-legacy-brain")
    assert [store.root for store in select()] == [brain.root]


def test_authored_scope_and_types_follow_the_new_layout(brain: Store) -> None:
    authored = {
        "projects/transition.md": "project",
        "actions/2026-09-24_transition/ACTION.md": "action",
        "actions/2026-09-24_transition/inputs/context.md": "action",
        "actions/2026-09-24_transition/outputs/result.md": "action",
        "concepts/transition.md": "concept",
    }
    for path in authored:
        brain.write(path, b"# Transitionmarker\n")
    for directory in ("sensors", "routines", "settings", "skills", "tests", "inputs", "tasks", "wiki"):
        brain.write(f"{directory}/excluded.md", b"# Transitionmarker\n")
    reply = search([brain], Query(text="transitionmarker"))
    items = cast("list[dict[str, object]]", reply["items"])
    assert {item["ref"]: item["type"] for item in items} == authored
    for path in authored:
        assert read([brain], path)["text"] == "# Transitionmarker\n"
    with pytest.raises(Error, match="not found"):
        read([brain], "inputs/excluded.md")
    assert note("concepts/domain.md", b"---\ntype: decision\n---\n# Domain\n").knowledge.type == "decision"
    assert reference("concepts/nested/note.md", "/transition.md") == "concepts/transition.md"


def test_disabled_sensor_keeps_source_identity_and_memories_readable(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 5\nname: fixture\nsensors:\n  meetings:\n    command: [unavailable-provider]\n    enabled: false\n",
    )
    reply = read([brain], "meetings:decision-1")
    assert reply["brain"] == "fixture"
    assert reply["ref"] == "meetings:decision-1"
    assert reply["path"] == "memories/meetings/2026-08.jsonl"
    assert cast("dict[str, object]", reply["collection"])["state"] == "disabled"
    assert cast("dict[str, object]", reply["record"])["id"] == "decision-1"
    found = search([brain], Query(text="offline", prefix="memories/meetings"))
    assert {item["source"] for item in cast("list[dict[str, object]]", found["items"])} == {"meetings"}
    sources = cast("list[dict[str, object]]", read([brain], "memories")["sources"])
    assert [(entry["source"], entry["state"]) for entry in sources] == [("meetings", "disabled")]
