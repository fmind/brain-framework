"""Documented assistant paths: ACK after durable admission, blocked schedules, delivery recovery and watch stops.

Every provider, harness and channel is a local fake; nothing contacts the network or runs a model.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import cast

import pytest

from bf import assistants, channels, health, records
from bf.collect import collect
from bf.config import load
from bf.messages import Message
from bf.models import Command, Error, Send, Stream, encode, timestamp
from bf.runtime import Queue
from bf.storage import Store

NAMES = {"personal"}
CONFIG = b"""version: 8
name: paths
sensors:
  calendar:
    command: [calendar]
channels:
  chat:
    send: {command: [send-chat], idempotent: true}
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
      - channel: {name: chat, when: [schedule]}
      - channel: {name: desktop, when: [schedule]}
    schedules:
      review: {every: 1800}
    triggers:
      calendar: {event: sensor.changed, sensor: {name: calendar}}
"""


@pytest.fixture
def store(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    brain.write("prompts/personal.md", b"Review {{start}} to {{end}}.\nMessage: {{input}}\n")
    return brain


def message(key: str = "message-1") -> Message:
    return Message(
        id=key,
        sender="owner",
        conversation="thread-1",
        time=timestamp((datetime.now(UTC) - timedelta(seconds=1)).isoformat()),
        text="Please explain",
    )


def fake(calls: list[str] | None = None, answer: bytes = b"Answer") -> Callable[..., bytes]:
    """A harness that answers and channels that return a receipt for the requested conversation."""

    def runner(argv: list[str], program: Command, _store: Store, _name: str, stdin: bytes) -> bytes:
        if calls is not None:
            calls.append(argv[0])
        if isinstance(program, Send):
            outgoing = json.loads(stdin)
            return encode({"id": "sent-" + outgoing["id"], "conversation": outgoing["conversation"] or "default"})
        return answer

    return runner


def failing(*_: object) -> bytes:
    raise Error("program exited with status 3")


def deliveries(store: Store) -> dict[str, dict[str, object]]:
    queue = Queue(store)
    rows = cast("list[dict[str, object]]", queue.report()["deliveries"])
    return {str(row["channel"]): queue.inspect(str(row["id"])) for row in rows}


def gone(pid: int, timeout: float = 5) -> bool:
    """Whether a process ended within `timeout`; a killed zombie waiting for its reaper counts as ended."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except OSError, IndexError:
            pass
        time.sleep(0.05)
    return False


def kill(*pids: int) -> None:
    for pid in pids:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def test_changed_assistant_fails_attempted_work_until_each_job_is_retried(store: Store) -> None:
    """Restoring a swapped command does not resume work a worker tried meanwhile (troubleshooting's warning)."""
    queue = Queue(store)
    assistants.accept(store, "chat", [message("first"), message("second")], NAMES)
    at = datetime.now(UTC)
    assert len(assistants.schedule(store, NAMES, at)[0]) == 1
    reviewed = store.read("bf.yaml")
    store.write("bf.yaml", reviewed.replace(b"command: [harness]", b"command: [cat]"))
    calls: list[str] = []
    results = assistants.cycle(store, NAMES, at=at, runner=fake(calls))
    assert calls == []
    failed = [item for item in results if item.get("status") == "failed"]
    assert len(failed) == 2
    assert all("configuration changed" in str(item.get("error")) for item in failed)
    assert queue.report()["counts"] == {"failed": 2, "pending": 1}
    store.write("bf.yaml", reviewed)
    assistants.cycle(store, NAMES, at=datetime.now(UTC), runner=fake(calls))
    # The failed input job blocks the conversation's later message, and the failed review blocks its schedule.
    assert calls == []
    assert queue.report()["counts"] == {"failed": 2, "pending": 1}
    assert assistants.due_schedules(store, NAMES, at + timedelta(hours=2)) == []
    jobs = cast("list[dict[str, object]]", queue.report()["jobs"])
    for job in jobs:
        if job["state"] == "failed":
            queue.retry(str(job["id"]))
    for _ in range(3):
        assistants.cycle(store, NAMES, at=datetime.now(UTC), runner=fake(calls))
    assert queue.report()["counts"] == {"complete": 3}
    assert calls.count("harness") == 3


def test_failed_schedule_blocks_its_window_until_cancelled_and_shows_in_attention(store: Store) -> None:
    queue = Queue(store)
    at = datetime.now(UTC)
    key = assistants.schedule(store, NAMES, at)[0][0]
    with pytest.raises(Error, match="status 3"):
        assistants.execute(store, key, runner=failing)
    # The harness might have acted before failing, so the attempt is uncertain and never replayed on its own.
    assert queue.get(key)["state"] == "uncertain"
    assert assistants.due_schedules(store, NAMES, at + timedelta(hours=2)) == []
    # The copied session hook and weekly review read this entry.
    assert {"assistant": "personal", "freshness": "never", "failed": True} in health.attention(store)
    end = queue.work(key).end
    queue.retry(key, cancel=True)
    assert queue.progress("schedule:personal:schedule:review")["end"] == end
    assert health.attention(store) == []
    later = at + timedelta(seconds=1800 + 60)
    assert assistants.due_schedules(store, NAMES, later) == [
        ("personal", "schedule:review", end, timestamp(later.isoformat()))
    ]


def test_idempotent_delivery_retries_later_while_another_waits_for_review(store: Store) -> None:
    queue = Queue(store)
    key = assistants.schedule(store, NAMES, datetime.now(UTC))[0][0]
    assistants.execute(store, key, runner=fake(answer=b"Proactive summary"))
    before = datetime.now(UTC)
    assert assistants.deliver(store, runner=failing)
    assert assistants.deliver(store, runner=failing)
    assert not assistants.deliver(store, runner=failing)
    sent = deliveries(store)
    assert sent["chat"]["state"] == "pending"
    assert sent["chat"]["attempts"] == 1
    assert datetime.fromisoformat(str(sent["chat"]["due"])) > before + timedelta(seconds=1)
    # A send that is not idempotent might have reached its destination: it waits for explicit review.
    assert sent["desktop"]["state"] not in {"pending", "sending", "delivered"}
    assert "status 3" in str(sent["desktop"]["error"])
    assert queue.delivery(names=NAMES) is None
    queue.retry(str(sent["desktop"]["id"]))
    assert assistants.deliver(store, runner=fake(), key=str(sent["desktop"]["id"]))
    assert queue.inspect(str(sent["desktop"]["id"]))["state"] == "delivered"


def test_channel_edited_after_the_answer_blocks_only_its_delivery(store: Store) -> None:
    queue = Queue(store)
    key = assistants.schedule(store, NAMES, datetime.now(UTC))[0][0]
    assistants.execute(store, key, runner=fake(answer=b"Proactive summary"))
    reviewed = store.read("bf.yaml")
    store.write("bf.yaml", reviewed.replace(b"notify-desktop", b"notify-elsewhere"))
    calls: list[str] = []
    while assistants.deliver(store, runner=fake(calls)):
        pass
    # Old work is never sent through the new command, and the unchanged channel still delivers.
    assert calls == ["send-chat"]
    blocked = deliveries(store)["desktop"]
    # Nothing was sent, yet it holds its conversation like any unresolved delivery.
    assert blocked["state"] == "uncertain"
    assert str(blocked["error"]) == (
        "channel configuration changed; restore it, then retry or cancel this delivery with bf queue"
    )
    store.write("bf.yaml", reviewed)
    # Restoring bf.yaml alone sends nothing: the delivery waits for its explicit retry.
    assert not assistants.deliver(store, runner=fake(calls), key=str(blocked["id"]))
    queue.retry(str(blocked["id"]))
    assert assistants.deliver(store, runner=fake(calls), key=str(blocked["id"]))
    assert calls == ["send-chat", "notify-desktop"]
    assert queue.report()["delivery_counts"] == {"delivered": 2}


def test_collection_inside_an_assistant_run_triggers_no_assistant(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = Queue(store)
    end = datetime.now(UTC)
    window = timestamp((end - timedelta(days=1)).isoformat()), timestamp(end.isoformat())
    monkeypatch.setenv("BF_ASSISTANT_RUN", "job")
    collect(
        store,
        "calendar",
        start=window[0],
        end=window[1],
        runner=lambda *_: b'[{"id":"event-1","title":"Moved","text":"New time"}]',
    )
    assert len(records.files(store, "calendar")) == 1
    # Trigger jobs are admitted by the next dispatch pass, from the current bf.yaml.
    assert assistants.materialize(store, NAMES, load(store)) == []
    assert queue.next(False, NAMES) is None
    monkeypatch.delenv("BF_ASSISTANT_RUN")
    collect(
        store,
        "calendar",
        start=window[0],
        end=window[1],
        runner=lambda *_: b'[{"id":"event-1","title":"Moved","text":"Later time"}]',
    )
    assert assistants.materialize(store, NAMES, load(store)) == []
    key = queue.next(False, NAMES)
    assert key
    assert queue.work(key).kind == "trigger"


def run_stream(
    store: Store, command: Stream, accept: Callable[[Message], None], stopped: Event
) -> tuple[Thread, list[BaseException]]:
    failures: list[BaseException] = []

    def target() -> None:
        try:
            channels.stream(
                store, "chat", command, {"since": timestamp(datetime.now(UTC).isoformat())}, accept, stopped
            )
        except BaseException as error:  # the test asserts which error ended the stream
            failures.append(error)

    thread = Thread(target=target)
    thread.start()
    return thread, failures


def adapter(store: Store, source: Message) -> Stream:
    """A stream that starts a helper in its process group, emits one message and records the first ACK it reads."""
    store.write(
        "channels/stream.py",
        f"""import json, os, pathlib, subprocess, sys, time
json.loads(sys.stdin.readline())
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path("helper.pid").write_text(str(helper.pid))
print({json.dumps(source.model_dump())!r}, flush=True)
ack = sys.stdin.readline()
pathlib.Path("ack.tmp").write_text(ack)
os.replace("ack.tmp", "ack.json")
time.sleep(60)
""".encode(),
    )
    return Stream(command=["python3", "channels/stream.py"])


def wait_for(path: Path, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return path.exists()


def test_stream_acknowledges_only_after_admission_returns(store: Store) -> None:
    source = message()
    entered, release, stopped = Event(), Event(), Event()

    def accept(value: Message) -> None:
        entered.set()
        assert release.wait(10)
        assistants.accept(store, "chat", [value], NAMES)

    thread, failures = run_stream(store, adapter(store, source), accept, stopped)
    try:
        assert entered.wait(10)
        time.sleep(0.3)
        assert not (store.root / "ack.json").exists(), "ACK sent before the message was durably admitted"
        assert Queue(store).report()["counts"] == {}
        release.set()
        assert wait_for(store.root / "ack.json")
        assert json.loads(store.read("ack.json")) == {"type": "ack", "id": source.id, "cursor": ""}
        assert Queue(store).report()["counts"] == {"pending": 1}
    finally:
        release.set()
        stopped.set()
        thread.join(10)
    assert not thread.is_alive()
    assert failures == []
    # Stopping kills the adapter's whole process group, including helpers it started.
    helper = int(store.read("helper.pid"))
    try:
        assert gone(helper)
    finally:
        kill(helper)


def test_failed_admission_sends_no_acknowledgement_and_stops_the_adapter(store: Store) -> None:
    source = message()

    def accept(_value: Message) -> None:
        raise Error("assistant queue has no unfinished-work slots; resolve its backlog before accepting more input")

    stopped = Event()
    thread, failures = run_stream(store, adapter(store, source), accept, stopped)
    thread.join(10)
    stopped.set()
    assert not thread.is_alive()
    assert len(failures) == 1
    assert "unfinished-work slots" in str(failures[0])
    assert wait_for(store.root / "helper.pid")
    helper = int(store.read("helper.pid"))
    try:
        assert gone(helper)
    finally:
        kill(helper)
    assert not (store.root / "ack.json").exists()
    assert Queue(store).report()["counts"] == {}


# Messages whose 4,000-character ids make each ACK frame about 4 KiB.
FLOOD = (
    "[print(json.dumps({'id': f'm{i:03d}-' + 'x' * 4000, 'sender': 'owner', 'conversation': 'c', "
    "'time': '2026-10-16T09:00:00Z', 'text': 'hi'}), flush=True) for i in range(64)]"
)


@pytest.mark.parametrize(
    ("emit", "max_bytes", "error"),
    [
        # One frame longer than max_bytes, with and without its line end.
        ("print('x' * 2048, flush=True)", 1024, "stream frame exceeded max_bytes"),
        ("sys.stdout.write('x' * 2048); sys.stdout.flush()", 1024, "stream frame exceeded max_bytes"),
        ("print(json.dumps({'id': 'missing-fields'}), flush=True)", 1024, "invalid stream message envelope"),
        # Never reading ACKs fills the pipe, then BF's bounded buffer or its blocked-write timeout ends the stream.
        (FLOOD, 8192, "stopped reading acknowledgements|acknowledgement buffer is full"),
    ],
    ids=["long-frame", "unterminated-frame", "invalid-envelope", "unread-acks"],
)
def test_stream_bounds_fail_without_acknowledging(store: Store, emit: str, max_bytes: int, error: str) -> None:
    store.write(
        "channels/stream.py",
        f"import json, sys, time\nsys.stdin.readline()\n{emit}\ntime.sleep(60)\n".encode(),
    )
    accepted: list[str] = []
    stopped = Event()
    thread, failures = run_stream(
        store,
        Stream(command=["python3", "channels/stream.py"], max_bytes=max_bytes, timeout=1),
        lambda value: accepted.append(value.id),
        stopped,
    )
    thread.join(20)
    stopped.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], Error)
    assert str(failures[0]).startswith("channel chat: ")
    assert re.search(error, str(failures[0])), failures[0]
    if "envelope" in error or "max_bytes" in error:
        assert accepted == []


def test_stopping_watch_kills_a_running_harness_and_leaves_its_job_uncertain(tmp_path: Path) -> None:
    root = tmp_path / "stop"
    root.mkdir()
    store = Store(root)
    store.write(
        "bf.yaml",
        b"""version: 8
name: stop
assistants:
  review:
    prompt: prompts/review.md
    command: [python3, harness.py]
    schedules:
      hourly: {every: 3600}
""",
    )
    store.write("prompts/review.md", b"Review {{start}} to {{end}}.\n")
    store.write(
        "harness.py",
        b"""import os, subprocess, sys, time
sys.stdin.read()
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open("harness.tmp", "w") as output:
    output.write(f"{os.getpid()} {helper.pid}")
os.replace("harness.tmp", "harness.pids")
time.sleep(60)
print("too late")
""",
    )
    watch = subprocess.Popen(  # noqa: S603 - a fake harness in an isolated brain
        [sys.executable, "-m", "bf", "watch", "--brain", str(root), "--json"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, "BF_BRAIN": str(root)},
    )
    pids: list[int] = []
    try:
        # A cold watch start plus the harness's own gets the suite's 120-second cold-start budget.
        if not wait_for(root / "harness.pids", 120):
            watch.kill()
            pytest.fail(f"the scheduled harness never started: {watch.communicate(timeout=30)[1].decode()}")
        pids = [int(pid) for pid in store.read("harness.pids").split()]
        watch.terminate()
        _, stderr = watch.communicate(timeout=30)
        assert watch.returncode == 130, stderr.decode()
        assert all(gone(pid) for pid in pids)
    finally:
        if watch.poll() is None:
            watch.kill()
            watch.communicate(timeout=30)
        kill(*pids)
    report = Queue(store).report()
    assert report["counts"] == {"uncertain": 1}
    jobs = cast("list[dict[str, object]]", report["jobs"])
    assert jobs[0]["kind"] == "schedule"
    assert not records.files(store, "review")
