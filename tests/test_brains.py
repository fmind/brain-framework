"""Single-brain selection precedence, registration, ownership checks and portable local scoping."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import cast

import pytest
import yaml
from mcp.types import CallToolResult
from pydantic import ValidationError
from typer.testing import CliRunner

from bf import records
from bf.cli import app
from bf.config import ABSENT, execution, load, one, register, select, unregister, user_config, user_path
from bf.evaluate import evaluate
from bf.mcp import server
from bf.models import Config, Error, InputError, NotFoundError, Query, Record
from bf.retrieve import read, search
from bf.storage import Store
from bf.validate import validate


def make(root: Path, name: str) -> Store:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root)
    store.write("bf.yaml", yaml.safe_dump({"version": 10, "name": name}).encode())
    store.write(
        "projects/example.md",
        f"# Shared evidence\n\n{name} decision.\n\n## Decision {{#decision}}\n\n{name} durable answer.\n".encode(),
    )
    return store


def test_selection_precedence_and_unconfigured_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default_brain = make(tmp_path / "default-brain", "default-brain")
    enclosing = make(tmp_path / "enclosing", "enclosing")
    env_brain = make(tmp_path / "env-brain", "env-brain")
    explicit = make(tmp_path / "explicit", "explicit")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    # No registry and outside any brain -> explicit message without creating global state.
    with pytest.raises(Error, match="no brain selected; pass --brain"):
        select()

    # Registered without default -> asks for --default or --brain.
    register(default_brain)
    with pytest.raises(Error, match="no default brain configured"):
        select()

    # Precedence: --brain -> BF_BRAIN -> enclosing -> default.
    registered = register(default_brain, default=True)
    assert registered["default"] is True
    assert select().root == default_brain.root

    monkeypatch.chdir(enclosing.root / "projects")
    assert select().root == enclosing.root

    register(env_brain)
    monkeypatch.setenv("BF_BRAIN", "env-brain")
    assert select().root == env_brain.root
    assert execution().root == env_brain.root

    assert select("explicit" if False else str(explicit.root)).root == explicit.root
    assert execution(str(explicit.root)).root == explicit.root

    # Invalid optional global registry blocks name/default lookup without breaking explicit paths or enclosing discovery.
    monkeypatch.delenv("BF_BRAIN")
    user_path().parent.mkdir(parents=True, exist_ok=True)
    user_path().write_text("invalid: [")
    assert one().root == enclosing.root
    assert one(str(enclosing.root)).root == enclosing.root
    with pytest.raises(Error, match="invalid YAML"):
        one("default-brain")


def test_register_and_unregister_contracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    brain = make(tmp_path / "work", "work")
    alias_dir = make(tmp_path / "other", "other")
    # Invalid alias fails before creating config.yaml.
    with pytest.raises(InputError, match="lowercase letter"):
        register(brain, name="Bad_Name")
    assert not user_path().exists()

    assert register(brain, name="team-work", default=True) == {
        "brain": "team-work",
        "path": str(brain.root),
        "config": str(user_path()),
        "default": True,
    }
    assert user_config().default == "team-work"

    # Re-registering the same physical brain under a second name refuses silently aliasing it.
    with pytest.raises(Error, match="already registered as team-work"):
        register(brain, name="duplicate")

    # Different brain cannot steal an active registered name.
    with pytest.raises(Error, match="already registered as team-work"):
        register(alias_dir, name="team-work")

    # Unregistering by Store clears `default` when removing the default brain.
    assert unregister(brain) == {
        "brain": "team-work",
        "path": str(brain.root),
        "config": str(user_path()),
        "cleared_default": True,
    }
    assert user_config().default is None
    assert user_config().brains == {}

    # Unregister by path works too; missing registration errors cleanly.
    register(alias_dir)
    monkeypatch.chdir(tmp_path)
    assert unregister(str(alias_dir.root))["brain"] == "other"
    with pytest.raises(Error, match="not registered"):
        unregister("other")


def test_brains_key_is_rejected_and_foreign_addresses_fail(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"version": 10, "name": "test", "brains": {"other": {"path": "../other"}}})
    first = make(tmp_path / "first", "first")
    second = make(tmp_path / "second", "second")
    first.write("bf.yaml", b"version: 10\nname: first\nbrains:\n  second:\n    path: ../second\n")
    with pytest.raises(Error, match=r"invalid bf\.yaml"):
        load(first)
    first.write("bf.yaml", b"version: 10\nname: first\n")
    assert "first durable answer" in str(read(first, "bf://first/projects/example.md#decision")["text"])
    with pytest.raises(NotFoundError):
        read(first, "bf://second/projects/example.md")
    assert "second durable answer" in str(read(second, "projects/example.md#decision")["text"])


def test_init_never_writes_global_config_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "new"
    result = CliRunner().invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.output
    assert not user_path().exists()
    monkeypatch.chdir(target)
    assert search(select(), Query(text="welcome"))["items"]


def test_mcp_and_evaluations_use_single_selected_brain(tmp_path: Path) -> None:
    first = make(tmp_path / "first", "first")
    first.write(
        "evals/retrieval.yaml",
        b"version: 10\ncases:\n- name: own\n  query: durable\n  expect: [projects/example.md]\n",
    )
    assert evaluate(first)["passed"]
    mcp = server(first)

    async def check() -> None:
        result = await mcp.call_tool("search", {"query": "durable"})
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None
        assert [item["ref"] for item in result.structured_content["items"]] == ["projects/example.md#decision"]
        result = await mcp.call_tool("read", {"ref": "bf://first/projects/example.md#decision"})
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None
        assert "first durable answer" in result.structured_content["text"]

    asyncio.run(check())


def test_registry_paths_are_absolute_and_names_win_over_local_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = make(tmp_path / "registered", "shared")
    register(registered)
    # A same-named directory below the working directory does not replace the registered brain.
    (tmp_path / "work").mkdir()
    make(tmp_path / "work" / "shared", "shared")
    monkeypatch.chdir(tmp_path / "work")
    assert select("shared").root == registered.root
    # A relative registry path would resolve against the working directory and could select another clone.
    user_path().write_text("brains:\n  shared:\n    path: shared\n")
    with pytest.raises(Error, match="absolute"):
        select("shared")


def test_registering_again_leaves_the_registry_and_its_comments_unchanged(tmp_path: Path) -> None:
    brain = make(tmp_path / "laptop", "laptop")
    register(brain)
    commented = user_path().read_text().replace(f"path: {brain.root}", f"path: {brain.root}  # this laptop")
    user_path().write_text(commented)
    assert "replaced" not in register(brain)
    assert user_path().read_text() == commented


def test_a_mistyped_brain_name_suggests_close_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    register(make(tmp_path / "work", "work"))
    make(tmp_path / "personal", "personal")
    monkeypatch.chdir(tmp_path / "personal")
    # Registered names and the enclosing brain's are what a mistyped name may have meant.
    for typed, meant in (("wrok", "work"), ("persnal", "personal")):
        with pytest.raises(Error, match=f"^brain directory does not exist; .*; did you mean {meant}\\?$"):
            select(typed)
    with pytest.raises(Error, match=r"^brain directory does not exist; pass an existing --brain or BF_BRAIN$"):
        select("zzz")
    with pytest.raises(Error, match=r"neither registered nor the enclosing brain; .*; did you mean work\?$"):
        execution("wrok")


def test_an_enclosing_brain_cannot_replace_a_registered_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registered = make(tmp_path / "registered", "shared")
    register(registered)
    # An untrusted checkout claims the owner's brain name for itself.
    clone = make(tmp_path / "clone", "shared")
    monkeypatch.chdir(clone.root / "projects")
    for selector in (select, execution):
        with pytest.raises(Error, match=r"ambiguous brain name shared.*--brain PATH"):
            selector("shared")
    monkeypatch.setenv("BF_BRAIN", "shared")
    with pytest.raises(Error, match="ambiguous brain name shared"):
        select()
    monkeypatch.delenv("BF_BRAIN")
    # The same physical brain is no conflict, and an unrelated enclosing brain selects cleanly by its own name.
    monkeypatch.chdir(registered.root)
    assert execution("shared").root == registered.root
    other = make(tmp_path / "other", "other")
    monkeypatch.chdir(other.root)
    assert execution("other").root == other.root
    monkeypatch.chdir(clone.root / "projects")
    result = CliRunner().invoke(app, ["update", "--dry-run", "--brain", "shared"])
    assert result.exit_code == 1
    assert "ambiguous brain name shared" in str(result.exception)


def test_enclosing_discovery_requires_your_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    planted = make(tmp_path / "planted", "planted")
    mine = make(tmp_path / "mine", "planted")
    register(mine)
    (planted.root / "work").mkdir()
    monkeypatch.chdir(planted.root / "work")
    assert one().root == planted.root
    with pytest.raises(Error, match="ambiguous brain name planted"):
        select("planted")
    # Another account's bf.yaml above the working directory is never trusted implicitly, as in Git.
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    for selector in (select, execution):
        with pytest.raises(Error, match=r"^\.\./bf\.yaml is not a regular file owned by you; pass --brain PATH"):
            selector()
    result = CliRunner().invoke(app, ["update", "--dry-run"])
    assert result.exit_code == 1
    assert "not a regular file owned by you" in str(result.exception)
    # An explicit path remains the owner's decision.
    assert one(str(planted.root)).root == planted.root
    # A brain you do not own claims no name, so your registered name still selects your brain below it.
    assert one("planted").root == execution("planted").root == mine.root
    monkeypatch.setenv("BF_BRAIN", "planted")
    assert one().root == execution().root == mine.root


def test_default_brain_is_selected_for_retrieval_and_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    team = make(tmp_path / "team", "team")
    team.write("sensors/pulled.sh", b"#!/bin/sh\necho '[]'\n")
    (team.root / "sensors/pulled.sh").chmod(0o700)
    team.write(
        "bf.yaml",
        b"version: 10\nname: team\njobs:\n  pulled:\n    run: [sensors/pulled.sh]\n    emit: records\n"
        b"    on:\n      every: 900\n",
    )
    register(team, default=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    found = cast(dict, search(select(), Query(text="durable")))
    assert [item["ref"] for item in found["items"]] == ["projects/example.md#decision"]
    update_reply = CliRunner().invoke(app, ["update", "--dry-run"])
    assert update_reply.exit_code == 0, update_reply.output
    assert json.loads(update_reply.stdout)["brain"] == "team"
    collect_reply = CliRunner().invoke(app, ["collect", "pulled", "--dry-run"])
    assert collect_reply.exit_code == 0, collect_reply.output
    assert json.loads(collect_reply.stdout)["job"] == "pulled"
    result = CliRunner().invoke(app, ["update", "--dry-run", "--brain", "team"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["brain"] == "team"


def test_execution_names_only_registered_or_enclosing_brains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    team = make(tmp_path / "team", "team")
    marker = tmp_path / "ran"
    team.write("sensors/s.sh", f"#!/bin/sh\ntouch '{marker}'\n".encode())
    (team.root / "sensors/s.sh").chmod(0o700)
    team.write(
        "bf.yaml",
        b"version: 10\nname: team\njobs:\n  s:\n    run: [sensors/s.sh]\n    emit: records\n    on:\n      every: 60\n",
    )
    # A subdirectory below the working directory is not checked for ownership, so execution requires a deliberate path.
    monkeypatch.chdir(tmp_path)
    for command in (["update", "--dry-run", "--brain", "team"], ["collect", "s", "--brain", "team"]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 1, command
        assert "brain team is neither registered nor the enclosing brain; pass --brain PATH" in str(result.exception)
    monkeypatch.setenv("BF_BRAIN", "team")
    with pytest.raises(Error, match="neither registered nor the enclosing brain"):
        execution()
    assert not marker.exists()
    monkeypatch.delenv("BF_BRAIN")
    assert one("team").root == execution("./team").root == team.root
    monkeypatch.chdir(team.root / "projects")
    assert execution("team").root == team.root


def test_a_registered_name_does_not_depend_on_the_enclosing_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team = make(tmp_path / "team", "team")
    register(team)
    outdated = make(tmp_path / "outdated", "outdated")
    outdated.write("bf.yaml", b"version: 5\nname: outdated\n")
    monkeypatch.chdir(outdated.root)
    assert one("team").root == execution("team").root == team.root
    result = CliRunner().invoke(app, ["search", "durable", "--brain", "team"])
    assert result.exit_code == 0, result.output


def test_absent_registered_brain_is_reported_clearly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    moved = make(tmp_path / "moved", "moved")
    register(moved, default=True)
    moved.root.rename(tmp_path / "elsewhere")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Error, match=re.escape(ABSENT)):
        select("moved")
    with pytest.raises(Error, match=re.escape(ABSENT)):
        select()


def test_a_registered_folder_that_lost_bf_yaml_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    emptied = make(tmp_path / "emptied", "emptied")
    register(emptied, default=True)
    emptied.delete("bf.yaml")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Error, match=r"registered brain emptied has no bf\.yaml"):
        select("emptied")
    with pytest.raises(Error, match=r"default brain emptied has no bf\.yaml"):
        select()


@pytest.mark.parametrize(
    "command",
    [["search", "durable"], ["read", "projects"], ["export"], ["status"], ["validate"], ["eval"], ["build"]],
    ids=lambda command: command[0],
)
def test_a_folder_without_bf_yaml_is_named_before_anything_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    brain = make(tmp_path / "brain", "brain")
    # Passing a subfolder or the parent of the brain is a common mistake; eval once blamed missing suites instead.
    for folder in (brain.root / "projects", tmp_path):
        result = CliRunner().invoke(app, [*command, "--brain", str(folder)])
        assert result.exit_code == 1, result.output
        assert f"{folder} has no bf.yaml; pass the brain's root directory" in str(result.exception)
        assert not (folder / ".bf").exists()
    # A registered brain whose bf.yaml went missing, by name or as the default brain.
    register(brain, default=True)
    brain.delete("bf.yaml")
    for arguments in ([*command, "--brain", "brain"], command):
        result = CliRunner().invoke(app, arguments)
        # Named as selected: the resolved directory never appears, as MCP errors require.
        assert "bf.yaml; pass the brain's root directory" in str(result.exception), arguments
    assert not (brain.root / ".bf").exists()
    monkeypatch.setenv("BF_BRAIN", str(brain.root / "projects"))
    with pytest.raises(Error, match=r"has no bf\.yaml"):
        execution()


def test_a_huge_integer_in_a_note_leaves_the_brain_answering(tmp_path: Path) -> None:
    team = make(tmp_path / "team", "team")
    # A hex literal escapes Python's digit limit for decimal text; printing it in a reply or the cache once crashed.
    team.write("concepts/count.md", b"---\ntype: concept\nattributes: {count: 0x" + b"f" * 3600 + b"}\n---\n# Count\n")
    found = cast(dict, search(team, Query(text="durable")))
    assert [item["ref"] for item in found["items"]] == ["projects/example.md#decision"]
    assert found["problems"] == [{"file": "concepts/count.md", "error": "invalid YAML at line 3, column 21"}]
    assert not validate(team)["valid"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_an_unreachable_registered_brain_is_not_called_absent(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    store = make(locked / "brain", "kept")
    register(store)
    locked.chmod(0)
    try:
        # As bf register refuses to free the name, selection names the access problem instead of suggesting a move.
        with pytest.raises(Error, match=r"^cannot reach the brain registered as kept; restore access"):
            select("kept")
    finally:
        locked.chmod(0o700)
    store.root.rename(tmp_path / "gone")
    with pytest.raises(Error, match=f"^registered brain kept: {re.escape(ABSENT)}"):
        select("kept")


def test_register_tells_unreachable_from_moved_brains(tmp_path: Path) -> None:
    (tmp_path / "locked").mkdir()
    locked = make(tmp_path / "locked" / "brain", "locked")
    filed = make(tmp_path / "filed", "filed")
    for store in (locked, filed):
        register(store)
    # A registered path that became a regular file no longer holds a brain: it is absent, and bf register frees it.
    filed.root.rename(tmp_path / "elsewhere")
    filed.root.write_text("")
    (tmp_path / "locked").chmod(0)
    try:
        with pytest.raises(Error, match=r"^cannot reach the brain registered as locked at"):
            register(make(tmp_path / "other", "locked"))
    finally:
        (tmp_path / "locked").chmod(0o700)
    assert register(Store(tmp_path / "elsewhere"))["replaced"] == str(filed.root)


def test_outline_and_claim_refs_stay_readable(tmp_path: Path) -> None:
    first = make(tmp_path / "first", "first")
    log = "# Log\n\n" + "".join(f"## Day {day}\n\n{'Worked on the site today. ' * 120}\n\n" for day in range(1, 29))
    first.write("concepts/log.md", log.encode())
    first.write("concepts/alice.md", b"---\ntype: person\nentity: bf://first/people/alice\n---\n# Alice\n")
    first.write("projects/site.md", b"---\ntype: project\n---\n# Site\n\n[Owner](bf://first/people/alice?rel=owner)\n")
    outline = cast("list[dict]", read(first, "bf://first/concepts/log.md")["outline"])
    assert outline[0]["ref"] == "concepts/log.md#day-1"
    assert read(first, outline[0]["ref"])["ref"] == "concepts/log.md#day-1"
    [claim] = cast("list[dict]", read(first, "bf://first/projects/site.md")["claims"])
    assert claim["target_ref"] == "concepts/alice.md"
    assert read(first, claim["target_ref"])["ref"] == "concepts/alice.md"
    # A record ref containing `#` is percent-encoded so its `bf://` address is not parsed as a section fragment.
    records.upsert(
        first,
        "github",
        [Record(id="org/repo#1", title="Issue 1", aliases=["bf://first/issues/one"])],
        snapshot=True,
    )
    first.write("projects/issue.md", b"---\ntype: project\n---\n# Issue\n\n[Owner](bf://first/issues/one?rel=owner)\n")
    [issue_claim] = cast("list[dict]", read(first, "bf://first/projects/issue.md")["claims"])
    assert issue_claim["target_ref"] == "github:org/repo#1"
    assert read(first, "bf://first/github:org/repo%231")["ref"] == "github:org/repo#1"
