"""Runtime queue durability, delivery recovery, deduplication, capacity and CLI channel roundtrips."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Any, cast

import pytest
import yaml
from pydantic import ValidationError

from bf import health, jobs, retrieve, runtime
from bf.config import load, yaml_object
from bf.models import Config, Error, Job, Query, encode, timestamp
from bf.runtime import Message, Outgoing, Queue, QueueFullError, Receipt
from bf.storage import Store, lock_file, program_state

CURRENT = f"{runtime._SCHEMA}PRAGMA user_version={runtime.VERSION};"  # noqa: SLF001 - schema integrity test


@pytest.fixture
def messaging(brain: Store) -> Store:
    brain.write(
        "bf.yaml",
        b"""version: 11
name: fixture
jobs:
  calendar:
    run: [calendar]
    emit: records
  chat:
    run: [receive-chat]
    emit: messages
    send: [send-chat]
  desktop:
    send: [notify-desktop]
  personal:
    prompt: prompts/personal.md
    harness: [harness]
    emit: message
    post: chat
    on:
      - every: 1800
      - event:
          kind: message.received
          source: chat
""",
    )
    brain.write("prompts/personal.md", b"Review {{start}} to {{end}}.\nContext: {{context}}\n")
    return brain


def message(key: str = "message-1", *, own: bool = False) -> Message:
    return Message(
        id=key,
        sender="owner",
        conversation="thread-1",
        time=timestamp((datetime.now(UTC) - timedelta(seconds=1)).isoformat()),
        text="Please explain",
        own=own,
    )


def answers(_argv: list[str], job: object, *_rest: object) -> bytes:
    """Fake runner replying to both harness prompts and `send:` commands."""
    if isinstance(job, Job) and job.send and not job.run:
        return b'{"id":"sent","conversation":"thread-1"}'
    if isinstance(job, Job) and job.send:
        return b'{"id":"sent","conversation":"thread-1"}'
    return b"Answer"


def configure(store: Store, change: Callable[[dict[str, object]], None]) -> None:
    raw = yaml_object(store.read("bf.yaml"), "bf.yaml")
    change(raw)
    store.write("bf.yaml", yaml.safe_dump(raw).encode())


def due(queue: Queue, delivery: str) -> None:
    with queue.connect() as db:
        db.execute("UPDATE deliveries SET due=? WHERE id=?", (runtime.now(), delivery))


def window() -> tuple[str, str]:
    end = datetime.now(UTC)
    return timestamp((end - timedelta(days=1)).isoformat()), timestamp(end.isoformat())


def test_replayed_input_admits_once_and_delivers_receipt_with_conversation(messaging: Store) -> None:
    incoming = message()
    raw = encode({"messages": [incoming.model_dump()], "cursor": "c1"})
    first = jobs.run_job(messaging, "chat", runner=lambda *_: raw)
    assert first["messages"] == 1
    second = jobs.run_job(messaging, "chat", runner=lambda *_: raw)
    assert second["messages"] == 0
    assert second["dropped"] == 1
    queue = Queue(messaging)
    assert queue.state_of("chat")["cursor"] == "c1"
    calls: list[tuple[list[str], object]] = []

    def runner(argv: list[str], job: Job, _store: Store, _name: str, stdin: bytes) -> bytes:
        calls.append((argv, json.loads(stdin) if job.send and not job.prompt else stdin.decode()))
        if job.send and not job.prompt:
            return b'{"id":"answer-1","conversation":"thread-1"}'
        return b"Here is why."

    dispatched = jobs.dispatch_events(messaging, ["personal"], runner=runner)
    assert len(dispatched) == 1
    assert cast("dict[str, object]", dispatched[0]["delivery"])["state"] == "delivered"
    assert len(calls) == 2
    assert "message-1" in str(calls[0][1])
    assert queue.report()["deliveries"] == []


def test_answer_survives_failed_delivery_without_reinvoking_harness(messaging: Store) -> None:
    calls: list[str] = []

    def runner(argv: list[str], job: Job, *_rest: object) -> bytes:
        calls.append(argv[0])
        if job.send:
            raise Error("simulated uncertain send")
        return b"Saved answer"

    res = jobs.run_job(messaging, "personal", thread="thread-1", runner=runner)
    assert cast("dict[str, object]", res["delivery"])["state"] == "uncertain"
    queue = Queue(messaging)
    pending = cast("list[dict[str, object]]", queue.report()["deliveries"])
    assert len(pending) == 1
    did = str(pending[0]["id"])
    assert pending[0]["state"] == "uncertain"
    queue.retry(did)
    assert jobs.deliver_due(messaging, runner=lambda *_: b'{"id":"sent","conversation":"thread-1"}')
    assert calls == ["harness", "send-chat"]
    assert queue.report()["deliveries"] == []


def test_quiet_output_sends_nothing_and_requires_no_delivery(messaging: Store) -> None:
    for body in (b"", b"   \n", b"NO_REPLY\n"):
        res = jobs.run_job(messaging, "personal", runner=lambda *_args, body=body: body)
        assert res["quiet"] is True
    assert Queue(messaging).report()["deliveries"] == []


def test_passive_message_read_checks_valid_shape(messaging: Store) -> None:
    incoming = message()
    res = jobs.run_job(
        messaging,
        "chat",
        dry_run=True,
        runner=lambda *_: encode({"messages": [incoming.model_dump()]}),
    )
    assert res["dry_run"] is True
    assert res["messages"] == 1
    assert not Queue(messaging).exists()


def test_receipt_cannot_change_reply_conversation(messaging: Store) -> None:
    queue = Queue(messaging)
    res = jobs.post_command(
        messaging,
        "Answer",
        to="chat",
        thread="thread-1",
        runner=lambda *_: b'{"id":"sent","conversation":"another-thread"}',
    )
    assert res["ok"] is False
    saved = queue.inspect(str(res["id"]))
    assert saved["state"] == "uncertain"
    assert "different conversation" in str(saved["error"])


@pytest.mark.parametrize(
    "change",
    [
        {"refresh": 10},
        {"lookback": 10},
        {"post": ["chat"]},
        {"post": "unknown"},
        {"extract": "result"},
        {"on": [{"every": 0}]},
        {"run": ["/usr/bin/env"]},
        {"outputs": [{"channel": {"name": "chat"}}]},
    ],
)
def test_invalid_configuration_fails_before_execution(messaging: Store, change: dict[str, object]) -> None:
    raw = yaml.safe_load(messaging.read("bf.yaml"))
    raw["jobs"]["personal"].update(change)
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_v9_trigger_shape_fails_with_unknown_key_diagnostic(messaging: Store) -> None:
    trigger = {"calendar": {"event": "sensor.changed"}}
    configure(messaging, lambda c: cast("dict[str, dict[str, object]]", c["jobs"])["personal"].update(on=trigger))
    with pytest.raises(Error, match=re.escape("jobs.personal.on")):
        load(messaging)


def test_channel_identity_is_required_and_own_echo_is_not_queued(messaging: Store) -> None:
    with pytest.raises(ValidationError):
        Message.model_validate({"id": "x", "conversation": "t", "time": "2026-01-01T00:00:00Z", "text": "hello"})
    queue = Queue(messaging)
    assert queue.admit("chat", [message(own=True)]) == []
    assert queue.pending_messages("chat") == []


def test_scoped_delivery_respects_job_filter(messaging: Store) -> None:
    configure(
        messaging,
        lambda c: cast("dict[str, dict[str, object]]", c["jobs"]).update(
            other={"prompt": "prompts/personal.md", "harness": ["harness"], "emit": "message", "post": "chat"}
        ),
    )
    queue = Queue(messaging)
    jobs.post_command(messaging, "From personal", to="chat", deliver=False)
    queue.enqueue_delivery(
        "chat", Outgoing(id="other-1", text="From other", conversation="t2"), ["send-chat"], job="other"
    )
    delivering_calls: list[str] = []

    def fail_send(argv: list[str], *_rest: object) -> bytes:
        delivering_calls.append(argv[0])
        return b'{"id":"sent","conversation":"t2"}'

    assert jobs.deliver_due(messaging, job="other", runner=fail_send)
    assert queue.inspect("other-1")["state"] == "delivered"
    assert len(cast("list[dict[str, object]]", queue.report()["deliveries"])) == 1


def test_edited_message_keeps_its_admitted_version_and_advances_checkpoint(messaging: Store) -> None:
    source = message()
    queue = Queue(messaging)
    assert len(queue.admit("chat", [source])) == 1
    changed = source.model_copy(update={"text": "changed"})
    admitted = queue.admit("chat", [changed, message("message-2")], checkpoint=("test", {"end": "x"}))
    assert [m.id for m in admitted] == ["message-2"]
    assert queue.progress("test") == {"end": "x"}
    pending = queue.pending_messages("chat")
    assert len(pending) == 2
    assert pending[0]["text"] == "Please explain"


def test_command_that_never_started_is_failed_or_retried_when_idempotent(messaging: Store) -> None:
    queue = Queue(messaging)
    missing = lambda *_args: (_ for _ in ()).throw(jobs.NotStartedError("send-chat is not on PATH"))  # noqa: E731
    res = jobs.post_command(messaging, "Hello", to="chat", idempotency_key="standalone", runner=missing)
    assert res["ok"] is False
    sent = queue.inspect("standalone")
    assert sent["state"] == "failed"
    assert "retry it with bf queue ID --retry" in str(sent["error"])

    configure(
        messaging,
        lambda c: cast("dict[str, dict[str, object]]", c["jobs"])["chat"].update(idempotent=True),
    )
    res_idem = jobs.post_command(messaging, "Hello", to="chat", idempotency_key="idem-standalone", runner=missing)
    assert res_idem["ok"] is True
    assert queue.inspect("idem-standalone")["state"] == "pending"


def test_delivery_rescheduled_while_waiting_for_its_lock_is_not_sent(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = Queue(messaging)
    queue.enqueue_delivery("chat", Outgoing(id="standalone", text="Hello"), ["send-chat"])
    original = jobs.storage.collecting

    @contextmanager
    def raced(store: Store, name: str) -> Generator[None]:
        later = timestamp((datetime.now(UTC) + timedelta(minutes=5)).isoformat())
        queue.delivery_failed("standalone", "send failed", outcome="pending", due=later)
        with original(store, name):
            yield

    monkeypatch.setattr(jobs.storage, "collecting", raced)
    assert not jobs.deliver_due(messaging, runner=lambda *_: pytest.fail("must wait for its backoff"))
    assert queue.inspect("standalone")["attempts"] == 0


def test_old_backlog_message_keeps_its_admission_marker(messaging: Store) -> None:
    queue = Queue(messaging)
    old = message().model_copy(update={"time": timestamp((datetime.now(UTC) - timedelta(days=40)).isoformat())})
    assert len(queue.admit("chat", [old])) == 1
    with queue.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='message.received'").fetchone()[0] == 1


def test_receipts_suppress_echoes_and_duplicate_send_ids(messaging: Store) -> None:
    queue = Queue(messaging)
    outgoing = Outgoing(id="manual-delivery", text="Hello", conversation="thread-1")
    _, created = queue.enqueue_delivery("chat", outgoing, ["send-chat"], idempotent=True)
    assert created
    assert jobs.deliver_due(messaging, runner=lambda *_: b'{"id":"echo","conversation":"thread-1"}')
    _, created_again = queue.enqueue_delivery("chat", outgoing, ["send-chat"], idempotent=True)
    assert not created_again
    assert not jobs.deliver_due(messaging, runner=lambda *_: pytest.fail("already delivered"))
    assert queue.admit("chat", [message("echo")]) == []
    assert queue.report()["deliveries"] == []
    with pytest.raises(Error, match="already used"):
        queue.enqueue_delivery(
            "chat", outgoing.model_copy(update={"text": "different"}), ["send-chat"], idempotent=True
        )


def test_queue_capacity_rejects_excess_obligations_without_advancing_progress(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "MAX_PENDING", 1)
    queue = Queue(messaging)
    queue.enqueue_delivery("chat", Outgoing(id="first", text="hi"), ["send-chat"])
    with pytest.raises(QueueFullError, match="unresolved-work slots"):
        queue.enqueue_delivery("chat", Outgoing(id="second", text="hi"), ["send-chat"])
    assert len(cast("list[dict[str, object]]", queue.report()["deliveries"])) == 1
    assert queue.progress("cursor:chat") == {}


def test_changed_send_command_never_reroutes_queued_work(messaging: Store) -> None:
    queue = Queue(messaging)
    jobs.post_command(messaging, "Hello", to="chat", idempotency_key="pending-1", deliver=False)
    messaging.write("bf.yaml", messaging.read("bf.yaml").replace(b"send-chat", b"different-destination"))
    assert jobs.deliver_due(messaging, runner=lambda *_: pytest.fail("must not execute"))
    saved = queue.inspect("pending-1")
    assert saved["state"] == "uncertain"
    assert "configuration changed" in str(saved["error"])


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b"{}"])
def test_invalid_messages_output_fails_closed(messaging: Store, raw: bytes) -> None:
    if raw == b"[]":
        res = jobs.run_job(messaging, "chat", runner=lambda *_: raw)
        assert res["messages"] == 0
        return
    with pytest.raises(Error, match="invalid"):
        jobs.run_job(messaging, "chat", runner=lambda *_: raw)


def test_queue_corruption_is_preserved_and_never_rebuilt(messaging: Store) -> None:
    queue = Queue(messaging)
    queue.set_progress("check", {"ok": True})
    queue.store.write(runtime.QUEUE, b"corrupt evidence")
    (messaging.root / runtime.QUEUE).chmod(0o600)
    with pytest.raises(Error, match="runtime database is unavailable"):
        queue.report()
    assert queue.store.read(runtime.QUEUE) == b"corrupt evidence"


def test_queue_connection_preflight_preserves_another_threads_sqlite_lock(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event, current_thread

    queue = Queue(messaging)
    queue.set_progress("init", {"ok": True})
    connect: Any = runtime.sqlite3.connect
    preflight, resume = Event(), Event()
    errors: list[Exception] = []

    def opening(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if current_thread().name == "queue-probe":
            preflight.set()
            assert resume.wait(5)
        return cast("sqlite3.Connection", connect(*args, **kwargs))

    def probe() -> None:
        try:
            queue.progress("owner")
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(runtime.sqlite3, "connect", opening)
    thread = Thread(target=probe, name="queue-probe", daemon=True)
    try:
        with queue.connect() as owner:
            queue.checkpoint(owner, "owner", {"committed": True})
            thread.start()
            assert preflight.wait(5)
            competitor = subprocess.run(  # noqa: S603 - isolated SQLite lock probe
                [
                    sys.executable,
                    "-c",
                    (
                        "import sqlite3,sys\n"
                        "db=sqlite3.connect(sys.argv[1], timeout=.1)\n"
                        "try: db.execute('BEGIN IMMEDIATE')\n"
                        "except sqlite3.OperationalError as error: sys.exit(0 if 'locked' in str(error) else 2)\n"
                        "else: db.rollback(); sys.exit(1)\n"
                    ),
                    str(queue.store.root / runtime.QUEUE),
                ],
                check=False,
                capture_output=True,
                timeout=5,
            )
            assert competitor.returncode == 0, "another process acquired a writer lock inside our transaction"
    finally:
        resume.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert queue.progress("owner") == {"committed": True}


def test_cli_post_run_and_queue_roundtrip(brain: Store) -> None:
    from typer.testing import CliRunner, Result

    from bf.cli import app

    brain.write(
        "bf.yaml",
        b"""version: 11
name: fixture
jobs:
  local:
    run: [python3, channels/fake.py, read, "{{start}}", "{{end}}"]
    emit: messages
    send: [python3, channels/fake.py, send]
""",
    )
    brain.write(
        "channels/fake.py",
        b"""import json, pathlib, sys
op = sys.argv[1]
if op == "read":
    print(json.dumps({"messages": []}))
else:
    value = json.load(sys.stdin)
    if pathlib.Path("fail").exists():
        print("PRIVATE PROVIDER ERROR", file=sys.stderr)
        sys.exit(1)
    pathlib.Path("sent.json").write_text(json.dumps(value))
    print(json.dumps({"id": "native-id", "conversation": value.get("conversation") or "thread"}))
""",
    )
    runner = CliRunner()

    def invoke(*args: str, body: str | None = None, code: int = 0) -> Result:
        result = runner.invoke(app, [*args, "--brain", str(brain.root)], input=body)
        assert result.exit_code == code, result.output + str(result.exception)
        return result

    history = invoke("run", "local", "--since", "2026-10-15T00:00:00Z", "--until", "2026-10-16T00:00:00Z")
    assert json.loads(history.stdout)["messages"] == 0
    assert Queue(brain).report()["deliveries"] == []
    brain.write("fail", b"yes")
    failed = invoke("post", "--to", "local", "--key", "stable", "--stdin", body="PRIVATE ANSWER", code=1)
    saved = json.loads(failed.stdout)
    assert saved["state"] == "uncertain"
    assert "PRIVATE" not in failed.stdout
    listed = invoke("queue")
    assert "PRIVATE ANSWER" not in listed.stdout
    detailed = invoke("queue", "stable")
    assert json.loads(detailed.stdout)["payload"]["text"] == "PRIVATE ANSWER"
    brain.root.joinpath("fail").unlink()
    retried = invoke("queue", "stable", "--retry")
    assert json.loads(retried.stdout) == {"id": "stable", "status": "delivered"}
    duplicate = invoke("post", "--to", "local", "--key", "stable", "--stdin", body="PRIVATE ANSWER")
    assert json.loads(duplicate.stdout) == {
        "ok": True,
        "id": "stable",
        "job": "local",
        "state": "delivered",
        "created": False,
        "receipt": {"id": "native-id", "conversation": "thread"},
    }
    assert json.loads(brain.read("sent.json"))["id"] == "stable"
    invoke("queue", "--retry", code=2)
    invoke("queue", "stable", "--retry", "--cancel", code=2)


def test_cancelled_work_cannot_be_resurrected_by_a_late_failure(messaging: Store) -> None:
    queue = Queue(messaging)
    wake_id, _ = queue.request_wake("personal", reason="test")
    queue.retry(wake_id, cancel=True)
    queue.finish_wake(wake_id, state="done", error="late")
    # Cancelled delivery ignores later delivery_failed calls.
    queue.enqueue_delivery("chat", Outgoing(id="late-send", text="answer"), ["send-chat"])
    queue.retry("late-send", cancel=True)
    queue.delivery_failed("late-send", "late failure")
    assert queue.inspect("late-send")["state"] == "cancelled"


def test_compaction_bounds_terminal_bodies_and_keeps_deduplication(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "BODY_BUDGET", 64)
    queue = Queue(messaging)
    source = message()
    assert len(queue.admit("chat", [source])) == 1
    out = Outgoing(id="out-1", text="Saved reply " * 100, conversation="thread-1")
    queue.enqueue_delivery("chat", out, ["send-chat"])
    queue.delivered("out-1", "chat", out, Receipt(id="native-answer", conversation="thread-1"))
    assert queue.inspect("out-1")["payload"] == "{}"
    assert queue.admit("chat", [source]) == []
    assert len(queue.admit("chat", [message("new")])) == 1


def test_retention_counts_utf8_bytes_without_losing_unresolved_delivery(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "BODY_BUDGET", 64)
    incoming = message().model_copy(update={"text": "🧠" * 60})
    queue = Queue(messaging)
    assert len(queue.admit("chat", [incoming])) == 1
    assert queue.conversation("chat", "thread-1", incoming.time) == []
    queue.enqueue_delivery("chat", Outgoing(id="pending-big", text="🧠" * 60, conversation="thread-1"), ["send-chat"])
    queue.compact()
    # Unresolved delivery payload is never stripped while still pending.
    assert json.loads(str(queue.inspect("pending-big")["payload"]))["text"] == "🧠" * 60
    with queue.connect() as db:
        assert db.execute("SELECT data FROM events WHERE kind='message.received'").fetchone()[0] == "{}"


def test_finite_manual_sends_compact_without_a_watcher(messaging: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "BODY_BUDGET", 128)
    queue = Queue(messaging)
    for index in range(3):
        outgoing = Outgoing(id=f"send-{index}", text="🧠" * 200)
        queue.enqueue_delivery("chat", outgoing, ["send-chat"])
        queue.delivered(outgoing.id, "chat", outgoing, Receipt(id=f"receipt-{index}", conversation="thread-1"))
        assert queue.inspect(outgoing.id)["payload"] == "{}"
    assert queue.report()["deliveries"] == []
    assert queue.conversation("chat", "thread-1", "2099-01-01T00:00:00Z") == []


def test_removed_job_still_exposes_unresolved_work(messaging: Store) -> None:
    from bf.health import report

    queue = Queue(messaging)
    queue.enqueue_delivery("chat", Outgoing(id="u-1", text="hello"), ["send-chat"], job="personal")
    queue.delivery_failed("u-1", "needs review", outcome="uncertain")
    messaging.write("bf.yaml", b"version: 11\nname: fixture\njobs: {}\n")
    status = report(messaging)
    assert not status["healthy"]
    assert cast("dict[str, object]", status["queue"])["uncertain"] == 1
    attention = cast("list[dict[str, object]]", status["attention"])
    assert {"kind": "job", "name": "personal", "freshness": "unknown", "failed": True} in attention


def test_real_watch_replies_once_and_recovers_stream_redelivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "demo"
    shutil.copytree(Path(__file__).parents[1] / "examples/channels", root)
    store = Store(root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    deadline = time.monotonic() + 120
    for attempt in range(2):
        with (tmp_path / f"watch-{attempt}.jsonl").open("wb") as output:
            child = subprocess.Popen(  # noqa: S603 - fictional local adapter in an isolated brain
                [sys.executable, "-m", "bf", "watch", "--brain", str(root), "--json"],
                stdout=output,
                stderr=subprocess.PIPE,
                env={**os.environ, "BF_BRAIN": str(root)},
            )
            try:
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        pytest.fail(f"watch exited: {child.communicate(timeout=30)[1].decode()}")
                    q = Queue(store)
                    report = q.report()
                    lines = (
                        [json.loads(line) for line in store.read("logs/deliveries.jsonl").splitlines()]
                        if (root / "logs/deliveries.jsonl").exists()
                        else []
                    )
                    acked = [line for line in lines if line.get("text") == "Acknowledged."]
                    daemon_ok = attempt == 0 or bool(q.state_of("local").get("last_success"))
                    settled = (
                        len(acked) == 2
                        and not report["deliveries"]
                        and report["status"]
                        and cast("dict[str, object]", report["status"])["pending"] == 0
                        and daemon_ok
                    )
                    if settled and q.event_cursor("personal")[0]:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("watch did not settle examples/channels deliveries cleanly")
            finally:
                child.terminate()
                _, stderr = child.communicate(timeout=30)
            unexpected = [line for line in stderr.decode().splitlines() if "owner-chat refreshes every 1s" not in line]
            assert not unexpected, unexpected
        lines = [json.loads(line) for line in store.read("logs/deliveries.jsonl").splitlines()]
        acked = [line for line in lines if line.get("text") == "Acknowledged."]
        assert len(acked) == 2
        assert Queue(store).report()["deliveries"] == []


def test_interrupted_idempotent_delivery_retries_while_other_waits_for_inspection(messaging: Store) -> None:
    configure(messaging, lambda c: cast("dict[str, dict[str, object]]", c["jobs"])["chat"].update(idempotent=True))
    queue = Queue(messaging)
    queue.enqueue_delivery("chat", Outgoing(id="c-1", text="one"), ["send-chat"], idempotent=True)
    queue.enqueue_delivery("desktop", Outgoing(id="d-1", text="two"), ["notify-desktop"], idempotent=False)
    for did in ("c-1", "d-1"):
        assert queue.sending(did)
    queue.recover_attempts()
    assert queue.inspect("c-1")["state"] == "pending"
    assert queue.inspect("d-1")["state"] == "uncertain"
    due(queue, "c-1")
    assert jobs.deliver_due(messaging, runner=answers, key="c-1")
    assert queue.inspect("c-1")["state"] == "delivered"


def test_failing_idempotent_delivery_stops_retrying_at_max_attempts(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(messaging, lambda c: cast("dict[str, dict[str, object]]", c["jobs"])["chat"].update(idempotent=True))
    monkeypatch.setattr(runtime, "MAX_ATTEMPTS", 2)
    queue = Queue(messaging)
    queue.enqueue_delivery("chat", Outgoing(id="d-fail", text="Answer"), ["send-chat"], idempotent=True, job="personal")

    def rejected(*_: object) -> bytes:
        raise Error("provider rejected the message")

    for _ in range(2):
        due(queue, "d-fail")
        assert jobs.deliver_due(messaging, runner=rejected, key="d-fail")
    saved = queue.inspect("d-fail")
    assert saved["state"] == "failed"
    assert "stopped after 2 attempts" in str(saved["error"])


def test_misrouted_receipt_waits_for_inspection_even_on_idempotent_send_job(messaging: Store) -> None:
    configure(messaging, lambda c: cast("dict[str, dict[str, object]]", c["jobs"])["chat"].update(idempotent=True))
    queue = Queue(messaging)
    res = jobs.post_command(
        messaging,
        "Reply",
        to="chat",
        thread="thread-1",
        idempotency_key="mis-1",
        runner=lambda *_: b'{"id":"sent","conversation":"another-thread"}',
    )
    assert res["state"] == "uncertain"
    saved = queue.inspect("mis-1")
    assert saved["state"] == "uncertain"
    assert "different conversation" in str(saved["error"])


def test_identical_resend_after_compaction_returns_saved_delivery(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = Queue(messaging)
    outgoing = Outgoing(id="weekly-2026-42", text="Weekly review", conversation="thread-1")
    queue.enqueue_delivery("chat", outgoing, ["send-chat"])
    assert jobs.deliver_due(messaging, runner=lambda *_: b'{"id":"native","conversation":"thread-1"}')
    monkeypatch.setattr(runtime, "BODY_BUDGET", 0)
    queue.compact()
    assert queue.inspect(outgoing.id)["payload"] == "{}"
    _, created = queue.enqueue_delivery("chat", outgoing, ["send-chat"])
    assert not created
    assert not jobs.deliver_due(messaging, runner=lambda *_: pytest.fail("already delivered"))
    with pytest.raises(Error, match="already used"):
        queue.enqueue_delivery("chat", outgoing.model_copy(update={"text": "different"}), ["send-chat"])


def test_send_receipts_suppress_echoes_after_conversation_cache_trims(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = Queue(messaging)
    queue.enqueue_delivery("chat", Outgoing(id="manual", text="Hello", conversation="thread-1"), ["send-chat"])
    assert jobs.deliver_due(messaging, runner=lambda *_: b'{"id":"echo","conversation":"thread-1"}')
    monkeypatch.setattr(runtime, "BODY_BUDGET", 0)
    queue.compact()
    assert queue.conversation("chat", "thread-1", "2099-01-01T00:00:00Z") == []
    assert queue.admit("chat", [message("echo")]) == []
    assert queue.report()["deliveries"] == []


def test_finished_runs_and_deliveries_leave_no_lock_files(messaging: Store) -> None:
    jobs.run_job(messaging, "personal", thread="thread-1", runner=answers)
    locks, _ = lock_file(messaging, "write.lock")
    assert not [path.name for path in locks.root.iterdir() if ".run-" in path.name or ".delivery-" in path.name]


def test_queue_commits_are_durable_when_they_return(messaging: Store) -> None:
    with Queue(messaging).connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 3
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_unreadable_queue_does_not_block_retrieval_or_program_state(messaging: Store) -> None:
    queue_file = messaging.root / runtime.QUEUE
    Queue(messaging).set_progress("init", {"ok": True})
    queue_file.write_bytes(b"not a database")
    (messaging.root / runtime.QUEUE).chmod(0o600)
    program_state(messaging, "calendar").write("cursor", b"1")
    with pytest.raises(Error, match="runtime database is unavailable"):
        Queue(messaging).report()
    assert program_state(messaging, "calendar").read("cursor") == b"1"
    assert retrieve.search(messaging, Query(text="offline"))["items"]
    assert queue_file.read_bytes() == b"not a database"


def test_interrupted_run_or_send_is_recorded_before_process_ends(messaging: Store) -> None:
    queue = Queue(messaging)

    def interrupted(*_: object) -> bytes:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        jobs.run_job(messaging, "personal", runner=interrupted)
    runs = cast("list[dict[str, object]]", queue.report()["active_runs"])
    assert len(runs) == 1
    assert runs[0]["status"] == "uncertain"

    queue.enqueue_delivery("chat", Outgoing(id="manual", text="Hello"), ["send-chat"])
    with pytest.raises(KeyboardInterrupt):
        jobs.deliver_due(messaging, runner=interrupted, key="manual")
    sent = queue.inspect("manual")
    assert (sent["state"], sent["error"]) == ("uncertain", "interrupted send; inspect before retrying")


def test_queue_resolves_abandoned_delivery_via_cli(brain: Store) -> None:
    from typer.testing import CliRunner

    from bf.cli import app

    brain.write("bf.yaml", b"version: 11\nname: fixture\njobs:\n  local:\n    send: [send-local]\n")
    queue = Queue(brain)
    queue.enqueue_delivery("local", Outgoing(id="stable", text="Hello"), ["send-local"])
    assert queue.sending("stable")
    res = CliRunner().invoke(app, ["queue", "stable", "--cancel", "--brain", str(brain.root)])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == {"id": "stable", "status": "cancelled"}


def test_identity_tombstones_expire_after_retention(messaging: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = Queue(messaging)
    jobs.post_command(messaging, "Done", to="chat", idempotency_key="done-1", runner=answers)
    queue.admit("chat", [message("m-done")])
    queue.enqueue_delivery("chat", Outgoing(id="waiting", text="Hold", conversation="t-2"), ["send-chat"])
    monkeypatch.setattr(runtime, "RETENTION", timedelta(seconds=-60))
    queue.compact()
    with queue.connect() as db:
        assert db.execute("SELECT count(*) FROM deliveries WHERE state='delivered'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert queue.inspect("waiting")["state"] == "pending"


def test_drained_queue_follows_renamed_or_moved_brain(messaging: Store) -> None:
    Queue(messaging).set_progress("init", {"ok": True})
    messaging.write("bf.yaml", messaging.read("bf.yaml").replace(b"name: fixture", b"name: renamed"))
    assert Queue(messaging).report()["deliveries"] == []
    moved = messaging.root.parent / "moved"
    messaging.root.rename(moved)
    store = Store(moved)
    assert Queue(store).report()["deliveries"] == []
    assert len(Queue(store).admit("chat", [message()])) == 1


def test_renamed_brain_with_unresolved_work_refuses_queue_until_restored(messaging: Store) -> None:
    queue = Queue(messaging)
    queue.enqueue_delivery("chat", Outgoing(id="u-1", text="Hello"), ["send-chat"])
    original = messaging.read("bf.yaml")
    messaging.write("bf.yaml", original.replace(b"name: fixture", b"name: renamed"))
    with pytest.raises(Error, match="unresolved work under a previous path or name"):
        Queue(messaging).report()
    messaging.write("bf.yaml", original)
    assert len(cast("list[dict[str, object]]", Queue(messaging).report()["deliveries"])) == 1


def test_new_device_number_and_other_inode_isolation(messaging: Store, tmp_path: Path) -> None:
    queue = Queue(messaging)
    queue.enqueue_delivery("chat", Outgoing(id="u-1", text="Hello"), ["send-chat"])
    program_state(messaging, "calendar").write("cursor", b"42")
    again = Store(messaging.root)
    again.identity = f"999-{again.identity.rsplit('-', 1)[1]}"
    assert program_state(again, "calendar").read("cursor") == b"42"
    assert len(cast("list[dict[str, object]]", Queue(again).report()["deliveries"])) == 1

    (tmp_path / "other").mkdir()
    other = Store(tmp_path / "other")
    other.write("bf.yaml", messaging.read("bf.yaml"))
    other.identity = f"999-{messaging.identity.rsplit('-', 1)[1]}"
    assert Queue(other).report()["deliveries"] == []
    assert Queue(other).store.root != Queue(messaging).store.root


@pytest.mark.parametrize(
    ("script", "refusal"),
    [
        (
            CURRENT.replace(f"user_version={runtime.VERSION}", f"user_version={runtime.VERSION - 1}"),
            "runtime database predates this release; preserve it and follow upgrade steps in the changelog",
        ),
        (
            CURRENT + "CREATE TABLE extra (x);",
            "unexpected runtime database schema; preserve it and repair it",
        ),
        (
            CURRENT.replace(f"user_version={runtime.VERSION}", f"user_version={runtime.VERSION + 1}"),
            "unsupported runtime database version; preserve it and use its matching BF release",
        ),
        (
            "CREATE TABLE notes (x);",
            "unsupported runtime database version; preserve it and use its matching BF release",
        ),
    ],
    ids=["older-version", "extra-table", "newer-version", "foreign"],
)
def test_another_queue_format_is_refused_and_preserved(messaging: Store, script: str, refusal: str) -> None:
    queue = Queue(messaging)
    queue.set_progress("init", {"ok": True})
    path = queue.store.root / runtime.QUEUE
    path.unlink()
    db = sqlite3.connect(path)
    try:
        db.executescript(script)
    finally:
        db.close()
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(Error, match=re.escape(refusal)):
        queue.report()
    assert path.read_bytes() == before
    entry = health.report(messaging)
    problems = [cast("dict[str, object]", problem)["error"] for problem in cast("list[object]", entry["problems"])]
    assert refusal in problems
    assert path.read_bytes() == before


def test_runtime_queue_guards_recovery_and_edge_paths(messaging: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # environment without PATH and absent-DB read-only helpers.
    monkeypatch.delenv("PATH", raising=False)
    assert "PATH" not in runtime.environment()
    queue = Queue(messaging)
    assert queue.failing() == ([], [])
    queue.finish_run("absent")
    with pytest.raises(Error, match="unknown queue entry"):
        queue.inspect("missing")
    with pytest.raises(Error, match="unknown queue entry"):
        queue.retry("missing")

    # start_run / get_run validation, event digest mismatch, mark_message, conversation 32 KiB cap.
    with pytest.raises(Error, match="invalid run id"):
        queue.start_run("personal", "manual", run_id="bad\nid")
    with pytest.raises(Error, match="invalid wake depth"):
        queue.start_run("personal", "manual", wake_depth=runtime.MAX_WAKE_DEPTH + 1)
    with pytest.raises(Error, match="unknown run id"):
        queue.get_run("")

    queue.record_event("record.added", "mail", {"n": 1}, key="evt-1")
    with pytest.raises(Error, match="event id reused with different content"):
        queue.record_event("record.added", "mail", {"n": 2}, key="evt-1")

    m1 = Message(
        id="m1",
        sender="owner",
        conversation="conv-cap",
        time="2026-10-01T00:00:01Z",
        text="x" * 20_000,
    )
    m2 = Message(
        id="m2",
        sender="owner",
        conversation="conv-cap",
        time="2026-10-01T00:00:02Z",
        text="y" * 20_000,
    )
    queue.admit("chat", [m1, m2])
    assert len(queue.conversation("chat", "conv-cap")) == 1
    queue.mark_message("chat", "m1", "done")
    assert [m["id"] for m in queue.pending_messages("chat")] == ["chat:m2"]

    # recover_attempts interrupts running active_run and retry cancels uncertain run / rejects done wake or delivery.
    rid = queue.start_run("personal", "manual")
    queue.recover_attempts(rid)
    assert queue.inspect(rid)["status"] == "uncertain"
    with pytest.raises(Error, match="only uncertain runs"):
        queue.retry(rid)
    queue.retry(rid, cancel=True)
    assert queue.inspect(rid)["status"] == "cancelled"

    wid, _ = queue.request_wake("personal", reason="done")
    assert queue.claim_wake(wid)
    queue.finish_wake(wid, state="done")
    with pytest.raises(Error, match="wake cannot be retried"):
        queue.retry(wid)

    did, _ = queue.enqueue_delivery("chat", Outgoing(id="done-d", text="ok"), ["send-chat"])
    assert queue.sending(did)
    queue.delivered(did, "chat", Outgoing(id="done-d", text="ok"), Receipt(id="r", conversation="c"))
    with pytest.raises(Error, match="delivery cannot be retried"):
        queue.retry(did)

    # _check_binding survives broken bf.yaml, corrupt checkpoint fails closed, reserve trim -> QueueFullError.
    good_yaml = messaging.read("bf.yaml")
    messaging.write("bf.yaml", b":::invalid yaml:::")
    assert queue.status()["pending"] == 0
    messaging.write("bf.yaml", good_yaml)

    with queue.connect() as db:
        db.execute("INSERT OR REPLACE INTO checkpoints VALUES ('bad', '[1,2]')")
        with pytest.raises(QueueFullError, match="size limit"):
            queue.reserve(db, 0, 8192, limit=4096)
    with pytest.raises(Error, match="invalid runtime checkpoint"):
        queue.progress("bad")

    # Non-private permissions on runtime.sqlite3 fail closed; user_version=0 DB with tables fails closed.
    db_path = messaging.root / runtime.QUEUE
    db_path.chmod(0o644)
    with pytest.raises(Error, match="private regular file"):
        queue.status()
    db_path.unlink()
    raw_db = sqlite3.connect(db_path)
    try:
        raw_db.execute("CREATE TABLE stray (x INT)")
    finally:
        raw_db.close()
    db_path.chmod(0o600)
    with pytest.raises(Error, match="unknown runtime database format"):
        queue.set_progress("k", {"v": 1})


def test_runtime_v4_schema_and_incident_alert_cooldown_and_recovery(messaging: Store) -> None:
    assert runtime.VERSION == 4
    queue = Queue(messaging)
    assert queue.incidents() == []
    assert queue.status()["incidents"] == 0

    # First opening with no defaults.post still records incident silently, and resolves silently.
    t0 = timestamp("2026-10-01T00:00:00Z")
    queue.open_incident("calendar", "offline error", at=t0)
    with queue.read() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "item_checkpoints", "event_cursors", "incidents"} <= tables
    assert len(queue.incidents()) == 1
    assert queue.status()["incidents"] == 1
    assert queue.report()["deliveries"] == []
    queue.resolve_incident("calendar")
    assert queue.incidents() == []
    assert queue.report()["deliveries"] == []

    # With defaults.post configured: alert once, suppress within INCIDENT_REMIND, remind after 6h, and notify recovery.
    configure(messaging, lambda raw: raw.__setitem__("defaults", {"post": "chat"}))
    queue.open_incident("calendar", "network timeout", at=t0)
    inc1 = queue.incidents()
    assert len(inc1) == 1
    assert (inc1[0]["count"], inc1[0]["last_error"], inc1[0]["last_alert_at"]) == (1, "network timeout", t0)
    assert len(cast("list[object]", queue.report()["deliveries"])) == 1

    # Repeat failure after 1h (< INCIDENT_REMIND): count & last_error update without enqueueing a second alert.
    t1 = timestamp("2026-10-01T01:00:00Z")
    queue.open_incident("calendar", "still failing", at=t1)
    inc2 = queue.incidents()
    assert (inc2[0]["count"], inc2[0]["last_error"], inc2[0]["last_alert_at"]) == (2, "still failing", t0)
    assert len(cast("list[object]", queue.report()["deliveries"])) == 1

    # Repeat failure after INCIDENT_REMIND (6h+): enqueues a reminder delivery and advances last_alert_at.
    t2 = timestamp("2026-10-01T07:00:00Z")
    queue.open_incident("calendar", "still failing 6h", at=t2)
    inc3 = queue.incidents()
    assert (inc3[0]["count"], inc3[0]["last_alert_at"]) == (3, t2)
    assert len(cast("list[object]", queue.report()["deliveries"])) == 2

    # Resolution clears incident row and enqueues one recovery notice (idempotent if called twice).
    queue.resolve_incident("calendar")
    assert queue.incidents() == []
    assert queue.status()["incidents"] == 0
    deliveries = cast("list[dict[str, object]]", queue.report()["deliveries"])
    assert len(deliveries) == 3
    assert any("recovered" in str(queue.inspect(str(d["id"]))["payload"]) for d in deliveries)
    queue.resolve_incident("calendar")
    assert len(cast("list[object]", queue.report()["deliveries"])) == 3
