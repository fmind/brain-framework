"""Durable local assistant work, independent of the disposable search cache and run history."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Collection, Generator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from functools import cache
from threading import Lock
from typing import Annotated, cast

from pydantic import Field, JsonValue, ValidationError, field_validator

from bf.config import load
from bf.messages import Message, Outgoing, Receipt
from bf.models import (
    Assistant,
    Error,
    Invocation,
    Model,
    Name,
    ProposedAction,
    Record,
    Send,
    decode,
    digest,
    encode,
    retried,
    timestamp,
)
from bf.storage import QUEUE, BusyError, Store, collecting, lock_file, runtime_store

# The queue format this release reads and writes; any other is preserved, never adapted.
VERSION = 2
MAX_PENDING = 10_000
MAX_DATABASE = 256 << 20
BODY_BUDGET = 8 << 20
# Identity tombstones outlive the longest provider redelivery window, Pub/Sub's 31 days, then go: they must
# never exhaust the queue.
RETENTION = timedelta(days=35)
# A failing idempotent delivery retries after 2, 4, ... up to 300 seconds; past this many attempts it waits for
# an explicit retry or cancellation instead of starting its adapter forever.
MAX_ATTEMPTS = 20
MAX_WAKES = 16
_CREATE = Lock()


def now() -> str:
    return timestamp(datetime.now(UTC).isoformat())


def _limit() -> int:
    """Stored bytes plus the answer space of claimed jobs never exceed this; admission stops at half of it, so
    admitted work can always claim the answer space one job needs once running work finishes."""
    return MAX_DATABASE * 3 // 4


def answer_limit() -> int:
    """The most answer space one job may reserve: what admission leaves free once the queue is full."""
    return _limit() - _limit() // 2


class QueueFullError(Error):
    """Admission or a job's start waits for unfinished work to free space; nothing changed."""


class Origin(Model):
    channel: Name
    message: Message


class Route(Model):
    channel: Name
    command: Send
    conversation: str = ""
    reply_to: str = ""


class Work(Model):
    id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    assistant: Name
    kind: Invocation
    slot: str = ""
    start: str
    end: str
    settings: Assistant
    template: str
    text: str = ""
    # Why a wake run runs: the reason the run that asked for it gave.
    reason: str = ""
    origin: Origin | None = None
    routes: list[Route] = Field(default_factory=list)

    _time = field_validator("start", "end")(timestamp)


class Requested(Model):
    """A pending wake: when it is due and why."""

    due: str
    reason: str = ""


class Prepared(Model):
    record: Record
    wake: Requested | None = None
    actions: list[ProposedAction] = Field(default_factory=list)


def wake_slot(work: Work) -> str:
    """The per-slot wake key suffix for `work`, keeping conversation and schedule wakes independent."""
    return work.slot if work.slot.startswith(("input:", "schedule:")) else "wake"


def job_retry_budget(
    work: Work,
    *,
    gate: bool = False,
    tempfail: bool = False,
) -> tuple[int, bool]:
    """Return `(retry_budget, is_idempotent)` for `work` given how the attempt failed."""
    spec = (
        work.settings.schedules.get(work.slot.removeprefix("schedule:")) if work.slot.startswith("schedule:") else None
    )
    configured = spec.retries if (spec is not None and spec.retries is not None) else work.settings.retries
    is_idempotent = retried(work.settings.idempotent, work.kind)
    default_budget = configured if configured is not None else (3 if is_idempotent else 0)
    if gate or tempfail:
        return (0 if configured == 0 else max(default_budget, 3)), is_idempotent
    return default_budget, is_idempotent


_SCHEMA = """
CREATE TABLE events (id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL,
                     digest TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE jobs (id TEXT PRIMARY KEY, assistant TEXT NOT NULL, kind TEXT NOT NULL, slot TEXT NOT NULL,
                   state TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL,
                   raw BLOB, prepared TEXT, error TEXT NOT NULL DEFAULT '', slots INTEGER NOT NULL);
CREATE INDEX ready_jobs ON jobs(state,kind,created);
CREATE INDEX job_slots ON jobs(assistant,slot,state);
CREATE TABLE deliveries (id TEXT PRIMARY KEY, job TEXT REFERENCES jobs(id), channel TEXT NOT NULL,
                         state TEXT NOT NULL, payload TEXT NOT NULL, command TEXT NOT NULL, digest TEXT NOT NULL,
                         receipt TEXT, receipt_id TEXT, error TEXT NOT NULL DEFAULT '',
                         attempts INTEGER NOT NULL DEFAULT 0, due TEXT NOT NULL);
CREATE INDEX ready_deliveries ON deliveries(state,due);
CREATE INDEX job_deliveries ON deliveries(job);
CREATE INDEX delivery_receipts ON deliveries(channel,receipt_id);
CREATE TABLE checkpoints (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE messages (id TEXT PRIMARY KEY, channel TEXT NOT NULL, conversation TEXT NOT NULL,
                       role TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL);
CREATE INDEX conversation_messages ON messages(channel,conversation,at);
"""
_OBJECTS = "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"


@cache
def _expected() -> frozenset[tuple[str, str, str]]:
    """The one current schema, exactly as SQLite records it: any other database is preserved, never adapted."""
    db = sqlite3.connect(":memory:")
    try:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        return frozenset(db.execute(_OBJECTS).fetchall())
    finally:
        db.close()


def check_version(version: int) -> None:
    """Refuse a queue another release wrote: an older one needs that release's upgrade steps, never adaptation."""
    if 0 < version < VERSION:
        raise Error("assistant queue predates this release; preserve it and follow the upgrade steps in the changelog")
    if version != VERSION:
        raise Error("unsupported assistant queue version; preserve it and use its matching BF release")


def check_schema(connection: sqlite3.Connection) -> None:
    """Refuse a queue whose tables differ from this release's, such as one a pre-release build created."""
    if frozenset(tuple(row) for row in connection.execute(_OBJECTS)) != _expected():
        raise Error("unexpected assistant queue schema; preserve it and repair it")


def _identity(channel: str, payload: str, command: str) -> str:
    """What one delivery id stands for; it outlives body compaction, so an identical resend stays recognizable."""
    return digest(encode([channel, payload, command]))


def answer_space(size: int, payload: int, routes: int) -> int:
    """Bytes a started job can still write: `size` of raw output, then its record and one delivery per route, each
    JSON-escaped up to sixfold (a control character becomes \\u00XX), plus identity fields per copy."""
    return size * (1 + 6 * (1 + routes)) + (payload + 65_536) * (1 + routes)


def retry_policy(
    idempotent: bool, owned: bool, attempts: int, reason: str, *, not_started: bool = False
) -> tuple[str, str, str]:
    """The state, error and due time of a delivery after a failed or interrupted attempt.

    Idempotent deliveries (both assistant replies and standalone sends) retry with bounded backoff, as do assistant
    deliveries whose send command never started; a non-idempotent standalone send that never started waits as failed,
    and an unknown non-idempotent outcome waits as uncertain.
    """
    if not idempotent:
        if not not_started:
            return "uncertain", reason, ""
        if not owned:
            return "failed", f"{reason}; retry it with bf queue ID --retry", ""
    if attempts >= MAX_ATTEMPTS:
        return "failed", f"{reason}; stopped retrying after {attempts} attempts; retry or cancel it with bf queue", ""
    delay = min(300, 2 ** min(attempts, 9))
    return "pending", reason, timestamp((datetime.now(UTC) + timedelta(seconds=delay)).isoformat())


def forget(store: Store, name: str) -> None:
    """Remove the lock file of a finished job or delivery while holding that lock.

    A finished job or delivery never runs again: a process that still locks the removed file finds nothing to do.
    """
    state, leaf = lock_file(store, f"collect-{name}.lock")
    with suppress(Error, OSError):
        state.delete(leaf, durable=False)


class Queue:
    """Short SQLite transactions. Never hold one while running a process or acquiring the brain writer lock."""

    def __init__(self, store: Store) -> None:
        self.brain = store
        self.store = runtime_store(store)

    def exists(self) -> bool:
        try:
            self.store.fingerprint(QUEUE)
        except FileNotFoundError:
            return False
        return True

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        """One immediate transaction that checks, or rebinds, the brain path and name the queue serves."""
        try:
            # Never open/close a live SQLite inode ourselves: POSIX close drops this process's locks,
            # including another thread's transaction. Serialize creation before SQLite can see the file.
            with _CREATE, self.store.parent(QUEUE) as (parent, name):
                try:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    try:
                        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    except FileExistsError:
                        pass  # A different process created it; closing its own descriptor cannot drop our locks.
                    else:
                        os.close(fd)
                        os.fsync(parent)
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise Error("assistant queue must be a private regular file owned by you")
            for suffix in ("-wal", "-shm", "-journal"):
                with suppress(FileNotFoundError):
                    self.store.fingerprint(QUEUE + suffix)
            connection = sqlite3.connect(self.store.root / QUEUE, timeout=10, isolation_level=None)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                # In the rollback-journal mode, only EXTRA also syncs the directory once deleting the journal has
                # committed: each commit, such as the sending mark before an external send, is durable when it
                # returns, not after a later write. That mode keeps the queue one file to back up.
                connection.execute("PRAGMA synchronous=EXTRA")
                connection.execute("BEGIN IMMEDIATE")
                page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                connection.execute(f"PRAGMA max_page_count={MAX_DATABASE // page_size}").fetchall()
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        raise Error("unknown assistant queue format; preserve the queue and repair it")
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(f"PRAGMA user_version={VERSION}")
                else:
                    check_version(version)
                check_schema(connection)
                self._bind(connection)
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        except sqlite3.DatabaseError as error:
            raise Error(
                "assistant queue is unavailable; preserve its files and check permissions, disk space or corruption"
            ) from error

    def _bind(self, db: sqlite3.Connection) -> None:
        """Follow a drained brain to its new path or name; refuse while work admitted under the old one waits.

        The queue is keyed by physical identity: the binding keeps a reused identity, or another path to the
        same brain, from running work it did not admit.
        """
        try:
            name: str | None = load(self.brain).name
        except Error:
            # A bf.yaml mid-edit must not keep running work from saving its answer: the path still binds the queue.
            name = None
        binding: dict[str, JsonValue] = {"path": str(self.brain.root), "name": name}
        row = db.execute("SELECT value FROM checkpoints WHERE key='binding'").fetchone()
        previous = decode(row[0]) if row is not None else None
        if isinstance(previous, dict) and (
            previous == binding or (name is None and previous.get("path") == binding["path"])
        ):
            return
        if previous is not None and self.unresolved(db):
            raise Error(
                "assistant queue holds unresolved work admitted under this brain's previous path or name; restore "
                "them, resolve the work with bf queue, then move or rename the brain again"
            )
        if name is not None:
            self.checkpoint(db, "binding", binding)

    @staticmethod
    def unresolved(db: sqlite3.Connection) -> bool:
        """Whether any job or delivery still waits."""
        return (
            db.execute(
                "SELECT 1 FROM jobs WHERE state NOT IN ('complete','cancelled') UNION ALL "
                "SELECT 1 FROM deliveries WHERE state NOT IN ('delivered','cancelled') LIMIT 1"
            ).fetchone()
            is not None
        )

    @staticmethod
    def checkpoint(db: sqlite3.Connection, key: str, value: dict[str, JsonValue]) -> None:
        db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?)", (key, encode(value).decode()))

    def progress(self, key: str) -> dict[str, JsonValue]:
        with self.connect() as db:
            row = db.execute("SELECT value FROM checkpoints WHERE key=?", (key,)).fetchone()
        if row is None:
            return {}
        value = decode(row[0])
        if not isinstance(value, dict):
            raise Error("invalid assistant checkpoint; preserve the queue and repair it")
        return cast(dict[str, JsonValue], value)

    def set_progress(self, key: str, value: dict[str, JsonValue]) -> None:
        with self.connect() as db:
            self.checkpoint(db, key, value)

    @staticmethod
    def event(db: sqlite3.Connection, key: str, kind: str, source: str, data: dict[str, JsonValue], at: str) -> None:
        encoded = encode(data).decode()
        checksum = digest(encoded.encode())
        previous = db.execute("SELECT digest FROM events WHERE id=?", (key,)).fetchone()
        if previous is not None and previous[0] != checksum:
            raise Error(
                "a channel reused an event id with different content; preserve the queue and repair the adapter"
            )
        db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)", (key, kind, source, at, checksum, encoded))

    @staticmethod
    def used(db: sqlite3.Connection) -> int:
        """Occupied database bytes plus the answer space that started and answered jobs may still write."""
        pages = db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]
        used = pages * db.execute("PRAGMA page_size").fetchone()[0]
        for row in db.execute(
            "SELECT state,length(CAST(payload AS BLOB)),coalesce(length(raw),0),"
            "json_extract(payload,'$.settings.max_bytes'),json_array_length(payload,'$.routes') "
            "FROM jobs WHERE state IN ('running','answered')"
        ):
            size = row[3] if row[0] == "running" else row[2]
            used += answer_space(int(size), int(row[1]), int(row[4]))
        return used

    @staticmethod
    def reserve(db: sqlite3.Connection, slots: int, size: int, *, limit: int = 0, reason: str = "") -> None:
        """Admit `slots` obligations and `size` bytes, or raise QueueFullError; admission stops at half the limit."""
        if slots:
            # An unfinished job counts its future deliveries as well as its own slot. Once complete, only actual
            # unresolved deliveries count; never double-count already prepared fan-out.
            count = db.execute(
                "SELECT coalesce(sum(slots),0) FROM jobs WHERE state NOT IN ('complete','cancelled')"
            ).fetchone()[0]
            count += db.execute(
                "SELECT count(*) FROM deliveries WHERE state NOT IN ('delivered','cancelled') "
                "AND (job IS NULL OR job IN (SELECT id FROM jobs WHERE state IN ('complete','cancelled')))"
            ).fetchone()[0]
            if count + slots > MAX_PENDING:
                raise QueueFullError(
                    "assistant queue has no unfinished-work slots; resolve its backlog before accepting more input"
                )
        ceiling = limit or _limit() // 2
        if Queue.used(db) + size > ceiling:
            # Expired tombstones and bodies beyond their budgets may free enough: compact only when needed.
            Queue.trim(db)
            if Queue.used(db) + size > ceiling:
                raise QueueFullError(
                    reason or "assistant queue is full; finish, retry or cancel unfinished work before accepting more"
                )

    @staticmethod
    def _enqueue_failure_notice(db: sqlite3.Connection, work: Work, state: str, *, brain: Store | None = None) -> None:
        """Enqueue one concise deterministic failure notice on the thread when an input job fails terminally."""
        if work.kind != "input" or work.origin is None or state not in {"failed", "uncertain"}:
            return
        route: Route | None = work.routes[0] if work.routes else None
        if route is None and brain is not None:
            with suppress(Error, OSError):
                config = load(brain)
                target = work.settings.post or work.origin.channel
                channel = config.channels.get(target)
                if channel is not None and channel.send is not None:
                    route = Route(
                        channel=target,
                        command=channel.send,
                        conversation=work.origin.message.conversation,
                        reply_to=work.origin.message.id if target == work.origin.channel else "",
                    )
        if route is None or not route.conversation:
            return
        if db.execute(
            "SELECT 1 FROM deliveries WHERE job=? AND id LIKE 'notice:%' AND state NOT IN ('delivered','cancelled')",
            (work.id,),
        ).fetchone():
            return
        seq = int(
            db.execute("SELECT count(*) FROM deliveries WHERE job=? AND id LIKE 'notice:%'", (work.id,)).fetchone()[0]
        )
        notice_id = f"notice:{digest(f'{work.id}:{seq}'.encode())[:25]}"
        message = Outgoing(
            id=notice_id,
            text=f"[bf] {work.assistant} did not finish ({state}); run bf queue to inspect.",
            conversation=route.conversation,
            reply_to=route.reply_to,
        )
        payload, encoded = message.model_dump_json(), route.command.model_dump_json()
        with suppress(QueueFullError):
            Queue.reserve(db, 0 if work.routes else 1, 2 * (len(payload.encode()) + len(encoded.encode())))
            db.execute(
                "INSERT OR IGNORE INTO deliveries(id,job,channel,state,payload,command,digest,due) "
                "VALUES (?,?,?,'pending',?,?,?,?)",
                (
                    notice_id,
                    work.id,
                    route.channel,
                    payload,
                    encoded,
                    _identity(route.channel, payload, encoded),
                    now(),
                ),
            )

    @staticmethod
    def _supersede_failed_schedule(db: sqlite3.Connection, assistant: str, slot: str, at: str) -> None:
        superseded = [
            str(row[0])
            for row in db.execute(
                "SELECT id FROM jobs WHERE assistant=? AND slot=? AND state='failed'",
                (assistant, slot),
            )
        ]
        for old_id in superseded:
            db.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=?", (at, old_id))
            db.execute("DELETE FROM checkpoints WHERE key IN (?,?)", (f"retry:{old_id}", f"gate:{old_id}"))
            db.execute(
                "DELETE FROM checkpoints WHERE key LIKE ? AND json_extract(value,'$.job')=?",
                (f"wake:{assistant}:%", old_id),
            )
            db.execute("UPDATE deliveries SET state='cancelled',error='' WHERE job=? AND state='pending'", (old_id,))

    @staticmethod
    def put(db: sqlite3.Connection, work: Work, *, error: str = "", brain: Store | None = None) -> None:
        """Store one job; its answer space is reserved only when a worker starts it. An `error` stores it failed."""
        if db.execute("SELECT 1 FROM jobs WHERE id=?", (work.id,)).fetchone():
            return
        at = now()
        if not error and work.slot.startswith("schedule:"):
            # Periodic schedules supersede earlier terminal `failed` (never `uncertain`) runs on the next due window.
            Queue._supersede_failed_schedule(db, work.assistant, work.slot, at)
        payload, slots = work.model_dump_json(), 1 + len(work.routes)
        Queue.reserve(db, slots, 2 * len(payload.encode()))
        db.execute(
            "INSERT INTO jobs(id,assistant,kind,slot,state,created,updated,payload,error,slots) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                work.id,
                work.assistant,
                work.kind,
                work.slot,
                "failed" if error else "pending",
                at,
                at,
                payload,
                error,
                slots,
            ),
        )
        if error:
            Queue._enqueue_failure_notice(db, work, "failed", brain=brain)

    def advance_gated_schedule(
        self,
        assistant: str,
        slot: str,
        end: str,
        *,
        when_key: str = "",
        when_cursor: str = "",
    ) -> None:
        """Supersede any prior failed run on `(assistant, slot)` and advance its schedule/when checkpoints atomically."""
        with self.connect() as db:
            self._supersede_failed_schedule(db, assistant, slot, now())
            self.checkpoint(db, f"schedule:{assistant}:{slot}", {"end": end, "success": end})
            if when_key and when_cursor:
                self.checkpoint(db, when_key, {"cursor": when_cursor})

    def enqueue(
        self,
        work: Work,
        *,
        events: list[JsonValue] | None = None,
        when_key: str = "",
        when_cursor: str = "",
    ) -> None:
        with self.connect() as db:
            self.put(db, work, brain=self.brain)
            gate_data: dict[str, JsonValue] = {}
            if events is not None:
                gate_data["events"] = events
            if when_key and when_cursor:
                gate_data["when_key"] = when_key
                gate_data["when_cursor"] = when_cursor
            if gate_data:
                self.checkpoint(db, f"gate:{work.id}", gate_data)

    def wakes(self, assistant: str) -> list[tuple[str, dict[str, JsonValue]]]:
        """All saved wake checkpoints `(slot, value)` for `assistant`, in key order."""
        prefix = f"wake:{assistant}:"
        with self.connect() as db:
            rows = db.execute(
                "SELECT key,value FROM checkpoints WHERE key LIKE ? ORDER BY key", (f"{prefix}%",)
            ).fetchall()
        result: list[tuple[str, dict[str, JsonValue]]] = []
        for key, raw in rows:
            value = decode(raw)
            if isinstance(value, dict):
                result.append((str(key).removeprefix(prefix), cast(dict[str, JsonValue], value)))
        return result

    def enqueue_wake(self, work: Work, wake: dict[str, JsonValue]) -> bool:
        """Queue a wake run and mark its wake taken in one transaction, so it starts once.

        Returns False, queueing nothing, when the wake changed since it was read: a run that finished meanwhile
        replaced or took it, and the newer request must keep its own due time.
        """
        key = f"wake:{work.assistant}:{wake_slot(work)}"
        with self.connect() as db:
            row = db.execute("SELECT value FROM checkpoints WHERE key=?", (key,)).fetchone()
            if row is None or decode(row[0]) != wake or wake.get("job"):
                return False
            self.put(db, work, brain=self.brain)
            self.checkpoint(db, key, {**wake, "job": work.id})
        return True

    def save_gate(self, key: str, events: list[JsonValue]) -> bool:
        """Keep a pending job's gate events for its retries; False when the job was cancelled meanwhile."""
        with self.connect() as db:
            existing_row = db.execute("SELECT value FROM checkpoints WHERE key=?", (f"gate:{key}",)).fetchone()
            existing = (
                existing_val
                if existing_row is not None and isinstance(existing_val := decode(existing_row[0]), dict)
                else {}
            )
            merged: dict[str, JsonValue] = {**existing, "events": events}
            cursor = db.execute(
                "INSERT OR REPLACE INTO checkpoints SELECT ?,? WHERE EXISTS "
                "(SELECT 1 FROM jobs WHERE id=? AND state='pending')",
                (f"gate:{key}", encode(merged).decode(), key),
            )
            return cursor.rowcount > 0

    def admit(
        self,
        name: str,
        messages: list[Message],
        jobs: list[tuple[Work, str]],
        *,
        checkpoint: tuple[str, dict[str, JsonValue]] | None = None,
    ) -> None:
        """Persist messages, every consumer obligation and transport progress before acknowledging.

        Each job comes with the error that stores it failed, such as a message too large for its prompt, or "".
        """
        with self.connect() as db:
            accepted: set[str] = set()
            for message in messages:
                key = f"{name}:{message.id}"
                # A delivery receipt suppresses echoes even when an adapter omits own=true, and after the
                # conversation cache trimmed the sent text.
                echoed = (
                    db.execute("SELECT 1 FROM messages WHERE id=? AND role='assistant'", (key,)).fetchone()
                    or (
                        db.execute("SELECT 1 FROM deliveries WHERE channel=? AND receipt_id=?", (name, message.id))
                    ).fetchone()
                )
                if message.own or echoed:
                    continue
                payload = message.model_dump(mode="json", exclude={"cursor"})
                previous = db.execute("SELECT digest FROM events WHERE id=?", (f"input:{key}",)).fetchone()
                if previous is not None and previous[0] != digest(encode(payload)):
                    # A provider edit reuses the message's id: keep the admitted version, so the edit neither
                    # starts another job nor stops the messages that follow it.
                    continue
                accepted.add(message.id)
                if previous is None:
                    self.reserve(db, 0, 2 * (len(encode(payload)) + len(message.text.encode())))
                    # Retention counts from admission: an old backlog message keeps its marker like a new one.
                    self.event(db, f"input:{key}", "channel.received", name, payload, now())
                    db.execute(
                        "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?)",
                        (key, name, message.conversation, "user", message.time, message.text),
                    )
            for work, error in jobs:
                if work.origin is not None and work.origin.message.id in accepted:
                    self.put(db, work, error=error, brain=self.brain)
            if checkpoint:
                self.checkpoint(db, *checkpoint)
            self.trim(db)

    def get(self, key: str) -> dict[str, object]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
        if row is None:
            raise Error("unknown queued job; use bf queue to list jobs")
        return dict(row)

    def inspect(self, key: str) -> dict[str, object]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (key,)).fetchone()
        return dict(row) if row is not None else self.get(key)

    @staticmethod
    def parse(row: dict[str, object]) -> Work:
        if row["payload"] == "{}" and row["state"] in {"complete", "cancelled"}:
            raise Error("completed job body expired; read its assistant records instead")
        try:
            return Work.model_validate_json(str(row["payload"]))
        except ValidationError as error:
            raise Error("invalid queued assistant job; preserve the queue and repair it") from error

    def work(self, key: str) -> Work:
        return self.parse(self.get(key))

    def active(self, assistant: str, slot: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM jobs WHERE assistant=? AND slot=? AND state NOT IN ('complete','cancelled') LIMIT 1",
                    (assistant, slot),
                ).fetchone()
                is not None
            )

    def slot_state(self, assistant: str, slot: str) -> tuple[bool, str]:
        """Return `(blocking, failed_at)` for a schedule or wake slot.

        On `schedule:<name>`, terminal `failed` jobs do not block the next scheduled window (`failed_at` records when
        the latest one failed so the schedule waits until the next due window); `uncertain` jobs always block.
        """
        if not slot.startswith("schedule:"):
            return self.active(assistant, slot), ""
        with self.connect() as db:
            blocking = (
                db.execute(
                    "SELECT 1 FROM jobs WHERE assistant=? AND slot=? "
                    "AND state NOT IN ('complete','cancelled','failed') LIMIT 1",
                    (assistant, slot),
                ).fetchone()
                is not None
            )
            failed_row = db.execute(
                "SELECT coalesce(max(updated),'') FROM jobs WHERE assistant=? AND slot=? AND state='failed'",
                (assistant, slot),
            ).fetchone()
        return blocking, str(failed_row[0] if failed_row else "")

    def scheduling_state(
        self, names: set[str]
    ) -> tuple[
        dict[str, list[tuple[str, dict[str, JsonValue]]]],
        dict[str, dict[str, JsonValue]],
        set[tuple[str, str]],
        set[tuple[str, str]],
        dict[tuple[str, str], str],
    ]:
        """Read wakes, schedule checkpoints and active/failed slot states for `names` in one transaction."""
        wakes: dict[str, list[tuple[str, dict[str, JsonValue]]]] = {}
        schedules: dict[str, dict[str, JsonValue]] = {}
        active_slots: set[tuple[str, str]] = set()
        blocking_schedules: set[tuple[str, str]] = set()
        failed_schedules: dict[tuple[str, str], str] = {}
        if not names:
            return wakes, schedules, active_slots, blocking_schedules, failed_schedules
        with self.connect() as db:
            for key, raw in db.execute(
                "SELECT key,value FROM checkpoints WHERE key LIKE 'wake:%' OR key LIKE 'schedule:%' ORDER BY key"
            ):
                key_str = str(key)
                if key_str.startswith("wake:"):
                    assistant, sep, slot_name = key_str.removeprefix("wake:").partition(":")
                    if sep and assistant in names and isinstance(val := decode(raw), dict):
                        wakes.setdefault(assistant, []).append((slot_name, cast(dict[str, JsonValue], val)))
                else:
                    assistant = key_str.removeprefix("schedule:").partition(":")[0]
                    if assistant not in names:
                        continue
                    val = decode(raw)
                    if not isinstance(val, dict):
                        raise Error("invalid assistant checkpoint; preserve the queue and repair it")
                    schedules[key_str] = cast(dict[str, JsonValue], val)
            for assistant, slot, state, updated in db.execute(
                "SELECT assistant,slot,state,updated FROM jobs WHERE state NOT IN ('complete','cancelled')"
            ):
                if assistant not in names:
                    continue
                pair = (str(assistant), str(slot))
                active_slots.add(pair)
                if str(slot).startswith("schedule:"):
                    if state != "failed":
                        blocking_schedules.add(pair)
                    else:
                        prev = failed_schedules.get(pair, "")
                        if str(updated) > prev:
                            failed_schedules[pair] = str(updated)
        return wakes, schedules, active_slots, blocking_schedules, failed_schedules

    def next(self, reactive: bool, names: set[str], *, excluding: Collection[str] = ()) -> str | None:
        """The oldest runnable job of `names` whose earlier work in its slot is resolved, skipping `excluding`.

        A delivery that stopped retrying (`failed`) no longer holds its conversation; an uncertain one does.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,assistant FROM jobs WHERE state IN ('pending','answered','prepared') "
                "AND (kind IN ('input','manual'))=? "
                "AND (state!='pending' OR NOT EXISTS (SELECT 1 FROM checkpoints WHERE key='retry:'||jobs.id AND json_extract(value,'$.due')>?)) "
                "AND NOT EXISTS (SELECT 1 FROM jobs earlier WHERE earlier.assistant=jobs.assistant "
                "AND earlier.slot=jobs.slot AND earlier.id!=jobs.id AND (earlier.state NOT IN ('complete','cancelled') "
                "OR EXISTS (SELECT 1 FROM deliveries WHERE job=earlier.id AND state NOT IN ('delivered','cancelled','failed'))) "
                "AND (earlier.created<jobs.created OR (earlier.created=jobs.created AND earlier.id<jobs.id))) "
                "ORDER BY created,id LIMIT 10000",
                (reactive, now()),
            ).fetchall()
        return next((row[0] for row in rows if row[1] in names and row[0] not in excluding), None)

    def claim(self, key: str) -> bool:
        """Start a pending job, reserving its answer space first; False when it is no longer pending, as when cancelled
        meanwhile. A pending job's body never changes, so the worker runs the job it read."""
        with self.connect() as db:
            row = db.execute("SELECT state,payload FROM jobs WHERE id=?", (key,)).fetchone()
            if row is None or row[0] != "pending":
                return False
            work = self.parse(dict(row))
            space = answer_space(work.settings.max_bytes, len(str(row[1]).encode()), len(work.routes))
            if space > answer_limit():
                raise Error(
                    f"assistant {work.assistant} needs {space >> 20} MiB of queue space per answer, more than one "
                    "job may reserve; lower its max_bytes"
                )
            self.reserve(
                db,
                0,
                space,
                limit=_limit(),
                reason="assistant queue lacks answer space for this job; it waits until running work finishes",
            )
            db.execute("UPDATE jobs SET state='running',updated=? WHERE id=? AND state='pending'", (now(), key))
            return True

    def answer(self, key: str, raw: bytes) -> None:
        with self.connect() as db:
            if not db.execute(
                "UPDATE jobs SET state='answered',raw=?,updated=? WHERE id=? AND state='running'", (raw, now(), key)
            ).rowcount:
                raise Error("assistant attempt no longer owns its job")

    def prepare(
        self,
        work: Work,
        record: Record,
        replies: list[Outgoing],
        wake: Requested | None = None,
        *,
        actions: Collection[ProposedAction] = (),
    ) -> None:
        prepared = Prepared(record=record, wake=wake, actions=list(actions))
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state='prepared',prepared=?,updated=? WHERE id=?",
                (prepared.model_dump_json(), now(), work.id),
            )
            for route, message in zip(work.routes if replies else [], replies, strict=True):
                payload, command = message.model_dump_json(), route.command.model_dump_json()
                db.execute(
                    "INSERT OR IGNORE INTO deliveries(id,job,channel,state,payload,command,digest,due) "
                    "VALUES (?,?,?,'pending',?,?,?,?)",
                    (
                        message.id,
                        work.id,
                        route.channel,
                        payload,
                        command,
                        _identity(route.channel, payload, command),
                        now(),
                    ),
                )

    def finish(self, work: Work, wake: Requested | None = None) -> None:
        """Complete a job; a wake it asked for replaces the slot's pending wake (capped at MAX_WAKES per assistant)."""
        with self.connect() as db:
            # A job cancelled while its gate ran stays cancelled: the cancellation already advanced its schedule.
            completed = db.execute(
                "UPDATE jobs SET state='complete',updated=?,error='' WHERE id=? AND state<>'cancelled'",
                (now(), work.id),
            )
            gate_row = db.execute("SELECT value FROM checkpoints WHERE key=?", (f"gate:{work.id}",)).fetchone()
            db.execute("DELETE FROM checkpoints WHERE key IN (?,?)", (f"retry:{work.id}", f"gate:{work.id}"))
            if not completed.rowcount:
                return
            if gate_row is not None and isinstance(gate_val := decode(gate_row[0]), dict):
                when_key, when_cursor = gate_val.get("when_key"), gate_val.get("when_cursor")
                if isinstance(when_key, str) and when_key and isinstance(when_cursor, str) and when_cursor:
                    existing_row = db.execute("SELECT value FROM checkpoints WHERE key=?", (when_key,)).fetchone()
                    existing_cursor = (
                        str(existing_val.get("cursor") or "")
                        if existing_row is not None and isinstance(existing_val := decode(existing_row[0]), dict)
                        else ""
                    )
                    if when_cursor > existing_cursor:
                        self.checkpoint(db, when_key, {"cursor": when_cursor})
            if work.kind == "schedule":
                self.checkpoint(db, f"schedule:{work.assistant}:{work.slot}", {"end": work.end, "success": now()})
            if work.kind == "wake":
                # Only the wake this job took: a newer one asked for meanwhile stays pending.
                db.execute(
                    "DELETE FROM checkpoints WHERE key=? AND json_extract(value,'$.job')=?",
                    (f"wake:{work.assistant}:{wake_slot(work)}", work.id),
                )
            if wake is not None:
                slot_name = wake_slot(work)
                wake_key = f"wake:{work.assistant}:{slot_name}"
                active_others: list[tuple[str, str]] = []
                for existing_key, existing_raw in db.execute(
                    "SELECT key,value FROM checkpoints WHERE key LIKE ? AND key!=?",
                    (f"wake:{work.assistant}:%", wake_key),
                ):
                    if isinstance(existing_val := decode(existing_raw), dict) and not existing_val.get("job"):
                        active_others.append((str(existing_val.get("set") or ""), str(existing_key)))
                if len(active_others) >= MAX_WAKES:
                    for _, evicted_key in sorted(active_others)[: len(active_others) - MAX_WAKES + 1]:
                        db.execute("DELETE FROM checkpoints WHERE key=?", (evicted_key,))
                self.checkpoint(
                    db,
                    wake_key,
                    {
                        "due": wake.due,
                        "reason": wake.reason,
                        "set": work.end,
                        "job": "",
                        **({"origin": work.origin.model_dump(mode="json")} if work.origin is not None else {}),
                    },
                )
            self.trim(db)

    def _retry_job(
        self,
        db: sqlite3.Connection,
        key: str,
        reason: str,
        *,
        budget: int,
        uncertain: bool,
        idempotent: bool,
    ) -> tuple[str, str]:
        row = db.execute("SELECT value FROM checkpoints WHERE key=?", (f"retry:{key}",)).fetchone()
        previous = decode(row[0]) if row is not None else {}
        attempts = (int(str(previous.get("attempts", 0))) if isinstance(previous, dict) else 0) + 1
        if attempts <= budget:
            delay = min(300, 2 ** min(attempts, 9))
            due = timestamp((datetime.now(UTC) + timedelta(seconds=delay)).isoformat())
            self.checkpoint(db, f"retry:{key}", {"attempts": attempts, "due": due})
            return "pending", reason
        db.execute("DELETE FROM checkpoints WHERE key=?", (f"retry:{key}",))
        outcome = "uncertain" if (uncertain and not idempotent) else "failed"
        message = (
            f"{reason}; stopped retrying after {attempts} attempts; retry or cancel it with bf queue"
            if budget > 0
            else reason
        )
        return outcome, message

    def fail(
        self,
        key: str,
        error: str,
        *,
        uncertain: bool = False,
        idempotent: bool | None = None,
        retry: bool = False,
        gate: bool = False,
        tempfail: bool = False,
        not_started: bool = False,
    ) -> None:
        """Record a failed attempt, retrying up to the job's retry budget with bounded backoff."""
        with self.connect() as db:
            job_row = db.execute("SELECT payload,state FROM jobs WHERE id=?", (key,)).fetchone()
            if job_row is None or job_row[1] in {"complete", "cancelled"}:
                return
            work: Work | None = None
            with suppress(Error):
                work = self.parse({"payload": job_row[0], "state": job_row[1]})
            if work is not None:
                budget, is_idempotent = job_retry_budget(work, gate=gate or retry, tempfail=tempfail)
                if idempotent is not None:
                    is_idempotent = idempotent
            else:
                is_idempotent = bool(idempotent)
                budget = (
                    3
                    if (retry or gate or tempfail or (not_started and is_idempotent) or (uncertain and is_idempotent))
                    else 0
                )
            outcome, message = self._retry_job(
                db, key, error, budget=budget, uncertain=uncertain, idempotent=is_idempotent
            )
            updated = db.execute(
                "UPDATE jobs SET state=?,error=?,updated=? WHERE id=? AND state NOT IN ('complete','cancelled')",
                (outcome, message, now(), key),
            )
            if updated.rowcount and work is not None and outcome in {"failed", "uncertain"}:
                self._enqueue_failure_notice(db, work, outcome, brain=self.brain)

    def delivery(
        self,
        *,
        job: str = "",
        key: str = "",
        names: set[str] | None = None,
        excluding: set[str] | None = None,
        skip: Collection[str] = (),
        include_standalone: bool = False,
    ) -> dict[str, object] | None:
        """The next due delivery; `excluding` names busy channels and `skip` delivery ids to pass over."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT deliveries.*,jobs.assistant FROM deliveries LEFT JOIN jobs ON jobs.id=deliveries.job "
                "WHERE deliveries.state='pending' "
                "AND (jobs.state='complete' OR deliveries.job IS NULL "
                "OR (deliveries.id LIKE 'notice:%' AND jobs.state IN ('failed','uncertain'))) "
                "AND deliveries.due<=? AND (?='' OR deliveries.job=?) AND (?='' OR deliveries.id=?) ORDER BY deliveries.due,id LIMIT 10000",
                (now(), job, job, key, key),
            ).fetchall()
        # Scoped execution must never send another assistant's work or unrelated manual messages.
        row = next(
            (
                row
                for row in rows
                if (names is None or row["assistant"] in names or (include_standalone and row["job"] is None))
                and row["channel"] not in (excluding or set())
                and row["id"] not in skip
            ),
            None,
        )
        return dict(row) if row is not None else None

    def sending(self, key: str) -> bool:
        with self.connect() as db:
            return bool(
                db.execute(
                    "UPDATE deliveries SET state='sending',attempts=attempts+1 WHERE id=? AND state='pending'", (key,)
                ).rowcount
            )

    def outcomes(self, job: str) -> list[dict[str, object]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("SELECT id,channel,state,error FROM deliveries WHERE job=? ORDER BY id", (job,))
            ]

    def outstanding(self, names: set[str], *, include_standalone: bool = False) -> list[dict[str, object]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT deliveries.id,deliveries.channel,deliveries.error,jobs.assistant,deliveries.job FROM deliveries "
                "LEFT JOIN jobs ON jobs.id=deliveries.job "
                "WHERE deliveries.state NOT IN ('delivered','cancelled') "
                "AND (jobs.state='complete' OR deliveries.job IS NULL "
                "OR (deliveries.id LIKE 'notice:%' AND jobs.state IN ('failed','uncertain')))"
            ).fetchall()
        return [dict(row) for row in rows if row["assistant"] in names or (include_standalone and row["job"] is None)]

    def delivered(self, key: str, name: str, message: Outgoing, receipt: Receipt) -> None:
        with self.connect() as db:
            db.execute(
                # A delivered row's due time dates its receipt, which RETENTION keeps.
                "UPDATE deliveries SET state='delivered',receipt=?,receipt_id=?,error='',due=? WHERE id=?",
                (receipt.model_dump_json(), receipt.id, now(), key),
            )
            db.execute(
                "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?)",
                (f"{name}:{receipt.id}", name, receipt.conversation, "assistant", now(), message.text),
            )
            # A receipt can arrive after an adapter's echo. Cancel only work which has not started.
            db.execute(
                "UPDATE jobs SET state='cancelled',error='own-message echo' WHERE kind='input' AND state='pending' "
                "AND json_extract(payload,'$.origin.channel')=? AND json_extract(payload,'$.origin.message.id')=?",
                (name, receipt.id),
            )
            self.trim(db)

    def delivery_failed(self, key: str, error: str, *, outcome: str = "uncertain", due: str = "") -> None:
        """Record a failed attempt: `pending` retries at `due`; `failed` and `uncertain` wait for bf queue."""
        with self.connect() as db:
            db.execute(
                "UPDATE deliveries SET state=?,error=?,due=? WHERE id=? AND state NOT IN ('delivered','cancelled')",
                (outcome, error, due or now(), key),
            )

    def recover_attempts(self, key: str = "") -> None:
        """Record the harness attempts and sends whose process ended without recording them, such as one killed.

        Any process may call this: a live attempt holds its job or delivery lock, so only abandoned ones are
        recorded. `key` limits it to one job or delivery.
        """
        with self.connect() as db:
            runs = [row[0] for row in db.execute("SELECT id FROM jobs WHERE state='running' AND ? IN ('',id)", (key,))]
            sends = [
                row[0] for row in db.execute("SELECT id FROM deliveries WHERE state='sending' AND ? IN ('',id)", (key,))
            ]
        for job in runs:
            with suppress(BusyError), collecting(self.brain, f".job-{job}"):
                self.interrupted(job=job)
        for delivery in sends:
            with suppress(BusyError), collecting(self.brain, f".delivery-{digest(delivery.encode())}"):
                self.interrupted(delivery=delivery)

    def interrupted(self, *, job: str = "", delivery: str = "") -> None:
        """Record one ended attempt; its caller holds the attempt's lock, or is the attempt itself.

        An interrupted harness attempt or non-idempotent send is never silently replayed; an interrupted idempotent
        delivery retries as a failed one would.
        """
        with self.connect() as db:
            # No job or delivery has an empty id: an omitted one matches nothing.
            job_row = db.execute("SELECT payload,kind FROM jobs WHERE id=? AND state='running'", (job,)).fetchone()
            if job_row is not None:
                # A payload BF cannot read retries nothing: the attempt waits for inspection.
                work: Work | None = None
                with suppress(Error):
                    work = self.parse({"payload": job_row[0], "state": "running"})
                if work is not None:
                    budget, is_idempotent = job_retry_budget(work)
                else:
                    try:
                        payload = decode(job_row[0])
                    except Error:
                        payload = {}
                    settings = payload.get("settings") if isinstance(payload, dict) else None
                    is_idempotent = isinstance(settings, dict) and retried(settings.get("idempotent"), job_row[1])
                    budget = 3 if is_idempotent else 0
                reason_text = (
                    "interrupted harness attempt"
                    if budget > 0 or is_idempotent
                    else "interrupted harness attempt; inspect before retrying"
                )
                outcome, message = self._retry_job(
                    db, job, reason_text, budget=budget, uncertain=True, idempotent=is_idempotent
                )
                updated = db.execute(
                    "UPDATE jobs SET state=?,error=?,updated=? WHERE id=? AND state='running'",
                    (outcome, message, now(), job),
                )
                if updated.rowcount and work is not None and outcome in {"failed", "uncertain"}:
                    self._enqueue_failure_notice(db, work, outcome, brain=self.brain)
            row = db.execute(
                "SELECT job IS NOT NULL,coalesce(json_extract(command,'$.idempotent'),0),attempts "
                "FROM deliveries WHERE id=? AND state='sending'",
                (delivery,),
            ).fetchone()
            if row is not None:
                owned, idempotent, attempts = row
                reason = "interrupted send" if idempotent else "interrupted send; inspect before retrying"
                outcome, error, due = retry_policy(bool(idempotent), bool(owned), int(attempts), reason)
                db.execute(
                    "UPDATE deliveries SET state=?,error=?,due=? WHERE id=? AND state='sending'",
                    (outcome, error, due or now(), delivery),
                )

    def conversation(self, channel: str, conversation: str, before: str, *, exclude: str = "") -> list[dict[str, str]]:
        """Up to 20 earlier messages of one conversation within 32 KiB, oldest first, without `exclude`."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT role,text,at FROM messages WHERE channel=? AND conversation=? AND at<=? AND id!=? "
                "ORDER BY at DESC,id DESC LIMIT 20",
                (channel, conversation, before, exclude),
            ).fetchall()
        result: list[dict[str, str]] = []
        size = 0
        for row in rows:
            size += len(row["text"].encode())
            if size > 32_768:
                break
            result.append(dict(row))
        return list(reversed(result))

    def report(self, *, limit: int = 50) -> dict[str, object]:
        with self.connect() as db:
            jobs = [
                dict(row)
                for row in db.execute(
                    "SELECT id,assistant,kind,state,created,updated,error FROM jobs ORDER BY created DESC LIMIT ?",
                    (limit,),
                )
            ]
            deliveries = [
                dict(row)
                for row in db.execute(
                    "SELECT id,job,channel,state,attempts,error FROM deliveries WHERE state NOT IN ('delivered','cancelled') ORDER BY due LIMIT ?",
                    (limit,),
                )
            ]
            counts = {row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM jobs GROUP BY state")}
            delivery_counts = {
                row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM deliveries GROUP BY state")
            }
            failures = db.execute(
                "SELECT count(*) FROM deliveries WHERE state NOT IN ('delivered','cancelled') AND error!=''"
            ).fetchone()[0]
            # A healthy entry, such as a stream that stayed connected after reconnecting, is recorded as an empty
            # error: list only failures.
            health = {
                key: value
                for key, raw in db.execute("SELECT key,value FROM checkpoints WHERE key LIKE 'health:%'")
                if isinstance(value := decode(raw), dict) and value.get("error")
            }
            wakes = []
            for key, raw in db.execute("SELECT key,value FROM checkpoints WHERE key LIKE 'wake:%' ORDER BY key"):
                if not isinstance(value := decode(raw), dict):
                    continue
                suffix = str(key).removeprefix("wake:")
                assistant_name, sep, slot_name = suffix.partition(":")
                if not sep:
                    continue
                wakes.append(
                    {
                        "assistant": assistant_name,
                        "slot": slot_name,
                        "due": value.get("due"),
                        "reason": str(value.get("reason") or ""),
                        "job": value.get("job") or None,
                    }
                )
        return {
            "jobs": jobs,
            "wakes": wakes,
            "deliveries": deliveries,
            "counts": counts,
            "delivery_counts": delivery_counts,
            "delivery_failures": failures,
            "transports": health,
        }

    def compact(self) -> None:
        """Bound retained bodies and expire old identity tombstones."""
        with self.connect() as db:
            self.trim(db)

    @staticmethod
    def trim(db: sqlite3.Connection) -> None:
        # Tombstones and receipts suppress replays and echoes for RETENTION, longer than providers redeliver.
        horizon = timestamp((datetime.now(UTC) - RETENTION).isoformat())
        db.execute("DELETE FROM deliveries WHERE state IN ('delivered','cancelled') AND due<?", (horizon,))
        db.execute(
            "DELETE FROM jobs WHERE state IN ('complete','cancelled') AND updated<? "
            "AND NOT EXISTS (SELECT 1 FROM deliveries WHERE job=jobs.id)",
            (horizon,),
        )
        db.execute("DELETE FROM events WHERE at<?", (horizon,))
        db.execute(
            "UPDATE jobs SET payload='{}',raw=NULL,prepared=NULL WHERE state IN ('complete','cancelled') "
            "AND id IN (SELECT id FROM (SELECT id,sum(length(CAST(payload AS BLOB))+coalesce(length(raw),0)+coalesce(length(CAST(prepared AS BLOB)),0)) "
            "OVER (ORDER BY created DESC,id DESC) AS bytes FROM jobs WHERE state IN ('complete','cancelled')) WHERE bytes>?) "
            "AND NOT EXISTS (SELECT 1 FROM deliveries WHERE job=jobs.id AND state NOT IN ('delivered','cancelled'))",
            (BODY_BUDGET,),
        )
        db.execute(
            "UPDATE events SET data='{}' WHERE id IN (SELECT id FROM (SELECT id,sum(length(CAST(data AS BLOB))) OVER "
            "(ORDER BY at DESC,id DESC) AS bytes FROM events) WHERE bytes>?)",
            (BODY_BUDGET,),
        )
        db.execute(
            "UPDATE deliveries SET payload='{}',command='{}' WHERE state IN ('delivered','cancelled') "
            "AND id IN (SELECT id FROM (SELECT id,sum(length(CAST(payload AS BLOB))+length(CAST(command AS BLOB))) OVER "
            "(ORDER BY due DESC,id DESC) AS bytes FROM deliveries WHERE state IN ('delivered','cancelled')) WHERE bytes>?)",
            (BODY_BUDGET,),
        )
        db.execute(
            "DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY at DESC LIMIT 10000) "
            "OR id IN (SELECT id FROM (SELECT id,sum(length(CAST(text AS BLOB))) OVER (ORDER BY at DESC,id DESC) AS bytes FROM messages) WHERE bytes>?)",
            (BODY_BUDGET,),
        )

    def send(self, name: str, command: Send, message: Outgoing) -> None:
        with self.connect() as db:
            payload, encoded = message.model_dump_json(), command.model_dump_json()
            identity = _identity(name, payload, encoded)
            previous = db.execute("SELECT digest FROM deliveries WHERE id=?", (message.id,)).fetchone()
            if previous is not None:
                if previous[0] != identity:
                    raise Error("delivery id already used; inspect its receipt or use a new id for a different message")
                return
            self.reserve(db, 1, 2 * (len(payload.encode()) + len(encoded.encode())))
            db.execute(
                "INSERT INTO deliveries(id,job,channel,state,payload,command,digest,due) VALUES (?,NULL,?,'pending',?,?,?,?)",
                (message.id, name, payload, encoded, identity, now()),
            )

    def retry(self, key: str, *, cancel: bool = False) -> None:
        """Explicitly retry or cancel a job or delivery.

        An attempt whose process ended without recording it, such as an interrupted `bf run`, is recorded first.
        """
        self.recover_attempts(key)
        lock = ""
        with self.connect() as db:
            delivery = db.execute("SELECT state FROM deliveries WHERE id=?", (key,)).fetchone()
            if delivery is not None:
                if delivery[0] == "sending":
                    raise Error("delivery is still being sent; retry or cancel it once its attempt ends")
                if delivery[0] not in {"uncertain", "failed", "pending"}:
                    raise Error("only pending or unresolved deliveries can be retried or cancelled")
                # An explicit retry starts the automatic backoff of an idempotent delivery over.
                db.execute(
                    "UPDATE deliveries SET state=?,error='',due=?,attempts=0 WHERE id=?",
                    ("cancelled" if cancel else "pending", now(), key),
                )
                lock = f".delivery-{digest(key.encode())}"
            else:
                row = db.execute(
                    "SELECT state,raw,prepared,payload,assistant,kind,slot FROM jobs WHERE id=?", (key,)
                ).fetchone()
                if row is not None and row[0] == "running":
                    raise Error("job is still running; retry or cancel it once its attempt ends")
                if row is None or row[0] not in {"failed", "uncertain", "pending"}:
                    raise Error("only pending or unresolved jobs can be retried or cancelled")
                state = (
                    "cancelled" if cancel else "prepared" if row[2] else "answered" if row[1] is not None else "pending"
                )
                if state == "answered":
                    work = self.parse(dict(row))
                    self.reserve(
                        db,
                        0,
                        answer_space(len(row[1]), len(str(row[3]).encode()), len(work.routes)),
                        limit=_limit(),
                        reason="assistant queue lacks space to record this saved answer; resolve other work first",
                    )
                db.execute("UPDATE jobs SET state=?,error='',updated=? WHERE id=?", (state, now(), key))
                db.execute("DELETE FROM checkpoints WHERE key=?", (f"retry:{key}",))
                db.execute(
                    "UPDATE deliveries SET state='cancelled',error='' WHERE job=? AND id LIKE 'notice:%' AND state='pending'",
                    (key,),
                )
                if cancel:
                    db.execute("DELETE FROM checkpoints WHERE key=?", (f"gate:{key}",))
                    # A cancelled wake run drops the wake it took, so `bf queue` lists only live wakes.
                    db.execute(
                        "DELETE FROM checkpoints WHERE key LIKE ? AND json_extract(value,'$.job')=?",
                        (f"wake:{row[4]}:%", key),
                    )
                    db.execute(
                        "UPDATE deliveries SET state='cancelled',error='' WHERE job=? AND state='pending'", (key,)
                    )
                    if row[5] == "schedule":
                        # Read the window's end without parsing the job: a job an earlier release queued, whose
                        # settings this release rejects, must stay cancellable.
                        payload = decode(str(row[3]))
                        end = payload.get("end") if isinstance(payload, dict) else None
                        if isinstance(end, str):
                            self.checkpoint(db, f"schedule:{row[4]}:{row[6]}", {"end": end, "success": now()})
                lock = f".job-{key}"
        if cancel and lock:
            # A worker holding the lock removes it itself once it sees the cancellation.
            with suppress(BusyError), collecting(self.brain, lock):
                forget(self.brain, lock)
