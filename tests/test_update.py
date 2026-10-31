"""Update cycles run only the selected due programs, one cycle of a brain at a time."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import pytest
from typer.testing import CliRunner

from bf.cli import app
from bf.models import Command, Error, Program
from bf.storage import Store, collecting
from bf.update import update

CONFIG = b"""version: 8
name: fixture
sensors:
  mail:
    command: [echo]
    refresh: 3600
  git:
    command: [echo]
    refresh: 300
  manual:
    command: [echo]
  disabled:
    command: [echo]
    refresh: 300
    enabled: false
routines:
  review:
    command: [echo]
    refresh: 86400
"""


@pytest.fixture
def selected(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    return brain


def test_update_selects_only_named_programs_and_keeps_due_rules(selected: Store) -> None:
    preview: dict = update(selected, sensors=("mail", "manual", "disabled"), dry_run=True)
    assert [s["sensor"] for s in preview["sensors"]] == ["mail"]
    assert preview["routines"] == []
    preview: dict = update(selected, routines=("review",), dry_run=True)
    assert preview["sensors"] == []
    assert [r["routine"] for r in preview["routines"]] == ["review"]
    executed = []

    def fake(*args: object) -> bytes:
        executed.append(args)
        return b'[{"id":"one","title":"One"}]'

    assert update(selected, sensors=("mail",), runner=fake)["ok"]
    assert len(executed) == 1
    assert update(selected, sensors=("mail",), runner=fake)["ok"]
    assert len(executed) == 1


def test_invalid_selection_is_rejected_before_any_execution(selected: Store) -> None:
    calls = []
    with pytest.raises(Error, match="unknown sensor"):
        update(selected, sensors=("mail", "absent"), runner=lambda *args: (calls.append(args), b"[]")[1])
    assert not calls
    # Like a busy collect, a cycle that cannot start fails instead of reporting an empty run.
    with collecting(selected, ".update"), pytest.raises(Error, match="another update is still active"):
        update(selected, wait=0.1)


def test_updates_of_other_selections_queue_instead_of_failing(selected: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # Timers for different selections can fire at the same minute; a second cycle waits for the first, then runs its
    # own due list.
    entered, release, queued = Event(), Event(), Event()
    released: list[bool] = []
    lock = collecting

    def asking(*args: Any, **kwargs: Any) -> Any:
        if entered.is_set():
            queued.set()
        return lock(*args, **kwargs)

    def slow(_argv: list[str], program: Command, *_: object) -> bytes:
        if isinstance(program, Program) and program.refresh == 3600:
            entered.set()
            assert release.wait(5)
        else:
            released.append(release.is_set())
        return b"[]"

    monkeypatch.setattr("bf.update.collecting", asking)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(update, selected, sensors=("mail",), runner=slow)
        assert entered.wait(5)
        second = executor.submit(update, selected, sensors=("git",), runner=slow)
        # Once the second cycle asks for the brain, an unserialized one would reach its program well within this.
        assert queued.wait(5)
        time.sleep(0.3)
        release.set()
        reports: list[Any] = [first.result(timeout=10), second.result(timeout=10)]
    # The second cycle ran its program only after the first one finished.
    assert released == [True]
    assert [report["ok"] for report in reports] == [True, True]
    assert [report["sensors"][0]["status"] for report in reports] == ["collected", "collected"]


ASSISTANTS = b"""assistants:
  ask:
    prompt: prompts/ask.md
    command: [harness]
  reviewer:
    prompt: prompts/ask.md
    command: [harness]
    schedules:
      review: {every: 3600}
  idle:
    prompt: prompts/ask.md
    command: [harness]
    enabled: false
"""


def test_update_names_the_manual_programs_it_skips(selected: Store) -> None:
    selected.write("bf.yaml", CONFIG + ASSISTANTS)
    # An assistant without schedules, inputs or triggers runs only through bf run, so update names it as skipped.
    assert update(selected, dry_run=True)["manual"] == ["ask", "manual"]
    assert update(selected, assistants=("ask",), dry_run=True)["manual"] == ["ask"]
    assert "manual" not in update(selected, assistants=("reviewer", "idle"), dry_run=True)
    # Watch's collection job runs no assistant, so it names none.
    assert update(selected, kinds=frozenset({"sensor", "routine"}), dry_run=True)["manual"] == ["manual"]


def test_cli_selection_previews_without_executing(selected: Store) -> None:
    result = CliRunner().invoke(app, ["update", "--brain", str(selected.root), "--sensor", "mail", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["sensors"]) == 1
