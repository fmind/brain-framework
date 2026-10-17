"""Assistant journeys, durable recovery and transport boundaries use fictional providers only."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from bf import assistants, channels, records, runtime
from bf.collect import collect
from bf.config import load
from bf.messages import Message, Outgoing
from bf.models import Config, Error, Send, encode, timestamp
from bf.runtime import Queue, QueueFullError
from bf.storage import BusyError, Store, collecting, writer


@pytest.fixture
def messaging(brain: Store) -> Store:
    brain.write(
        "bf.yaml",
        b"""version: 8
name: fixture
sensors:
  calendar:
    command: [calendar]
channels:
  chat:
    send: {command: [send-chat]}
    stream: {command: [receive-chat]}
  desktop:
    send: {command: [notify-desktop]}
assistants:
  personal:
    prompt: prompts/personal.md
    command: [harness]
    inputs:
      - channel: {name: chat, mode: stream}
    outputs:
      - reply: {}
      - channel: {name: chat, when: [schedule, trigger]}
      - channel: {name: desktop, when: [schedule]}
    schedules:
      review: {every: 1800}
    triggers:
      calendar: {event: sensor.changed, sensor: {name: calendar}}
""",
    )
    brain.write("prompts/personal.md", b"Review {{start}} to {{end}}.\n{{context}}\nMessage: {{input}}\n")
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


def claim(queue: Queue, key: str) -> bool:
    return queue.claim(key, str(queue.get(key)["payload"]))


def test_replayed_input_creates_one_job_and_replies_in_its_thread(messaging: Store) -> None:
    incoming = message()
    for _ in range(2):
        assistants.accept(messaging, "chat", [incoming], {"personal"})
    queue = Queue(messaging)
    assert queue.report()["counts"] == {"pending": 1}
    calls = []

    def runner(argv, program, _store, _name, stdin):
        calls.append((argv, json.loads(stdin) if isinstance(program, Send) else stdin.decode()))
        return b'{"id":"answer-1","conversation":"thread-1"}' if isinstance(program, Send) else b"Here is why."

    key = queue.next(True, {"personal"})
    assert key is not None
    assistants.execute(messaging, key, runner=runner)
    assert assistants.deliver(messaging, runner=runner)
    assert len(calls) == 2
    assert calls[1][1]["reply_to"] == incoming.id
    assert calls[1][1]["conversation"] == incoming.conversation
    assert queue.report()["counts"] == {"complete": 1}
    assert len(records.files(messaging, "personal")) == 1
    assistants.accept(messaging, "chat", [incoming], {"personal"})
    assert queue.next(True, {"personal"}) is None


def test_answer_survives_failed_delivery_without_reinvoking_harness(messaging: Store) -> None:
    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    key = queue.next(True, {"personal"})
    assert key
    calls = []

    def runner(argv, *_):
        calls.append(argv[0])
        if argv[0] == "send-chat":
            raise Error("simulated uncertain send")
        return b"Saved answer"

    assistants.execute(messaging, key, runner=runner)
    assistants.deliver(messaging, runner=runner)
    pending = cast("list[dict]", queue.report()["deliveries"])
    assert pending[0]["state"] == "uncertain"
    queue.retry(pending[0]["id"])
    assistants.deliver(messaging, runner=lambda *_: b'{"id":"sent","conversation":"thread-1"}')
    assert calls == ["harness", "send-chat"]
    assert cast("list[dict]", queue.report()["deliveries"]) == []


def test_saved_answer_recovery_does_not_run_harness(messaging: Store) -> None:
    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    key = queue.next(True, {"personal"})
    assert key
    assert claim(queue, key)
    queue.answer(key, b"Already answered")
    queue.recover_attempts()
    assert (
        assistants.execute(messaging, key, runner=lambda *_: pytest.fail("must reuse saved answer"))["text"]
        == "Already answered"
    )


def test_interrupted_harness_requires_explicit_retry(messaging: Store) -> None:
    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    key = queue.next(True, {"personal"})
    assert key
    assert claim(queue, key)
    queue.recover_attempts()
    assert queue.get(key)["state"] == "uncertain"
    assert queue.next(True, {"personal"}) is None
    queue.retry(key)
    assert queue.next(True, {"personal"}) == key


def test_named_schedule_fans_out_without_moving_on_direct_input(messaging: Store) -> None:
    at = datetime.now(UTC)
    queue = Queue(messaging)
    key = assistants.schedule(messaging, {"personal"}, at)[0][0]
    assistants.execute(messaging, key, runner=lambda *_: b"Proactive summary")
    assert {item["channel"] for item in cast("list[dict]", queue.report()["deliveries"])} == {"chat", "desktop"}
    checkpoint = queue.progress("schedule:personal:schedule:review")
    assistants.accept(messaging, "chat", [message()], {"personal"})
    incoming = queue.next(True, {"personal"})
    assert incoming
    assistants.execute(messaging, incoming, runner=lambda *_: b"Direct answer")
    assert queue.progress("schedule:personal:schedule:review") == checkpoint
    assert assistants.due_schedules(messaging, {"personal"}, at + timedelta(seconds=10)) == []


def test_quiet_schedule_sends_nothing_but_empty_input_stays_unresolved(messaging: Store) -> None:
    queue = Queue(messaging)
    key = assistants.schedule(messaging, {"personal"}, datetime.now(UTC))[0][0]
    assistants.execute(messaging, key, runner=lambda *_: b"")
    assert cast("list[dict]", queue.report()["deliveries"]) == []
    assistants.accept(messaging, "chat", [message()], {"personal"})
    key = queue.next(True, {"personal"})
    assert key
    with pytest.raises(Error, match="no response"):
        assistants.execute(messaging, key, runner=lambda *_: b"")
    assert queue.get(key)["state"] == "failed"


def test_committed_sensor_event_recovers_after_queue_failure(messaging: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    from bf.history import SENSORS, state

    original = records.admit
    monkeypatch.setattr(records, "admit", lambda *_: (_ for _ in ()).throw(Error("queue unavailable")))
    start = timestamp((datetime.now(UTC) - timedelta(days=1)).isoformat())
    end = timestamp(datetime.now(UTC).isoformat())
    with pytest.raises(Error, match="calendar records were committed but their assistant event is not queued: queue"):
        collect(
            messaging,
            "calendar",
            start=start,
            end=end,
            runner=lambda *_: b'[{"id":"event-1","title":"Changed calendar","text":"New time"}]',
        )
    assert len(records.files(messaging, "calendar")) == 1
    # Run history names the cause, as for any failed collection.
    assert "queue unavailable" in str(state(messaging, SENSORS)["calendar"]["error"])
    monkeypatch.setattr(records, "admit", original)
    with writer(messaging):
        records.recover(messaging)
    queue = Queue(messaging)
    assert queue.report()["trigger_counts"] == {"pending": 1}
    assert assistants.materialize(messaging, {"personal"}, load(messaging)) == []
    assert queue.report()["counts"] == {"pending": 1}
    key = queue.next(False, {"personal"})
    assert key
    assert queue.work(key).event["added"] == 1


def test_read_is_passive_and_checks_complete_window(messaging: Store) -> None:
    incoming = message()
    command = load(messaging).channels["chat"].send
    assert command
    start = timestamp((datetime.fromisoformat(incoming.time) - timedelta(seconds=1)).isoformat())
    end = timestamp((datetime.fromisoformat(incoming.time) + timedelta(seconds=1)).isoformat())
    result = channels.read(
        messaging, "chat", start, end, command=command, runner=lambda *_: encode({"messages": [incoming.model_dump()]})
    )
    assert result == [incoming]
    assert Queue(messaging).report()["counts"] == {}


def test_receipt_cannot_change_reply_conversation(messaging: Store) -> None:
    command = load(messaging).channels["chat"].send
    assert command
    with pytest.raises(Error, match="different conversation"):
        channels.send(
            messaging,
            "chat",
            command,
            Outgoing(id="delivery", text="Answer", conversation="thread-1"),
            runner=lambda *_: b'{"id":"sent","conversation":"another-thread"}',
        )


def test_stream_acknowledges_after_durable_admission(messaging: Store) -> None:
    source = message()
    script = f"""import json,sys,pathlib,time
resume=json.loads(sys.stdin.readline())
print({json.dumps(source.model_dump())!r},flush=True)
ack=json.loads(sys.stdin.readline())
pathlib.Path('ack.json').write_text(json.dumps(ack))
time.sleep(10)
"""
    messaging.write("channels/stream.py", script.encode())
    command = channels.Stream(command=["python3", "channels/stream.py"])
    stopped, accepted = Event(), Event()
    failures = []

    def accept(value):
        assistants.accept(messaging, "chat", [value], {"personal"})
        accepted.set()

    def run_stream():
        try:
            channels.stream(messaging, "chat", command, {"since": source.time}, accept, stopped)
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run_stream)
    thread.start()
    try:
        assert accepted.wait(5)
        deadline = time.monotonic() + 5
        while not (messaging.root / "ack.json").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert json.loads(messaging.read("ack.json"))["id"] == source.id
        assert Queue(messaging).report()["counts"] == {"pending": 1}
    finally:
        stopped.set()
        thread.join(5)
    assert not thread.is_alive()
    assert failures == []


def test_reactive_capacity_remains_available_during_proactive_run(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    proactive, release, reactive = Event(), Event(), Event()

    def runner(_self, _argv, program, _store, _name, stdin):
        if isinstance(program, Send):
            return b'{"id":"sent","conversation":"thread-1"}'
        if b'"kind":"schedule"' in stdin:
            proactive.set()
            assert release.wait(5)
            return b""
        reactive.set()
        return b"Answer"

    monkeypatch.setattr(assistants.Dispatcher, "runner", runner)
    with assistants.Dispatcher(messaging, {"personal"}) as dispatcher:
        try:
            dispatcher.tick()
            assert proactive.wait(3)
            assistants.accept(messaging, "chat", [message()], {"personal"})
            dispatcher.tick()
            assert reactive.wait(3)
        finally:
            release.set()


@pytest.mark.parametrize(
    "change",
    [
        {"refresh": 10},
        {"lookback": 10},
        {"outputs": [{"channel": "chat"}]},
        {"inputs": [{"channel": {"name": "chat", "mode": "stream", "every": 10}}]},
        {"inputs": [{"channel": {"name": "chat", "mode": "read"}}]},
        {"outputs": [{"reply": {}, "channel": {"name": "chat"}}]},
        {"outputs": [{"reply": {}}, {"reply": {}}]},
        {"outputs": [{"channel": {"name": "chat"}}, {"channel": {"name": "chat"}}]},
        {"triggers": {"chain": {"event": "assistant.completed", "sensor": {"name": "calendar"}}}},
    ],
)
def test_invalid_configuration_fails_before_execution(messaging: Store, change: dict) -> None:
    config = yaml.safe_load(messaging.read("bf.yaml"))
    config["assistants"]["personal"].update(change)
    with pytest.raises(ValidationError):
        Config.model_validate(config)


def test_channel_identity_is_required_and_own_echo_is_not_queued(messaging: Store) -> None:
    with pytest.raises(ValidationError):
        Message.model_validate({"id": "x", "conversation": "t", "time": "2026-01-01T00:00:00Z", "text": "hello"})
    assistants.accept(messaging, "chat", [message(own=True)], {"personal"})
    assert Queue(messaging).report()["counts"] == {}


def test_scoped_dispatch_does_not_send_another_assistants_work(messaging: Store) -> None:
    config = yaml.safe_load(messaging.read("bf.yaml"))
    config["assistants"]["other"] = json.loads(json.dumps(config["assistants"]["personal"]))
    config["assistants"]["other"]["schedules"] = {}
    config["assistants"]["personal"]["schedules"] = {}
    messaging.write("bf.yaml", yaml.safe_dump(config).encode())
    incoming = message()
    assistants.accept(messaging, "chat", [incoming], {"personal", "other"})
    queue = Queue(messaging)
    for name in ("personal", "other"):
        key = queue.next(True, {name})
        assert key
        assistants.execute(messaging, key, runner=lambda *_: b"Answer")
    other = queue.delivery(names={"other"})
    assert other
    assistants.cycle(
        messaging, {"personal"}, at=datetime.now(UTC), runner=lambda *_: b'{"id":"sent","conversation":"thread-1"}'
    )
    assert queue.inspect(str(other["id"]))["state"] == "pending"
    assert queue.delivery(names={"personal"}) is None


def test_unresolved_delivery_blocks_later_replies_until_cancelled(messaging: Store) -> None:
    queue = Queue(messaging)
    assistants.accept(messaging, "chat", [message()], {"personal"})
    first = queue.next(True, {"personal"})
    assert first
    assistants.execute(messaging, first, runner=lambda *_: b"First answer")
    delivery = queue.delivery()
    assert delivery
    queue.delivery_failed(str(delivery["id"]), "unknown send result")
    assistants.accept(messaging, "chat", [message("second")], {"personal"})
    assert queue.next(True, {"personal"}) is None
    queue.retry(str(delivery["id"]), cancel=True)
    assert queue.next(True, {"personal"}) is not None


def test_sensor_journal_already_admitted_recovers_after_configuration_edit(messaging: Store) -> None:
    event = assistants.sensor_event(load(messaging), "calendar", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z")
    assert event
    queue = Queue(messaging)
    queue.admit_event(event)
    messaging.write("bf.yaml", messaging.read("bf.yaml").replace(b"command: [harness]", b"command: [changed]"))
    messaging.root.joinpath("prompts/personal.md").unlink()
    queue.admit_event(event)
    assert queue.report()["trigger_counts"] == {"pending": 1}


def test_duplicate_event_with_different_content_is_rejected_atomically(messaging: Store) -> None:
    source = message()
    queue = Queue(messaging)
    assistants.accept(messaging, "chat", [source], {"personal"})
    changed = source.model_copy(update={"text": "changed"})
    with pytest.raises(Error, match="different content"):
        assistants.accept(messaging, "chat", [changed], {"personal"}, checkpoint=("test", {"cursor": "bad"}))
    assert queue.progress("test") == {}
    assert queue.report()["counts"] == {"pending": 1}


def test_receipts_suppress_echoes_and_duplicate_send_ids(messaging: Store) -> None:
    queue = Queue(messaging)
    command = Send(command=["send-chat"], idempotent=True)
    config = yaml.safe_load(messaging.read("bf.yaml"))
    config["channels"]["chat"]["send"] = command.model_dump()
    messaging.write("bf.yaml", yaml.safe_dump(config).encode())
    outgoing = Outgoing(id="manual-delivery", text="Hello", conversation="thread-1")
    queue.send("chat", command, outgoing)
    assert assistants.deliver(messaging, runner=lambda *_: b'{"id":"echo","conversation":"thread-1"}')
    queue.send("chat", command, outgoing)
    assert not assistants.deliver(messaging, runner=lambda *_: pytest.fail("already delivered"))
    assistants.accept(messaging, "chat", [message("echo")], {"personal"})
    assert queue.report()["counts"] == {}
    with pytest.raises(Error, match="already used"):
        queue.send("chat", command, outgoing.model_copy(update={"text": "different"}))


def test_queue_capacity_rejects_input_without_advancing_progress(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "MAX_PENDING", 2)
    assistants.accept(messaging, "chat", [message()], {"personal"})
    with pytest.raises(Error, match="unfinished"):
        assistants.accept(
            messaging, "chat", [message("second")], {"personal"}, checkpoint=("stream:chat", {"cursor": "second"})
        )
    assert Queue(messaging).progress("stream:chat") == {}
    assert Queue(messaging).report()["counts"] == {"pending": 1}
    with pytest.raises(Error, match="unfinished"):
        Queue(messaging).send("chat", Send(command=["fake"]), Outgoing(id="manual", text="another obligation"))


def test_changed_configuration_never_reroutes_queued_work(messaging: Store) -> None:
    queue = Queue(messaging)
    assistants.accept(messaging, "chat", [message()], {"personal"})
    key = queue.next(True, {"personal"})
    assert key
    messaging.write("bf.yaml", messaging.read("bf.yaml").replace(b"send-chat", b"different-destination"))
    with pytest.raises(Error, match="configuration changed"):
        assistants.execute(messaging, key, runner=lambda *_: pytest.fail("must not execute"))
    assert queue.get(key)["state"] == "failed"


def test_read_subscription_progress_is_separate_from_manual_history(messaging: Store) -> None:
    from bf.models import ReadInput

    config = yaml.safe_load(messaging.read("bf.yaml"))
    config["channels"]["chat"]["read"] = {"command": ["read-chat", "{{start}}", "{{end}}"]}
    config["assistants"]["personal"]["inputs"] = [{"channel": {"name": "chat", "mode": "read", "every": 1}}]
    messaging.write("bf.yaml", yaml.safe_dump(config).encode())
    binding = ReadInput(name="chat", mode="read", every=1)
    queue = Queue(messaging)
    assistants.poll(messaging, "personal", binding, runner=lambda *_: pytest.fail("first subscription starts now"))
    first = queue.progress("read:personal:chat")
    source = message()
    since = timestamp((datetime.fromisoformat(source.time) - timedelta(seconds=1)).isoformat())
    queue.set_progress("read:personal:chat", {"since": since, "end": since})
    raw = encode({"messages": [source.model_dump()]})
    assistants.poll(messaging, "personal", binding, runner=lambda *_: raw)
    advanced = queue.progress("read:personal:chat")
    assert str(advanced["end"]) > str(first["end"])
    channels.read(messaging, "chat", since, str(advanced["end"]), runner=lambda *_: raw)
    assert queue.progress("read:personal:chat") == advanced
    assert queue.report()["counts"] == {"pending": 1}


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b'{"messages": [], "next": "incomplete"}'])
def test_incomplete_channel_batches_fail_closed(messaging: Store, raw: bytes) -> None:
    command = Send(command=["fake"])
    with pytest.raises(Error, match="complete JSON"):
        channels.read(
            messaging, "chat", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z", command=command, runner=lambda *_: raw
        )


def test_queue_corruption_is_preserved_and_never_rebuilt(messaging: Store) -> None:
    queue = Queue(messaging)
    queue.store.write("queue.sqlite3", b"corrupt evidence")
    with pytest.raises(Error, match="preserve"):
        queue.report()
    assert queue.store.read("queue.sqlite3") == b"corrupt evidence"


def test_queue_connection_preflight_preserves_another_threads_sqlite_lock(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import sys
    from threading import current_thread

    queue = Queue(messaging)
    queue.report()
    connect = runtime.sqlite3.connect
    preflight, resume = Event(), Event()
    errors: list[Exception] = []

    def opening(*args, **kwargs):
        if current_thread().name == "queue-probe":
            preflight.set()
            assert resume.wait(5)
        return connect(*args, **kwargs)

    def probe():
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
            competitor = subprocess.run(  # noqa: S603 - isolated SQLite lock probe, no provider calls
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
                    str(queue.store.root / runtime.DATABASE),
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


def test_cli_channel_and_queue_roundtrip_without_an_assistant(brain: Store) -> None:
    from typer.testing import CliRunner

    from bf.cli import app

    brain.write(
        "bf.yaml",
        b"""version: 8
name: fixture
channels:
  local:
    send: {command: [python3, channels/fake.py, send]}
    read: {command: [python3, channels/fake.py, read]}
""",
    )
    brain.write(
        "channels/fake.py",
        b"""import sys,json,pathlib
if sys.argv[1]=='read':
 print(json.dumps({'messages': []}))
else:
 value=json.load(sys.stdin)
 if pathlib.Path('fail').exists():
  print('PRIVATE PROVIDER ERROR',file=sys.stderr)
  sys.exit(1)
 pathlib.Path('sent.json').write_text(json.dumps(value))
 print(json.dumps({'id':'native-id','conversation':value.get('conversation') or 'thread'}))
""",
    )
    runner = CliRunner()

    def invoke(*args, body=None, code=0):
        result = runner.invoke(app, [*args, "--brain", str(brain.root)], input=body)
        assert result.exit_code == code, result.output + str(result.exception)
        return result

    history = invoke("channel", "read", "local", "--since", "2026-10-15", "--until", "2026-10-16")
    assert json.loads(history.stdout)["messages"] == []
    assert Queue(brain).report()["counts"] == {}
    brain.write("fail", b"yes")
    failed = invoke("channel", "send", "local", "--stdin", body='{"id":"stable","text":"PRIVATE ANSWER"}', code=1)
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
    duplicate = invoke("channel", "send", "local", "--stdin", body='{"id":"stable","text":"PRIVATE ANSWER"}')
    assert json.loads(duplicate.stdout)["receipt"]["id"] == "native-id"
    assert json.loads(brain.read("sent.json"))["id"] == "stable"
    for payload in ("[1]", "no-json", "{}", '{"text":"hi","unknown":true}'):
        invalid = invoke("channel", "send", "local", "--stdin", body=payload, code=2)
        assert "--stdin" in invalid.output
    invoke("channel", "read", "local", "--since", "2026-10-16", "--until", "2026-10-15", code=2)
    invoke("channel", "read", "local", code=2)
    invoke("channel", "send", "local", code=2)
    invoke("queue", "--retry", code=2)
    invoke("queue", "stable", "--retry", "--cancel", code=2)


def test_queue_retry_reports_saved_answer_state(messaging: Store) -> None:
    from typer.testing import CliRunner

    from bf.cli import app

    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    key = queue.next(True, {"personal"})
    assert key
    assert claim(queue, key)
    queue.answer(key, b"Saved answer")
    queue.fail(key, "recording interrupted")
    result = CliRunner().invoke(app, ["queue", key, "--retry", "--brain", str(messaging.root)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "answered"
    detail = CliRunner().invoke(app, ["queue", key, "--brain", str(messaging.root)])
    assert json.loads(detail.stdout)["raw"] == "Saved answer"


def test_cancelled_work_cannot_be_resurrected_by_a_late_failure(messaging: Store) -> None:
    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    key = queue.next(True, {"personal"})
    assert key
    queue.retry(key, cancel=True)
    queue.fail(key, "late failure", uncertain=True)
    assert queue.get(key)["state"] == "cancelled"
    command = Send(command=["fake"])
    queue.send("chat", command, Outgoing(id="late-send", text="answer"))
    queue.retry("late-send", cancel=True)
    queue.delivery_failed("late-send", "late failure")
    assert queue.inspect("late-send")["state"] == "cancelled"


def test_changed_trigger_configuration_preserves_separate_obligations(messaging: Store) -> None:
    queue = Queue(messaging)
    first = assistants.sensor_event(load(messaging), "calendar", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z")
    assert first
    queue.admit_event(first)
    assistants.materialize(messaging, {"personal"}, load(messaging))
    messaging.write("bf.yaml", messaging.read("bf.yaml").replace(b"command: [harness]", b"command: [another-harness]"))
    second = assistants.sensor_event(load(messaging), "calendar", "2026-10-16T00:00:00Z", "2026-10-17T00:00:00Z")
    assert second
    queue.admit_event(second)
    assistants.materialize(messaging, {"personal"}, load(messaging))
    assert queue.report()["counts"] == {"pending": 2}


def test_compatible_trigger_events_coalesce_window_and_counts(messaging: Store) -> None:
    queue = Queue(messaging)
    for day in (15, 16):
        event = assistants.sensor_event(
            load(messaging), "calendar", f"2026-10-{day}T00:00:00Z", f"2026-10-{day + 1}T00:00:00Z"
        )
        assert event
        event.data["added"] = 1
        queue.admit_event(event)
    assert assistants.materialize(messaging, {"personal"}, load(messaging)) == []
    key = queue.next(False, {"personal"})
    assert key
    work = queue.work(key)
    assert work.event["added"] == 2
    assert work.start.startswith("2026-10-15")
    assert work.end.startswith("2026-10-17")
    assert work.event["end"] == work.end
    assert queue.report()["counts"] == {"pending": 1}


def test_compaction_bounds_terminal_bodies_and_keeps_deduplication(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "BODY_BUDGET", 64)
    queue = Queue(messaging)
    source = message()
    assistants.accept(messaging, "chat", [source], {"personal"})
    key = queue.next(True, {"personal"})
    assert key
    assistants.execute(messaging, key, runner=lambda *_: b"Saved record " * 100)
    assistants.deliver(messaging, runner=lambda *_: b'{"id":"native-answer","conversation":"thread-1"}')
    assert queue.get(key)["payload"] == "{}"
    assert queue.get(key)["raw"] is None
    assistants.accept(messaging, "chat", [source], {"personal"})
    assert queue.next(True, {"personal"}) is None
    assert len(records.files(messaging, "personal")) == 1
    assistants.accept(messaging, "chat", [message("new")], {"personal"})
    assert queue.next(True, {"personal"}) is not None


def test_retention_counts_utf8_bytes_without_losing_pending_input(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "BODY_BUDGET", 64)
    incoming = message().model_copy(update={"text": "🧠" * 60})
    assistants.accept(messaging, "chat", [incoming], {"personal"})
    queue = Queue(messaging)
    assert queue.conversation("chat", "thread-1", incoming.time) == []
    key = queue.next(True, {"personal"})
    assert key
    assert queue.work(key).text == incoming.text
    with queue.connect() as db:
        assert db.execute("SELECT data FROM events WHERE kind='channel.received'").fetchone()[0] == "{}"


def test_completed_job_with_uncertain_delivery_keeps_its_answer_when_the_queue_fills(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "BODY_BUDGET", 0)
    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    key = queue.next(True, {"personal"})
    assert key
    assistants.execute(messaging, key, runner=lambda *_: b"x" * 64_000)
    delivery = queue.delivery(job=key)
    assert delivery
    queue.delivery_failed(str(delivery["id"]), "uncertain provider outcome")
    monkeypatch.setattr(runtime, "MAX_DATABASE", 1 << 20)
    with pytest.raises(runtime.QueueFullError, match="queue is full"):
        queue.send("chat", Send(command=["fake"]), Outgoing(id="fill", text="y" * 600_000))
    queue.compact()
    assert queue.get(key)["raw"] == b"x" * 64_000
    assert queue.inspect(str(delivery["id"]))["state"] == "uncertain"


def test_manual_sends_cannot_consume_an_active_harness_answer_reservation(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    messaging.write(
        "bf.yaml", messaging.read("bf.yaml").replace(b"command: [harness]", b"command: [harness]\n    max_bytes: 16384")
    )
    monkeypatch.setattr(runtime, "MAX_DATABASE", 2 << 20)
    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    key = queue.next(True, {"personal"})
    assert key
    queue.send("chat", Send(command=["fake"]), Outgoing(id="fits", text="y" * 150_000))
    # A started job reserves the space its answer, record and deliveries may still need.
    assert claim(queue, key)
    with pytest.raises(runtime.QueueFullError, match="queue is full"):
        queue.send("chat", Send(command=["fake"]), Outgoing(id="fill", text="y" * 150_000))
    queue.answer(key, b"a" * 16384)
    assert queue.get(key)["state"] == "answered"


def test_finite_manual_sends_compact_without_a_watcher(messaging: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    from bf.messages import Receipt

    monkeypatch.setattr(runtime, "BODY_BUDGET", 128)
    queue = Queue(messaging)
    for index in range(3):
        outgoing = Outgoing(id=f"send-{index}", text="🧠" * 200)
        queue.send("chat", Send(command=["fake"]), outgoing)
        queue.delivered(outgoing.id, "chat", outgoing, Receipt(id=f"receipt-{index}", conversation="thread-1"))
        assert queue.inspect(outgoing.id)["payload"] == "{}"
    assert queue.report()["delivery_counts"] == {"delivered": 3}
    assert queue.conversation("chat", "thread-1", "2099-01-01T00:00:00Z") == []


def test_removed_assistant_still_exposes_unresolved_work(messaging: Store) -> None:
    from bf.health import report

    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    key = queue.next(True, {"personal"})
    assert key
    queue.fail(key, "needs review")
    messaging.write("bf.yaml", b"version: 8\nname: fixture\n")
    status = cast("dict", report([messaging]))
    assert not status["healthy"]
    assert status["brains"][0]["messaging"]["counts"] == {"failed": 1}


def test_real_watch_replies_once_and_recovers_stream_redelivery(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    root = tmp_path / "demo"
    shutil.copytree(Path(__file__).parents[1] / "examples/channels", root)
    store = Store(root)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for attempt in range(2):
        # The queue persists across both runs: the restart must re-accept hello-1 before its outcome counts.
        before = Queue(store).progress("health:channel:local") if attempt else {}
        with (tmp_path / f"watch-{attempt}.jsonl").open("wb") as output:
            child = subprocess.Popen(  # noqa: S603 - fictional local adapter in an isolated brain
                [sys.executable, "-m", "bf", "watch", "--brain", str(root), "--json"],
                stdout=output,
                stderr=subprocess.PIPE,
                env={**os.environ, "BF_BRAIN": str(root)},
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        pytest.fail(f"watch exited: {child.communicate()[1].decode()}")
                    outcomes = Queue(store).report()
                    health = Queue(store).progress("health:channel:local")
                    accepted = not attempt or (health != before and health.get("error") == "")
                    settled = set(cast("dict", outcomes["counts"])) == {"complete"} and not outcomes["deliveries"]
                    if accepted and settled and cast("dict", outcomes["delivery_counts"]).get("delivered") == 2:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("watch did not deliver its incoming reply and scheduled review")
            finally:
                child.terminate()
                _, stderr = child.communicate(timeout=5)
            assert not stderr, stderr.decode()
        lines = [json.loads(line) for line in store.read("logs/deliveries.jsonl").splitlines()]
        assert len(lines) == 2
        replies = [line for line in lines if line["reply_to"] == "hello-1"]
        assert len(replies) == 1
        assert replies[0]["conversation"] == "thread-1"
        assert replies[0]["text"] == "Acknowledged."
        assert Queue(store).report()["counts"] == {"complete": 2}


def answers(_argv: list[str], program: object, *_: object) -> bytes:
    """A fictional harness answering every prompt and a channel accepting every send."""
    return b'{"id":"sent","conversation":"thread-1"}' if isinstance(program, Send) else b"Answer"


def configure(store: Store, change) -> None:
    config = yaml.safe_load(store.read("bf.yaml"))
    change(config)
    store.write("bf.yaml", yaml.safe_dump(config).encode())


def window() -> tuple[str, str]:
    end = datetime.now(UTC)
    return timestamp((end - timedelta(days=1)).isoformat()), timestamp(end.isoformat())


def due(queue: Queue, delivery: str) -> None:
    with queue.connect() as db:
        db.execute("UPDATE deliveries SET due=? WHERE id=?", (runtime.now(), delivery))


def changed(record_id: str = "event-1"):
    return lambda *_: f'[{{"id":"{record_id}","title":"Changed calendar","text":"New time"}}]'.encode()


def test_default_queue_admits_a_full_read_batch_and_hundreds_of_jobs(messaging: Store) -> None:
    from bf.models import ReadInput

    def read_input(config):
        config["channels"]["chat"]["read"] = {"command": ["read-chat", "{{start}}", "{{end}}"]}
        config["assistants"]["personal"]["inputs"] = [{"channel": {"name": "chat", "mode": "read", "every": 60}}]

    configure(messaging, read_input)
    queue = Queue(messaging)
    since = timestamp((datetime.now(UTC) - timedelta(hours=1)).isoformat())
    queue.set_progress("read:personal:chat", {"since": since, "end": since})
    batch = [message(f"m-{index}").model_copy(update={"conversation": f"thread-{index}"}) for index in range(1000)]
    raw = encode({"messages": [item.model_dump() for item in batch]})
    assistants.poll(messaging, "personal", ReadInput(name="chat", mode="read", every=60), runner=lambda *_: raw)
    assert queue.report()["counts"] == {"pending": 1000}
    assert str(queue.progress("read:personal:chat")["end"]) > since
    # Schedules and sensor triggers are still admitted beside that backlog.
    assert assistants.schedule(messaging, {"personal"}, datetime.now(UTC))[1] == []
    start, end = window()
    collect(messaging, "calendar", start=start, end=end, runner=changed())
    assert assistants.materialize(messaging, {"personal"}, load(messaging)) == []
    assert queue.report()["counts"] == {"pending": 1002}


def test_read_batch_beyond_capacity_admits_a_prefix_and_resumes(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bf.models import ReadInput

    def read_input(config):
        config["channels"]["chat"]["read"] = {"command": ["read-chat", "{{start}}", "{{end}}"]}
        config["assistants"]["personal"]["inputs"] = [{"channel": {"name": "chat", "mode": "read", "every": 1}}]

    configure(messaging, read_input)
    # Each input job holds its own slot and one reply: three fit.
    monkeypatch.setattr(runtime, "MAX_PENDING", 6)
    queue = Queue(messaging)
    base = datetime.now(UTC) - timedelta(seconds=20)
    since = timestamp((base - timedelta(seconds=60)).isoformat())
    queue.set_progress("read:personal:chat", {"since": since, "end": since})
    batch = [
        message(f"m-{index}").model_copy(
            update={"time": timestamp((base + timedelta(seconds=index)).isoformat()), "conversation": f"t-{index}"}
        )
        for index in range(5)
    ]

    def read(argv, *_):
        start, end = argv[1:]
        return encode({"messages": [item.model_dump() for item in batch if start <= item.time < end]})

    binding = ReadInput(name="chat", mode="read", every=1)
    with pytest.raises(QueueFullError, match="admitted 3 of 5"):
        assistants.poll(messaging, "personal", binding, runner=read)
    assert queue.report()["counts"] == {"pending": 3}
    assert queue.progress("read:personal:chat")["end"] == batch[2].time
    for item in cast("list[dict]", queue.report()["jobs"]):
        queue.retry(str(item["id"]), cancel=True)
    assistants.poll(messaging, "personal", binding, runner=read)
    assert queue.report()["counts"] == {"cancelled": 3, "pending": 2}


def test_full_queue_never_blocks_collection_and_its_trigger_waits(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "MAX_PENDING", 2)
    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    start, end = window()
    collect(messaging, "calendar", start=start, end=end, runner=changed())
    assert not messaging.root.joinpath("memories/.pending").exists()
    [failure] = assistants.materialize(messaging, {"personal"}, load(messaging))
    assert failure["trigger"] == "calendar"
    assert "unfinished-work slots" in str(failure["error"])
    [waiting] = cast("list[dict]", queue.report()["triggers"])
    assert waiting["state"] == "pending"
    assert "unfinished-work slots" in waiting["error"]
    key = queue.next(True, {"personal"})
    assert key
    queue.retry(key, cancel=True)
    assert assistants.materialize(messaging, {"personal"}, load(messaging)) == []
    assert queue.report()["counts"] == {"cancelled": 1, "pending": 1}


def test_job_waits_for_answer_space_instead_of_starting_its_harness(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each started job reserves half the queue's space: a second one must wait.
    monkeypatch.setattr(runtime, "answer_space", lambda *_: runtime.MAX_DATABASE * 3 // 8)
    second = message("second").model_copy(update={"conversation": "thread-2"})
    assistants.accept(messaging, "chat", [message(), second], {"personal"})
    queue = Queue(messaging)
    first = queue.next(True, {"personal"})
    assert first
    later = queue.next(True, {"personal"}, excluding={first})
    assert later
    with collecting(messaging, f".job-{first}"):
        assert claim(queue, first)
        with pytest.raises(QueueFullError, match="lacks answer space"):
            assistants.execute(messaging, later, runner=lambda *_: pytest.fail("must wait for answer space"))
        assert queue.get(later)["state"] == "pending"
        results = assistants.cycle(messaging, {"personal"}, at=datetime.now(UTC), runner=answers)
    waiting = [item for item in results if item.get("job") == later]
    assert len(waiting) == 1
    assert waiting[0]["status"] == "failed"
    assert "answer space" in str(waiting[0]["error"])
    queue.answer(first, b"Done")
    assistants.execute(messaging, first, runner=answers)
    assert assistants.execute(messaging, later, runner=answers)["text"] == "Answer"


def test_job_too_large_for_the_queue_fails_visibly(messaging: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # A default 1 MiB answer with one reply needs about 13 MiB of answer space.
    monkeypatch.setattr(runtime, "MAX_DATABASE", 8 << 20)
    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    key = queue.next(True, {"personal"})
    assert key
    with pytest.raises(Error, match="lower its max_bytes"):
        assistants.execute(messaging, key, runner=lambda *_: pytest.fail("must not start"))
    assert queue.get(key)["state"] == "failed"


def test_identity_tombstones_expire_after_retention(messaging: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = Queue(messaging)
    assistants.accept(messaging, "chat", [message()], {"personal"})
    key = queue.next(True, {"personal"})
    assert key
    assistants.execute(messaging, key, runner=answers)
    assert assistants.deliver(messaging, runner=answers)
    assistants.accept(messaging, "chat", [message("waiting").model_copy(update={"conversation": "t-2"})], {"personal"})
    monkeypatch.setattr(runtime, "RETENTION", timedelta(seconds=-60))
    queue.compact()
    with queue.connect() as db:
        assert db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    # Unresolved work never expires.
    assert queue.report()["counts"] == {"pending": 1}


def test_configuration_edit_during_triggered_collection_never_blocks_writers(messaging: Store) -> None:
    def editing(*_):
        configure(messaging, lambda config: config["sensors"].update(mail={"command": ["mail"]}))
        return changed()()

    start, end = window()
    collect(messaging, "calendar", start=start, end=end, runner=editing)
    assert not messaging.root.joinpath("memories/.pending").exists()
    collect(messaging, "mail", start=start, end=end, runner=lambda *_: b'[{"id":"m-1","title":"Mail","text":"x"}]')
    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    key = queue.next(True, {"personal"})
    assert key
    assistants.execute(messaging, key, runner=answers)
    assert assistants.materialize(messaging, {"personal"}, load(messaging)) == []
    assert queue.report()["counts"] == {"complete": 1, "pending": 1}


def test_missing_prompt_or_removed_trigger_never_blocks_writers(messaging: Store) -> None:
    queue = Queue(messaging)
    prompt = messaging.read("prompts/personal.md")
    messaging.root.joinpath("prompts/personal.md").unlink()
    start, end = window()
    collect(messaging, "calendar", start=start, end=end, runner=changed())
    [failure] = assistants.materialize(messaging, {"personal"}, load(messaging))
    assert "prompts/personal.md: cannot read" in str(failure["error"])
    assert cast("list[dict]", queue.report()["triggers"])[0]["state"] == "pending"
    collect(messaging, "calendar", start=start, end=end, runner=changed("event-2"))
    assert len(records.files(messaging, "calendar")) == 2
    messaging.write("prompts/personal.md", prompt)
    assert assistants.materialize(messaging, {"personal"}, load(messaging)) == []
    # Both changes extend one pending review.
    assert queue.report()["counts"] == {"pending": 1}
    collect(messaging, "calendar", start=start, end=end, runner=changed("event-3"))
    configure(messaging, lambda config: config["assistants"]["personal"].update(triggers={}))
    assert assistants.materialize(messaging, {"personal"}, load(messaging)) == []
    assert queue.report()["trigger_counts"] == {"dispatched": 2, "dropped": 1}
    assert "dropped sensor event" in messaging.read("logs/personal.log").decode()


def test_only_sensors_an_enabled_trigger_names_emit_events(messaging: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load(messaging)
    assert assistants.sensor_event(config, "mail", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z") is None
    event = assistants.sensor_event(config, "calendar", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z")
    assert event
    assert [(target.assistant, target.trigger) for target in event.targets] == [("personal", "calendar")]
    # A collection the harness itself runs never triggers assistants.
    monkeypatch.setenv("BF_ASSISTANT_RUN", "job")
    assert assistants.sensor_event(config, "calendar", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z") is None
    start, end = window()
    collect(messaging, "calendar", start=start, end=end, runner=changed())
    assert not Queue(messaging).exists()


def test_cycle_reports_one_assistants_failures_and_runs_the_others(messaging: Store) -> None:
    def assistants_beside(config):
        config["channels"]["inbox"] = {"read": {"command": ["read-inbox", "{{start}}", "{{end}}"]}}
        config["assistants"]["reader"] = {
            "prompt": "prompts/personal.md",
            "command": ["harness"],
            "inputs": [{"channel": {"name": "inbox", "mode": "read", "every": 60}}],
        }
        config["assistants"]["absent"] = {
            "prompt": "prompts/absent.md",
            "command": ["harness"],
            "schedules": {"daily": {"every": 86400}},
        }

    configure(messaging, assistants_beside)
    queue = Queue(messaging)
    old = timestamp((datetime.now(UTC) - timedelta(hours=1)).isoformat())
    queue.set_progress("read:reader:inbox", {"since": old, "end": old})

    def runner(argv, program, *rest):
        if argv[0] == "read-inbox":
            raise Error("program exited with status 1")
        return answers(argv, program, *rest)

    results = assistants.cycle(messaging, {"reader", "absent", "personal"}, at=datetime.now(UTC), runner=runner)
    [read] = [item for item in results if item.get("input") == "inbox"]
    assert read["error"] == (
        "channel inbox: read failed: program exited with status 1; see logs/channel.inbox.read.log"
    )
    assert queue.progress("health:read:reader:inbox")["error"] == read["error"]
    [schedule] = [item for item in results if item["assistant"] == "absent"]
    assert schedule["schedule"] == "daily"
    assert "prompts/absent.md: cannot read" in str(schedule["error"])
    assert queue.progress("health:assistant:absent")["error"] == schedule["error"]
    assert queue.progress("health:assistant:personal")["error"] == ""
    assert any(item["assistant"] == "personal" and item["status"] == "ran" for item in results)
    queue.set_progress("read:reader:inbox", {"since": old, "end": old})
    assistants.cycle(messaging, {"reader"}, at=datetime.now(UTC), runner=lambda *_: b'{"messages":[]}')
    assert queue.progress("health:read:reader:inbox")["error"] == ""


def test_cycle_skips_a_job_another_process_runs_once(messaging: Store) -> None:
    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    key = queue.next(True, {"personal"})
    assert key
    with collecting(messaging, f".job-{key}"):
        results = assistants.cycle(messaging, {"personal"}, at=datetime.now(UTC), runner=answers)
    busy = [item for item in results if item.get("job") == key]
    assert [item["status"] for item in busy] == ["skipped"]
    # The due review still ran.
    assert any(item["status"] == "ran" for item in results)
    assert queue.get(key)["state"] == "pending"


def test_dispatcher_tick_survives_bad_configuration_missing_prompts_and_a_full_queue(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def absent(config):
        config["assistants"]["absent"] = {
            "prompt": "prompts/absent.md",
            "command": ["harness"],
            "schedules": {"daily": {"every": 86400}},
        }

    configure(messaging, absent)
    valid = messaging.read("bf.yaml")
    monkeypatch.setattr(assistants.Dispatcher, "runner", lambda _self, argv, program, *_: answers(argv, program))
    monkeypatch.setattr(runtime, "MAX_PENDING", 2)
    monkeypatch.setattr(assistants, "BACKOFF", 0.1)
    assistants.accept(messaging, "chat", [message()], {"personal"})
    queue = Queue(messaging)
    with assistants.Dispatcher(messaging, {"personal", "absent"}) as dispatcher:
        dispatcher.tick()
        # The full queue refuses the due review, yet the waiting reply still starts.
        assert "reactive" in dispatcher.tasks
        assert "unfinished-work slots" in str(queue.progress("health:assistant:personal")["error"])
        assert "prompts/absent.md" in str(queue.progress("health:assistant:absent")["error"])
        messaging.write("bf.yaml", valid + b"unknown: 1\n")
        dispatcher.tick()
        assert str(queue.progress("health:dispatcher")["error"]).startswith("invalid bf.yaml")
        messaging.write("bf.yaml", valid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            dispatcher.tick()
            if cast("dict", queue.report()["counts"]).get("complete") == 1 and not dispatcher.tasks:
                break
            time.sleep(0.05)
        assert queue.progress("health:dispatcher")["error"] == ""
    # A bf.yaml mid-edit delays the waiting reply instead of failing it.
    assert cast("dict", queue.report()["counts"]) == {"complete": 1}


def test_dispatcher_backs_off_a_job_another_process_holds(messaging: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(messaging, lambda config: config["assistants"]["personal"].update(schedules={}))
    starts = []
    original = assistants.execute

    def counted(store, key, **options):
        starts.append(key)
        return original(store, key, **options)

    monkeypatch.setattr(assistants, "execute", counted)
    assistants.accept(messaging, "chat", [message()], {"personal"})
    key = Queue(messaging).next(True, {"personal"})
    assert key
    with collecting(messaging, f".job-{key}"), assistants.Dispatcher(messaging, {"personal"}) as dispatcher:
        for _ in range(20):
            dispatcher.tick()
            time.sleep(0.02)
    assert starts == [key]


def test_drained_queue_follows_a_renamed_or_moved_brain(messaging: Store) -> None:
    Queue(messaging).report()
    messaging.write("bf.yaml", messaging.read("bf.yaml").replace(b"name: fixture", b"name: renamed"))
    assert Queue(messaging).report()["counts"] == {}
    moved = messaging.root.parent / "moved"
    messaging.root.rename(moved)
    store = Store(moved)
    assert Queue(store).report()["counts"] == {}
    start, end = window()
    collect(store, "calendar", start=start, end=end, runner=changed())
    assert assistants.materialize(store, {"personal"}, load(store)) == []
    assert Queue(store).report()["counts"] == {"pending": 1}


def test_renamed_brain_with_unresolved_work_refuses_execution_but_never_record_writes(messaging: Store) -> None:
    assistants.accept(messaging, "chat", [message()], {"personal"})
    original = messaging.read("bf.yaml")
    messaging.write("bf.yaml", original.replace(b"name: fixture", b"name: renamed"))
    with pytest.raises(Error, match="previous path or name; restore them, resolve the work with bf queue"):
        Queue(messaging).report()
    start, end = window()
    collect(messaging, "calendar", start=start, end=end, runner=changed())
    assert not messaging.root.joinpath("memories/.pending").exists()
    messaging.write("bf.yaml", original)
    assert Queue(messaging).report()["trigger_counts"] == {"pending": 1}


def test_interrupted_idempotent_delivery_retries_but_others_wait_for_inspection(messaging: Store) -> None:
    configure(messaging, lambda config: config["channels"]["chat"]["send"].update(idempotent=True))
    queue = Queue(messaging)
    key = assistants.schedule(messaging, {"personal"}, datetime.now(UTC))[0][0]
    assistants.execute(messaging, key, runner=answers)
    deliveries = {str(item["channel"]): str(item["id"]) for item in queue.outcomes(key)}
    for delivery in deliveries.values():
        assert queue.sending(delivery)
    queue.recover_attempts()
    assert queue.inspect(deliveries["chat"])["state"] == "pending"
    assert queue.inspect(deliveries["desktop"])["state"] == "uncertain"
    due(queue, deliveries["chat"])
    assert assistants.deliver(messaging, runner=answers, key=deliveries["chat"])
    assert queue.inspect(deliveries["chat"])["state"] == "delivered"


def test_failing_idempotent_delivery_stops_retrying_and_frees_its_conversation(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(messaging, lambda config: config["channels"]["chat"]["send"].update(idempotent=True))
    monkeypatch.setattr(runtime, "MAX_ATTEMPTS", 2)
    queue = Queue(messaging)
    assistants.accept(messaging, "chat", [message()], {"personal"})
    first = queue.next(True, {"personal"})
    assert first
    assistants.execute(messaging, first, runner=answers)
    [delivery] = [str(item["id"]) for item in queue.outcomes(first)]

    def rejected(*_):
        raise Error("provider rejected the message")

    for _ in range(2):
        due(queue, delivery)
        assert assistants.deliver(messaging, runner=rejected, key=delivery)
    saved = queue.inspect(delivery)
    assert saved["state"] == "failed"
    assert "stopped retrying after 2 attempts" in str(saved["error"])
    assistants.accept(messaging, "chat", [message("second")], {"personal"})
    assert queue.next(True, {"personal"}) is not None


def test_failed_standalone_send_waits_for_an_explicit_retry(messaging: Store) -> None:
    command = Send(command=["send-chat"], idempotent=True)
    configure(messaging, lambda config: config["channels"]["chat"].update(send=command.model_dump()))
    queue = Queue(messaging)
    queue.send("chat", command, Outgoing(id="manual", text="Review ready"))

    def unavailable(*_):
        raise Error("provider unavailable")

    assert assistants.deliver(messaging, runner=unavailable, key="manual")
    saved = queue.inspect("manual")
    assert saved["state"] == "failed"
    assert "bf queue ID --retry" in str(saved["error"])
    queue.retry("manual")
    assert assistants.deliver(messaging, runner=answers, key="manual")
    assert queue.inspect("manual")["state"] == "delivered"


def test_identical_resend_after_compaction_returns_the_saved_delivery(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = Send(command=["send-chat"])
    queue = Queue(messaging)
    outgoing = Outgoing(id="weekly-2026-42", text="Weekly review", conversation="thread-1")
    queue.send("chat", command, outgoing)
    assert assistants.deliver(messaging, runner=lambda *_: b'{"id":"native","conversation":"thread-1"}')
    monkeypatch.setattr(runtime, "BODY_BUDGET", 0)
    queue.compact()
    assert queue.inspect(outgoing.id)["payload"] == "{}"
    queue.send("chat", command, outgoing)
    assert not assistants.deliver(messaging, runner=lambda *_: pytest.fail("already delivered"))
    with pytest.raises(Error, match="already used"):
        queue.send("chat", command, outgoing.model_copy(update={"text": "different"}))


def test_send_receipts_suppress_echoes_after_the_conversation_cache_trims(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = Queue(messaging)
    queue.send("chat", Send(command=["send-chat"]), Outgoing(id="manual", text="Hello", conversation="thread-1"))
    assert assistants.deliver(messaging, runner=lambda *_: b'{"id":"echo","conversation":"thread-1"}')
    monkeypatch.setattr(runtime, "BODY_BUDGET", 0)
    queue.compact()
    assert queue.conversation("chat", "thread-1", "2099-01-01T00:00:00Z") == []
    assistants.accept(messaging, "chat", [message("echo")], {"personal"})
    assert queue.report()["counts"] == {}


def test_message_text_reaches_the_prompt_once_and_oversized_input_fails_visibly(messaging: Store) -> None:
    queue = Queue(messaging)
    prompts: list[str] = []

    def runner(_argv, program, _store, _name, stdin):
        if isinstance(program, Send):
            return b'{"id":"sent","conversation":"thread-1"}'
        prompts.append(stdin.decode())
        return b"Answer"

    for key, text in (("first", "EARLIER-MARKER"), ("second", "CURRENT-MARKER")):
        assistants.accept(messaging, "chat", [message(key).model_copy(update={"text": text})], {"personal"})
        job = queue.next(True, {"personal"})
        assert job
        assistants.execute(messaging, job, runner=runner)
        assert assistants.deliver(messaging, runner=runner)
    assert prompts[1].count("CURRENT-MARKER") == 1
    assert prompts[1].count("EARLIER-MARKER") == 1
    large = message("large").model_copy(update={"text": "🧠" * 32_000})
    assistants.accept(messaging, "chat", [large], {"personal"}, checkpoint=("stream:chat", {"cursor": "large"}))
    assert queue.progress("stream:chat") == {"cursor": "large"}
    failed = next(item for item in cast("list[dict]", queue.report()["jobs"]) if item["state"] == "failed")
    assert "exceeds 120000 bytes" in failed["error"]


def test_trigger_window_extended_while_starting_runs_with_the_merged_window(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = Queue(messaging)

    def admit(day: int) -> None:
        event = assistants.sensor_event(
            load(messaging), "calendar", f"2026-10-{day}T00:00:00Z", f"2026-10-{day + 1}T00:00:00Z"
        )
        assert event
        queue.admit_event(event)
        assistants.materialize(messaging, {"personal"}, load(messaging))

    admit(15)
    key = queue.next(False, {"personal"})
    assert key
    current, raced = assistants._current, []  # noqa: SLF001 - the gap between reading and claiming a job

    def racing(store, work):
        current(store, work)
        if not raced:
            raced.append(True)
            admit(16)

    monkeypatch.setattr(assistants, "_current", racing)
    prompts: list[str] = []
    assistants.execute(messaging, key, runner=lambda *call: prompts.append(call[4].decode()) or b"")
    assert len(prompts) == 1
    assert "2026-10-17" in prompts[0]
    assert queue.report()["counts"] == {"complete": 1}


def test_finished_jobs_and_deliveries_leave_no_lock_files(messaging: Store) -> None:
    from bf.storage import lock_file

    queue = Queue(messaging)
    for key in ("first", "second"):
        assistants.accept(messaging, "chat", [message(key)], {"personal"})
        job = queue.next(True, {"personal"})
        assert job
        assistants.execute(messaging, job, runner=answers)
        assert assistants.deliver(messaging, runner=answers)
    locks, _ = lock_file(messaging, "write.lock")
    assert not [path.name for path in locks.root.iterdir() if ".job-" in path.name or ".delivery-" in path.name]


def test_every_harness_run_carries_the_recursion_guard(messaging: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    guards = []

    def harness(_argv, _program, _store, _name, _stdin=b"", /, *, overrides=None, **_):
        guards.append(dict(overrides or {}).get("BF_ASSISTANT_RUN"))
        return b"Answer"

    monkeypatch.setattr(assistants, "run", harness)
    assert assistants.manual(messaging, "personal", text="Hi", dry_run=True, runner=harness)["ok"]
    assert assistants.manual(messaging, "personal", text="Hi", runner=harness)["ok"]
    assert guards[0] == "dry-run"
    assert guards[1]


def test_queue_commits_are_durable_when_they_return(messaging: Store) -> None:
    with Queue(messaging).connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 3
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_direct_run_keeps_its_job_and_deliveries_from_background_workers(
    messaging: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(
        messaging,
        lambda config: config["assistants"]["personal"].update(
            outputs=[{"channel": {"name": "chat", "when": ["manual"]}}], schedules={}
        ),
    )
    current, background = assistants._current, []  # noqa: SLF001 - the gap between enqueueing and claiming

    def racing(store, work):
        if work.kind == "manual" and not background:
            background.extend(assistants.cycle(store, {"personal"}, at=datetime.now(UTC), runner=answers))
        current(store, work)

    monkeypatch.setattr(assistants, "_current", racing)
    deliver, contended = assistants.deliver, []

    def sending(store, **options):
        if not contended:
            # A dispatcher sends the delivery meanwhile: this call finds its lock held.
            contended.append(deliver(store, **options))
            raise BusyError("delivery is already being sent")
        return deliver(store, **options)

    monkeypatch.setattr(assistants, "deliver", sending)
    result = assistants.manual(messaging, "personal", text="Hi", runner=answers)
    assert result["ok"], result
    assert [item["status"] for item in background if item.get("job")] == ["skipped"]
    [run] = cast("list[dict]", result["assistants"])
    assert [item["state"] for item in run["deliveries"]] == ["delivered"]
