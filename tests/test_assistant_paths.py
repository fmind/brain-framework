"""Documented job, wake, stream-daemon, delivery recovery and watch stop paths.

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
from typing import cast

import pytest

from bf import health, jobs, records, update, watch
from bf.collect import collect
from bf.models import Job, encode, timestamp
from bf.runtime import MAX_BATCH, Message, Queue
from bf.storage import Store

CONFIG = b"""version: 10
name: paths
jobs:
  calendar:
    run: [calendar]
    emit: records
  chat:
    send: [send-chat]
    idempotent: true
  desktop:
    send: [notify-desktop]
  personal:
    prompt: prompts/personal.md
    harness: [harness]
    emit: message
    post: chat
    on:
      - every: 1800
  notifier:
    prompt: prompts/personal.md
    harness: [harness]
    emit: message
    post: desktop
    on:
      - every: 1800
"""


@pytest.fixture
def store(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    brain.write("prompts/personal.md", b"Review {{start}} to {{end}}.\nContext: {{context}}\n")
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
    """A harness that answers and send commands that return a receipt for the requested conversation."""

    def runner(argv: list[str], program: Job, _store: Store, _name: str, stdin: bytes) -> bytes:
        if calls is not None:
            calls.append(argv[0])
        if program.send:
            outgoing = json.loads(stdin)
            return encode({"id": "sent-" + outgoing["id"], "conversation": outgoing["conversation"] or "default"})
        return answer

    return runner


def failing_send(calls: list[str] | None = None, answer: bytes = b"Proactive summary") -> Callable[..., bytes]:
    """A harness that answers while any `send:` command exits non-zero."""

    def runner(argv: list[str], program: Job, _store: Store, _name: str, _stdin: bytes) -> bytes:
        if calls is not None:
            calls.append(argv[0])
        if program.send:
            raise jobs.ExitError(3, "program exited with status 3")
        return answer

    return runner


def failing(*_: object) -> bytes:
    raise jobs.ExitError(3, "program exited with status 3")


def deliveries(store: Store) -> dict[str, dict[str, object]]:
    queue = Queue(store)
    rows = cast("list[dict[str, object]]", queue.report()["deliveries"])
    return {str(row["send_job"]): queue.inspect(str(row["id"])) for row in rows}


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


def wait_for(path: Path, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return path.exists()


def test_failed_wake_blocks_reexecution_until_retried_and_shows_in_attention(store: Store) -> None:
    queue = Queue(store)
    wake = jobs.wake_command(store, job="personal", reason_text="check failure handling")
    wake_id = str(wake["id"])
    cfg = jobs.cfg.load(store)
    results, failed = update.run_wakes(store, cfg, {"personal"}, runner=failing)
    assert failed
    assert results[0]["status"] == "failed"
    assert queue.inspect(wake_id)["state"] == "failed"
    assert {"kind": "job", "name": "personal", "freshness": "never", "failed": True} in health.attention(store)
    # A failed wake does not replay on its own across later cycles.
    again, _ = update.run_wakes(store, cfg, {"personal"}, runner=fake())
    assert again == []
    # Cancelling or retrying explicitly resolves the failed wake; cancelling clears it without replay.
    queue.retry(wake_id, cancel=True)
    assert queue.inspect(wake_id)["state"] == "cancelled"
    assert queue.report()["wakes"] == []
    # Running the job successfully clears its failure state in attention.
    jobs.run_job(store, "personal", runner=fake())
    assert all(item["name"] != "personal" for item in health.attention(store))


def test_idempotent_delivery_retries_later_while_another_waits_for_review(store: Store) -> None:
    queue = Queue(store)
    before = datetime.now(UTC)
    jobs.run_job(store, "personal", runner=failing_send())
    jobs.run_job(store, "notifier", runner=failing_send())
    assert not jobs.deliver_due(store, runner=failing)
    sent = deliveries(store)
    assert sent["chat"]["state"] == "pending"
    assert sent["chat"]["attempts"] == 1
    assert datetime.fromisoformat(str(sent["chat"]["due"])) > before + timedelta(seconds=1)
    # A send that is not idempotent might have reached its destination: it waits for explicit review.
    assert sent["desktop"]["state"] == "uncertain"
    assert "status 3" in str(sent["desktop"]["error"])
    assert queue.due_delivery() is None
    queue.retry(str(sent["desktop"]["id"]))
    assert jobs.deliver_due(store, runner=fake(), key=str(sent["desktop"]["id"]))
    assert queue.inspect(str(sent["desktop"]["id"]))["state"] == "delivered"


def test_send_job_edited_after_the_answer_blocks_only_its_delivery(store: Store) -> None:
    queue = Queue(store)
    jobs.post_command(store, "Proactive summary", to="chat", deliver=False)
    jobs.post_command(store, "Proactive summary", to="desktop", deliver=False)
    reviewed = store.read("bf.yaml")
    store.write("bf.yaml", reviewed.replace(b"notify-desktop", b"notify-elsewhere"))
    calls: list[str] = []
    while jobs.deliver_due(store, runner=fake(calls)):
        pass
    # Old work is never sent through the changed command, and the unchanged send job still delivers.
    assert calls == ["send-chat"]
    blocked = deliveries(store)["desktop"]
    assert blocked["state"] == "uncertain"
    assert str(blocked["error"]) == "send job configuration changed or is disabled; inspect bf queue before retrying"
    store.write("bf.yaml", reviewed)
    # Restoring bf.yaml alone sends nothing: the delivery waits for its explicit retry.
    assert not jobs.deliver_due(store, runner=fake(calls), key=str(blocked["id"]))
    queue.retry(str(blocked["id"]))
    assert jobs.deliver_due(store, runner=fake(calls), key=str(blocked["id"]))
    assert calls == ["send-chat", "notify-desktop"]
    assert queue.report()["deliveries"] == []


def test_collection_records_state_without_queueing_runs_or_deliveries(store: Store) -> None:
    end = datetime.now(UTC)
    collect(
        store,
        "calendar",
        start=timestamp((end - timedelta(days=1)).isoformat()),
        end=timestamp(end.isoformat()),
        runner=lambda *_: b'[{"id":"event-1","title":"Moved","text":"New time"}]',
    )
    assert len(records.files(store, "calendar")) == 1
    queue = Queue(store)
    report = queue.report()
    assert report["status"] == {
        "pending": 0,
        "failed": 0,
        "uncertain": 0,
        "wakes": 0,
        "active_runs": 0,
        "oldest_pending": None,
    }
    assert report["deliveries"] == []
    assert report["wakes"] == []
    assert report["active_runs"] == []
    assert len(queue.recent_events("job.completed", "calendar")) == 1


def test_stream_daemon_admits_messages_and_kills_process_group_on_stop(store: Store) -> None:
    source = message()
    store.write(
        "bf.yaml",
        CONFIG
        + b"  chat-in:\n    run: [python3, channels/stream.py]\n    emit: messages\n    on:\n      - daemon: true\n",
    )
    store.write(
        "channels/stream.py",
        f"""import json, os, pathlib, subprocess, sys, time
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path("helper.pid").write_text(str(helper.pid))
print({json.dumps(source.model_dump())!r}, flush=True)
time.sleep(60)
""".encode(),
    )
    queue = Queue(store)
    with watch.Dispatcher(store, {"chat-in"}):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if queue.exists() and queue.pending_messages("chat-in") and (store.root / "helper.pid").exists():
                break
            time.sleep(0.02)
        pending = queue.pending_messages("chat-in")
        assert len(pending) == 1
        assert pending[0]["text"] == source.text
        assert wait_for(store.root / "helper.pid")
    # Stopping the dispatcher kills the stream daemon's whole process group, including helpers it started.
    helper = int(store.read("helper.pid"))
    try:
        assert gone(helper)
    finally:
        kill(helper)


@pytest.mark.parametrize(
    ("emit", "max_line", "error"),
    [
        ("print('x' * 2048, flush=True)", 1024, "output line exceeds 1024 bytes"),
        ("sys.stdout.write('x' * 2048); sys.stdout.flush()", 1024, "output line exceeds 1024 bytes"),
        ("print(json.dumps({'id': 'missing-fields'}), flush=True)", 1 << 20, "invalid stream message"),
        (
            f"print(json.dumps([{{'id': str(i), 'sender': 'u', 'conversation': 'c', 'time': '2026-10-16T09:00:00Z', 'text': 'hi'}} for i in range({MAX_BATCH + 1})]), flush=True)",
            1 << 20,
            f"batch exceeds {MAX_BATCH} messages",
        ),
    ],
    ids=["long-line", "unterminated-line", "invalid-envelope", "oversized-batch"],
)
def test_stream_bounds_fail_without_admitting(
    store: Store, monkeypatch: pytest.MonkeyPatch, emit: str, max_line: int, error: str
) -> None:
    store.write(
        "bf.yaml",
        CONFIG
        + b"  chat-in:\n    run: [python3, channels/stream.py]\n    emit: messages\n    on:\n      - daemon: true\n",
    )
    store.write(
        "channels/stream.py",
        f"import json, sys, time\n{emit}\ntime.sleep(60)\n".encode(),
    )
    monkeypatch.setattr(watch, "_MAX_STREAM_LINE", max_line)
    monkeypatch.setattr(watch, "_MAX_STREAM_FAILS", 1)
    monkeypatch.setattr(jobs, "BACKOFF", 0)

    def wait_fatal(disp: watch.Dispatcher) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            disp.tick()
            time.sleep(0.02)
        pytest.fail("dispatcher did not raise StreamStoppedError")

    with watch.Dispatcher(store, {"chat-in"}) as disp:
        with pytest.raises(watch.StreamStoppedError) as caught:
            wait_fatal(disp)
        msg = str(caught.value)
    assert msg.startswith("daemon chat-in: ")
    assert re.search(error, msg), msg
    queue = Queue(store)
    assert queue.pending_messages("chat-in") == []
    assert error in str(queue.progress("health:daemon:chat-in").get("error", ""))


def test_stopping_watch_kills_a_running_harness_and_leaves_its_job_uncertain(tmp_path: Path) -> None:
    root = tmp_path / "stop"
    root.mkdir()
    store = Store(root)
    store.write(
        "bf.yaml",
        b"""version: 10
name: stop
jobs:
  out:
    send: [cat]
  review:
    prompt: prompts/review.md
    harness: [python3, harness.py]
    emit: message
    post: out
    on:
      - every: 3600
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
    proc = subprocess.Popen(  # noqa: S603 - a fake harness in an isolated brain
        [sys.executable, "-m", "bf", "watch", "--brain", str(root), "--json"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, "BF_BRAIN": str(root)},
    )
    pids: list[int] = []
    try:
        if not wait_for(root / "harness.pids", 120):
            proc.kill()
            pytest.fail(f"the scheduled harness never started: {proc.communicate(timeout=30)[1].decode()}")
        pids = [int(pid) for pid in store.read("harness.pids").split()]
        proc.terminate()
        _, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 130, stderr.decode()
        assert all(gone(pid) for pid in pids)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=30)
        kill(*pids)
    report = Queue(store).report()
    assert report["status"] == {
        "pending": 0,
        "failed": 0,
        "uncertain": 1,
        "wakes": 0,
        "active_runs": 0,
        "oldest_pending": None,
    }
    runs = cast("list[dict[str, object]]", report["active_runs"])
    assert runs[0]["job"] == "review"
    assert runs[0]["kind"] == "schedule"
    assert runs[0]["status"] == "uncertain"
    assert not records.files(store, "review")
