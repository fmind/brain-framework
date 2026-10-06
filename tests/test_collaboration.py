"""Independent contributions merge; competing evidence stays visible and cannot pass validation."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest

from bf import records
from bf.collect import routine
from bf.models import Error, Query, Record
from bf.retrieve import search
from bf.storage import Store, writer
from bf.validate import validate


@pytest.mark.parametrize("marker", ["<<<<<<< ours", "||||||| base", ">>>>>>> theirs", "<<<<<<<< ours"])
@pytest.mark.parametrize("path", ["projects/conflict.md", "actions/2026-09-27_review/inputs/conflict.md"])
def test_conflicted_markdown_is_invalid_and_never_indexed(brain: Store, marker: str, path: str) -> None:
    brain.write(path, f"---\ntype: project\nstatus: draft\n---\n# Decision\n\n{marker}\nconflictneedle\n".encode())
    report = validate(brain)
    assert not report["valid"]
    assert any(
        problem["file"] == path and problem["error"].startswith("line 7: unresolved merge conflict")
        for problem in cast("list[dict[str, str]]", report["problems"])
    )
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


def test_stray_memory_files_stay_visible_and_duplicate_inputs_fail_without_writes(brain: Store) -> None:
    # Collection and search read only <sha256-id>.json record files; validation reports other visible files.
    stray = "memories/team/export.csv"
    brain.write(stray, b'{"id":"old","title":"Original"}\n')
    brain.write("memories/team/.DS_Store", b"operating-system metadata")
    for snapshot in (False, True):
        records.upsert(brain, "team", [Record(id="new", title="New")], snapshot=snapshot)
    assert records.files(brain, "team") == [records.path("team", "new")]
    assert brain.read(stray) == b'{"id":"old","title":"Original"}\n'
    assert validate(brain)["problems"] == [{"file": stray, "error": "expected memories/<source>/<sha256-id>.json"}]
    with pytest.raises(Error, match="duplicate incoming"):
        records.upsert(brain, "other", [Record(id="x", title="A"), Record(id="x", title="B")], snapshot=False)
    assert not records.files(brain, "other")


def test_hidden_record_like_files_are_reported_wherever_they_are_read(brain: Store) -> None:
    # A macOS AppleDouble file ends in .json: collection and search read it, so validation reports it too.
    hidden = "memories/team/._" + records.path("team", "x").rsplit("/", 1)[1]
    brain.write(hidden, b"\x00\x05\x16\x07")
    expected = "expected memories/<source>/<sha256-id>.json"
    for problems in (validate(brain)["problems"], search([brain], Query(text="durable"))["problems"]):
        assert hidden in json.dumps(problems)
        assert expected in json.dumps(problems)
    with pytest.raises(Error, match=expected):
        records.upsert(brain, "team", [Record(id="new", title="New")], snapshot=True)
    assert records.files(brain, "team") == [hidden]


def test_same_routine_on_two_clones_creates_distinct_actions(tmp_path: Path) -> None:
    actions = []
    now = datetime(2026, 9, 27, tzinfo=UTC)
    for clone in ("alice", "bob"):
        root = tmp_path / clone
        root.mkdir()
        store = Store(root)
        store.write(
            "bf.yaml", b"version: 7\nname: team\nroutines:\n  review:\n    command: [fake]\n    output: action\n"
        )
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
    script = Path(__file__).parents[1] / "src/bf/skills/bf-action/scripts/new-action.py"
    created = []
    for options in ((), (), ("--unique",)):
        reply = subprocess.run(  # noqa: S603 - execute only the bundled helper on a synthetic brain
            [sys.executable, str(script), "review", "--brain", str(brain.root), *options],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        created.append(json.loads(reply.stdout)["action"])
    # The first session takes the plain name; a later one on the same day, or --unique, adds a suffix.
    today = date.today().isoformat()
    assert created[0] == f"actions/{today}_review/ACTION.md"
    for path in created[1:]:
        assert re.fullmatch(rf"actions/{today}_review-[0-9a-f]{{12}}/ACTION\.md", path)
    assert len(set(created)) == 3
    assert validate(brain)["valid"]
    assert all(brain.read(path).startswith(b"---\ntype: action") for path in created)


def test_a_session_named_after_a_routine_never_counts_as_its_action(brain: Store) -> None:
    # A routine skips a day that already holds its own action; a person's suffixed session must not be taken for one.
    brain.write(
        "bf.yaml", b"version: 7\nname: fixture\nroutines:\n  review:\n    command: [fake]\n    output: action\n"
    )
    script = Path(__file__).parents[1] / "src/bf/skills/bf-action/scripts/new-action.py"
    reply = subprocess.run(  # noqa: S603 - execute only the bundled helper on a synthetic brain
        [sys.executable, str(script), "review", "--brain", str(brain.root), "--unique"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    session = json.loads(reply.stdout)["action"]
    before = brain.read(session)
    day = date.fromisoformat(session.removeprefix("actions/")[:10])
    result = routine(
        brain,
        "review",
        start="2026-09-26T00:00:00Z",
        end="2026-09-27T00:00:00Z",
        runner=lambda *_: b"---\ntype: action\nstatus: draft\n---\n# Review\n",
        clock=lambda: datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
    )
    assert "skipped" not in result
    assert re.fullmatch(rf"actions/{day}_review-[0-9a-f]{{8}}/ACTION\.md", str(result["action"]))
    assert brain.read(str(result["action"])).startswith(b"---\ntype: action")
    assert brain.read(session) == before


def test_action_helper_refuses_redirected_actions(brain: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (brain.root / "actions").symlink_to(outside, target_is_directory=True)
    script = Path(__file__).parents[1] / "src/bf/skills/bf-action/scripts/new-action.py"
    reply = subprocess.run(  # noqa: S603 - execute only the bundled helper on a synthetic brain
        [sys.executable, str(script), "review", "--brain", str(brain.root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert reply.returncode == 1
    assert not list(outside.iterdir())


def test_action_helper_follows_a_linked_brain_root_only(brain: Store, tmp_path: Path) -> None:
    # bf itself accepts a linked root, such as ~/brain pointing at a synced folder; links below it stay refused.
    linked = tmp_path / "linked-brain"
    linked.symlink_to(brain.root, target_is_directory=True)
    script = Path(__file__).parents[1] / "src/bf/skills/bf-action/scripts/new-action.py"
    reply = subprocess.run(  # noqa: S603 - execute only the bundled helper on a synthetic brain
        [sys.executable, str(script), "review", "--brain", str(linked)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert reply.returncode == 0, reply.stderr
    action = json.loads(reply.stdout)["action"]
    assert (brain.root / action).is_file()
