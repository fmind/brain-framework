"""Provider-neutral message envelopes and bounded finite or streaming channel commands."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from threading import Event

from pydantic import ValidationError

from bf.collect import Runner, arguments, executable, run, starting
from bf.config import load
from bf.history import append_log, environment
from bf.messages import Batch, Message, Outgoing, Receipt
from bf.models import Command, Error, Send, Stream, decode, encode, explain, local, timestamp
from bf.storage import Store

# Envelope keys a validation error may name; a provider's output controls any other key.
_FIELDS = frozenset({"messages", *Message.model_fields})
# Diagnostics kept from a stream's stderr; the log itself is bounded too.
_STDERR = 256 << 10
# Seconds an adapter must keep running after it starts before its connection counts as established.
ESTABLISHED = 5.0


class BatchOverflowError(Error):
    """A read window holds more messages than one batch carries; a shorter window may fit."""


def _invalid(error: Error | ValidationError) -> str:
    """Which envelope field failed and why, never its value."""
    return f": {explain(error, _FIELDS)}" if isinstance(error, ValidationError) else ""


def operation(store: Store, name: str, kind: str) -> Command:
    channel = load(store).channels.get(name)
    if channel is None:
        raise Error(f"unknown channel {name}; check channels in bf.yaml")
    command = getattr(channel, kind, None)
    if not isinstance(command, Command):
        raise Error(f"channel {name} does not support {kind}")
    return command


def read(
    store: Store, name: str, start: str, end: str, *, command: Command | None = None, runner: Runner = run
) -> list[Message]:
    """Read one complete [start,end) batch without changing subscriptions or invoking an assistant."""
    start, end = timestamp(start), timestamp(end)
    if start >= end:
        raise Error("channel read start must be earlier than end")
    command = command or operation(store, name, "read")
    raw = runner(arguments(store, command, start, end), command, store, f"channel.{name}.read", b"")
    try:
        batch = Batch.model_validate(decode(raw))
    except (Error, ValidationError) as error:
        overflow = isinstance(error, ValidationError) and any(
            item["type"] == "too_long" and item["loc"] == ("messages",) for item in error.errors()
        )
        raise (BatchOverflowError if overflow else Error)(
            f"channel {name}: read must return a complete JSON messages batch{_invalid(error)}"
        ) from error
    if len({item.id for item in batch.messages}) != len(batch.messages):
        raise Error(f"channel {name}: read returned duplicate message ids")
    if any(not start <= item.time < end for item in batch.messages):
        raise Error(f"channel {name}: read returned a message outside its requested window")
    return batch.messages


def send(store: Store, name: str, command: Send, message: Outgoing, *, runner: Runner = run) -> Receipt:
    """Send one persisted delivery id; an execution error leaves its external outcome uncertain."""
    raw = runner(
        arguments(store, command, "", ""), command, store, f"channel.{name}.send", encode(message.model_dump())
    )
    try:
        receipt = Receipt.model_validate(decode(raw))
    except (Error, ValidationError) as error:
        raise Error(f"channel {name}: send must return a JSON id and conversation receipt") from error
    if message.conversation and receipt.conversation != message.conversation:
        raise Error(f"channel {name}: send receipt names a different conversation; inspect delivery before retrying")
    return receipt


def stream(
    store: Store,
    name: str,
    command: Stream,
    resume: dict[str, str],
    accept: Callable[[Message], None],
    stopped: Event,
    *,
    connected: Callable[[], None] | None = None,
) -> None:
    """Pump bounded JSON Lines; acknowledge only after accept has durably recorded all recipients.

    The adapter receives one resume frame, then ack frames. Idle streams have no total time limit. A broken or
    blocked control pipe restarts the adapter instead of growing an unbounded buffer or delaying other channels.
    `connected` runs once, when BF wrote the resume frame and the adapter still runs ESTABLISHED seconds after starting.
    Every run ends with one timed log entry naming its outcome and, when the adapter exited on its own, its status.
    """
    env = {**environment(), "BF_BRAIN": str(store.root)}
    argv = arguments(store, command, resume.get("since", ""), "")
    argv[0] = executable(store, argv[0], env)
    child: subprocess.Popen[bytes] | None = None
    pending = bytearray(encode({"type": "resume", **resume}))
    buffered, errors = bytearray(), bytearray()
    blocked = started = time.monotonic()
    outcome = "failed"
    try:
        with starting():
            if stopped.is_set():
                return
            child = subprocess.Popen(  # noqa: S603 - owner-configured argv, never a shell
                argv,
                cwd=store.root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        if child.stdin is None or child.stdout is None or child.stderr is None:
            raise Error("stream pipes were not created")
        with selectors.DefaultSelector() as selector:
            for pipe in (child.stdin, child.stdout, child.stderr):
                os.set_blocking(pipe.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            selector.register(child.stderr, selectors.EVENT_READ)
            selector.register(child.stdin, selectors.EVENT_WRITE)
            writing = resuming = True
            while not stopped.is_set():
                if pending and time.monotonic() - blocked > command.timeout:
                    raise Error(f"channel {name}: stream stopped reading acknowledgements")
                if child.poll() is not None:
                    raise Error(f"channel {name}: stream exited; reconnecting from durable progress")
                if connected is not None and not resuming and time.monotonic() - started >= ESTABLISHED:
                    # A crash-looping adapter exits before this, so its failure stays visible.
                    connected()
                    connected = None
                if pending and not writing:
                    selector.register(child.stdin, selectors.EVENT_WRITE)
                    writing = True
                for key, events in selector.select(0.05):
                    if events & selectors.EVENT_WRITE:
                        try:
                            size = os.write(key.fd, pending[:65_536])
                        except BrokenPipeError as error:
                            raise Error(f"channel {name}: stream closed its input") from error
                        del pending[:size]
                        blocked = time.monotonic()
                        if not pending:
                            selector.unregister(child.stdin)
                            writing = resuming = False
                        continue
                    data = os.read(key.fd, 65_536)
                    if not data:
                        selector.unregister(key.fileobj)
                        if key.fileobj is child.stdout:
                            raise Error(f"channel {name}: stream closed its output")
                        continue
                    if key.fileobj is child.stderr:
                        errors.extend(data)
                        del errors[:-_STDERR]
                        continue
                    buffered.extend(data)
                    while b"\n" in buffered:
                        if stopped.is_set():
                            outcome = "stopped"
                            return
                        if pending and time.monotonic() - blocked > command.timeout:
                            raise Error(f"channel {name}: stream stopped reading acknowledgements")
                        boundary = buffered.index(b"\n")
                        if boundary > command.max_bytes:
                            raise Error(f"channel {name}: stream frame exceeded max_bytes")
                        raw = bytes(buffered[:boundary])
                        del buffered[: boundary + 1]
                        try:
                            message = Message.model_validate(decode(raw))
                        except (Error, ValidationError) as error:
                            raise Error(f"channel {name}: invalid stream message envelope{_invalid(error)}") from error
                        accept(message)
                        if not pending:
                            blocked = time.monotonic()
                        pending.extend(encode({"type": "ack", "id": message.id, "cursor": message.cursor}))
                        if len(pending) > 65_536:
                            raise Error(f"channel {name}: acknowledgement buffer is full; reconnecting safely")
                    if len(buffered) > command.max_bytes:
                        raise Error(f"channel {name}: stream frame exceeded max_bytes")
            outcome = "stopped"
    except Error as error:
        outcome = str(error)
        raise
    finally:
        if child is not None:
            if outcome != "stopped":
                # An adapter that closed its output is usually exiting: wait briefly so its status is not lost.
                with suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=0.2)
            code = child.poll()
            if child.stderr is not None:
                # Keep what the adapter wrote before it stopped; a bounded read never waits for descendants.
                with suppress(BlockingIOError, OSError):
                    for _ in range(_STDERR // 65_536 + 1):
                        if not (chunk := os.read(child.stderr.fileno(), 65_536)):
                            break
                        errors.extend(chunk)
                del errors[:-_STDERR]
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None:
                    pipe.close()
            if code is not None:
                outcome += f"; adapter {_ended(code)}"
            with suppress(Error, OSError):
                append_log(
                    store,
                    f"channel.{name}.stream",
                    f"{local(timestamp(datetime.now(UTC).isoformat()))} {outcome} after {time.monotonic() - started:.1f}s",
                    bytes(errors),
                )


def _ended(code: int) -> str:
    if code >= 0:
        return f"exited with status {code}"
    try:
        return f"was killed by {signal.Signals(-code).name}"
    except ValueError:
        # Real-time signals between SIGRTMIN and SIGRTMAX have no name.
        return f"was killed by signal {-code}"
