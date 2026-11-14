"""Prompt jobs preserve expansion, execution, routing, extraction, gates, wakes and runtime bounds."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bf import collect, health, jobs, watch
from bf.cli import app
from bf.map import extract_pointer
from bf.models import Config, Error, Job, OnSpec
from bf.runtime import MAX_WAKE_DEPTH, MAX_WAKES, Message, Queue, QueueFullError, log_path
from bf.storage import Store
from bf.update import update
from bf.validate import validate

START = "2026-09-24T08:00:00.000000Z"
END = "2026-09-25T08:00:00.000000Z"
NOW = datetime(2026, 9, 25, 8, tzinfo=UTC)

CONFIG = b"""version: 11
name: fixture
defaults:
  post: inbox
jobs:
  inbox:
    run: [send-inbox]
    emit: messages
    send: [send-inbox]
    on: { every: 3600 }
  assistant:
    prompt: prompts/assistant.md
    harness: [agent, --print, "{{prompt}}"]
    extract: /response/say
    emit: message
    on: { every: 1800 }
  piped:
    prompt: prompts/assistant.md
    harness: [agent]
    emit: log
  paused:
    prompt: prompts/assistant.md
    harness: [agent]
    enabled: false
"""
TEMPLATE = b"# Assistant\nWindow {{start}} to {{end}} in {{brain}}.\nOwner: {{input}}\nKeep {{prompt}} and {{other}}.\n"
ANSWER = "## Two replies owed\n\nAnswer the Monday thread."
REPLY = json.dumps({"response": {"say": ANSWER + "\n"}, "usage": {"total_tokens": 42}}).encode()


@pytest.fixture
def configured(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    brain.write("prompts/assistant.md", TEMPLATE)
    return brain


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir()
    script, seen = folder / "agent", tmp_path / "seen"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {seen}.argv\ncat > {seen}.stdin\nprintf "Answer: done\\n"\n')
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{folder}:/usr/bin:/bin")
    return seen


def test_configuration_validates_prompt_jobs_and_triggers() -> None:
    assert Config.model_validate({"version": 11, "name": "x", "jobs": None}).jobs == {}
    default = Job.model_validate({"prompt": "prompts/a.md", "harness": ["agy", "-p={{prompt}}"]})
    assert (default.emit, default.timeout, default.max_bytes, default.extract, default.triggers) == (
        "log",
        300,
        64 << 20,
        "",
        [],
    )
    for path in ("notes/a.md", "prompts/../a.md", "prompts/a.txt"):
        with pytest.raises(ValidationError):
            Job(prompt=path, harness=["agy"])
    with pytest.raises(ValidationError):
        Job.model_validate({"prompt": "prompts/a.md", "harness": ["agy"], "extract": "response"})
    with pytest.raises(ValidationError):
        Job.model_validate({"prompt": "prompts/a.md", "run": ["echo"], "harness": ["agy"]})
    with pytest.raises(ValidationError):
        Job.model_validate({"harness": ["agy"]})
    # Inline JSON objects may end in `}}` alongside valid `{{prompt}}` placeholders.
    inline = Job(prompt="prompts/a.md", harness=["agy", '{"a":{"b":1}}', "{{prompt}}"])
    assert inline.harness[1] == '{"a":{"b":1}}'
    with pytest.raises(ValidationError):
        Job(prompt="prompts/a.md", harness=["agy", "{{schema_file}}"])
    with pytest.raises(ValidationError):
        Job(run=["echo", "{{prompt}}"])


@pytest.mark.parametrize(
    "trigger",
    [
        {},
        {"every": 60, "at": "08:00"},
        {"at": "09:00", "timezone": "Mars/Base"},
        {"every": 60, "days": ["mon", "mon"]},
        {"daemon": True, "every": 60},
    ],
)
def test_on_spec_validates_schedule_timezone_days_and_conflicts(trigger: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OnSpec.model_validate(trigger)


def test_days_filter_an_interval_trigger_and_at_moves_to_the_next_day() -> None:
    sunday = datetime(2026, 9, 27, 9, tzinfo=UTC)
    weekdays = OnSpec.model_validate({"every": 3600, "days": ["mon"], "timezone": "Europe/Paris"})
    # An interval due on Sunday moves to Monday's local midnight.
    assert weekdays.schedule_due(sunday, "2026-09-27T08:00:00.000000Z") == datetime(2026, 9, 27, 22, tzinfo=UTC)
    assert weekdays.schedule_due(sunday + timedelta(days=1), "2026-09-28T08:00:00.000000Z") == datetime(
        2026, 9, 28, 9, tzinfo=UTC
    )
    daily = OnSpec.model_validate({"at": "08:00", "timezone": "UTC"})
    assert daily.schedule_due(sunday, "2026-09-27T08:00:05.000000Z") == datetime(2026, 9, 28, 8, tzinfo=UTC)


def test_expansion_argv_and_stdin_preserve_harness_contract(configured: Store, agent: Path) -> None:
    calls: list[tuple[list[str], bytes]] = []

    def runner(argv: list[str], _job: Job, _store: Store, _name: str, stdin: bytes) -> bytes:
        calls.append((argv, stdin))
        if argv[0] == "send-inbox":
            payload = json.loads(stdin)
            return json.dumps({"id": "sent-" + payload["id"], "conversation": "c1"}).encode()
        return REPLY

    res = jobs.run_job(configured, "assistant", kind="schedule", since=START, until=END, runner=runner)
    argv, stdin = calls[0]
    assert stdin == b""
    assert argv[:2] == ["agent", "--print"]
    assert f"Window {START} to {END} in {configured.root}" in argv[2]
    assert "Keep {{prompt}} and {{other}}." in argv[2]
    assert cast("dict", res["delivery"])["state"] == "delivered"
    assert Queue(configured).state_of("assistant")["runs"] == 1

    # Without `{{prompt}}` in harness argv, expanded prompt flows to stdin; dry_run writes no state.
    piped = jobs.run_job(configured, "piped", stdin=b"Can you check my inbox?")
    assert piped["status"] == "ok"
    assert "Owner: Can you check my inbox?\n" in agent.with_suffix(".stdin").read_text()
    assert Queue(configured).state_of("piped")["runs"] == 1

    preview = jobs.run_job(configured, "piped", stdin=b"again", dry_run=True)
    assert preview["status"] == "ok"
    assert preview["text"] == "Answer: done"
    assert "Owner: again\n" in agent.with_suffix(".stdin").read_text()
    assert Queue(configured).state_of("piped")["runs"] == 1


def test_input_is_expanded_once_and_prompt_bounds_apply(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def capture(argv: list[str], _job: Job, _store: Store, _name: str, _stdin: bytes) -> bytes:
        seen.append(argv[-1])
        return b"ok"

    configured.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n  once:\n    prompt: prompts/assistant.md\n    harness: [agent, '{{prompt}}']\n",
    )
    jobs.run_job(configured, "once", stdin=b"literal {{brain}}", runner=capture)
    assert "Owner: literal {{brain}}" in seen[0]

    for bad_input in (b"a\x00b", b"\xff"):
        with pytest.raises(Error):
            jobs.run_job(configured, "once", stdin=bad_input, runner=capture)
    with pytest.raises(Error, match="exceeds 120000 bytes"):
        jobs.run_job(configured, "once", stdin=b"x" * 120_000, runner=capture)
    monkeypatch.setattr(jobs, "MAX_ARGUMENT", 64)
    with pytest.raises(Error, match="128 KiB"):
        jobs.run_job(configured, "once", runner=capture)


def test_unreadable_disabled_or_unknown_prompt_jobs_do_not_launch(configured: Store) -> None:
    called = False

    def fail(*_args: object) -> bytes:
        nonlocal called
        called = True
        return b""

    for name in ("paused", "absent"):
        with pytest.raises(Error):
            jobs.run_job(configured, name, dry_run=True, runner=fail)
    configured.write("prompts/assistant.md", b"\xff")
    with pytest.raises(Error, match="UTF-8"):
        jobs.run_job(configured, "assistant", dry_run=True, runner=fail)
    assert not called


def test_extract_pointers_quiet_no_reply_and_secret_redaction(configured: Store) -> None:
    assert extract_pointer(b'{"a/b":{"~x":"hi"},"usage":1}', "/a~1b/~0x") == "hi"
    assert extract_pointer(b'{"answers":["ok","context"]}', "/answers/0") == "ok"
    assert extract_pointer(b'"root"', "") == "root"

    deliveries: list[str] = []

    def ok_runner(argv: list[str], _job: Job, _store: Store, _name: str, stdin: bytes) -> bytes:
        if argv[0] == "send-inbox":
            deliveries.append(json.loads(stdin)["text"])
            return b'{"id":"d1","conversation":"c1"}'
        return b'{"response":{"say":"Launch moved."}}'

    jobs.run_job(configured, "assistant", runner=ok_runner)
    assert deliveries == ["Launch moved."]

    quiet = jobs.run_job(configured, "assistant", runner=lambda *_: b'{"response":{"say":"NO_REPLY"}}')
    assert quiet["quiet"] is True
    assert "delivery" not in quiet
    assert len(deliveries) == 1

    for bad in (b"not json", b'{"response":"secret"}', b'{"response":{"say":""}}', b'{"response":{"say":["secret"]}}'):
        with pytest.raises(Error, match="assistant") as raised:
            jobs.run_job(configured, "assistant", runner=lambda *_, out=bad: out)
        assert "secret" not in str(raised.value)
        assert log_path("assistant") in str(raised.value)
    assert "secret" not in configured.read(log_path("assistant")).decode()


def test_emit_action_writes_validated_drafts_and_prunes_oldest(configured: Store) -> None:
    configured.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n  reviewer:\n    prompt: prompts/assistant.md\n"
        b"    harness: [agent]\n    emit: action\n    keep: 2\n",
    )
    valid = b"---\ntype: action\nstatus: draft\n---\n\n# Review item\n"
    paths = [str(jobs.run_job(configured, "reviewer", runner=lambda *_: valid)["action"]) for _ in range(3)]
    assert len(set(paths)) == 3
    existing = [p for p in configured.files("actions") if p.endswith("/ACTION.md")]
    assert len(existing) == 2
    assert paths[-1] in existing

    # Non-draft or broken links fail validation before writing any draft file.
    for bad in (
        b"---\ntype: action\nstatus: stable\n---\n\n# Bad\n",
        b"---\ntype: action\nstatus: draft\n---\n\n# Bad\n\nSee [missing](../../projects/nope.md).\n",
    ):
        with pytest.raises(Error):
            jobs.run_job(configured, "reviewer", runner=lambda *_, out=bad: out)
    assert [p for p in configured.files("actions") if p.endswith("/ACTION.md")] == existing


def test_gate_skips_harness_on_nonzero_and_runs_with_placeholders_on_zero(configured: Store) -> None:
    configured.write(
        "bf.yaml",
        b"version: 11\nname: fixture\njobs:\n  dev:\n    prompt: prompts/assistant.md\n"
        b"    harness: [agent]\n    emit: log\n"
        b'    gate: [routines/gate.py, "{{start}}", "{{end}}", "{{state}}"]\n'
        b"    on: { every: 600 }\n",
    )
    seen: list[list[str]] = []

    def gated_runner(argv: list[str], _job: Job, _store: Store, _name: str, _stdin: bytes) -> bytes:
        seen.append(argv)
        raise jobs.ExitError(1, "program exited with status 1")

    res = jobs.run_job(configured, "dev", kind="schedule", since=START, until=END, runner=gated_runner)
    assert res["status"] == "gated"
    assert len(seen) == 1
    assert seen[0][:3] == ["routines/gate.py", START, END]
    assert "/.bf/state/dev" in seen[0][3]

    seen.clear()

    def pass_runner(argv: list[str], _job: Job, _store: Store, _name: str, _stdin: bytes) -> bytes:
        seen.append(argv)
        return b"Gate cleared"

    ran = jobs.run_job(configured, "dev", kind="schedule", since=START, until=END, runner=pass_runner)
    assert ran["status"] == "ok"
    assert [argv[0] for argv in seen] == ["routines/gate.py", "agent"]


def test_wakes_and_posts_enforce_bounds_depth_idempotency_and_retries(configured: Store) -> None:
    configured.write(
        "bf.yaml",
        CONFIG
        + b"  checked:\n    prompt: prompts/assistant.md\n    harness: [agent]\n"
        + b"    emit: message\n    post: inbox\n",
    )
    configured.write("prompts/assistant.md", b"Why: {{context}}\n")
    queue = Queue(configured)

    with pytest.raises(Error, match="either --in or --at"):
        jobs.wake_command(configured, job="checked", in_seconds=60, at=END)

    run_id = queue.start_run("checked", "manual", wake_depth=0)
    first = jobs.wake_command(configured, run_id=run_id, in_seconds=300, reason_text="Recheck CI", idempotency_key="k1")
    dup = jobs.wake_command(configured, run_id=run_id, in_seconds=300, reason_text="Recheck CI", idempotency_key="k1")
    assert first["created"] is True
    assert (dup["created"], dup["id"]) == (False, first["id"])

    # Active run action creation is also idempotent per `(run_id, slug)`.
    a1 = jobs.action_new(configured, "follow-up", run_id=run_id)
    a2 = jobs.action_new(configured, "follow-up", run_id=run_id)
    assert a1["action"] == a2["action"]
    assert a2.get("reused") is True
    queue.finish_run(run_id, status="completed")

    # Exceeding MAX_WAKES fails closed with QueueFullError, and wake depth limit blocks deeper wakes.
    for idx in range(MAX_WAKES - 1):
        jobs.wake_command(configured, job="checked", in_seconds=60, reason_text=f"w{idx}")
    with pytest.raises(QueueFullError, match="wake limit"):
        jobs.wake_command(configured, job="checked", in_seconds=60, reason_text="overflow")
    deep_run = queue.start_run("checked", "wake", wake_depth=MAX_WAKE_DEPTH - 1)
    with pytest.raises(Error, match="depth limit"):
        jobs.wake_command(configured, run_id=deep_run, in_seconds=60, reason_text="too deep")
    queue.finish_run(deep_run, status="completed")

    # Running due wakes passes `"kind":"wake"` and `"reason"` inside invocation context.
    due_id = str(first["id"])
    with queue.connect() as db:
        db.execute("UPDATE wakes SET due='2000-01-01T00:00:00.000000Z' WHERE id=?", (due_id,))
    contexts: list[str] = []

    def wake_runner(argv: list[str], _job: Job, _store: Store, _name: str, stdin: bytes) -> bytes:
        if argv[0] == "send-inbox":
            return b'{"id":"rcpt","conversation":"c1"}'
        contexts.append(stdin.decode())
        return b"Done"

    res = update(configured, jobs=("checked",), runner=wake_runner)
    assert res["ok"] is True
    assert '"kind":"wake"' in contexts[0]
    assert '"reason":"Recheck CI"' in contexts[0]
    assert queue.inspect(due_id)["state"] == "done"

    # post_command records misrouted receipts and failed deliveries as `uncertain` with ok=False.
    misrouted = jobs.post_command(
        configured,
        "Hi",
        to="inbox",
        thread="expected-conv",
        runner=lambda *_: b'{"id":"r1","conversation":"wrong-conv"}',
    )
    assert misrouted["ok"] is False
    assert misrouted["state"] == "uncertain"
    assert "different conversation" in str(misrouted["error"])
    failed = jobs.post_command(
        configured,
        "Hello",
        to="inbox",
        idempotency_key="out-1",
        runner=lambda *_: (_ for _ in ()).throw(jobs.ExitError(1, "program exited with status 1")),
    )
    assert (failed["ok"], failed["state"]) == (False, "uncertain")


def test_update_health_validate_and_watch_report_prompt_jobs(configured: Store) -> None:
    def runner(argv: list[str], *_args: object) -> bytes:
        if argv[0] == "send-inbox":
            return b'{"id":"d1","conversation":"c1"}'
        return REPLY

    res = update(configured, jobs=("assistant", "piped"), runner=runner)
    assert res["ok"] is True
    assert "piped" in cast("list[str]", res["manual"])
    assert any(item["job"] == "assistant" and item["status"] == "ran" for item in cast("list[dict]", res["jobs"]))

    reported = cast("dict[str, Any]", health.report(configured)["jobs"])
    assert reported["assistant"]["freshness"] == "fresh"
    assert reported["piped"]["freshness"] == "manual"
    assert reported["paused"]["enabled"] is False

    rows = {row.name: row for row in watch.snapshot(configured)}
    assert rows["assistant"].emit == "message"
    assert rows["assistant"].runs == 1
    lane = watch.Job(configured, ("assistant",))
    assert lane.argv[-2:] == ["--job", "assistant"]

    configured.root.joinpath("prompts/assistant.md").unlink()
    errors = [item["error"] for item in cast("list[dict]", validate(configured)["problems"])]
    assert "jobs.assistant.prompt: prompts/assistant.md is not a regular file" in errors


def test_cli_runs_piped_prompt_and_update_dispatches_channel_events(configured: Store, agent: Path) -> None:
    result = CliRunner().invoke(app, ["run", "piped", "--stdin", "--brain", str(configured.root)], input=b"Hello")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "ok"
    assert "Owner: Hello\n" in agent.with_suffix(".stdin").read_text()

    binary = CliRunner().invoke(app, ["run", "piped", "--stdin", "--brain", str(configured.root)], input=b"\xff")
    assert binary.exit_code == 1
    assert "UTF-8" in str(binary.exception)

    configured.write(
        "bf.yaml",
        CONFIG
        + b"  responder:\n    prompt: prompts/assistant.md\n    harness: [agent]\n"
        + b"    emit: message\n    post: inbox\n"
        + b"    on:\n      event: { kind: message.received, source: inbox }\n",
    )
    configured.write("prompts/assistant.md", b"Events: {{context}}\n")
    Queue(configured).admit(
        "inbox",
        [Message(id="m-1", sender="owner", conversation="t-1", time=START, text="Please triage")],
    )
    seen_contexts: list[str] = []

    def event_runner(argv: list[str], _job: Job, _store: Store, _name: str, stdin: bytes) -> bytes:
        if argv[0] == "send-inbox":
            return b'{"id":"sent-reply","conversation":"t-1"}'
        seen_contexts.append(stdin.decode())
        return b"Triaged"

    tick = update(configured, jobs=("responder",), runner=event_runner)
    assert tick["ok"] is True
    assert len(seen_contexts) == 1
    assert '"kind":"message.received"' in seen_contexts[0]
    assert "Please triage" in seen_contexts[0]


def test_reactive_runs_preserve_schedule_anchor_and_due_eligibility(configured: Store) -> None:
    configured.write(
        "bf.yaml",
        CONFIG
        + b"  hybrid:\n    prompt: prompts/assistant.md\n    harness: [agent]\n"
        + b"    emit: message\n    post: inbox\n"
        + b"    on:\n      - every: 1800\n      - event: { kind: message.received, source: inbox }\n",
    )

    def ok(*_args: object) -> bytes:
        return b"NO_REPLY"

    jobs.run_job(configured, "hybrid", kind="event", until="2026-09-01T12:00:05Z", runner=ok)
    assert any(j[0] == "hybrid" for j in collect.due_jobs(configured, datetime(2026, 9, 1, 12, 0, 10, tzinfo=UTC)))

    jobs.run_job(configured, "hybrid", kind="schedule", until="2026-09-01T12:00:10Z", runner=ok)
    assert not any(j[0] == "hybrid" for j in collect.due_jobs(configured, datetime(2026, 9, 1, 12, 0, 15, tzinfo=UTC)))
    state = Queue(configured).state_of("hybrid")
    assert state["runs"] == 2
    assert "scheduled_success" not in state

    jobs.run_job(configured, "hybrid", kind="event", until="2026-09-01T12:05:00Z", runner=ok)
    after_event = Queue(configured).state_of("hybrid")
    assert after_event["scheduled_success"] == "2026-09-01T12:00:10.000000Z"
    assert not any(j[0] == "hybrid" for j in collect.due_jobs(configured, datetime(2026, 9, 1, 12, 30, 9, tzinfo=UTC)))
    assert any(j[0] == "hybrid" for j in collect.due_jobs(configured, datetime(2026, 9, 1, 12, 30, 10, tzinfo=UTC)))
