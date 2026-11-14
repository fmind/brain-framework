"""Inbound message jobs (`emit: messages`) and daemon NDJSON streams (`on: daemon`)."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from threading import Event, Lock, Thread

import pytest
from pydantic import JsonValue, ValidationError

from bf import jobs, watch
from bf.models import Config, Error, encode, explain
from bf.runtime import Message, Queue, QueueFullError
from bf.storage import Store

START, END, INSIDE = "2026-10-15T00:00:00.000000Z", "2026-10-16T00:00:00.000000Z", "2026-10-15T12:00:00.000000Z"
DAEMON_CONFIG = b"""version: 10
name: fixture
jobs:
  chat:
    run: [python3, channels/adapter.py]
    on: {daemon: true}
    emit: messages
"""
BATCH_CONFIG = b"""version: 10
name: fixture
jobs:
  chat:
    run: [read-chat]
    on: {every: 60}
    emit: messages
"""


def envelope(**fields: object) -> dict[str, object]:
    return {"id": "m-1", "sender": "owner", "conversation": "thread-1", "time": INSIDE, "text": "Hello", **fields}


def batch(messages: list[dict[str, object]], *, cursor: str = "") -> bytes:
    payload: dict[str, object] = {"messages": messages}
    if cursor:
        payload["cursor"] = cursor
    return encode(payload)


def write_adapter(brain: Store, body: str) -> None:
    brain.write("channels/adapter.py", ("import json, os, signal, subprocess, sys, time\n" + body).encode())
    brain.write("bf.yaml", DAEMON_CONFIG)


def entries(brain: Store, name: str = "chat") -> list[str]:
    log = (brain.root / f"logs/{name}.log").read_text()
    return re.findall(r"^== (.*) ==$", log, flags=re.MULTILINE)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b'"hello"', "job chat: emit: messages expects JSON array or {messages: [...]}"),
        (
            batch([envelope(id=f"m-{index}") for index in range(1001)]),
            "job chat: invalid messages output: messages: List should have at most 1000 items",
        ),
        (batch([envelope(id="i" * 4097)]), "messages.0.id: String should have at most 4096 characters"),
        (batch([envelope(text="t" * 32_769)]), "messages.0.text: String should have at most 32768 characters"),
        (batch([envelope(sender="own\u200ber")]), "messages.0.sender: message identifiers cannot contain control"),
        (batch([envelope(conversation="bell\u0007")]), "messages.0.conversation: message identifiers cannot contain"),
        (batch([envelope(cursor="   ")]), "messages.0.cursor: message identifiers cannot contain control"),
    ],
    ids=["non-array", "1001", "long-id", "long-text", "format", "control", "blank"],
)
def test_message_jobs_reject_invalid_batches_without_quoting_them(brain: Store, raw: bytes, reason: str) -> None:
    brain.write("bf.yaml", BATCH_CONFIG)
    with pytest.raises(Error, match=re.escape(reason)) as failure:
        jobs.run_job(brain, "chat", runner=lambda *_: raw)
    # The field and rule are named; provider values never are.
    assert "own\u200ber" not in str(failure.value)
    assert "iiii" not in str(failure.value)


def test_a_message_batch_at_every_bound_and_cursor_is_admitted(brain: Store) -> None:
    brain.write("bf.yaml", BATCH_CONFIG)
    messages = [envelope(id=f"m-{index}", time=START) for index in range(999)]
    messages.append(envelope(id="i" * 4096, sender="s" * 4096, text="t" * 32_768, cursor="c" * 4096))
    result = jobs.run_job(brain, "chat", runner=lambda *_: batch(messages, cursor="c" * 4096))
    assert result["messages"] == 1000
    assert result["dropped"] == 0
    queue = Queue(brain)
    assert queue.state_of("chat")["cursor"] == "c" * 4096
    assert queue.progress("cursor:chat") == {"cursor": "c" * 4096}
    # Re-reading the exact same batch deduplicates idempotently.
    replay = jobs.run_job(brain, "chat", runner=lambda *_: batch(messages[:1]))
    assert replay["messages"] == 0
    assert replay["dropped"] == 1


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('print("x" * 2000, flush=True)\ntime.sleep(30)\n', "daemon chat: output line exceeds 1024 bytes"),
        (
            'sys.stdout.write("x" * 2000)\nsys.stdout.flush()\ntime.sleep(30)\n',
            "daemon chat: output line exceeds 1024 bytes",
        ),
        ('print("not json", flush=True)\ntime.sleep(30)\n', "daemon chat: invalid JSON document"),
        (
            f"print({json.dumps(json.dumps({**envelope(), 'sender': None}))}, flush=True)\ntime.sleep(30)\n",
            "daemon chat: invalid stream message",
        ),
    ],
    ids=["oversized-line", "oversized-partial", "not-json", "missing-sender"],
)
def test_invalid_stream_frames_fail_daemon_and_leave_log_entry(
    brain: Store, monkeypatch: pytest.MonkeyPatch, body: str, reason: str
) -> None:
    monkeypatch.setattr(watch, "_MAX_STREAM_LINE", 1024)
    monkeypatch.setattr(watch, "_MAX_STREAM_FAILS", 1)
    write_adapter(brain, body)
    fatal: list[str] = []
    started = time.monotonic()
    watch._stream_job(brain, "chat", Event(), {}, Lock(), fatal)  # noqa: SLF001 - single-daemon failure path
    # The adapter is stopped instead of waiting out its sleep.
    assert time.monotonic() - started < 10
    assert len(fatal) == 1
    assert reason in fatal[0]
    assert reason in str(Queue(brain).progress("health:daemon:chat").get("error"))
    assert any(reason in (brain.root / "logs/chat.log").read_text() for _ in [0])
    assert entries(brain)


@pytest.mark.parametrize("stderr", ["token expired", ""])
def test_an_exited_daemon_logs_its_status_and_stderr(
    brain: Store, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    monkeypatch.setattr(watch, "_MAX_STREAM_FAILS", 1)
    write_adapter(brain, f"sys.stderr.write({stderr!r})\nsys.exit(3)\n")
    fatal: list[str] = []
    watch._stream_job(brain, "chat", Event(), {}, Lock(), fatal)  # noqa: SLF001 - daemon exit path
    assert fatal == ["daemon chat: exited with status 3; see logs/chat.log"]
    log = (brain.root / "logs/chat.log").read_text()
    if stderr:
        assert f"{stderr}\n" in log
    assert "failed: daemon chat: exited with status 3; see logs/chat.log" in log


def test_a_killed_daemon_records_its_signal_exit(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "_MAX_STREAM_FAILS", 1)
    write_adapter(brain, "os.kill(os.getpid(), 15)\ntime.sleep(30)\n")
    fatal: list[str] = []
    watch._stream_job(brain, "chat", Event(), {}, Lock(), fatal)  # noqa: SLF001 - daemon killed path
    assert fatal == ["daemon chat: exited with status -15; see logs/chat.log"]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    return not (status.exists() and status.read_text().rsplit(")", 1)[1].split()[0] == "Z")


def test_stopping_a_daemon_kills_its_descendants(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    write_adapter(
        brain,
        f"""
child = subprocess.Popen(["sleep", "60"])
open("child.pid", "w").write(str(child.pid))
print({json.dumps(json.dumps(envelope()))}, flush=True)
time.sleep(60)
""",
    )
    stopped, procs, lock = Event(), {}, Lock()
    real_admit = Queue.admit

    def admit_and_stop(
        self: Queue,
        channel: str,
        messages: list[Message],
        *,
        checkpoint: tuple[str, dict[str, JsonValue]] | None = None,
    ) -> list[Message]:
        res = real_admit(self, channel, messages, checkpoint=checkpoint)
        stopped.set()
        return res

    monkeypatch.setattr(Queue, "admit", admit_and_stop)
    worker = Thread(target=watch._stream_job, args=(brain, "chat", stopped, procs, lock, []), daemon=True)  # noqa: SLF001
    worker.start()
    deadline = time.monotonic() + 10
    while not stopped.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stopped.is_set(), "daemon did not admit initial message"
    with lock:
        active = list(procs.values())
    for proc in active:
        jobs.stop(proc)
    worker.join(timeout=10)
    assert not worker.is_alive()
    pid = int(brain.read("child.pid"))
    deadline = time.monotonic() + 5
    while alive(pid):
        assert time.monotonic() < deadline, "daemon descendant outlived stop"
        time.sleep(0.05)


def test_a_stopped_daemon_gets_sigterm_before_the_kill(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    write_adapter(
        brain,
        f"""
def clean(*_):
    open("cleaned", "w").close()
    sys.exit(0)
signal.signal(signal.SIGTERM, clean)
print({json.dumps(json.dumps(envelope()))}, flush=True)
time.sleep(60)
""",
    )
    stopped, procs, lock = Event(), {}, Lock()
    real_admit = Queue.admit

    def admit_and_stop(
        self: Queue,
        channel: str,
        messages: list[Message],
        *,
        checkpoint: tuple[str, dict[str, JsonValue]] | None = None,
    ) -> list[Message]:
        res = real_admit(self, channel, messages, checkpoint=checkpoint)
        stopped.set()
        return res

    monkeypatch.setattr(Queue, "admit", admit_and_stop)
    started = time.monotonic()
    worker = Thread(target=watch._stream_job, args=(brain, "chat", stopped, procs, lock, []), daemon=True)  # noqa: SLF001
    worker.start()
    deadline = time.monotonic() + 10
    while not stopped.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stopped.is_set()
    with lock:
        active = list(procs.values())
    for proc in active:
        jobs.stop(proc)
    worker.join(timeout=10)
    assert time.monotonic() - started < 10
    assert brain.read("cleaned") == b""


@pytest.mark.parametrize(("limit", "accepted"), [(None, True), (65_536, False)])
def test_the_default_stream_line_limit_holds_the_largest_valid_text(
    brain: Store, monkeypatch: pytest.MonkeyPatch, limit: int | None, *, accepted: bool
) -> None:
    # 32,768 CJK characters, escaped by Python's default json.dumps: about 196 KB in one line.
    if limit is not None:
        monkeypatch.setattr(watch, "_MAX_STREAM_LINE", limit)
    monkeypatch.setattr(watch, "_MAX_STREAM_FAILS", 1)
    write_adapter(brain, f"print(json.dumps({envelope(text='漢' * 32_768)!r}), flush=True)\ntime.sleep(60)\n")
    stopped, procs, lock, fatal = Event(), {}, Lock(), []
    real_admit = Queue.admit

    def stop_on_admit(
        self: Queue,
        channel: str,
        messages: list[Message],
        *,
        checkpoint: tuple[str, dict[str, JsonValue]] | None = None,
    ) -> list[Message]:
        res = real_admit(self, channel, messages, checkpoint=checkpoint)
        stopped.set()
        return res

    if accepted:
        monkeypatch.setattr(Queue, "admit", stop_on_admit)
        worker = Thread(target=watch._stream_job, args=(brain, "chat", stopped, procs, lock, fatal), daemon=True)  # noqa: SLF001
        worker.start()
        deadline = time.monotonic() + 10
        while not stopped.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stopped.is_set()
        with lock:
            active = list(procs.values())
        for proc in active:
            jobs.stop(proc)
        worker.join(timeout=10)
        assert not fatal
        assert Queue(brain).pending_messages("chat")[0]["text"] == "漢" * 32_768
    else:
        watch._stream_job(brain, "chat", stopped, procs, lock, fatal)  # noqa: SLF001
        assert fatal
        assert "output line exceeds 65536 bytes" in fatal[0]


def test_a_failing_daemon_reconnects_with_capped_backoff_reset_when_established(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain.write("bf.yaml", DAEMON_CONFIG)
    brain.write("channels/adapter.py", b"")
    calls, delays, clock = [], [], [0.0]

    class FakeProc:
        stdout = None
        stderr = None

        def poll(self) -> int:
            return 1

    class FakeClock:
        def monotonic(self) -> float:
            return clock[0]

    def fake_popen(*_args: object, **_kwargs: object) -> FakeProc:
        return FakeProc()

    def fake_pump(_store: Store, _name: str, _proc: object, _stopped: Event, started: float) -> None:
        calls.append(started)
        if len(calls) == 8:
            # Stay connected long enough to clear backoff before disconnecting again.
            clock[0] += jobs.ESTABLISHED + 1.0
        raise Error("stream closed")

    class ClockEvent(Event):
        def is_set(self) -> bool:
            return len(delays) >= 8

        def wait(self, timeout: float | None = None) -> bool:
            delays.append(timeout)
            return len(delays) >= 8

        def set(self) -> None:
            pass

    monkeypatch.setattr(watch, "_MAX_STREAM_FAILS", 20)
    monkeypatch.setattr(watch.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(watch, "_pump_stdout", fake_pump)
    monkeypatch.setattr(jobs, "stop", lambda _p: None)
    monkeypatch.setattr(watch, "time", FakeClock())
    watch._stream_job(brain, "chat", ClockEvent(), {}, Lock(), [])  # noqa: SLF001 - backoff progression test
    assert delays == [10, 20, 40, 80, 160, 300.0, 300.0, 10]
    assert Queue(brain).progress("health:daemon:chat")["error"] == "daemon chat: stream closed"


def test_a_daemon_admits_messages_and_sets_cursor_checkpoint(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    write_adapter(
        brain,
        f"""
print(json.dumps({{"messages": [{json.dumps(envelope(cursor="c-1"))}], "cursor": "c-1"}}), flush=True)
time.sleep(60)
""",
    )
    stopped, procs, lock = Event(), {}, Lock()
    real_admit = Queue.admit

    def admit_and_stop(
        self: Queue,
        channel: str,
        messages: list[Message],
        *,
        checkpoint: tuple[str, dict[str, JsonValue]] | None = None,
    ) -> list[Message]:
        res = real_admit(self, channel, messages, checkpoint=checkpoint)
        stopped.set()
        return res

    monkeypatch.setattr(Queue, "admit", admit_and_stop)
    worker = Thread(target=watch._stream_job, args=(brain, "chat", stopped, procs, lock, []), daemon=True)  # noqa: SLF001
    worker.start()
    deadline = time.monotonic() + 10
    while not stopped.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stopped.is_set()
    with lock:
        active = list(procs.values())
    for proc in active:
        jobs.stop(proc)
    worker.join(timeout=10)
    queue = Queue(brain)
    assert queue.progress("cursor:chat") == {"cursor": "c-1"}
    assert len(queue.pending_messages("chat")) == 1
    assert queue.report()["transports"] == {}


def test_only_a_daemon_that_stays_up_clears_health(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    Queue(brain).set_progress("health:daemon:chat", {"error": "previous failure"})
    write_adapter(brain, "time.sleep(60)\n")
    monkeypatch.setattr(jobs, "ESTABLISHED", 0.05)
    stopped, procs, lock = Event(), {}, Lock()
    worker = Thread(target=watch._stream_job, args=(brain, "chat", stopped, procs, lock, []), daemon=True)  # noqa: SLF001
    worker.start()
    deadline = time.monotonic() + 10
    while Queue(brain).progress("health:daemon:chat").get("error") and time.monotonic() < deadline:
        time.sleep(0.02)
    stopped.set()
    with lock:
        active = list(procs.values())
    for proc in active:
        jobs.stop(proc)
    worker.join(timeout=10)
    assert Queue(brain).progress("health:daemon:chat") == {}


def test_a_refused_message_stays_reported_after_daemon_disconnect(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch, "_MAX_STREAM_FAILS", 1)
    write_adapter(brain, f"print({json.dumps(json.dumps(envelope()))}, flush=True)\ntime.sleep(30)\n")
    full = "runtime queue is full; finish, retry or cancel unfinished work before accepting more"
    monkeypatch.setattr(Queue, "admit", lambda *_, **__: (_ for _ in ()).throw(QueueFullError(full)))
    fatal: list[str] = []
    watch._stream_job(brain, "chat", Event(), {}, Lock(), fatal)  # noqa: SLF001 - refused admission keeps health
    assert fatal == [f"daemon chat: {full}"]
    assert Queue(brain).progress("health:daemon:chat")["error"] == f"daemon chat: {full}"


@pytest.mark.parametrize(
    ("job_cfg", "reason"),
    [
        ({"prompt": "prompts/a.md", "post": "nope"}, "jobs.a.post: unknown job nope"),
        ({"prompt": "prompts/a.md", "post": "listen"}, "jobs.a.post: job listen requires send"),
        ({"run": ["cmd"], "emit": "PRIVATE-TOKEN"}, "jobs.a.emit: Input should be"),
    ],
)
def test_job_config_names_missing_post_target_without_quoting_bad_values(job_cfg: dict, reason: str) -> None:
    with pytest.raises(ValidationError) as failure:
        Config.model_validate(
            {
                "version": 10,
                "name": "fixture",
                "jobs": {
                    "listen": {"run": ["listen"], "on": {"daemon": True}, "emit": "messages"},
                    "a": job_cfg,
                },
            }
        )
    assert reason in explain(failure.value)
    assert "PRIVATE" not in explain(failure.value)
