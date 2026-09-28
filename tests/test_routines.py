"""Routines are deterministic programs whose Markdown becomes the day's action; failures write nothing."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import bf.collect as collector
from bf import pages
from bf.cli import app
from bf.collect import Runner, due_routines, routine
from bf.health import attention, routine_health
from bf.history import ROUTINES, log_path, state
from bf.models import Config, Error, Program
from bf.retrieve import read
from bf.storage import Store
from bf.update import update

NOW = datetime(2026, 9, 25, 8, tzinfo=UTC)
START = "2026-09-24T08:00:00.000000Z"
END = "2026-09-25T08:00:00.000000Z"
CONFIG = b"""version: 6
name: fixture
routines:
  digest:
    command: [routines/digest.py, "{{brain}}", "{{start}}", "{{end}}"]
    refresh: 86400
  manual:
    command: [echo]
  paused:
    command: [echo]
    enabled: false
    refresh: 60
"""
ACTION = b"---\ntype: action\nstatus: draft\n---\n# Digest\n\nSee [offline](../../projects/offline.md).\n"
FOLDER = f"actions/{NOW.astimezone().date().isoformat()}_digest-00000000000000000000000000000001"


@pytest.fixture
def configured(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    return brain


def printing(output: bytes, calls: list[list[str]] | None = None) -> Runner:
    def runner(argv: list[str], program: Program, _store: Store, _log: Path) -> bytes:
        assert program.max_bytes == 1 << 20
        if calls is not None:
            calls.append(argv)
        return output

    return runner


def test_configuration_names_routines_like_action_slugs() -> None:
    assert Config.model_validate(
        {"version": 6, "name": "b", "routines": {"weekly-review": {"command": ["x"]}}}
    ).routines
    for name in ("Weekly", "weekly-", "a--b", "1st"):
        with pytest.raises(ValidationError):
            Config.model_validate({"version": 6, "name": "b", "routines": {name: {"command": ["x"]}}})
    with pytest.raises(ValidationError, match="distinct"):
        Config.model_validate(
            {"version": 6, "name": "b", "sensors": {"x": {"command": ["x"]}}, "routines": {"x": {"command": ["x"]}}}
        )
    for settings in ({"command": ["x"], "max_bytes": 5 << 20}, {"command": ["x", "{{secret}}"]}, {"command": []}):
        with pytest.raises(ValidationError):
            Config.model_validate({"version": 6, "name": "b", "routines": {"digest": settings}})


def test_a_routine_writes_one_action_per_day_after_success(configured: Store) -> None:
    calls: list[list[str]] = []
    preview = routine(
        configured, "digest", start=START, end=END, runner=printing(ACTION, calls), dry_run=True, clock=lambda: NOW
    )
    assert preview == {"routine": "digest", "action": f"{FOLDER}/ACTION.md", "text": ACTION.decode()}
    assert calls == [["routines/digest.py", str(configured.root), START, END]]
    assert not configured.files("actions")
    assert state(configured, ROUTINES) == {}
    result = routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)
    assert result == {"routine": "digest", "action": f"{FOLDER}/ACTION.md"}
    assert configured.read(f"{FOLDER}/ACTION.md") == ACTION
    assert state(configured, ROUTINES)["digest"] == {
        "run": END,
        "success": END,
        "start": START,
        "end": END,
        "error": "",
        "failures": 0,
        "action": f"{FOLDER}/ACTION.md",
    }
    # People may already be working on today's action: a second run never replaces it.
    configured.write(f"{FOLDER}/ACTION.md", b"# Digest\n\nReviewed by a person.\n")
    again = routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)
    assert again == {"routine": "digest", "skipped": "an action for this routine already exists today"}
    assert configured.read(f"{FOLDER}/ACTION.md") == b"# Digest\n\nReviewed by a person.\n"
    assert refs(read([configured], "actions")) == [f"{FOLDER}/ACTION.md"]


def test_an_editor_lock_in_todays_action_skips_the_rerun_instead_of_failing(configured: Store, tmp_path: Path) -> None:
    routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)
    # Emacs keeps a dangling `.#NAME` link beside a file while its buffer has unsaved edits.
    (configured.root / FOLDER / ".#ACTION.md").symlink_to("owner@host.12345:1790000000")
    skipped = {"routine": "digest", "skipped": "an action for this routine already exists today"}
    assert routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW) == skipped
    # Someone is still editing there: nothing is written beside the lock, even once ACTION.md is gone.
    configured.delete(f"{FOLDER}/ACTION.md")
    assert routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW) == skipped
    assert not (configured.root / FOLDER / "ACTION.md").exists()
    # A linked actions/ folder still fails the run by name.
    shutil.rmtree(configured.root / "actions")
    (tmp_path / "elsewhere").mkdir()
    (configured.root / "actions").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(Error, match="actions: expected a directory"):
        routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)


def refs(reply: dict[str, object]) -> list[str]:
    return [str(item["ref"]) for item in cast("list[dict[str, object]]", reply["items"])]


def test_empty_output_means_nothing_to_review(configured: Store) -> None:
    assert routine(configured, "digest", start=START, end=END, runner=printing(b" \n"), clock=lambda: NOW) == {
        "routine": "digest"
    }
    assert not configured.files("actions")
    assert state(configured, ROUTINES)["digest"]["success"] == END


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"---\nupdated: soon\n---\n# Bad\n", "invalid frontmatter"),
        (b"# Missing metadata\n", "nonempty type"),
        (b"---\ntype: action\nstatus: wip\n---\n# Bad\n", "OKF status"),
        (b"---\ntype: action\nsources: [https://example.com]\n---\n# Bad\n", "sources require mappings"),
        (b"---\ntype: action\nverified: [{by: human:owner}]\n---\n# Bad\n", "require by and at"),
        (b"---\nunclosed\n", "unclosed frontmatter"),
        (b"\xff\xfe", "UTF-8"),
        (b"# Bad\n\n[x](bf://fixture/projects/offline.md?rel=undeclared)\n", "declare an identity relationship"),
        (b"---\nentity: bf://other/people/x\n---\n# Foreign\n", "own brain namespace"),
        (b"---\naliases: [bf://other/people/x]\n---\n# Foreign\n", "own brain namespace"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "preview"])
def test_invalid_output_writes_nothing_and_records_the_error(
    configured: Store, output: bytes, message: str, dry_run: bool
) -> None:
    with pytest.raises(Error, match=message) as raised:
        routine(configured, "digest", start=START, end=END, runner=printing(output), clock=lambda: NOW, dry_run=dry_run)
    assert str(log_path(configured, "digest")) in str(raised.value)
    assert not configured.files("actions")
    if dry_run:
        # A failed preview saves no run history, so the routine neither fails nor becomes due.
        assert state(configured, ROUTINES) == {}
    else:
        assert message in str(state(configured, ROUTINES)["digest"]["error"])
        assert "success" not in state(configured, ROUTINES)["digest"]


def test_routines_require_enabled_programs_and_valid_windows(configured: Store, tmp_path: Path) -> None:
    for name, start, end, message in [
        ("absent", START, END, "unknown or disabled"),
        ("paused", START, END, "unknown or disabled"),
        ("digest", END, START, "earlier"),
        ("digest", "2026-09-24", END, "timezone"),
    ]:
        with pytest.raises(Error, match=message):
            routine(configured, name, start=start, end=end, runner=printing(ACTION))
    clone = tmp_path / "clone"
    clone.mkdir()

    shared = Store(clone)
    shared.write("bf.yaml", CONFIG.replace(b"name: fixture", b"name: clone"))
    assert routine(shared, "digest", start=START, end=END, runner=printing(ACTION))["action"]


def test_due_routines_cover_the_time_since_their_last_reviewed_window(configured: Store) -> None:
    assert due_routines(configured, NOW) == [("digest", START, END)]
    routine(configured, "digest", start=START, end=END, runner=printing(b""), clock=lambda: NOW)
    assert due_routines(configured, NOW + timedelta(hours=1)) == []
    later = NOW + timedelta(days=1)
    assert due_routines(configured, later) == [("digest", END, "2026-09-26T08:00:00.000000Z")]


def test_failed_routines_back_off_until_a_success(configured: Store) -> None:
    def failing(*_: object) -> bytes:
        raise Error("synthetic failure")

    def due_at(when: datetime) -> bool:
        return any(window[0] == "digest" for window in due_routines(configured, when))

    retry = NOW
    for delay in (timedelta(minutes=1), timedelta(minutes=2)):
        with pytest.raises(Error, match="synthetic failure"):
            routine(configured, "digest", start=START, end=END, runner=failing, clock=lambda at=retry: at)
        assert not due_at(retry + delay - timedelta(seconds=1))
        retry += delay
        assert due_at(retry)
    assert state(configured, ROUTINES)["digest"]["failures"] == 2
    routine(configured, "digest", start=START, end=END, runner=printing(b""), clock=lambda: retry)
    assert not due_at(retry + timedelta(days=1) - timedelta(seconds=1))
    assert due_at(retry + timedelta(days=1))


def test_a_skipped_review_keeps_its_window_for_the_next_action(configured: Store) -> None:
    configured.write("bf.yaml", CONFIG.replace(b"refresh: 86400", b"refresh: 3600"))
    routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)
    hour = NOW + timedelta(hours=1)
    ((_, start, end),) = [window for window in due_routines(configured, hour) if window[0] == "digest"]
    assert start == END
    skipped = routine(configured, "digest", start=start, end=end, runner=printing(ACTION), clock=lambda: hour)
    assert "skipped" in skipped
    # The hour whose output was discarded stays in the next window, which a later action covers.
    ((_, start, _),) = [w for w in due_routines(configured, NOW + timedelta(days=1)) if w[0] == "digest"]
    assert start == END


def test_update_runs_routines_after_sensors_and_isolates_failures(configured: Store) -> None:
    configured.write(
        "bf.yaml",
        CONFIG
        + b"  broken:\n    command: [broken]\n    refresh: 60\n"
        + b"sensors:\n  mail:\n    command: [mail]\n    refresh: 60\n",
    )
    order: list[str] = []

    def runner(argv: list[str], _program: Program, _store: Store, _log: Path) -> bytes:
        order.append(argv[0])
        if argv[0] == "broken":
            return b"---\nupdated: soon\n---\n"
        return b"[]" if argv[0] == "mail" else ACTION

    dry = cast("Any", update(configured, dry_run=True, now=NOW))
    assert [r["status"] for r in dry["routines"]] == ["due", "due"]
    assert order == []
    report = cast("Any", update(configured, now=NOW, runner=runner))
    assert not report["ok"]
    assert report["sensors"][0]["status"] == "collected"
    assert [(r["routine"], r["status"]) for r in report["routines"]] == [("broken", "failed"), ("digest", "ran")]
    assert order == ["mail", "broken", "routines/digest.py"]
    assert configured.read(f"{FOLDER}/ACTION.md") == ACTION
    assert report["index"]["skipped"] == 0
    # A later run the same day keeps the existing action and says so.
    configured.write("bf.yaml", configured.read("bf.yaml").replace(b"refresh: 86400", b"refresh: 60"))
    later = cast("Any", update(configured, now=NOW + timedelta(minutes=5), runner=runner))
    assert ("digest", "skipped") in [(r["routine"], r["status"]) for r in later["routines"]]
    assert state(configured, ROUTINES)["digest"]["action"] == f"{FOLDER}/ACTION.md"


def test_status_and_home_report_failing_routines(configured: Store) -> None:
    with pytest.raises(Error):
        routine(configured, "digest", start=START, end=END, runner=printing(b"\xff"), clock=lambda: NOW)
    health = routine_health(configured, now=NOW)
    assert health["digest"] == {
        "state": "active",
        "freshness": "never",
        "failed": True,
        "error": "routine must print UTF-8 Markdown",
        "failures": 1,
        "log": str(log_path(configured, "digest")),
    }
    assert health["manual"] == {"state": "active", "freshness": "manual"}
    assert health["paused"] == {"state": "disabled", "freshness": "unknown"}
    assert attention(configured, NOW) == [{"routine": "digest", "freshness": "never", "failed": True}]
    assert pages.home([configured], NOW)["attention"] == [
        {"brain": "fixture", "routine": "digest", "freshness": "never", "failed": True}
    ]
    result = CliRunner().invoke(app, ["status", "--check", "--brain", str(configured.root)])
    assert result.exit_code == 1


def test_routine_executables_run_from_the_brain_root(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    configured.write(
        "routines/digest.py",
        b'#!/bin/sh\nprintf -- \'---\\ntype: action\\nstatus: draft\\n---\\n# Digest %s\\n\' "$(basename "$PWD")"\n',
    )
    (configured.root / "routines/digest.py").chmod(0o700)
    result = update(configured, now=NOW)
    assert result["ok"], result
    assert configured.read(f"{FOLDER}/ACTION.md").decode().endswith(f"# Digest {configured.root.name}\n")
    configured.write("bf.yaml", CONFIG.replace(b"routines/digest.py", b"routines/../bf.yaml"))
    with pytest.raises(Error):
        routine(configured, "digest", start=START, end=END, clock=lambda: NOW + timedelta(days=1))


@pytest.fixture(autouse=True)
def fixed_action_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "uuid4", lambda: UUID(int=1))
