"""Channel transports: complete read batches, bounded envelopes, and stream adapters that fail, stall or exit."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from typing import cast

import pytest
from pydantic import ValidationError

from bf import assistants, channels
from bf.config import load
from bf.messages import Message
from bf.models import Command, Config, Error, Stream, encode, explain
from bf.runtime import Queue, QueueFullError
from bf.storage import Store

START, END, INSIDE = "2026-10-15T00:00:00.000000Z", "2026-10-16T00:00:00.000000Z", "2026-10-15T12:00:00.000000Z"
ASSISTED = b"""version: 8
name: fixture
channels:
  chat:
    send: {command: [send-chat]}
    stream: {command: [receive-chat]}
assistants:
  personal:
    prompt: prompts/personal.md
    command: [harness]
    inputs:
      - channel: {name: chat, mode: stream}
"""


def envelope(**fields: object) -> dict[str, object]:
    return {"id": "m-1", "sender": "owner", "conversation": "thread-1", "time": INSIDE, "text": "Hello", **fields}


def batch(messages: list[dict[str, object]]) -> bytes:
    return encode({"messages": messages})


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (batch([envelope(), envelope()]), "read returned duplicate message ids"),
        (batch([envelope(time=END)]), "read returned a message outside its requested window"),
        (batch([envelope(time="2026-10-14T23:59:59.000000Z")]), "read returned a message outside its requested window"),
        (
            batch([envelope(id=f"m-{index}") for index in range(1001)]),
            "complete JSON messages batch: messages: List should have at most 1000 items",
        ),
        (batch([envelope(id="i" * 4097)]), "messages.0.id: String should have at most 4096 characters"),
        (batch([envelope(text="t" * 32_769)]), "messages.0.text: String should have at most 32768 characters"),
        (batch([envelope(sender="own\u200ber")]), "messages.0.sender: message identifiers cannot contain control"),
        (batch([envelope(conversation="bell\u0007")]), "messages.0.conversation: message identifiers cannot contain"),
        (batch([envelope(cursor="   ")]), "messages.0.cursor: message identifiers cannot contain control"),
    ],
    ids=["duplicate", "at-end", "before-start", "1001", "long-id", "long-text", "format", "control", "blank"],
)
def test_reads_reject_incomplete_or_invalid_batches_without_quoting_them(brain: Store, raw: bytes, reason: str) -> None:
    with pytest.raises(Error, match=re.escape(reason)) as failure:
        channels.read(brain, "chat", START, END, command=Command(command=["fake"]), runner=lambda *_: raw)
    # The field and the rule are named; the provider's value never is.
    assert "own\u200ber" not in str(failure.value)
    assert "iiii" not in str(failure.value)


def test_a_read_batch_at_every_bound_is_accepted(brain: Store) -> None:
    messages = [envelope(id=f"m-{index}", time=START) for index in range(999)]
    messages.append(envelope(id="i" * 4096, sender="s" * 4096, text="t" * 32_768, cursor="c" * 4096))
    read = channels.read(
        brain, "chat", START, END, command=Command(command=["fake"]), runner=lambda *_: batch(messages)
    )
    assert len(read) == 1000


def adapter(brain: Store, body: str, **settings: int) -> Stream:
    """A python3 stream adapter under channels/ that records each control frame it reads in seen.txt."""
    brain.write("channels/adapter.py", ("import json, os, subprocess, sys, time\n" + body).encode())
    return Stream(command=["python3", "channels/adapter.py"], **settings)


def entries(brain: Store) -> list[str]:
    log = (brain.root / "logs/channel.chat.stream.log").read_text()
    return re.findall(r"^== (.*) ==$", log, flags=re.MULTILINE)


HEADING = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00 {outcome} after \d+\.\ds"


def heading(outcome: str) -> re.Pattern[str]:
    return re.compile(HEADING.replace("{outcome}", outcome))


RECORD = """
def record(line):
    with open("seen.txt", "a") as seen:
        seen.write(line)
"""


@pytest.mark.parametrize("admitted", [True, False])
def test_a_message_is_acknowledged_only_after_its_admission(brain: Store, *, admitted: bool) -> None:
    command = adapter(
        brain,
        RECORD
        + f"""
record(sys.stdin.readline())
print({json.dumps(json.dumps(envelope()))}, flush=True)
for line in sys.stdin:
    record(line)
""",
    )
    stopped = Event()

    def accept(_message: Message) -> None:
        if not admitted:
            # A full queue or changed configuration refuses the message before any acknowledgement.
            raise Error("assistant queue has no unfinished-work slots")

    def acknowledged() -> None:
        deadline = time.monotonic() + 10
        while "ack" not in ((brain.root / "seen.txt").read_text() if (brain.root / "seen.txt").exists() else ""):
            assert time.monotonic() < deadline, "the adapter received no acknowledgement"
            time.sleep(0.01)
        stopped.set()

    if admitted:
        watcher = Thread(target=acknowledged)
        watcher.start()
        channels.stream(brain, "chat", command, {"since": START, "cursor": ""}, accept, stopped)
        watcher.join(10)
    else:
        with pytest.raises(Error, match="no unfinished-work slots"):
            channels.stream(brain, "chat", command, {"since": START, "cursor": ""}, accept, stopped)
    frames = [json.loads(line) for line in brain.read("seen.txt").decode().splitlines()]
    assert frames[0] == {"type": "resume", "since": START, "cursor": ""}
    assert [frame["type"] for frame in frames[1:]] == (["ack"] if admitted else [])


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('print("x" * 2000, flush=True)\ntime.sleep(30)\n', "stream frame exceeded max_bytes"),
        ('sys.stdout.write("x" * 2000)\nsys.stdout.flush()\ntime.sleep(30)\n', "stream frame exceeded max_bytes"),
        ('print("not json", flush=True)\ntime.sleep(30)\n', "invalid stream message envelope"),
        (
            f"print({json.dumps(json.dumps({**envelope(), 'sender': None}))}, flush=True)\ntime.sleep(30)\n",
            "invalid stream message envelope: sender: Input should be a valid string",
        ),
    ],
    ids=["oversized-line", "oversized-partial", "not-json", "missing-sender"],
)
def test_invalid_frames_restart_the_adapter_and_leave_a_timed_log_entry(brain: Store, body: str, reason: str) -> None:
    command = adapter(brain, body, max_bytes=1024)
    started = time.monotonic()
    with pytest.raises(Error, match=re.escape(f"channel chat: {reason}")):
        channels.stream(brain, "chat", command, {"since": START}, lambda _: None, Event())
    # The adapter is killed instead of waiting out its sleep.
    assert time.monotonic() - started < 10
    # Even a silent adapter's failure leaves a timed entry naming the outcome.
    assert [entry for entry in entries(brain) if heading(re.escape(f"channel chat: {reason}")).fullmatch(entry)]


@pytest.mark.parametrize("stderr", ["token expired", ""])
def test_an_exited_adapter_logs_its_status_and_diagnostics(brain: Store, stderr: str) -> None:
    command = adapter(brain, f"sys.stderr.write({stderr!r})\nsys.exit(3)\n")
    with pytest.raises(Error, match=r"channel chat: stream (exited|closed its output)"):
        channels.stream(brain, "chat", command, {"since": START}, lambda _: None, Event())
    [entry] = entries(brain)
    assert heading(r"channel chat: stream (exited|closed its output).*; adapter exited with status 3").fullmatch(entry)
    log = (brain.root / "logs/channel.chat.stream.log").read_text()
    assert log.endswith(f"{stderr}\n" if stderr else "==\n")


def ack_size(index: int) -> int:
    return len(encode({"type": "ack", "id": f"m-{index:05d}", "cursor": ""}))


@pytest.mark.parametrize(
    ("count", "timeout", "reason"),
    [
        # More acknowledgements than any pipe buffer holds, yet fewer than BF's own buffer: the write blocks.
        (70_000 // ack_size(0) + 1, 1, "stream stopped reading acknowledgements"),
        # More than a pipe and BF's buffer hold together: BF reconnects instead of buffering without bound.
        (200_000 // ack_size(0) + 1, 30, "acknowledgement buffer is full; reconnecting safely"),
    ],
    ids=["blocked", "overflow"],
)
def test_an_adapter_that_stops_reading_acknowledgements_is_restarted(
    brain: Store, count: int, timeout: int, reason: str
) -> None:
    template = json.dumps(envelope(id="ID"))
    command = adapter(
        brain,
        f"""
for index in range({count}):
    sys.stdout.write({template!r}.replace("ID", f"m-{{index:05d}}") + "\\n")
sys.stdout.flush()
time.sleep(60)
""",
        timeout=timeout,
    )
    accepted: list[str] = []
    with pytest.raises(Error, match=re.escape(f"channel chat: {reason}")):
        channels.stream(brain, "chat", command, {"since": START}, lambda message: accepted.append(message.id), Event())
    assert accepted


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An orphan its new parent has not reaped yet is already dead.
    status = Path(f"/proc/{pid}/stat")
    return not (status.exists() and status.read_text().rsplit(")", 1)[1].split()[0] == "Z")


def test_stopping_a_stream_kills_the_adapters_descendants(brain: Store) -> None:
    command = adapter(
        brain,
        f"""
child = subprocess.Popen(["sleep", "60"])
open("child.pid", "w").write(str(child.pid))
print({json.dumps(json.dumps(envelope()))}, flush=True)
time.sleep(60)
""",
    )
    stopped = Event()
    channels.stream(brain, "chat", command, {"since": START}, lambda _: stopped.set(), stopped)
    pid = int(brain.read("child.pid"))
    deadline = time.monotonic() + 5
    while alive(pid):
        assert time.monotonic() < deadline, "the adapter's descendant outlived the stream"
        time.sleep(0.05)
    assert heading("stopped").fullmatch(entries(brain)[-1])


@pytest.mark.parametrize(("limit", "accepted"), [(None, True), (65_536, False)])
def test_the_default_frame_limit_holds_the_largest_valid_text(
    brain: Store, limit: int | None, *, accepted: bool
) -> None:
    # 32,768 CJK characters, escaped by Python's default json.dumps: about 196 KB in one frame.
    command = adapter(
        brain,
        f"print(json.dumps({envelope(text='漢' * 32_768)!r}), flush=True)\ntime.sleep(60)\n",
        **({"max_bytes": limit} if limit else {}),
    )
    stopped, received = Event(), []

    def accept(message: Message) -> None:
        received.append(message)
        stopped.set()

    if accepted:
        channels.stream(brain, "chat", command, {"since": START}, accept, stopped)
        assert received[0].text == "漢" * 32_768
    else:
        with pytest.raises(Error, match="stream frame exceeded max_bytes"):
            channels.stream(brain, "chat", command, {"since": START}, accept, stopped)


def test_a_failing_stream_reconnects_with_capped_backoff_reset_by_admission(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain.write("bf.yaml", ASSISTED)
    brain.write("prompts/personal.md", b"Answer {{input}}.\n")
    calls, delays = [], []

    def failing(_store: Store, _name: str, _command: Stream, _resume: dict, accept, _stopped: Event, **_) -> None:
        calls.append(1)
        if len(calls) == 9:
            accept(Message.model_validate(envelope(time=INSIDE)))
        raise Error("channel chat: stream exited; reconnecting from durable progress")

    class Clock(Event):
        """Records each reconnect delay instead of sleeping; stops after the ninth."""

        def is_set(self) -> bool:
            return len(delays) >= 9

        def wait(self, timeout: float | None = None) -> bool:
            delays.append(timeout)
            return len(delays) >= 9

        def set(self) -> None:
            pass

    monkeypatch.setattr(channels, "stream", failing)
    dispatcher = assistants.Dispatcher(brain, {"personal"})
    try:
        dispatcher.stopped = Clock()
        stream = load(brain).channels["chat"].stream
        assert stream is not None
        dispatcher._stream("chat", stream)  # noqa: SLF001 - the reconnect loop of one stream worker
    finally:
        dispatcher.close()
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 1]
    # The admitted message is durable before its acknowledgement, and the restart reason is current health.
    queue = Queue(brain)
    assert queue.report()["counts"] == {"pending": 1}
    assert (
        queue.progress("health:channel:chat")["error"]
        == "channel chat: stream exited; reconnecting from durable progress"
    )


def drive(brain: Store, monkeypatch: pytest.MonkeyPatch, *runs: Callable[..., None]) -> tuple[list[dict], list]:
    """Run the stream worker of a watch selecting only `personal` through `runs`, one per adapter run, each called
    with the worker's accept and connected callbacks; it raises to fail or returns to stop. Never sleeps between
    reconnects; returns each run's resume frame and each reconnect delay."""
    resumes: list[dict] = []
    delays: list = []

    def fake(_store: Store, _name: str, _command: Stream, resume: dict, accept, _stopped: Event, **options) -> None:
        resumes.append(dict(resume))
        runs[len(resumes) - 1](accept, options["connected"])

    class Instant(Event):
        def wait(self, timeout: float | None = None) -> bool:
            delays.append(timeout)
            return len(resumes) >= len(runs)

    monkeypatch.setattr(channels, "stream", fake)
    dispatcher = assistants.Dispatcher(brain, {"personal"})
    try:
        dispatcher.stopped = Instant()
        stream = load(brain).channels["chat"].stream
        assert stream is not None
        dispatcher._stream("chat", stream)  # noqa: SLF001 - the reconnect loop of one stream worker
    finally:
        dispatcher.close()
    return resumes, delays


def disconnect(*_: object) -> None:
    raise Error("channel chat: stream closed its output")


def test_a_scoped_watch_admits_every_subscriber_before_acknowledging_and_resumes_from_its_cursor(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    subscriber = "    prompt: prompts/personal.md\n    command: [harness]\n    inputs:\n      - channel: {name: chat, mode: stream}\n"
    brain.write("bf.yaml", ASSISTED + f"  other:\n{subscriber}  paused:\n    enabled: false\n{subscriber}".encode())
    brain.write("prompts/personal.md", b"Answer {{input}}.\n")

    def admit(accept, _connected) -> None:
        accept(Message.model_validate(envelope(cursor="c-1")))
        disconnect()

    resumes, _ = drive(brain, monkeypatch, admit, lambda *_: None)
    queue = Queue(brain)
    # Watch selects only personal, yet the acknowledged message is durable for every enabled subscriber.
    assert sorted(job["assistant"] for job in cast("list[dict]", queue.report()["jobs"])) == ["other", "personal"]
    assert queue.report()["counts"] == {"pending": 2}
    # The reconnect resumes from the admitted cursor; `since` stays the stream's first start.
    assert resumes[1] == {"since": resumes[0]["since"], "cursor": "c-1"}
    assert resumes[0]["cursor"] == ""


def test_a_reconnected_stream_that_stays_up_is_healthy_before_its_next_message(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain.write("bf.yaml", ASSISTED)
    brain.write("prompts/personal.md", b"Answer {{input}}.\n")
    _, delays = drive(brain, monkeypatch, disconnect, disconnect, lambda _accept, connected: connected())
    # Staying connected resets the backoff and clears the disconnect, though no message arrived.
    assert delays == [1, 2, 1]
    assert Queue(brain).progress("health:channel:chat")["error"] == ""
    assert Queue(brain).report()["transports"] == {}


def test_a_refused_message_stays_reported_after_its_stream_reconnects(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain.write("bf.yaml", ASSISTED)
    brain.write("prompts/personal.md", b"Answer {{input}}.\n")
    full = "assistant queue is full; finish, retry or cancel unfinished work before accepting more"

    def refuse(*_: object, **__: object) -> None:
        raise QueueFullError(full)

    monkeypatch.setattr(assistants, "accept", refuse)
    incoming = Message(id="m-1", sender="owner", conversation="t-1", time=START, text="Hello?")
    _, delays = drive(
        brain, monkeypatch, lambda accept, _connected: accept(incoming), lambda _accept, connected: connected()
    )
    # The reconnect proves the transport, not that the provider's redelivery can be admitted: the refusal stays.
    assert Queue(brain).progress("health:channel:chat")["error"] == full
    assert delays == [1, 2]


@pytest.mark.parametrize(("body", "established"), [("time.sleep(60)", 0.2), ("sys.exit(3)", 60.0)], ids=["up", "exits"])
def test_only_an_adapter_that_stays_up_counts_as_connected(
    brain: Store, monkeypatch: pytest.MonkeyPatch, body: str, established: float
) -> None:
    monkeypatch.setattr(channels, "ESTABLISHED", established)
    command = adapter(brain, f"sys.stdin.readline()\n{body}\n")
    stopped, connections = Event(), []

    def connected() -> None:
        connections.append(True)
        stopped.set()

    if body.startswith("sys.exit"):
        # A crash-looping adapter never clears its own failure.
        with pytest.raises(Error, match="channel chat: stream"):
            channels.stream(brain, "chat", command, {"since": START}, lambda _: None, stopped, connected=connected)
        assert connections == []
    else:
        channels.stream(brain, "chat", command, {"since": START}, lambda _: None, stopped, connected=connected)
        assert connections == [True]


@pytest.mark.parametrize(
    ("assistant", "reason"),
    [
        ({"inputs": [{"channel": {"name": "nope", "mode": "stream"}}]}, "assistants.a.inputs: unknown channel nope"),
        ({"outputs": [{"channel": {"name": "nope"}}]}, "assistants.a.outputs: unknown channel nope"),
        ({"inputs": [{"channel": {"name": "chat", "mode": "read", "every": 60}}]}, "channel chat requires read"),
        ({"outputs": [{"channel": {"name": "listen"}}]}, "assistants.a.outputs: channel listen requires send"),
    ],
)
def test_an_undeclared_channel_is_named_apart_from_a_missing_capability(assistant: dict, reason: str) -> None:
    with pytest.raises(ValidationError) as failure:
        Config.model_validate(
            {
                "version": 8,
                "name": "fixture",
                "channels": {"chat": {"send": {"command": ["send"]}}, "listen": {"stream": {"command": ["listen"]}}},
                "assistants": {"a": {"prompt": "prompts/a.md", "command": ["harness"], **assistant}},
            }
        )
    assert reason in explain(failure.value)
