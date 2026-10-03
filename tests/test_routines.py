"""Routines are deterministic programs whose Markdown becomes the day's action; failures write nothing."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
import sys
import time
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
from bf.validate import validate
from conftest import plain

NOW = datetime(2026, 9, 25, 8, tzinfo=UTC)
START = "2026-09-24T08:00:00.000000Z"
END = "2026-09-25T08:00:00.000000Z"
CONFIG = b"""version: 7
name: fixture
routines:
  digest:
    command: [routines/digest.py, "{{brain}}", "{{start}}", "{{end}}"]
    refresh: 86400
    output: action
  manual:
    command: [echo]
  paused:
    command: [echo]
    enabled: false
    refresh: 60
"""
ACTION = b"---\ntype: action\nstatus: draft\n---\n# Digest\n\nSee [offline](../../projects/offline.md).\n"
FOLDER = f"actions/{NOW.astimezone().date().isoformat()}_digest-00000001"
SKIPPED = "an action for this routine already exists today"


@pytest.fixture
def configured(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    return brain


def printing(output: bytes, calls: list[list[str]] | None = None) -> Runner:
    def runner(argv: list[str], program: Program, _store: Store, _name: str, _stdin: bytes) -> bytes:
        assert program.max_bytes == 1 << 20
        if calls is not None:
            calls.append(argv)
        return output

    return runner


def test_configuration_names_routines_like_action_slugs() -> None:
    assert Config.model_validate(
        {"version": 7, "name": "b", "routines": {"weekly-review": {"command": ["x"]}}}
    ).routines
    for name in ("Weekly", "weekly-", "a--b", "1st"):
        with pytest.raises(ValidationError):
            Config.model_validate({"version": 7, "name": "b", "routines": {name: {"command": ["x"]}}})
    with pytest.raises(ValidationError, match="distinct"):
        Config.model_validate(
            {"version": 7, "name": "b", "sensors": {"x": {"command": ["x"]}}, "routines": {"x": {"command": ["x"]}}}
        )
    for settings in ({"command": ["x"], "max_bytes": 5 << 20}, {"command": ["x", "{{secret}}"]}, {"command": []}):
        with pytest.raises(ValidationError):
            Config.model_validate({"version": 7, "name": "b", "routines": {"digest": settings}})


def test_a_routine_writes_one_action_per_day_after_success(configured: Store) -> None:
    calls: list[list[str]] = []
    preview = routine(
        configured, "digest", start=START, end=END, runner=printing(ACTION, calls), dry_run=True, clock=lambda: NOW
    )
    # A preview names no action path: the real run draws its own random suffix.
    assert preview == {"routine": "digest", "text": ACTION.decode()}
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
    assert again == {"routine": "digest", "skipped": SKIPPED}
    assert configured.read(f"{FOLDER}/ACTION.md") == b"# Digest\n\nReviewed by a person.\n"
    assert refs(read([configured], "actions")) == [f"{FOLDER}/ACTION.md"]
    # Before, a preview then still reported a new action that the real run would never write.
    preview = routine(
        configured, "digest", start=START, end=END, runner=printing(ACTION), dry_run=True, clock=lambda: NOW
    )
    assert preview == {"routine": "digest", "skipped": SKIPPED, "text": ACTION.decode()}


def test_only_an_action_md_makes_todays_folder_an_action(
    configured: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)
    # Emacs keeps a dangling `.#NAME` link beside a file while its buffer has unsaved edits: the rerun skips.
    (configured.root / FOLDER / ".#ACTION.md").symlink_to("owner@host.12345:1790000000")
    skipped = {"routine": "digest", "skipped": SKIPPED}
    assert routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW) == skipped
    # Without its ACTION.md, the folder holds no action: a rerun writes one in a new folder, nothing beside the lock.
    configured.delete(f"{FOLDER}/ACTION.md")
    monkeypatch.setattr(collector, "uuid4", lambda: UUID(int=2))
    written = routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)
    assert written == {"routine": "digest", "action": f"{FOLDER[:-1]}2/ACTION.md"}
    assert [path.name for path in (configured.root / FOLDER).iterdir()] == [".#ACTION.md"]
    # A linked folder of the day cannot be inspected, so it counts as today's action.
    shutil.rmtree(configured.root / "actions")
    (tmp_path / "elsewhere").mkdir()
    (configured.root / "actions").mkdir()
    (configured.root / FOLDER).symlink_to(tmp_path / "elsewhere")
    assert routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW) == skipped
    # A linked actions/ folder still fails the run by name.
    shutil.rmtree(configured.root / "actions")
    (configured.root / "actions").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(Error, match="actions: expected a directory"):
        routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)


def test_a_killed_write_never_stops_todays_action(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # A run killed while writing its action can leave the folder holding only the write's temporary file.
    configured.write(f"{FOLDER}/.write-{'0' * 32}", b"---\ntype: act")
    monkeypatch.setattr(collector, "uuid4", lambda: UUID(int=2))
    preview = routine(
        configured, "digest", start=START, end=END, runner=printing(ACTION), dry_run=True, clock=lambda: NOW
    )
    assert "skipped" not in preview
    # Before, the leftover counted as today's action: the routine skipped for the rest of the day.
    result = routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)
    assert result == {"routine": "digest", "action": f"{FOLDER[:-1]}2/ACTION.md"}
    assert configured.read(f"{FOLDER[:-1]}2/ACTION.md") == ACTION
    # Validation names the leftover in a warning, not as a broken action, so a pre-commit validation still passes.
    checked = validate(configured)
    leftover = f"{FOLDER}/.write-{'0' * 32}"
    assert not [p for p in cast("list[dict[str, str]]", checked["problems"]) if p["file"].startswith(FOLDER)]
    assert {"warning": "an interrupted write left this temporary file; delete it", "file": leftover} in cast(
        "list[dict[str, str]]", checked["warnings"]
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_an_unreadable_actions_folder_fails_the_run_without_blaming_a_link(configured: Store) -> None:
    # The scan skips an unreadable actions/ like a linked one, so the retrieval of other folders still answers.
    actions = configured.root / "actions"
    actions.mkdir(exist_ok=True)
    actions.chmod(0)
    try:
        with pytest.raises(Error, match=r"actions: unreadable folder; grant read and search permission; no action"):
            routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)
    finally:
        actions.chmod(0o700)
    assert not any(actions.iterdir())


def test_a_retry_finds_todays_action_when_its_run_history_was_not_saved(
    configured: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = iter(UUID(int=n) for n in (1, 2, 3))
    monkeypatch.setattr(collector, "uuid4", lambda: next(ids))
    original, calls = collector.remember, []

    def lost(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)
        if len(calls) == 1:
            raise OSError(errno.EIO, "synthetic lost history write")
        original(*args, **kwargs)

    monkeypatch.setattr(collector, "remember", lost)
    # Another routine's action and a person's folder with a similar name do not count as today's digest.
    configured.write(f"{FOLDER.replace('digest', 'weekly')}/ACTION.md", ACTION)
    configured.write(f"{FOLDER.rsplit('-', 1)[0]}-notes/ACTION.md", ACTION)
    # The action exists: the failure says so instead of claiming nothing was written.
    with pytest.raises(Error, match=rf"^digest: wrote {FOLDER}/ACTION.md but local run history could not be saved;"):
        routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)
    assert configured.read(f"{FOLDER}/ACTION.md") == ACTION
    assert "action" not in state(configured, ROUTINES)["digest"]
    skipped = {"routine": "digest", "skipped": SKIPPED}
    assert routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW) == skipped
    assert not (configured.root / f"{FOLDER[:-1]}2").exists()


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
        (b"# Bad\n\n[x](bf://fixture/projects/offline.md?rel=undeclared)\n", "declare it in bf.yaml fields"),
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
    assert log_path("digest") in str(raised.value)
    assert not configured.files("actions")
    if dry_run:
        # A failed preview saves no run history, so the routine neither fails nor becomes due.
        assert state(configured, ROUTINES) == {}
    else:
        assert message in str(state(configured, ROUTINES)["digest"]["error"])
        assert "success" not in state(configured, ROUTINES)["digest"]


def test_routines_require_enabled_programs_and_valid_windows(configured: Store, tmp_path: Path) -> None:
    for name, start, end, message in [
        # An unknown name suggests only routines that can run; a disabled one says how to enable it.
        ("pause", START, END, "^unknown routine pause; check names in bf.yaml$"),
        ("paused", START, END, "^routine paused is disabled in bf.yaml; set enabled: true to run it$"),
        ("digest", END, START, "earlier"),
        ("digest", "2026-09-24", END, "timezone"),
    ]:
        with pytest.raises(Error, match=message):
            routine(configured, name, start=start, end=end, runner=printing(ACTION))
    clone = tmp_path / "clone"
    clone.mkdir()

    shared = Store(clone)
    shared.write("bf.yaml", CONFIG.replace(b"name: fixture", b"name: clone"))
    shared.write("projects/offline.md", b"# Offline\n")
    assert routine(shared, "digest", start=START, end=END, runner=printing(ACTION))["action"]


@pytest.mark.parametrize(
    ("link", "problem"),
    [
        ("projects/offline.md", "broken link: projects/offline.md"),
        ("../../projects/offline.md#nowhere", "missing heading: ../../projects/offline.md#nowhere"),
    ],
)
def test_an_action_with_a_broken_link_is_never_written(configured: Store, link: str, problem: str) -> None:
    output = ACTION.replace(b"../../projects/offline.md", link.encode()) + b"\n## Notes {#notes}\n\n[Here](#notes)\n"
    # Before 17 only metadata and relations were checked: bf validate then rejected the written action.
    with pytest.raises(Error, match=re.escape(problem) + r"; no action was written"):
        routine(configured, "digest", start=START, end=END, runner=printing(output), clock=lambda: NOW)
    assert not configured.root.joinpath("actions").exists() or not list(configured.root.joinpath("actions").iterdir())
    assert routine(configured, "digest", start=START, end=END, runner=printing(ACTION), clock=lambda: NOW)["action"]


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
        + b"  broken:\n    command: [broken]\n    refresh: 60\n    output: action\n"
        + b"sensors:\n  mail:\n    command: [mail]\n    refresh: 60\n",
    )
    order: list[str] = []

    def runner(argv: list[str], _program: Program, _store: Store, _name: str, _stdin: bytes) -> bytes:
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
        "error": "routine must print UTF-8 Markdown; no action was written",
        "failures": 1,
        "log": "logs/digest.log",
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


HOOKED = b"""version: 7
name: fixture
routines:
  check:
    command: [routines/check.sh, first]
    hooks: [pre-push]
  guard:
    command: [routines/guard.sh]
    hooks: [pre-push, pre-commit]
  paused:
    command: [routines/check.sh]
    hooks: [pre-push]
    enabled: false
"""


@pytest.fixture
def hooked(brain: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    brain.write("bf.yaml", HOOKED)
    # Echo arguments and input to stdout, which a log routine keeps in its log, and a note to stderr.
    brain.write("routines/check.sh", b'#!/bin/sh\necho "args:$*"\ncat\necho note >&2\n')
    brain.write("routines/guard.sh", b"#!/bin/sh\nexit 3\n")
    for name in ("check", "guard"):
        (brain.root / f"routines/{name}.sh").chmod(0o700)
    return brain


def test_log_routines_keep_their_output_and_hooks_run_every_listed_routine(hooked: Store) -> None:
    runner = CliRunner()
    ran = runner.invoke(
        app, ["run", "check", "--brain", str(hooked.root), "--stdin", "--", "--flag", "x"], input="ref\n"
    )
    assert ran.exit_code == 0, ran.output
    assert json.loads(ran.stdout) == {"ok": True, "dry_run": False, "routines": [{"routine": "check", "status": "ran"}]}
    log = hooked.read(log_path("check")).decode()
    # The configured arguments come first, then the caller's; --stdin passes piped input to the routine.
    assert "args:first --flag x\nref\n-- stderr --\nnote\n" in log
    # Without --stdin, a direct run passes none.
    assert runner.invoke(app, ["run", "check", "--brain", str(hooked.root)], input="unread\n").exit_code == 0
    assert "unread" not in hooked.read(log_path("check")).decode()
    assert log.rstrip().endswith("succeeded ==")
    assert not hooked.root.joinpath("actions").exists() or not list(hooked.root.joinpath("actions").iterdir())
    # A hook runs its enabled routines in name order; one failure never blocks the others.
    pushed = runner.invoke(app, ["run", "--hook", "pre-push", "--brain", str(hooked.root)])
    assert pushed.exit_code == 1
    reply = json.loads(pushed.stdout)
    assert reply["hook"] == "pre-push"
    assert [(r["routine"], r["status"]) for r in reply["routines"]] == [("check", "ran"), ("guard", "failed")]
    assert "status 3" in reply["routines"][1]["error"]
    assert state(hooked, ROUTINES)["guard"]["failures"] == 1
    # A hook that no routine lists runs nothing, so a Git hook can call it before any routine exists.
    idle = runner.invoke(app, ["run", "--hook", "post-merge", "--brain", str(hooked.root)])
    assert (idle.exit_code, json.loads(idle.stdout)["routines"]) == (0, [])


@pytest.mark.parametrize("source", ["devnull", "file"])
def test_run_reads_files_and_devices_as_input(hooked: Store, tmp_path: Path, source: str) -> None:
    # Git runs a pre-commit hook with /dev/null as its input: epoll cannot wait on it, so it is read directly.
    path = tmp_path / "input.txt"
    path.write_text("from a file\n")
    with Path(os.devnull if source == "devnull" else path).open("rb") as stdin:
        result = subprocess.run(  # noqa: S603 - synthetic CLI boundary
            [sys.executable, "-m", "bf", "run", "check", "--stdin", "--brain", str(hooked.root)],
            stdin=stdin,
            capture_output=True,
            timeout=60,
            check=False,
            env={**os.environ, "PATH": "/usr/bin:/bin"},
        )
    assert result.returncode == 0, result.stderr
    expected = "args:first\n" + ("" if source == "devnull" else "from a file\n")
    assert expected + "-- stderr --" in hooked.read(log_path("check")).decode()


def test_run_forwards_piped_input_from_a_real_process(hooked: Store) -> None:
    result = subprocess.run(  # noqa: S603 - synthetic CLI boundary
        [sys.executable, "-m", "bf", "run", "check", "--stdin", "--brain", str(hooked.root)],
        input=b"line one\nline two\n",
        capture_output=True,
        timeout=60,
        check=False,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "args:first\nline one\nline two\n" in hooked.read(log_path("check")).decode()


def test_a_direct_run_never_waits_for_input_an_agent_shell_holds_open(hooked: Store) -> None:
    # Before 17 a direct run waited for any open pipe to close and failed after 10 seconds in such shells.
    with subprocess.Popen(  # noqa: S603 - synthetic CLI boundary
        [sys.executable, "-m", "bf", "run", "check", "--brain", str(hooked.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    ) as process:
        started = time.monotonic()
        assert process.wait(timeout=60) == 0
        assert time.monotonic() - started < 10
        assert process.stdin is not None
        process.stdin.close()
    assert "args:first\n-- stderr --" in hooked.read(log_path("check")).decode()


@pytest.mark.parametrize(
    ("arguments", "code", "message"),
    [
        ([], 2, "name a routine, or pass --hook EVENT"),
        (["--hook", "Pre Push"], 2, "hook names are lowercase words"),
        (["missing"], 1, "unknown routine missing"),
        (["paused"], 1, "routine paused is disabled in bf.yaml; set enabled: true to run it"),
    ],
)
def test_run_rejects_missing_unknown_or_disabled_routines(
    hooked: Store, arguments: list[str], code: int, message: str
) -> None:
    result = CliRunner().invoke(app, ["run", *arguments, "--brain", str(hooked.root)])
    assert result.exit_code == code
    assert message in plain(result.output) + str(result.exception)
    # Like an unknown name, a disabled one fails before running anything, without a reply.
    assert not result.stdout
    assert not hooked.root.joinpath("logs").exists()


def test_a_log_routine_keeps_the_tail_of_output_beyond_max_bytes(hooked: Store) -> None:
    # A verbose backup prints a line per file: its log keeps the newest 256 KiB instead of the run failing.
    hooked.write("routines/backup.sh", b"#!/bin/sh\nhead -c 600000 /dev/zero | tr '\\0' x\necho done\n")
    (hooked.root / "routines/backup.sh").chmod(0o700)
    settings = "  backup:\n    command: [routines/backup.sh]\n    max_bytes: 1024\n"
    hooked.write("bf.yaml", HOOKED.replace(b"routines:\n", b"routines:\n" + settings.encode()))
    assert routine(hooked, "backup", start=START, end=END) == {"routine": "backup"}
    # The run's entry holds exactly the last 256 KiB, followed by the routine's outcome.
    body = hooked.read(log_path("backup")).split(b"==\n", 1)[1]
    assert body.startswith(b"x" * ((256 << 10) - len("done\n")) + b"done\n== ")
    # An action routine's output becomes a note: it still fails beyond max_bytes.
    hooked.write("bf.yaml", hooked.read("bf.yaml").replace(b"max_bytes: 1024", b"max_bytes: 1024\n    output: action"))
    with pytest.raises(Error, match="program output exceeded max_bytes; no action was written"):
        routine(hooked, "backup", start=START, end=END)


def test_update_selection_suggests_close_names(hooked: Store) -> None:
    with pytest.raises(Error, match=r"unknown routine chek; check names in bf.yaml; did you mean check\?"):
        update(hooked, now=NOW, runner=printing(b""), routines=("chek",))


def test_hooks_are_distinct_slugs() -> None:
    for hooks in (["pre-push", "pre-push"], ["Pre-Push"], ["pre push"]):
        with pytest.raises(ValidationError):
            Config.model_validate({"version": 7, "name": "b", "routines": {"x": {"command": ["x"], "hooks": hooks}}})


def test_update_names_the_manual_programs_it_skipped(hooked: Store) -> None:
    report = cast("Any", update(hooked, now=NOW, runner=printing(b"")))
    assert report["ok"]
    assert (report["sensors"], report["routines"], report["manual"]) == ([], [], ["check", "guard"])
