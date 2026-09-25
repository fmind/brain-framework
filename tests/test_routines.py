"""Routines are trusted, deterministic programs whose Markdown becomes the day's action; failures write nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bf import pages
from bf.cli import app
from bf.collect import ROUTINES, Runner, due_routines, log_path, routine, state
from bf.config import register
from bf.health import attention, routine_health
from bf.models import Config, Error, Program
from bf.retrieve import read
from bf.storage import Store
from bf.update import update

NOW = datetime(2026, 9, 25, 8, tzinfo=UTC)
START = "2026-09-24T08:00:00.000000Z"
END = "2026-09-25T08:00:00.000000Z"
CONFIG = b"""version: 5
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
ACTION = b"---\nstatus: active\n---\n# Digest\n\nSee [offline](../../projects/offline.md).\n"
FOLDER = f"actions/{NOW.astimezone().date().isoformat()}_digest"


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
    assert Config.model_validate({"name": "b", "routines": {"weekly-review": {"command": ["x"]}}}).routines
    for name in ("Weekly", "weekly-", "a--b", "1st"):
        with pytest.raises(ValidationError):
            Config.model_validate({"name": "b", "routines": {name: {"command": ["x"]}}})
    with pytest.raises(ValidationError, match="distinct"):
        Config.model_validate(
            {"name": "b", "sensors": {"x": {"command": ["x"]}}, "routines": {"x": {"command": ["x"]}}}
        )
    for settings in ({"command": ["x"], "max_bytes": 5 << 20}, {"command": ["x", "{{secret}}"]}, {"command": []}):
        with pytest.raises(ValidationError):
            Config.model_validate({"name": "b", "routines": {"digest": settings}})


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
        "run": NOW.isoformat(),
        "success": NOW.isoformat(),
        "start": START,
        "end": END,
        "error": "",
        "action": f"{FOLDER}/ACTION.md",
    }
    # People may already be working on today's action: a second run never replaces it.
    configured.write(f"{FOLDER}/ACTION.md", b"# Digest\n\nReviewed by a person.\n")
    again = routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)
    assert again == {"routine": "digest", "skipped": "an action for this routine already exists today"}
    assert configured.read(f"{FOLDER}/ACTION.md") == b"# Digest\n\nReviewed by a person.\n"
    assert refs(read([configured], "actions")) == [f"{FOLDER}/ACTION.md"]


def refs(reply: dict[str, object]) -> list[str]:
    return [str(item["ref"]) for item in cast("list[dict[str, object]]", reply["items"])]


def test_empty_output_means_nothing_to_review(configured: Store) -> None:
    assert routine(configured, "digest", start=START, end=END, runner=printing(b" \n"), clock=lambda: NOW) == {
        "routine": "digest"
    }
    assert not configured.files("actions")
    assert state(configured, ROUTINES)["digest"]["success"] == NOW.isoformat()


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"---\nstatus: nope\n---\n# Bad\n", "invalid frontmatter"),
        (b"---\nunclosed\n", "unclosed frontmatter"),
        (b"\xff\xfe", "UTF-8"),
        (b"# Bad\n\n[x](bf://fixture/projects/offline.md?rel=undeclared)\n", "declare an identity relationship"),
        (b"---\nentity: bf://other/people/x\n---\n# Foreign\n", "own brain namespace"),
    ],
)
def test_invalid_output_writes_nothing_and_records_the_error(configured: Store, output: bytes, message: str) -> None:
    with pytest.raises(Error, match=message) as raised:
        routine(configured, "digest", start=START, end=END, runner=printing(output), clock=lambda: NOW)
    assert str(log_path(configured, "digest")) in str(raised.value)
    assert not configured.files("actions")
    assert message in str(state(configured, ROUTINES)["digest"]["error"])
    assert "success" not in state(configured, ROUTINES)["digest"]


def test_routines_require_trust_and_valid_windows(configured: Store, tmp_path: Path) -> None:
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
    untrusted = Store(clone)
    untrusted.write("bf.yaml", CONFIG.replace(b"name: fixture", b"name: clone"))
    with pytest.raises(Error, match="may not run routines"):
        routine(untrusted, "digest", start=START, end=END, runner=printing(ACTION))
    register(untrusted, collect=False)
    assert update([untrusted], now=NOW)["brains"] == [
        {"brain": "clone", "skipped": "not trusted to collect on this machine"}
    ]


def test_due_routines_cover_the_time_since_their_last_reviewed_window(configured: Store) -> None:
    assert due_routines(configured, NOW) == [("digest", START, END)]
    routine(configured, "digest", start=START, end=END, runner=printing(b""), clock=lambda: NOW)
    assert due_routines(configured, NOW + timedelta(hours=1)) == []
    later = NOW + timedelta(days=1)
    assert due_routines(configured, later) == [("digest", END, "2026-09-26T08:00:00.000000Z")]


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
            return b"---\nstatus: nope\n---\n"
        return b"[]" if argv[0] == "mail" else ACTION

    dry = cast("Any", update([configured], dry_run=True, now=NOW))["brains"][0]
    assert [r["status"] for r in dry["routines"]] == ["due", "due"]
    assert order == []
    report = cast("Any", update([configured], now=NOW, runner=runner))
    assert not report["ok"]
    brain = report["brains"][0]
    assert brain["sensors"][0]["status"] == "collected"
    assert [(r["routine"], r["status"]) for r in brain["routines"]] == [("broken", "failed"), ("digest", "ran")]
    assert order == ["mail", "broken", "routines/digest.py"]
    assert configured.read(f"{FOLDER}/ACTION.md") == ACTION
    assert brain["index"]["problems"] == 0
    # A later run the same day keeps the existing action and says so.
    configured.write("bf.yaml", configured.read("bf.yaml").replace(b"refresh: 86400", b"refresh: 60"))
    later = cast("Any", update([configured], now=NOW + timedelta(minutes=5), runner=runner))["brains"][0]
    assert ("digest", "skipped") in [(r["routine"], r["status"]) for r in later["routines"]]
    assert state(configured, ROUTINES)["digest"]["action"] == f"{FOLDER}/ACTION.md"


def test_status_and_home_report_failing_routines(configured: Store) -> None:
    with pytest.raises(Error):
        routine(configured, "digest", start=START, end=END, runner=printing(b"\xff"), clock=lambda: NOW)
    health = routine_health(configured, now=NOW, trusted=True)
    assert health["digest"]["freshness"] == "never"
    assert health["digest"]["log"] == str(log_path(configured, "digest"))
    assert health["manual"] == {"enabled": True, "freshness": "manual"}
    assert health["paused"] == {"enabled": False, "freshness": "unknown"}
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
        b'#!/bin/sh\nprintf -- \'---\\nstatus: active\\n---\\n# Digest %s\\n\' "$(basename "$PWD")"\n',
    )
    (configured.root / "routines/digest.py").chmod(0o700)
    result = update([configured], now=NOW)
    assert result["ok"], result
    assert configured.read(f"{FOLDER}/ACTION.md").decode().endswith(f"# Digest {configured.root.name}\n")
    configured.write("bf.yaml", CONFIG.replace(b"routines/digest.py", b"routines/../bf.yaml"))
    with pytest.raises(Error):
        routine(configured, "digest", start=START, end=END, clock=lambda: NOW + timedelta(days=1))
