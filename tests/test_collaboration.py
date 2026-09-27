"""Independent contributions merge; competing evidence stays visible and cannot pass validation."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from bf import records
from bf.collect import routine
from bf.models import Error, Query, Record
from bf.retrieve import search
from bf.storage import Store, writer
from bf.validate import validate


def test_two_git_writers_keep_independent_records_and_actions() -> None:
    script = Path(__file__).parents[1] / "examples/team/demo.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=True, timeout=30)  # noqa: S603
    assert json.loads(result.stdout) == {
        "independent_merge": "clean",
        "records": 3,
        "actions": 2,
        "same_record_merge": "conflict",
        "conflict_rejected": True,
    }


@pytest.mark.parametrize("marker", ["<<<<<<< ours", "||||||| base", ">>>>>>> theirs", "<<<<<<<< ours"])
@pytest.mark.parametrize("path", ["projects/conflict.md", "actions/2026-09-27_review/inputs/conflict.md"])
def test_conflicted_markdown_is_invalid_and_never_indexed(brain: Store, marker: str, path: str) -> None:
    brain.write(path, f"---\ntype: project\nstatus: draft\n---\n# Decision\n\n{marker}\nconflictneedle\n".encode())
    report = validate(brain)
    assert not report["valid"]
    assert any(f"{path}:7: unresolved merge conflict" in problem for problem in cast("list[str]", report["problems"]))
    reply = search([brain], Query(text="conflictneedle"))
    assert not reply["items"]
    assert reply["problems"]


def test_record_filename_survives_rescheduling_and_input_order(brain: Store) -> None:
    items = [
        Record(id="https://example.test/item?a=1/../../x", title="One", time="2026-01-01T00:00:00Z"),
        Record(id="two", title="Two"),
    ]
    with writer(brain):
        records.upsert(brain, "team", items, snapshot=True)
        before = {name: brain.fingerprint(name) for name in records.files(brain, "team")}
        records.upsert(brain, "team", list(reversed(items)), snapshot=True)
        assert {name: brain.fingerprint(name) for name in before} == before
        changed = items[0].model_copy(update={"time": "2026-02-01T00:00:00.000000Z"})
        records.upsert(brain, "team", [changed], snapshot=False)
    assert set(records.files(brain, "team")) == set(before)
    assert brain.fingerprint(records.path("team", "two")) == before[records.path("team", "two")]


def test_obsolete_storage_and_duplicate_inputs_fail_without_writes(brain: Store) -> None:
    old = "memories/team/2026-09.jsonl"
    brain.write(old, b'{"id":"old","title":"Original"}\n')
    with pytest.raises(Error, match="manual upgrade"):
        records.upsert(brain, "team", [Record(id="new", title="New")], snapshot=False)
    assert records.files(brain, "team") == [old]
    with pytest.raises(Error, match="duplicate incoming"):
        records.upsert(brain, "other", [Record(id="x", title="A"), Record(id="x", title="B")], snapshot=False)
    assert not records.files(brain, "other")


def test_same_routine_on_two_clones_creates_distinct_actions(tmp_path: Path) -> None:
    actions = []
    now = datetime(2026, 9, 27, tzinfo=UTC)
    for clone in ("alice", "bob"):
        root = tmp_path / clone
        root.mkdir()
        store = Store(root)
        store.write("bf.yaml", b"name: team\nroutines:\n  review:\n    command: [fake]\n")
        result = routine(
            store,
            "review",
            start="2026-09-26T00:00:00Z",
            end="2026-09-27T00:00:00Z",
            runner=lambda *_: b"---\ntype: action\nstatus: draft\n---\n# Review\n",
            clock=lambda: now,
        )
        actions.append(result["action"])
    assert len(set(actions)) == 2
    assert all(str(path).startswith("actions/2026-09-27_review-") for path in actions)


def test_action_helper_creates_two_sessions_without_replacing_work(brain: Store) -> None:
    script = Path(__file__).parents[1] / "skills/bf-action/scripts/new-action.py"
    created = []
    for _ in range(2):
        reply = subprocess.run(  # noqa: S603 - execute only the bundled helper on a synthetic brain
            [sys.executable, str(script), "review", "--brain", str(brain.root)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        created.append(json.loads(reply.stdout)["action"])
    assert len(set(created)) == 2
    assert validate(brain)["valid"]
    assert all(brain.read(path).startswith(b"---\ntype: action") for path in created)


def test_action_helper_refuses_redirected_actions(brain: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (brain.root / "actions").symlink_to(outside, target_is_directory=True)
    script = Path(__file__).parents[1] / "skills/bf-action/scripts/new-action.py"
    reply = subprocess.run(  # noqa: S603 - execute only the bundled helper on a synthetic brain
        [sys.executable, str(script), "review", "--brain", str(brain.root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert reply.returncode == 1
    assert not list(outside.iterdir())
