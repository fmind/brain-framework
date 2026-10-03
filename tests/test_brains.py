"""Brain-local discovery is portable, bounded and independent of registration."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import cast

import pytest
import yaml
from mcp.types import CallToolResult
from typer.testing import CliRunner

from bf import records
from bf.cli import app
from bf.config import ABSENT, execution, load, one, register, related, select, user_path
from bf.evaluate import evaluate
from bf.mcp import server
from bf.models import Error, Query, Record
from bf.retrieve import read, search
from bf.storage import Store
from bf.update import update
from bf.validate import validate


def make(root: Path, name: str, references: dict[str, str] | None = None) -> Store:
    root.mkdir()
    store = Store(root)
    store.write(
        "bf.yaml",
        yaml.safe_dump(
            {
                "version": 7,
                "name": name,
                "brains": {key: {"path": value} for key, value in (references or {}).items()},
            }
        ).encode(),
    )
    store.write(
        "projects/example.md",
        f"# Shared evidence\n\n{name} decision.\n\n## Decision {{#decision}}\n\n{name} durable answer.\n".encode(),
    )
    return store


def test_direct_scope_without_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = make(tmp_path / "first", "first", {"second": "../second"})
    second = make(tmp_path / "second", "second", {"third": "../third", "first": "../first"})
    make(tmp_path / "third", "third")
    # Invalid global configuration must not interfere with local discovery or explicit paths.
    user_path().parent.mkdir(parents=True, exist_ok=True)
    user_path().write_text("invalid: [")
    monkeypatch.chdir(first.root / "projects")
    assert one().root == first.root
    assert one(str(first.root)).root == first.root
    # A name resolves through the registry first, so an unreadable registry fails instead of guessing.
    with pytest.raises(Error, match="invalid YAML"):
        one("first")
    user_path().unlink()
    assert one("first").root == first.root
    assert one("second").root == second.root
    found = cast(dict, search(select(), Query(text="durable")))
    assert {item["brain"] for item in found["items"]} == {"first", "second"}
    assert "problems" not in found
    second.write(
        "memories/demo/7692c3ad3540bb803c020b3aee66cd8887123234ea0c6e7143c0add73ff431ed.json",
        b'{"id":"one","title":"Record evidence","text":"durable record"}\n',
    )
    record_results = cast(dict, search(select(), Query(text="durable", prefix="memories/demo")))
    assert len(record_results["items"]) == 1
    assert "problems" not in record_results
    assert "second durable answer" in str(read(select(), "bf://second/projects/example.md#decision")["text"])
    with pytest.raises(Error, match="brain-qualified"):
        read(select(), "projects/example.md")
    with pytest.raises(Error, match="unknown brain third"):
        read(select(), "bf://third/projects/example.md")
    cli = CliRunner().invoke(app, ["search", "durable"])
    assert cli.exit_code == 0, cli.output
    assert {item["brain"] for item in json.loads(cli.stdout)["items"]} == {"first", "second"}


def test_missing_and_mismatched_references_keep_available_answers(tmp_path: Path) -> None:
    first = make(tmp_path / "first", "first", {"missing": "../absent", "wrong": "../second"})
    make(tmp_path / "second", "second")
    found = cast(dict, search([first], Query(text="durable")))
    assert {item["brain"] for item in found["items"]} == {"first"}
    assert {(problem["brain"], problem["file"]) for problem in found["problems"]} == {("first", "bf.yaml")}
    assert {problem["error"].split(":")[0] for problem in found["problems"]} == {"brains.missing", "brains.wrong"}
    assert str(tmp_path) not in json.dumps(found)
    assert read([first], "projects/example.md")["problems"] == found["problems"]
    # Status checks the references retrieval uses: a broken one makes every answer incomplete.
    status = CliRunner().invoke(app, ["status", "--check", "--brain", str(first.root)])
    assert status.exit_code == 1
    entry = json.loads(status.stdout)["brains"][0]
    assert [(p["file"], p["error"].split(":")[0]) for p in entry["problems"]] == [
        ("bf.yaml", "brains.missing"),
        ("bf.yaml", "brains.wrong"),
    ]
    with pytest.raises(Error, match=r"referenced brain wrong is unavailable; check brains\.wrong in bf\.yaml"):
        read([first], "bf://wrong/projects/example.md")
    first.write(
        "evals/retrieval.yaml",
        b"version: 7\ncases:\n- name: incomplete\n  query: durable\n  expect: [projects/example.md]\n",
    )
    assert not evaluate(first)["passed"]


def test_conflicting_names_are_excluded_and_paths_deduplicated(tmp_path: Path) -> None:
    first = make(tmp_path / "first", "first", {"shared": "../shared"})
    second = make(tmp_path / "second", "second", {"shared": "../impostor"})
    shared = make(tmp_path / "shared", "shared")
    make(tmp_path / "impostor", "shared")
    found = cast(dict, search([first, second], Query(text="durable")))
    assert {item["brain"] for item in found["items"]} == {"first", "second"}
    assert "ambiguous brain name" in found["problems"][0]["error"]
    with pytest.raises(Error, match="brain shared is ambiguous or unavailable"):
        read([first, second], "bf://shared/projects/example.md")
    stores, problems = related([first, shared, first])
    assert {store.root for store in stores} == {first.root, shared.root}
    assert not problems


@pytest.mark.parametrize("ref", ["projects/absent.md", "repo:example/absent", "memories/absent"])
def test_missing_read_cannot_prove_absence_with_an_unavailable_brain(tmp_path: Path, ref: str) -> None:
    first = make(tmp_path / "first", "first", {"missing": "../absent"})
    first.write(
        "evals/retrieval.yaml",
        yaml.safe_dump({"version": 7, "cases": [{"name": "absent", "read": ref, "empty": True}]}).encode(),
    )
    assert not evaluate(first)["passed"]
    with pytest.raises(Error, match="incomplete"):
        read([first], ref)


def test_absolute_and_home_paths_and_no_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    first = make(tmp_path / "first", "first", {"second": "~/second", "third": str(tmp_path / "third")})
    make(tmp_path / "second", "second")
    make(tmp_path / "third", "third")
    assert len(cast(dict, search([first], Query(text="durable")))["items"]) == 3
    monkeypatch.chdir(first.root)
    # Mutation services act on one selected brain, regardless of configured references.
    report = cast(dict, update(execution(""), dry_run=True))
    assert report["brain"] == "first"


def test_init_never_writes_global_config_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "new"
    result = CliRunner().invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.output
    assert not user_path().exists()
    monkeypatch.chdir(target)
    assert search(select(), Query(text="welcome"))["items"]
    assert "direct `brains:` references" in (target / "AGENTS.md").read_text()


@pytest.mark.parametrize(
    "references",
    [
        {"first": {"path": "../first"}},
        {"second": {"path": ""}},
        {"second": {"path": " \n"}},
        {"second": {"path": "../second", "collect": True}},
        {"Upper": {"path": "../second"}},
        {f"brain-{i}": {"path": f"../{i}"} for i in range(33)},
    ],
)
def test_reference_schema_rejects_invalid_declarations(tmp_path: Path, references: dict) -> None:
    store = make(tmp_path / "first", "first")
    store.write("bf.yaml", yaml.safe_dump({"version": 7, "name": "first", "brains": references}).encode())
    with pytest.raises(Error, match=r"invalid bf\.yaml"):
        load(store)


def test_mcp_and_evaluations_share_local_scope(tmp_path: Path) -> None:
    import asyncio

    first = make(tmp_path / "first", "first", {"second": "../second"})
    second = make(tmp_path / "second", "second")
    first.write(
        "evals/retrieval.yaml",
        b"version: 7\ncases:\n- name: cross-brain\n  query: durable\n  expect: [bf://second/projects/example.md]\n",
    )
    assert evaluate(first)["passed"]
    mcp = server([first])

    async def check() -> None:
        result = await mcp.call_tool("search", {"query": "durable"})
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None
        assert {item["brain"] for item in result.structured_content["items"]} == {"first", "second"}
        result = await mcp.call_tool("read", {"ref": "bf://second/projects/example.md#decision"})
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None
        assert "second durable answer" in result.structured_content["text"]
        # A long-running host rereads direct declarations at request time.
        second.write("bf.yaml", b"version: 7\nname: changed\n")
        result = await mcp.call_tool("search", {"query": "durable"})
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None
        assert result.structured_content["problems"]

    asyncio.run(check())


def test_plain_eval_refs_name_only_the_evaluated_brain(tmp_path: Path) -> None:
    first = make(tmp_path / "first", "first", {"second": "../second"})
    make(tmp_path / "second", "second")
    first.delete("projects/example.md")
    first.write(
        "evals/retrieval.yaml",
        b"version: 7\ncases:\n"
        b"- name: own\n  query: durable\n  expect: [projects/example.md]\n"
        b"- name: referenced\n  query: durable\n  expect: [bf://second/projects/example.md]\n"
        b"- name: forbidden\n  query: durable\n  expect: [bf://second/projects/example.md]\n"
        b"  forbid: [projects/example.md]\n"
        b"- name: read\n  read: bf://second/projects/example.md\n  expect: [projects/example.md]\n",
    )
    # Before 17 the referenced brain's note at the same path passed the first case and failed the third.
    results = {case["name"]: case for case in cast("list[dict[str, object]]", evaluate(first)["cases"])}
    assert {name: case["passed"] for name, case in results.items()} == {
        "own": False,
        "referenced": True,
        "forbidden": True,
        "read": False,
    }
    assert results["own"]["rank"] == {"projects/example.md": None}
    assert results["own"]["returned"] == ["projects/example.md#decision"]


def test_registry_paths_are_absolute_and_names_win_over_local_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = make(tmp_path / "registered", "shared")
    register(registered)
    # A same-named directory below the working directory does not replace the registered brain.
    (tmp_path / "work").mkdir()
    make(tmp_path / "work" / "shared", "shared")
    monkeypatch.chdir(tmp_path / "work")
    assert [store.root for store in select("shared")] == [registered.root]
    # A relative registry path would resolve against the working directory and could select another clone.
    user_path().write_text("brains:\n  shared:\n    path: shared\n")
    with pytest.raises(Error, match="absolute"):
        select("shared")


def test_an_enclosing_brain_cannot_replace_a_registered_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registered = make(tmp_path / "registered", "shared")
    register(registered)
    # An untrusted checkout claims the owner's brain name for itself, or through a reference.
    clone = make(tmp_path / "clone", "shared")
    monkeypatch.chdir(clone.root / "projects")
    for selection in (select, execution):
        with pytest.raises(Error, match=r"ambiguous brain name shared.*--brain PATH"):
            selection("shared")
    monkeypatch.setenv("BF_BRAIN", "shared")
    with pytest.raises(Error, match="ambiguous brain name shared"):
        select()
    monkeypatch.delenv("BF_BRAIN")
    make(tmp_path / "other", "other", {"shared": "../clone"})
    monkeypatch.chdir(tmp_path / "other")
    with pytest.raises(Error, match="ambiguous brain name shared"):
        select("shared")
    # The same physical brain is no conflict, and unregistered names still resolve locally.
    monkeypatch.chdir(registered.root)
    assert execution("shared").root == registered.root
    monkeypatch.chdir(tmp_path / "other")
    assert execution("other").root == (tmp_path / "other")
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
    for selection in (select, execution):
        with pytest.raises(Error, match=r"^\.\./bf\.yaml is not a regular file owned by you; pass --brain PATH"):
            selection()
    result = CliRunner().invoke(app, ["update", "--dry-run"])
    assert result.exit_code == 1
    assert "not a regular file owned by you" in str(result.exception)
    # An explicit path remains the owner's decision.
    assert one(str(planted.root)).root == planted.root
    # A brain you do not own claims no name, so your registered name still selects your brain below it.
    assert one("planted").root == execution("planted").root == mine.root
    monkeypatch.setenv("BF_BRAIN", "planted")
    assert one().root == execution().root == mine.root


def test_registered_brains_are_searched_but_never_run_implicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    personal = make(tmp_path / "personal", "personal")
    team = make(tmp_path / "team", "team")
    team.write(
        "bf.yaml", b"version: 7\nname: team\nsensors:\n  pulled:\n    command: [sensors/pulled.sh]\n    refresh: 900\n"
    )
    for store in (personal, team):
        register(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert {item["brain"] for item in cast(dict, search(select(), Query(text="durable")))["items"]} == {
        "personal",
        "team",
    }
    for command in (["update", "--dry-run"], ["collect", "pulled", "--dry-run"], ["schedule"], ["watch", "--json"]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 1, command
        assert "registered brains are selected for retrieval only" in str(result.exception)
    result = CliRunner().invoke(app, ["update", "--dry-run", "--brain", "team"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["brain"] == "team"


def test_execution_names_only_registered_or_enclosing_brains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    main = make(tmp_path / "main", "main", {"team": "../team"})
    team = make(tmp_path / "team", "team")
    marker = tmp_path / "ran"
    team.write("sensors/s.sh", f"#!/bin/sh\ntouch '{marker}'\n".encode())
    (team.root / "sensors/s.sh").chmod(0o700)
    team.write("bf.yaml", b"version: 7\nname: team\nsensors:\n  s:\n    command: [sensors/s.sh]\n    refresh: 60\n")
    monkeypatch.chdir(main.root)
    # A reference is readable by name, but its programs never run through that name.
    assert one("team").root == team.root
    for command in (["update", "--dry-run", "--brain", "team"], ["collect", "s", "--brain", "team"]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 1, command
        assert "brain team is neither registered nor the enclosing brain; pass --brain PATH" in str(result.exception)
    monkeypatch.setenv("BF_BRAIN", "team")
    with pytest.raises(Error, match="neither registered nor the enclosing brain"):
        execution()
    assert not marker.exists()
    monkeypatch.delenv("BF_BRAIN")
    # A directory below the working directory is not checked for ownership, so it needs a deliberate path.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Error, match="neither registered nor the enclosing brain"):
        execution("team")
    assert one("team").root == execution("./team").root == team.root
    monkeypatch.chdir(main.root / "projects")
    assert execution("main").root == main.root


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
    # A same-named reference that cannot be loaded might name another directory: selection names it.
    make(tmp_path / "broken", "broken", {"team": "../missing"})
    monkeypatch.chdir(tmp_path / "broken")
    for selection in (one, execution):
        with pytest.raises(Error, match=r"^the enclosing bf\.yaml declares brains\.team, which is missing"):
            selection("team")


def test_absent_registered_brains_make_answers_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    present = make(tmp_path / "present", "present")
    moved = make(tmp_path / "moved", "moved")
    for store in (present, moved):
        register(store)
    moved.root.rename(tmp_path / "elsewhere")
    monkeypatch.chdir(tmp_path)
    absent = {"brain": "moved", "error": ABSENT}
    found = cast(dict, search(select(), Query(text="durable")))
    assert {item["brain"] for item in found["items"]} == {"present"}
    assert found["problems"] == [absent]
    assert read(select(), "projects")["problems"] == [absent]
    result = CliRunner().invoke(app, ["status", "--check"])
    assert result.exit_code == 1
    assert absent in json.loads(result.stdout)["brains"]
    # With every registered brain absent, the error names them instead of suggesting registration.
    present.root.rename(tmp_path / "away")
    with pytest.raises(Error, match="registered brains are absent on this machine: moved, present; restore them"):
        select()


def test_a_newer_referenced_format_is_named(tmp_path: Path) -> None:
    first = make(tmp_path / "first", "first", {"team": "../team"})
    team = make(tmp_path / "team", "team")
    team.write("bf.yaml", b"version: 8\nname: team\n")
    found = cast(dict, search([first], Query(text="durable")))
    assert found["problems"] == [
        {
            "brain": "first",
            "file": "bf.yaml",
            "error": "brains.team: bf.yaml declares version 8; this release reads version: 7",
        }
    ]


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
    # A registered brain whose bf.yaml went missing, by name or as the only registered brain.
    register(brain)
    brain.delete("bf.yaml")
    for arguments in ([*command, "--brain", "brain"], command):
        result = CliRunner().invoke(app, arguments)
        # Named as selected: the resolved directory never appears, as MCP errors require.
        assert str(result.exception).startswith("registered brain brain has no bf.yaml"), arguments
    assert not (brain.root / ".bf").exists()
    monkeypatch.setenv("BF_BRAIN", str(brain.root / "projects"))
    with pytest.raises(Error, match=r"has no bf\.yaml"):
        execution()


def test_a_huge_integer_in_a_referenced_note_leaves_every_brain_answering(tmp_path: Path) -> None:
    first = make(tmp_path / "first", "first", {"team": "../team"})
    team = make(tmp_path / "team", "team")
    team.write("bf.yaml", b"version: 7\nname: team\nfields:\n  count:\n    description: A count.\n    type: integer\n")
    # A hex literal escapes Python's digit limit for decimal text; printing it in a reply or the cache once crashed.
    team.write("concepts/count.md", b"---\ntype: concept\nfields: {count: 0x" + b"f" * 3600 + b"}\n---\n# Count\n")
    found = cast(dict, search([first], Query(text="durable")))
    assert {item["brain"] for item in found["items"]} == {"first", "team"}
    assert found["problems"] == [
        {"brain": "team", "file": "concepts/count.md", "error": "invalid YAML at line 3, column 17"}
    ]
    assert "team durable answer" in str(read([first], "bf://team/projects/example.md")["text"])
    # Validation once passed the brain, since the value is an integer.
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
    with pytest.raises(Error, match=f"^kept: {re.escape(ABSENT)}"):
        select("kept")


def test_an_exact_read_answers_while_another_brain_fails(tmp_path: Path) -> None:
    first = make(tmp_path / "first", "first", {"second": "../second"})
    second = make(tmp_path / "second", "second")
    records.upsert(first, "github", [Record(id="org/repo#12", title="Merged fix")], snapshot=False)
    records.upsert(second, "github", [Record(id="org/repo#7", title="Other")], snapshot=False)
    # A malformed record keeps the second brain from proving it holds no org/repo#12.
    second.write("memories/github/broken.json", b"{not json")
    with pytest.raises(Error):
        read([second], "github:org/repo#12")
    reply = cast(dict, read([first], "github:org/repo#12"))
    assert reply["record"]["title"] == "Merged fix"
    assert {problem["brain"] for problem in reply["problems"]} == {"second"}
    assert str(second.root) not in json.dumps(reply)
    # When no brain holds the ref, the failure stands instead of an absence the read cannot prove.
    with pytest.raises(Error):
        read([first], "github:org/repo#99")
