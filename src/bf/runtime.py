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
from typing import Annotated, Literal, cast
from uuid import uuid4

from pydantic import Field, JsonValue, ValidationError, field_validator

from bf.config import load
from bf.messages import Message, Outgoing, Receipt
from bf.models import Assistant, Error, Invocation, Model, Name, Record, Send, decode, digest, encode, timestamp
from bf.storage import BusyError, Store, collecting, lock_file, runtime_store

DATABASE = "queue.sqlite3"
# Settings that only decide when jobs start or how many runs stay: editing them leaves queued jobs compatible.
SCHEDULING = {"inputs", "schedules", "triggers", "keep", "idempotent"}
MAX_PENDING = 10_000
MAX_DATABASE = 256 << 20
BODY_BUDGET = 8 << 20
# Identity tombstones outlive the longest provider redelivery window, Pub/Sub's 31 days, then go: they must
# never exhaust the queue.
RETENTION = timedelta(days=35)
# A failing idempotent delivery retries after 2, 4, ... up to 300 seconds; past this many attempts it waits for
# an explicit retry or cancellation instead of starting its adapter forever.
MAX_ATTEMPTS = 20
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
    origin: Origin | None = None
    event: dict[str, JsonValue] = Field(default_factory=dict)
    routes: list[Route] = Field(default_factory=list)

    _time = field_validator("start", "end")(timestamp)


class EventTarget(Model):
    assistant: Name
    trigger: Name


class EventIntent(Model):
    """A committed sensor change and the triggers that named its sensor then: names only, never configuration."""

    id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    kind: Literal["sensor.changed"] = "sensor.changed"
    source: Name
    at: str
    data: dict[str, JsonValue] = Field(default_factory=dict)
    targets: list[EventTarget] = Field(default_factory=list, max_length=1024)

    _time = field_validator("at")(timestamp)


class Prepared(Model):
    record: Record


_SCHEMA = """
CREATE TABLE events (id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL,
                     digest TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE triggers (id TEXT PRIMARY KEY, event TEXT NOT NULL, assistant TEXT NOT NULL, trigger TEXT NOT NULL,
                       source TEXT NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL, state TEXT NOT NULL,
                       error TEXT NOT NULL DEFAULT '');
CREATE INDEX trigger_states ON triggers(state,at);
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
PRAGMA user_version=1;
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


def retry_policy(idempotent: bool, owned: bool, attempts: int, reason: str) -> tuple[str, str, str]:
    """The state, error and due time of a delivery after a failed or interrupted attempt.

    Only an idempotent assistant delivery retries by itself, with bounded backoff; a standalone send waits for
    `bf queue ID --retry`, and an unknown non-idempotent outcome for inspection.
    """
    if not idempotent:
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
            self.store.fingerprint(DATABASE)
        except FileNotFoundError:
            return False
        return True

    @contextmanager
    def connect(self, *, bound: bool = True) -> Generator[sqlite3.Connection]:
        """One immediate transaction; `bound` checks, or rebinds, the brain path and name the queue serves."""
        try:
            # Never open/close a live SQLite inode ourselves: POSIX close drops this process's locks,
            # including another thread's transaction. Serialize creation before SQLite can see the file.
            with _CREATE, self.store.parent(DATABASE) as (parent, name):
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
                    self.store.fingerprint(DATABASE + suffix)
            connection = sqlite3.connect(self.store.root / DATABASE, timeout=10, isolation_level=None)
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
                elif version != 1:
                    raise Error("unsupported assistant queue version; preserve it and use its matching BF release")
                check_schema(connection)
                if bound:
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
        """Whether any job or delivery still waits; pending sensor events are not bound to a path."""
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
    def put(db: sqlite3.Connection, work: Work, *, error: str = "") -> None:
        """Store one job; its answer space is reserved only when a worker starts it. An `error` stores it failed."""
        if db.execute("SELECT 1 FROM jobs WHERE id=?", (work.id,)).fetchone():
            return
        payload, slots = work.model_dump_json(), 1 + len(work.routes)
        Queue.reserve(db, slots, 2 * len(payload.encode()))
        at = now()
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
            Queue.event(db, uuid4().hex, "assistant.failed", "runtime", {"job": work.id, "uncertain": False}, at)

    def enqueue(self, work: Work) -> None:
        with self.connect() as db:
            self.put(db, work)

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
                    self.event(db, f"input:{key}", "channel.received", name, payload, message.time)
                    db.execute(
                        "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?)",
                        (key, name, message.conversation, "user", message.time, message.text),
                    )
            for work, error in jobs:
                if work.origin is not None and work.origin.message.id in accepted:
                    self.put(db, work, error=error)
            if checkpoint:
                self.checkpoint(db, *checkpoint)
            self.trim(db)

    def admit_event(self, event: EventIntent) -> None:
        """Record a committed sensor change for each trigger that named its sensor; dispatch admits the jobs.

        Record writers call this: it reads no configuration or prompt, reserves nothing and ignores the path
        binding, so only an unavailable queue can delay it. A record journal is portable evidence, not an
        executable configuration or prompt carrier.
        """
        with self.connect(bound=False) as db:
            self.event(db, event.id, event.kind, event.source, event.data, event.at)
            for target in event.targets:
                db.execute(
                    "INSERT OR IGNORE INTO triggers(id,event,assistant,trigger,source,at,data,state) "
                    "VALUES (?,?,?,?,?,?,?,'pending')",
                    (
                        digest(f"{event.id}:{target.assistant}:{target.trigger}".encode())[:32],
                        event.id,
                        target.assistant,
                        target.trigger,
                        event.source,
                        event.at,
                        encode(event.data).decode(),
                    ),
                )

    def pending_triggers(self) -> list[dict[str, object]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM triggers WHERE state='pending' ORDER BY at,id")]

    def settle(self, key: str, state: str, error: str = "") -> None:
        """Mark a pending trigger dispatched or dropped, or keep it pending with the reason it waits."""
        with self.connect() as db:
            db.execute("UPDATE triggers SET state=?,error=? WHERE id=? AND state='pending'", (state, error, key))

    def dispatch(self, key: str, work: Work) -> None:
        """Admit one pending trigger once; it extends a compatible pending job of its slot instead of adding one."""
        with self.connect() as db:
            row = db.execute("SELECT state FROM triggers WHERE id=?", (key,)).fetchone()
            if row is None or row[0] != "pending":
                return
            # Keep at most one pending successor to a running trigger, extending its evidence window.
            previous = db.execute(
                "SELECT id,payload FROM jobs WHERE assistant=? AND slot=? AND state='pending' ORDER BY created LIMIT 1",
                (work.assistant, work.slot),
            ).fetchone()
            combined = Work.model_validate_json(previous["payload"]) if previous is not None else None
            if combined is not None and (
                combined.settings.model_dump(exclude=SCHEDULING),
                combined.routes,
                combined.template,
            ) == (
                work.settings.model_dump(exclude=SCHEDULING),
                work.routes,
                work.template,
            ):
                combined.settings = work.settings
                combined.start = min(combined.start, work.start)
                combined.end = max(combined.end, work.end)
                combined.event.update(start=combined.start, end=combined.end)
                for name in ("added", "updated", "removed"):
                    combined.event[name] = int(str(combined.event.get(name, 0))) + int(str(work.event.get(name, 0)))
                db.execute("UPDATE jobs SET payload=? WHERE id=?", (combined.model_dump_json(), previous["id"]))
            else:
                self.put(db, work)
            db.execute("UPDATE triggers SET state='dispatched',error='' WHERE id=?", (key,))

    def lifecycle(self, kind: str, source: str, data: dict[str, JsonValue]) -> None:
        with self.connect() as db:
            self.event(db, uuid4().hex, kind, source, data, now())

    def get(self, key: str) -> dict[str, object]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
        if row is None:
            raise Error("unknown queued job; use bf queue to list jobs")
        return dict(row)

    def inspect(self, key: str) -> dict[str, object]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (key,)).fetchone()
            row = row or db.execute("SELECT * FROM triggers WHERE id=?", (key,)).fetchone()
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

    def claim(self, key: str, payload: str) -> bool:
        """Start a pending job exactly as its worker read it, reserving its answer space first.

        False when the job changed meanwhile, as when a sensor change extended its window, or is no longer pending.
        """
        with self.connect() as db:
            row = db.execute("SELECT state,payload FROM jobs WHERE id=?", (key,)).fetchone()
            if row is None or row[0] != "pending" or row[1] != payload:
                return False
            work = self.parse(dict(row))
            space = answer_space(work.settings.max_bytes, len(payload.encode()), len(work.routes))
            if space > answer_limit():
                raise Error(
                    f"assistant {work.assistant} needs {space >> 20} MiB of queue space per answer, more than one "
                    "job may reserve; lower its max_bytes or its outputs"
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

    def prepare(self, work: Work, record: Record, replies: list[Outgoing]) -> None:
        prepared = Prepared(record=record)
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

    def finish(self, work: Work) -> None:
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='complete',updated=?,error='' WHERE id=?", (now(), work.id))
            db.execute("DELETE FROM checkpoints WHERE key=?", (f"retry:{work.id}",))
            if work.kind == "schedule":
                self.checkpoint(db, f"schedule:{work.assistant}:{work.slot}", {"end": work.end, "success": now()})
            self.event(db, f"completed:{work.id}", "assistant.completed", work.assistant, {"job": work.id}, now())
            self.trim(db)

    def _retry_attempt(self, db: sqlite3.Connection, key: str, reason: str) -> tuple[str, str]:
        row = db.execute("SELECT value FROM checkpoints WHERE key=?", (f"retry:{key}",)).fetchone()
        previous = decode(row[0]) if row is not None else {}
        attempts = (int(str(previous.get("attempts", 0))) if isinstance(previous, dict) else 0) + 1
        outcome, message, due = retry_policy(True, True, attempts, reason)
        if outcome == "pending":
            self.checkpoint(db, f"retry:{key}", {"attempts": attempts, "due": due})
        else:
            db.execute("DELETE FROM checkpoints WHERE key=?", (f"retry:{key}",))
        return outcome, message

    def fail(self, key: str, error: str, *, uncertain: bool = False, idempotent: bool = False) -> None:
        with self.connect() as db:
            if uncertain and idempotent:
                outcome, message = self._retry_attempt(db, key, error)
                db.execute(
                    "UPDATE jobs SET state=?,error=?,updated=? WHERE id=? AND state NOT IN ('complete','cancelled')",
                    (outcome, message, now(), key),
                )
                if outcome != "pending":
                    self.event(db, uuid4().hex, "assistant.failed", "runtime", {"job": key, "uncertain": False}, now())
                return
            db.execute(
                "UPDATE jobs SET state=?,error=?,updated=? WHERE id=? AND state NOT IN ('complete','cancelled')",
                ("uncertain" if uncertain else "failed", error, now(), key),
            )
            self.event(db, uuid4().hex, "assistant.failed", "runtime", {"job": key, "uncertain": uncertain}, now())

    def delivery(
        self,
        *,
        job: str = "",
        key: str = "",
        names: set[str] | None = None,
        excluding: set[str] | None = None,
        skip: Collection[str] = (),
    ) -> dict[str, object] | None:
        """The next due delivery; `excluding` names busy channels and `skip` delivery ids to pass over."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT deliveries.*,jobs.assistant FROM deliveries LEFT JOIN jobs ON jobs.id=deliveries.job "
                "WHERE deliveries.state='pending' AND (jobs.state='complete' OR deliveries.job IS NULL) "
                "AND deliveries.due<=? AND (?='' OR deliveries.job=?) AND (?='' OR deliveries.id=?) ORDER BY deliveries.due,id LIMIT 10000",
                (now(), job, job, key, key),
            ).fetchall()
        # Scoped execution must never send another assistant's work or unrelated manual messages.
        row = next(
            (
                row
                for row in rows
                if (names is None or row["assistant"] in names)
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

    def outstanding(self, names: set[str]) -> list[dict[str, object]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT deliveries.id,deliveries.channel,deliveries.error,jobs.assistant FROM deliveries "
                "JOIN jobs ON jobs.id=deliveries.job WHERE deliveries.state NOT IN ('delivered','cancelled') AND jobs.state='complete'"
            ).fetchall()
        return [dict(row) for row in rows if row["assistant"] in names]

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
            self.event(db, f"sent:{key}", "channel.sent", name, {"delivery": key}, now())
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
            job_row = db.execute(
                "SELECT coalesce(json_extract(payload,'$.settings.idempotent'),0) FROM jobs WHERE id=? AND state='running'",
                (job,),
            ).fetchone()
            if job_row is not None:
                if bool(job_row[0]):
                    outcome, message = self._retry_attempt(db, job, "interrupted harness attempt")
                    db.execute(
                        "UPDATE jobs SET state=?,error=?,updated=? WHERE id=? AND state='running'",
                        (outcome, message, now(), job),
                    )
                else:
                    db.execute(
                        "UPDATE jobs SET state='uncertain',error='interrupted harness attempt; inspect before retrying' "
                        "WHERE id=? AND state='running'",
                        (job,),
                    )
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
            triggers = [
                dict(row)
                for row in db.execute(
                    "SELECT id,assistant,trigger,source,state,at,error FROM triggers "
                    "WHERE state IN ('pending','dropped') ORDER BY at DESC LIMIT ?",
                    (limit,),
                )
            ]
            counts = {row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM jobs GROUP BY state")}
            delivery_counts = {
                row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM deliveries GROUP BY state")
            }
            trigger_counts = {
                row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM triggers GROUP BY state")
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
        return {
            "jobs": jobs,
            "deliveries": deliveries,
            "triggers": triggers,
            "counts": counts,
            "delivery_counts": delivery_counts,
            "trigger_counts": trigger_counts,
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
        db.execute("DELETE FROM triggers WHERE state!='pending' AND at<?", (horizon,))
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
        """Explicitly retry or cancel a job, delivery or pending sensor trigger.

        An attempt whose process ended without recording it, such as an interrupted `bf run`, is recorded first.
        """
        self.recover_attempts(key)
        lock = ""
        with self.connect() as db:
            delivery = db.execute("SELECT state FROM deliveries WHERE id=?", (key,)).fetchone()
            trigger = db.execute("SELECT state FROM triggers WHERE id=?", (key,)).fetchone()
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
            elif trigger is not None:
                if trigger[0] not in {"pending", "dropped"}:
                    raise Error("only pending or dropped sensor triggers can be retried or cancelled")
                db.execute(
                    "UPDATE triggers SET state=?,error='' WHERE id=?", ("cancelled" if cancel else "pending", key)
                )
                return
            else:
                row = db.execute("SELECT state,raw,prepared,payload FROM jobs WHERE id=?", (key,)).fetchone()
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
                if cancel:
                    db.execute(
                        "UPDATE deliveries SET state='cancelled',error='' WHERE job=? AND state='pending'", (key,)
                    )
                    work = self.parse(dict(row))
                    if work.kind == "schedule":
                        self.checkpoint(
                            db, f"schedule:{work.assistant}:{work.slot}", {"end": work.end, "success": now()}
                        )
                lock = f".job-{key}"
        if cancel and lock:
            # A worker holding the lock removes it itself once it sees the cancellation.
            with suppress(BusyError), collecting(self.brain, lock):
                forget(self.brain, lock)


def admit(store: Store, event: EventIntent) -> None:
    """Idempotent handoff called while the committed record journal is still durable."""
    Queue(store).admit_event(event)
