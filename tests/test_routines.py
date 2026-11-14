"""Action, log and hook jobs execute safely with isolated state and strict validation."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bf import jobs, pages
from bf.cli import app
from bf.collect import due_jobs
from bf.health import attention, report
from bf.jobs import Runner, action_new, run_job
from bf.models import Config, Error, Job
from bf.retrieve import read
from bf.runtime import Queue, log_path
from bf.storage import Store, collecting, reader, writer
from bf.update import update
from bf.validate import validate
from conftest import plain

NOW = datetime(2026, 9, 25, 8, tzinfo=UTC)
START = "2026-09-24T08:00:00.000000Z"
END = "2026-09-25T08:00:00.000000Z"
TODAY = NOW.astimezone().date().isoformat()
UNIQUE_SUFFIX = "000000000001"
FOLDER = f"actions/{datetime.now(UTC).astimezone().date().isoformat()}_digest-{UNIQUE_SUFFIX}"
CONFIG = b"""version: 10
name: fixture
jobs:
  digest:
    run: [routines/digest.py, "{{brain}}", "{{start}}", "{{end}}"]
    emit: action
    on: {every: 86400}
  manual:
    run: [echo]
    emit: log
  paused:
    run: [echo]
    emit: log
    enabled: false
    on: {every: 60}
"""
ACTION = b"---\ntype: action\nstatus: draft\n---\n# Digest\n\nSee [offline](../../projects/offline.md).\n"


@pytest.fixture
def configured(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    return brain


def printing(output: bytes, calls: list[list[str]] | None = None) -> Runner:
    def runner(argv: list[str], job: Job, _store: Store, _name: str, _stdin: bytes) -> bytes:
        assert job.max_bytes == 64 << 20
        if calls is not None:
            calls.append(argv)
        return output

    return runner


def refs(reply: dict[str, object]) -> list[str]:
    return [str(item["ref"]) for item in cast("list[dict[str, object]]", reply["items"])]


def test_configuration_names_jobs_with_valid_identifiers() -> None:
    assert Config.model_validate({"version": 10, "name": "b", "jobs": {"weekly-review": {"run": ["x"]}}}).jobs
    for name in ("Weekly", "1st", "a b"):
        with pytest.raises(ValidationError):
            Config.model_validate({"version": 10, "name": "b", "jobs": {name: {"run": ["x"]}}})
    for settings in (
        {"run": ["x"], "max_bytes": 257 << 20},
        {"run": ["x", "{{secret}}"]},
        {"run": []},
    ):
        with pytest.raises(ValidationError):
            Config.model_validate({"version": 10, "name": "b", "jobs": {"digest": settings}})


def test_an_action_job_writes_and_remembers_validated_markdown(configured: Store) -> None:
    calls: list[list[str]] = []
    preview = run_job(configured, "digest", since=START, until=END, runner=printing(ACTION, calls), dry_run=True)
    assert preview["job"] == "digest"
    assert preview["status"] == "ok"
    assert preview["dry_run"] is True
    assert preview["text"] == ACTION.decode().strip()
    assert calls == [["routines/digest.py", str(configured.root), START, END]]
    assert not configured.files("actions")
    assert Queue(configured).all_states() == {}

    folder = f"actions/{datetime.now(UTC).astimezone().date().isoformat()}_digest-000000000002"
    result = run_job(configured, "digest", since=START, until=END, runner=printing(ACTION))
    assert result["job"] == "digest"
    assert result["status"] == "ok"
    assert result["action"] == f"{folder}/ACTION.md"
    assert configured.read(f"{folder}/ACTION.md") == ACTION
    saved = Queue(configured).state_of("digest")
    assert saved["last_run"] == END
    assert saved["last_success"] == END
    assert saved["window_start"] == START
    assert saved["window_end"] == END
    assert saved["action"] == f"{folder}/ACTION.md"
    assert saved["runs"] == 1
    assert refs(read(configured, "actions")) == [f"{folder}/ACTION.md"]


def test_action_allocation_and_new_respect_links_leftovers_and_run_idempotency(
    configured: Store, tmp_path: Path
) -> None:
    today = datetime.now(UTC).astimezone().date().isoformat()
    first = action_new(configured, "digest", title="First digest")
    assert first == {"ok": True, "action": f"actions/{today}_digest/ACTION.md", "status": "draft"}

    # An active editor lock `.#ACTION.md` or another run skips occupied unique folders safely.
    (configured.root / f"actions/{today}_digest/.#ACTION.md").symlink_to("owner@host.12345:1790000000")
    second = run_job(configured, "digest", since=START, until=END, runner=printing(ACTION))
    assert second["action"] == f"actions/{today}_digest-000000000006/ACTION.md"

    # Leftover temp files do not stop validation or allocation; interrupted writes warn cleanly.
    leftover = f"{Path(str(second['action'])).parent}/.write-{'0' * 32}"
    configured.write(leftover, b"---\ntype: act")
    checked = validate(configured)
    assert {"warning": "an interrupted write left this temporary file; delete it", "file": leftover} in cast(
        "list[dict[str, str]]", checked["warnings"]
    )
    third = run_job(configured, "digest", since=START, until=END, runner=printing(ACTION))
    assert third["action"] == f"actions/{today}_digest-00000000000b/ACTION.md"

    # Inside an active run, `action new --run` creates one action per slug idempotently.
    rid = Queue(configured).start_run("digest", "manual")
    a1 = action_new(configured, "follow-up", run_id=rid)
    a2 = action_new(configured, "follow-up", run_id=rid)
    assert a1["action"] == a2["action"]
    assert a2.get("reused") is True
    Queue(configured).finish_run(rid, status="completed")

    # A linked actions/ folder fails explicitly without writing outside the brain.
    shutil.rmtree(configured.root / "actions")
    (tmp_path / "elsewhere").mkdir()
    (configured.root / "actions").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(Error, match="actions: expected a directory"):
        run_job(configured, "digest", since=START, until=END, runner=printing(ACTION))


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_an_unreadable_actions_folder_fails_the_run_without_blaming_a_link(configured: Store) -> None:
    actions = configured.root / "actions"
    actions.mkdir(exist_ok=True)
    actions.chmod(0)
    try:
        with pytest.raises(Error, match=r"digest: job digest failed"):
            run_job(configured, "digest", since=START, until=END, runner=printing(ACTION))
    finally:
        actions.chmod(0o700)
    assert not any(actions.iterdir())


def test_only_writing_an_action_waits_for_the_brains_readers(
    configured: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail at once instead of after the 120-second wait for a busy brain.
    monkeypatch.setattr(jobs.storage, "writer", lambda store, **_: writer(store, 0))
    with reader(configured):
        # Log jobs and quiet/NO_REPLY action jobs never need the brain writer lock.
        assert run_job(configured, "manual", since=START, until=END, runner=printing(b"scanned\n"))["status"] == "ok"
        assert run_job(configured, "digest", since=START, until=END, runner=printing(b""))["quiet"] is True
        busy = "another process is reading or changing this brain's records"
        with pytest.raises(Error, match=f"^digest: {busy}; retry after it finishes; see "):
            run_job(configured, "digest", since=START, until=END, runner=printing(ACTION))
    states = Queue(configured).all_states()
    assert (states["manual"].get("error_count", 0), states["manual"].get("last_error", "")) == (0, "")
    assert states["digest"]["error_count"] == 1
    assert not (configured.root / "actions").exists()


def test_empty_output_means_nothing_to_review(configured: Store) -> None:
    res = run_job(configured, "digest", since=START, until=END, runner=printing(b" \n"))
    assert res["job"] == "digest"
    assert res["quiet"] is True
    assert not configured.files("actions")
    assert Queue(configured).state_of("digest")["last_success"] == END


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
        (b"# Bad\n\n[x](bf://fixture/projects/offline.md?rel=links)\n", "untyped links need no"),
        (b"---\nentity: bf://other/people/x\n---\n# Foreign\n", "own brain namespace"),
        (b"---\naliases: [bf://other/people/x]\n---\n# Foreign\n", "own brain namespace"),
    ],
)
def test_invalid_output_writes_nothing_and_records_the_error(configured: Store, output: bytes, message: str) -> None:
    with pytest.raises(Error, match=message) as raised:
        run_job(configured, "digest", since=START, until=END, runner=printing(output))
    assert log_path("digest") in str(raised.value)
    assert not configured.files("actions")
    saved = Queue(configured).state_of("digest")
    assert message in str(saved["last_error"])
    assert "last_success" not in saved
    if output == b"\xff\xfe":
        with pytest.raises(Error, match="UTF-8"):
            run_job(configured, "digest", since=START, until=END, runner=printing(output), dry_run=True)


def test_jobs_require_enabled_programs_and_valid_windows(configured: Store, tmp_path: Path) -> None:
    for args, message in [
        (["pause"], "^unknown job pause; check jobs in bf.yaml$"),
        (["paused"], "^job paused is disabled in bf.yaml; set enabled: true to run it$"),
    ]:
        with pytest.raises(Error, match=message):
            run_job(configured, args[0], runner=printing(ACTION))
    for bad_win, hint in [
        (["--since", END, "--until", START], "earlier"),
        (["--since", "2026-09-24T08:00:00", "--until", END], "timezone"),
    ]:
        res = CliRunner().invoke(app, ["run", "digest", *bad_win, "--brain", str(configured.root)])
        assert res.exit_code == 2
        assert hint in plain(res.output)

    clone = tmp_path / "clone"
    clone.mkdir()
    shared = Store(clone)
    shared.write("bf.yaml", CONFIG.replace(b"name: fixture", b"name: clone"))
    shared.write("projects/offline.md", b"# Offline\n")
    assert run_job(shared, "digest", since=START, until=END, runner=printing(ACTION))["action"]


@pytest.mark.parametrize(
    ("link", "problem"),
    [
        ("projects/offline.md", "broken link: projects/offline.md"),
        ("../../projects/offline.md#nowhere", "missing heading: ../../projects/offline.md#nowhere"),
    ],
)
def test_an_action_with_a_broken_link_is_never_written(configured: Store, link: str, problem: str) -> None:
    output = ACTION.replace(b"../../projects/offline.md", link.encode()) + b"\n## Notes {#notes}\n\n[Here](#notes)\n"
    with pytest.raises(Error, match=re.escape(problem)):
        run_job(configured, "digest", since=START, until=END, runner=printing(output))
    assert not configured.root.joinpath("actions").exists() or not list(configured.root.joinpath("actions").iterdir())
    assert run_job(configured, "digest", since=START, until=END, runner=printing(ACTION))["action"]


def test_a_broken_link_error_quotes_only_the_start_of_a_long_target(configured: Store) -> None:
    output = ACTION.replace(b"../../projects/offline.md", b"x" * 10_000)
    with pytest.raises(Error, match=r"^digest: broken link: x{186}…;") as raised:
        run_job(configured, "digest", since=START, until=END, runner=printing(output))
    assert len(str(raised.value).encode()) < 1024
    assert len(str(Queue(configured).state_of("digest")["last_error"]).encode()) < 1024


def test_due_jobs_cover_the_time_since_their_last_reviewed_window(configured: Store) -> None:
    assert due_jobs(configured, NOW) == [("digest", START, END)]
    run_job(configured, "digest", since=START, until=END, runner=printing(b""))
    assert due_jobs(configured, NOW + timedelta(hours=1)) == []
    later = NOW + timedelta(days=1)
    assert due_jobs(configured, later) == [("digest", END, "2026-09-26T08:00:00.000000Z")]


def test_failed_jobs_back_off_until_a_success(configured: Store) -> None:
    def failing(*_: object) -> bytes:
        raise Error("synthetic failure")

    def due_at(when: datetime) -> bool:
        return any(window[0] == "digest" for window in due_jobs(configured, when))

    queue = Queue(configured)
    retry = NOW
    for delay in (timedelta(minutes=1), timedelta(minutes=2)):
        with pytest.raises(Error, match="synthetic failure"):
            run_job(configured, "digest", since=START, until=END, runner=failing)
        # Stamp controlled retry time cleanly to verify exponential schedule backoff.
        queue.remember("digest", last_run=retry.isoformat().replace("+00:00", "Z"))
        assert not due_at(retry + delay - timedelta(seconds=1))
        retry += delay
        assert due_at(retry)
    assert queue.state_of("digest")["error_count"] == 2
    run_job(configured, "digest", since=START, until=retry.isoformat().replace("+00:00", "Z"), runner=printing(b""))
    assert not due_at(retry + timedelta(days=1) - timedelta(seconds=1))
    assert due_at(retry + timedelta(days=1))


def test_update_runs_jobs_after_records_and_isolates_failures(configured: Store) -> None:
    configured.write(
        "bf.yaml",
        CONFIG
        + b"  broken:\n    run: [broken]\n    emit: action\n    on: {every: 60}\n"
        + b"  mail:\n    run: [mail]\n    emit: records\n    on: {every: 60}\n",
    )
    order: list[str] = []

    def runner(argv: list[str], _job: Job, _store: Store, _name: str, _stdin: bytes) -> bytes:
        order.append(argv[0])
        if argv[0] == "broken":
            return b"---\nupdated: soon\n---\n"
        return b"[]" if argv[0] == "mail" else ACTION

    dry = cast("Any", update(configured, dry_run=True, now=NOW))
    assert [(r["job"], r["status"]) for r in dry["jobs"]] == [
        ("mail", "due"),
        ("broken", "due"),
        ("digest", "due"),
    ]
    assert order == []
    out = cast("Any", update(configured, now=NOW, runner=runner))
    assert not out["ok"]
    assert [(r["job"], r["status"]) for r in out["jobs"]] == [
        ("mail", "collected"),
        ("broken", "failed"),
        ("digest", "ran"),
    ]
    assert order == ["mail", "broken", "routines/digest.py"]
    action_path = str(out["jobs"][2]["action"])
    assert action_path == f"actions/{datetime.now(UTC).astimezone().date().isoformat()}_digest-000000000006/ACTION.md"
    assert configured.read(action_path) == ACTION
    assert out["index"]["skipped"] == 0
    assert Queue(configured).state_of("digest")["action"] == action_path


def test_status_and_home_report_failing_jobs(configured: Store) -> None:
    with pytest.raises(Error):
        run_job(configured, "digest", since=START, until=END, runner=printing(b"\xff"))
    full = cast("dict[str, Any]", report(configured, now=NOW))
    assert full["jobs"]["digest"] == {
        "enabled": True,
        "emit": "action",
        "priority": "normal",
        "state": "active",
        "freshness": "never",
        "next_due": "2026-09-25T08:00:00.000000Z",
        "failed": True,
        "error": "job digest: output must be UTF-8",
        "failures": 1,
        "log": "logs/digest.log",
    }
    assert full["jobs"]["manual"]["freshness"] == "manual"
    assert full["jobs"]["paused"]["state"] == "disabled"
    assert attention(configured, NOW) == [{"kind": "job", "name": "digest", "freshness": "never", "failed": True}]
    assert pages.home(configured, NOW)["attention"] == [
        {"kind": "job", "name": "digest", "freshness": "never", "failed": True}
    ]
    result = CliRunner().invoke(app, ["status", "--check", "--brain", str(configured.root)])
    assert result.exit_code == 1


def test_job_executables_run_from_the_brain_root(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
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
        run_job(configured, "digest", since=START, until=END)


@pytest.fixture(autouse=True)
def fixed_action_id(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = (UUID(int=n) for n in count(1))
    monkeypatch.setattr(jobs, "uuid4", lambda: next(ids))


HOOKED = b"""version: 10
name: fixture
jobs:
  check:
    run: [routines/check.sh, first]
    emit: log
    on: {hook: pre-push}
  guard:
    run: [routines/guard.sh]
    emit: log
    on:
      - {hook: pre-push}
      - {hook: pre-commit}
  paused:
    run: [routines/check.sh]
    emit: log
    on: {hook: pre-push}
    enabled: false
"""


@pytest.fixture
def hooked(brain: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    brain.write("bf.yaml", HOOKED)
    brain.write("routines/check.sh", b'#!/bin/sh\necho "args:$*"\ncat\necho note >&2\n')
    brain.write("routines/guard.sh", b"#!/bin/sh\nexit 3\n")
    for name in ("check", "guard"):
        (brain.root / f"routines/{name}.sh").chmod(0o700)
    return brain


def test_log_jobs_keep_their_output_and_hooks_run_every_listed_job(hooked: Store) -> None:
    runner = CliRunner()
    ran = runner.invoke(
        app, ["run", "check", "--brain", str(hooked.root), "--stdin", "--", "--flag", "x"], input="ref\n"
    )
    assert ran.exit_code == 0, ran.output
    ran_json = json.loads(ran.stdout)
    assert (ran_json["job"], ran_json["status"]) == ("check", "ok")
    log = hooked.read(log_path("check")).decode()
    # Configured arguments come first, then caller args; --stdin forwards piped input.
    assert "args:first --flag x\nref\n-- stderr --\nnote\n" in log
    # Without --stdin, a direct run forwards none.
    assert runner.invoke(app, ["run", "check", "--brain", str(hooked.root)], input="unread\n").exit_code == 0
    assert "unread" not in hooked.read(log_path("check")).decode()
    assert re.search(r"^== .* exited with status 0 after [0-9.]+s ==$", log, flags=re.MULTILINE)
    assert not hooked.root.joinpath("actions").exists() or not list(hooked.root.joinpath("actions").iterdir())
    # A hook runs enabled jobs in name order; one failure never blocks the others.
    pushed = runner.invoke(app, ["run", "--hook", "pre-push", "--brain", str(hooked.root)])
    assert pushed.exit_code == 1
    reply = json.loads(pushed.stdout)
    assert reply["hook"] == "pre-push"
    assert [(r["job"], r["status"]) for r in reply["jobs"]] == [("check", "ok"), ("guard", "failed")]
    assert "status 3" in reply["jobs"][1]["error"]
    assert Queue(hooked).state_of("guard")["error_count"] == 1
    # A hook that no job lists runs nothing.
    idle = runner.invoke(app, ["run", "--hook", "post-merge", "--brain", str(hooked.root)])
    assert (idle.exit_code, json.loads(idle.stdout)["jobs"]) == (0, [])


@pytest.mark.parametrize("source", ["devnull", "file"])
def test_run_reads_files_and_devices_as_input(hooked: Store, tmp_path: Path, source: str) -> None:
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
    with subprocess.Popen(  # noqa: S603 - synthetic CLI boundary
        [sys.executable, "-m", "bf", "run", "check", "--brain", str(hooked.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    ) as process:
        assert process.wait(timeout=60) == 0
        assert process.stdin is not None
        process.stdin.close()
    assert "args:first\n-- stderr --" in hooked.read(log_path("check")).decode()


@pytest.mark.parametrize(
    ("arguments", "code", "message"),
    [
        ([], 2, "name a job, or pass --hook EVENT"),
        (["--hook", "Pre Push"], 2, "use lowercase words joined by hyphens"),
        (["missing"], 1, "unknown job missing"),
        (["paused"], 1, "job paused is disabled in bf.yaml; set enabled: true to run it"),
    ],
)
def test_run_rejects_missing_unknown_or_disabled_jobs(
    hooked: Store, arguments: list[str], code: int, message: str
) -> None:
    result = CliRunner().invoke(app, ["run", *arguments, "--brain", str(hooked.root)])
    assert result.exit_code == code
    assert message in plain(result.output) + str(result.exception)
    assert not result.stdout
    assert not hooked.root.joinpath("logs").exists()


def test_a_log_job_keeps_the_tail_of_output_beyond_max_bytes(hooked: Store) -> None:
    hooked.write("routines/backup.sh", b"#!/bin/sh\nhead -c 600000 /dev/zero | tr '\\0' x\necho done\n")
    (hooked.root / "routines/backup.sh").chmod(0o700)
    settings = "  backup:\n    run: [routines/backup.sh]\n    emit: log\n    max_bytes: 1024\n"
    hooked.write("bf.yaml", HOOKED.replace(b"jobs:\n", b"jobs:\n" + settings.encode()))
    res = run_job(hooked, "backup")
    assert (res["job"], res["status"]) == ("backup", "ok")
    body = hooked.read(log_path("backup")).split(b"==\n", 1)[1]
    assert body == b"x" * ((256 << 10) - len("done\n")) + b"done\n"
    hooked.write(
        "bf.yaml",
        hooked.read("bf.yaml").replace(b"emit: log\n    max_bytes: 1024", b"emit: action\n    max_bytes: 1024"),
    )
    with pytest.raises(Error, match="program output exceeded max_bytes"):
        run_job(hooked, "backup")


def test_update_selection_suggests_close_names(hooked: Store) -> None:
    with pytest.raises(Error, match=r"unknown job chek; check jobs in bf.yaml; did you mean check\?"):
        update(hooked, now=NOW, runner=printing(b""), jobs=("chek",))


def test_hooks_are_distinct_slugs() -> None:
    for on in ({"hook": "pre-"}, {"hook": "Pre-Push"}, {"hook": "pre push"}, {"hook": "a--b"}):
        with pytest.raises(ValidationError):
            Config.model_validate({"version": 10, "name": "b", "jobs": {"x": {"run": ["x"], "on": on}}})


def test_update_names_the_manual_jobs_it_skipped(hooked: Store) -> None:
    report = cast("Any", update(hooked, now=NOW, runner=printing(b"")))
    assert report["ok"]
    assert report["jobs"] == []
    assert report["manual"] == ["check", "guard"]


def test_keep_removes_older_unchanged_reports_and_spares_edited_ones(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\njobs:\n  review:\n    run: [review]\n    emit: action\n    keep: 2\n",
    )
    report_body = b"---\ntype: action\nstatus: draft\n---\n\n# Review\n"
    written = []
    for day in range(1, 5):
        result = run_job(brain, "review", runner=printing(report_body))
        written.append(str(result["action"]))
        if day == 2:
            # The owner annotated this report: it is now their note, never a disposable one.
            brain.write(written[-1], report_body + b"\nKeep this.\n")
    remaining = sorted(name for name in brain.files("actions") if name.endswith("/ACTION.md"))
    assert remaining == written[1:]
    assert not (brain.root / written[0]).parent.exists()
    assert "removed" not in result
    with pytest.raises(ValidationError, match="keep applies only to emit: records or emit: action"):
        Job.model_validate({"run": ["x"], "emit": "log", "keep": 2})


def test_update_skips_a_job_another_process_is_running(configured: Store) -> None:
    configured.write("bf.yaml", CONFIG.replace(b"emit: action", b"emit: records"))
    with collecting(configured, "digest"):
        result = update(configured, now=NOW, runner=printing(b'{"items": []}\n'), jobs=("digest",))
    assert result["ok"] is True
    [skipped] = cast("list[dict[str, object]]", result["jobs"])
    assert (skipped["status"], skipped["skipped"]) == ("skipped", "another run of this job is still active")


def test_run_suggests_prompt_job_names_too(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\njobs:\n  assistant:\n    prompt: prompts/a.md\n    harness: [agent]\n    emit: log\n",
    )
    result = CliRunner().invoke(app, ["run", "asistant", "--brain", str(brain.root)])
    assert result.exit_code == 1
    assert "unknown job asistant; check jobs in bf.yaml; did you mean assistant?" in str(result.exception)


def test_keep_spares_a_report_folder_holding_anything_else(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\njobs:\n  review:\n    run: [review]\n    emit: action\n    keep: 1\n",
    )
    report_body = b"---\ntype: action\nstatus: draft\n---\n\n# Review\n"
    first = run_job(brain, "review", runner=printing(report_body))
    folder = Path(brain.root / str(first["action"])).parent
    (folder / "inputs").mkdir()
    second = run_job(brain, "review", runner=printing(report_body))
    assert "removed" not in second
    assert (folder / "ACTION.md").read_bytes() == report_body


def test_keep_never_removes_the_action_it_just_wrote(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\njobs:\n  review:\n    run: [review]\n    emit: action\n    keep: 1\n",
    )
    report_body = b"---\ntype: action\nstatus: draft\n---\n\n# Review\n"
    ahead = f"actions/{(NOW + timedelta(days=1)).astimezone().date().isoformat()}_review-0a0a0a0a/ACTION.md"
    brain.write(ahead, report_body)
    result = run_job(brain, "review", runner=printing(report_body))
    assert "removed" not in result
    assert brain.read(str(result["action"])) == report_body
    assert brain.read(ahead) == report_body


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_keep_logs_what_it_cannot_remove_and_never_fails_the_run(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\njobs:\n  review:\n    run: [review]\n    emit: action\n    keep: 1\n",
    )
    report_body = b"---\ntype: action\nstatus: draft\n---\n\n# Review\n"
    first = str(run_job(brain, "review", runner=printing(report_body))["action"])
    folder = (brain.root / first).parent
    folder.chmod(0o500)
    try:
        second = run_job(brain, "review", runner=printing(report_body))
    finally:
        folder.chmod(0o700)
    assert "removed" not in second
    state = Queue(brain).state_of("review")
    assert state.get("error_count", 0) == 0
    assert list(cast("dict[str, str]", Queue(brain).progress("reports:review"))) == [second["action"]]
    assert (folder / "ACTION.md").read_bytes() == report_body
    assert f"kept {first.removesuffix('/ACTION.md')}" in brain.read(log_path("review")).decode()


def test_keep_puts_a_report_back_when_its_folder_gains_an_entry(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\njobs:\n  review:\n    run: [review]\n    emit: action\n    keep: 1\n",
    )
    report_body = b"---\ntype: action\nstatus: draft\n---\n\n# Review\n"
    first = str(run_job(brain, "review", runner=printing(report_body))["action"])

    def gained(_store: Store, _name: str) -> None:
        raise OSError(errno.ENOTEMPTY, "synthetic entry")

    monkeypatch.setattr(Store, "rmdir", gained)
    second = run_job(brain, "review", runner=printing(report_body))
    assert "removed" not in second
    assert brain.read(first) == report_body


def test_jobs_support_wall_clock_schedules_and_shell_gates(brain: Store) -> None:
    with pytest.raises(ValidationError):
        Job.model_validate({"run": ["echo"], "on": {"every": 60, "at": "08:00"}})
    clock_job = Job.model_validate({"run": ["echo"], "on": {"at": "08:00", "days": ["fri"], "timezone": "UTC"}})
    assert clock_job.triggers[0].at == "08:00"

    brain.write(
        "bf.yaml",
        b"version: 10\nname: fixture\njobs:\n  digest:\n"
        b"    run: [routines/digest.py]\n"
        b"    gate: [sh, -c, 'exit 1']\n"
        b"    on: {at: '08:00', timezone: UTC}\n"
        b"    emit: action\n",
    )
    assert cast("dict", report(brain, now=NOW))["jobs"]["digest"]["freshness"] == "never"
    assert len(due_jobs(brain, now=NOW)) == 1

    # Dry run reports due; live run whose gate fails returns `gated` without running main command.
    dry_due = update(brain, dry_run=True, now=NOW)
    assert cast("list[dict]", dry_due["jobs"])[0]["status"] == "due"
    called: list[str] = []

    def gated_runner(argv: list[str], *_args: object) -> bytes:
        called.append(argv[0])
        if argv[0] == "sh":
            raise jobs.ExitError(1, "program exited with status 1")
        return ACTION

    live_gated = update(brain, now=NOW, runner=gated_runner)
    assert cast("list[dict]", live_gated["jobs"])[0]["status"] == "gated"
    assert called == ["sh"]
    assert cast("dict", report(brain, now=NOW))["jobs"]["digest"]["freshness"] == "fresh"

    # When gate passes on the next scheduled day, the main command runs and writes an action.
    day2 = NOW + timedelta(days=1)
    ran = update(brain, now=day2, runner=printing(ACTION))
    assert "action" in cast("list[dict]", ran["jobs"])[0]
