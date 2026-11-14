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
from bf.models import Error, Job
from bf.storage import Store, collecting
from bf.update import update

CONFIG = b"""version: 10
name: fixture
jobs:
  mail:
    run: [echo]
    emit: records
    on: {every: 3600}
  git:
    run: [echo]
    emit: records
    on: {every: 300}
  manual:
    run: [echo]
    emit: records
  disabled:
    run: [echo]
    emit: records
    enabled: false
    on: {every: 300}
  review:
    run: [echo]
    on: {every: 86400}
"""


@pytest.fixture
def selected(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    return brain


def test_update_selects_only_named_programs_and_keeps_due_rules(selected: Store) -> None:
    preview: dict = update(selected, jobs=("mail", "manual", "disabled"), dry_run=True)
    assert [j["job"] for j in preview["jobs"]] == ["mail"]
    assert preview["manual"] == ["manual"]
    review: dict[str, Any] = update(selected, jobs=("review",), dry_run=True)
    assert [j["job"] for j in review["jobs"]] == ["review"]
    executed = []

    def fake(*args: object) -> bytes:
        executed.append(args)
        return b'[{"id":"one","title":"One"}]'

    assert update(selected, jobs=("mail",), runner=fake)["ok"]
    assert len(executed) == 1
    assert update(selected, jobs=("mail",), runner=fake)["ok"]
    assert len(executed) == 1


def test_invalid_selection_is_rejected_before_any_execution(selected: Store) -> None:
    calls = []
    with pytest.raises(Error, match="unknown job"):
        update(selected, jobs=("mail", "absent"), runner=lambda *args: (calls.append(args), b"[]")[1])
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

    def slow(_argv: list[str], job: Job, *_: object) -> bytes:
        if any(trigger.every == 3600 for trigger in job.triggers):
            entered.set()
            assert release.wait(5)
        else:
            released.append(release.is_set())
        return b"[]"

    monkeypatch.setattr("bf.update.collecting", asking)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(update, selected, jobs=("mail",), runner=slow)
        assert entered.wait(5)
        second = executor.submit(update, selected, jobs=("git",), runner=slow)
        # Once the second cycle asks for the brain, an unserialized one would reach its program well within this.
        assert queued.wait(5)
        time.sleep(0.3)
        release.set()
        reports: list[Any] = [first.result(timeout=10), second.result(timeout=10)]
    # The second cycle ran its program only after the first one finished.
    assert released == [True]
    assert [report["ok"] for report in reports] == [True, True]
    assert [report["jobs"][0]["status"] for report in reports] == ["collected", "collected"]


ASSISTANTS = b"""  ask:
    prompt: prompts/ask.md
    harness: [harness]
  reviewer:
    prompt: prompts/ask.md
    harness: [harness]
    on: {every: 3600}
  idle:
    prompt: prompts/ask.md
    harness: [harness]
    enabled: false
"""


def test_update_names_the_manual_programs_it_skips(selected: Store) -> None:
    selected.write("bf.yaml", CONFIG + ASSISTANTS)
    # A job without triggers runs only through bf run, so update names it as skipped.
    assert update(selected, dry_run=True)["manual"] == ["ask", "manual"]
    assert update(selected, jobs=("ask",), dry_run=True)["manual"] == ["ask"]
    assert "manual" not in update(selected, jobs=("reviewer", "idle"), dry_run=True)
    assert update(selected, jobs=("mail", "git", "manual", "disabled", "review"), dry_run=True)["manual"] == ["manual"]


def test_cli_selection_previews_without_executing(selected: Store) -> None:
    result = CliRunner().invoke(app, ["update", "--brain", str(selected.root), "--job", "mail", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["jobs"]) == 1


def test_job_gate_halts_prompt_without_failing_update(selected: Store) -> None:
    from bf.jobs import ExitError

    selected.write(
        "bf.yaml",
        b"version: 10\nname: fixture\njobs:\n  triage:\n    prompt: prompts/triage.md\n    harness: [agent]\n"
        b"    gate: [check-pending]\n    on: {every: 3600}\n",
    )
    selected.write("prompts/triage.md", b"Review.\n")
    calls: list[list[str]] = []

    def gated(argv: list[str], *_args: object) -> bytes:
        calls.append(argv)
        if argv == ["check-pending"]:
            raise ExitError(1, "gate exited with status 1")
        return b"should not run"

    report: dict = update(selected, jobs=("triage",), runner=gated)
    assert report["ok"] is True
    assert calls == [["check-pending"]]
    assert report["jobs"][0]["status"] == "gated"


def test_run_wakes_and_deliveries_edge_paths(brain: Store) -> None:
    import sqlite3

    from bf.config import load
    from bf.models import digest
    from bf.runtime import Outgoing, Queue
    from bf.update import run_deliveries, run_wakes

    brain.write(
        "bf.yaml",
        b"""version: 10
name: fixture
jobs:
  worker:
    run: [echo]
    emit: log
    on: {every: 3600}
  watcher:
    run: [echo]
    emit: log
    on: {daemon: true}
  off:
    run: [echo]
    emit: log
    enabled: false
  send:
    send: [send-msg]
    idempotent: true
""",
    )
    cfg, queue = load(brain), Queue(brain)
    assert run_wakes(brain, cfg, {"worker"}) == ([], False)
    assert run_deliveries(brain) == (0, False)

    w1, _ = queue.request_wake("worker", reason="first")
    w2, _ = queue.request_wake("worker", reason="second")
    w3, _ = queue.request_wake("worker", reason="third")
    queue.request_wake("worker", reason="fourth")
    queue.request_wake("watcher", reason="daemon-skipped")
    queue.request_wake("off", reason="disabled-skipped")
    # dry_run leaves wakes pending and reports them due without claiming.
    dry_res, dry_err = run_wakes(brain, cfg, {"worker"}, dry_run=True)
    assert not dry_err
    assert len(dry_res) == 4
    assert all(r["status"] == "due" for r in dry_res)

    # Wakes for deselected, disabled, or daemon jobs are skipped; claiming w2 mid-loop skips it; stopped breaks.
    stop_wakes = Event()
    wake_calls = 0

    def wake_runner(*_args: object) -> bytes:
        nonlocal wake_calls
        wake_calls += 1
        if wake_calls == 1:
            queue.claim_wake(w2)
        else:
            stop_wakes.set()
        return b""

    res_w, err_w = run_wakes(brain, cfg, {"worker", "watcher", "off"}, runner=wake_runner, stopped=stop_wakes)
    assert not err_w
    assert [r["wake_id"] for r in res_w] == [w1, w3]

    # Pre-set stopped returns cleanly for deliveries.
    d1, _ = queue.enqueue_delivery("send", Outgoing(id="d1", text="one"), ["send-msg"], idempotent=True, job="worker")
    d2, _ = queue.enqueue_delivery("send", Outgoing(id="d2", text="two"), ["send-msg"], idempotent=True, job="worker")
    stopped_deliv = Event()
    stopped_deliv.set()
    assert run_deliveries(brain, ("worker",), stopped=stopped_deliv) == (0, False)

    # First delivery succeeds and sets stop event before d2; BusyError breaks cleanly; invalid delivery fails closed.
    stop_after_one = Event()

    def deliv_runner(*_args: object) -> bytes:
        stop_after_one.set()
        return b'{"id":"r1","conversation":"c1"}'

    assert run_deliveries(brain, ("worker",), runner=deliv_runner, stopped=stop_after_one) == (1, False)
    with collecting(brain, f".delivery-{digest(d2.encode())}"):
        assert run_deliveries(brain, ("worker",)) == (0, False)
    db = sqlite3.connect(brain.root / ".bf/runtime.sqlite3")
    try:
        db.execute("UPDATE deliveries SET command='not-json' WHERE id=?", (d2,))
        db.commit()
    finally:
        db.close()
    assert run_deliveries(brain, ("worker",)) == (0, True)
    assert d1 != d2
