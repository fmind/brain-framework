"""Durable local assistant work, independent of the disposable search cache and run history."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from threading import Lock
from typing import Annotated, Literal, cast
from uuid import uuid4

from pydantic import Field, JsonValue, ValidationError, field_validator

from bf.config import load
from bf.messages import Message, Outgoing, Receipt
from bf.models import Assistant, Error, Invocation, Model, Name, Record, Send, decode, digest, encode, timestamp
from bf.storage import BusyError, Store, collecting, runtime_store

DATABASE = "queue.sqlite3"
MAX_PENDING = 10_000
MAX_DATABASE = 256 << 20
BODY_BUDGET = 8 << 20
_CREATE = Lock()


def now() -> str:
    return timestamp(datetime.now(UTC).isoformat())


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
    configuration: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class EventIntent(Model):
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
CREATE TABLE jobs (id TEXT PRIMARY KEY, assistant TEXT NOT NULL, kind TEXT NOT NULL, slot TEXT NOT NULL,
                   state TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL,
                   raw BLOB, prepared TEXT, error TEXT NOT NULL DEFAULT '');
CREATE INDEX ready_jobs ON jobs(state,kind,created);
CREATE INDEX job_slots ON jobs(assistant,slot,state);
CREATE TABLE deliveries (id TEXT PRIMARY KEY, job TEXT REFERENCES jobs(id), channel TEXT NOT NULL,
                         state TEXT NOT NULL, payload TEXT NOT NULL, command TEXT NOT NULL,
                         receipt TEXT, error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                         due TEXT NOT NULL);
CREATE INDEX ready_deliveries ON deliveries(state,due);
CREATE TABLE checkpoints (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE messages (id TEXT PRIMARY KEY, channel TEXT NOT NULL, conversation TEXT NOT NULL,
                       role TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL);
CREATE INDEX conversation_messages ON messages(channel,conversation,at);
PRAGMA user_version=1;
"""


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
    def connect(self) -> Iterator[sqlite3.Connection]:
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
                connection.execute("PRAGMA synchronous=FULL")
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
                objects = connection.execute(
                    "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall()
                tables = {row[1] for row in objects if row[0] == "table"}
                if tables != {"events", "jobs", "deliveries", "checkpoints", "messages"} or any(
                    row[0] not in {"table", "index"} for row in objects
                ):
                    raise Error("unexpected assistant queue schema; preserve it and repair it")
                binding: dict[str, JsonValue] = {"path": str(self.brain.root), "name": load(self.brain).name}
                previous = connection.execute("SELECT value FROM checkpoints WHERE key='binding'").fetchone()
                if previous is not None and decode(previous[0]) != binding:
                    raise Error(
                        "assistant queue belongs to a different brain path or name; preserve pending work before rebinding it"
                    )
                if previous is None:
                    self.checkpoint(connection, "binding", binding)
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
    def reserve(db: sqlite3.Connection, slots: int, size: int) -> None:
        Queue.trim(db)
        # An unfinished job reserves its future deliveries as well as its own slot. Once complete,
        # only actual unresolved deliveries count; never double-count already prepared fan-out.
        count = db.execute(
            "SELECT coalesce(sum(1+json_array_length(payload,'$.routes')),0) FROM jobs "
            "WHERE state NOT IN ('complete','cancelled')"
        ).fetchone()[0]
        count += db.execute(
            "SELECT count(*) FROM deliveries WHERE state NOT IN ('delivered','cancelled') "
            "AND (job IS NULL OR job IN (SELECT id FROM jobs WHERE state IN ('complete','cancelled')))"
        ).fetchone()[0]
        if count + slots > MAX_PENDING:
            raise Error("assistant queue has no unfinished-work slots; resolve its backlog before accepting more input")
        # Reserve before acknowledging input: raw output, record text/metadata and every delivery.
        # Sixfold encoding headroom covers JSON escaping and number normalization. Count occupied pages
        # too: identity tombstones survive body retention and must not consume future answer space.
        reserved = db.execute(
            "SELECT coalesce(sum(json_extract(payload,'$.settings.max_bytes') * 6 * "
            "(3+json_array_length(payload,'$.routes')) + 2*length(CAST(payload AS BLOB))),0) "
            "FROM jobs WHERE state NOT IN ('complete','cancelled')"
        ).fetchone()[0]
        reserved += db.execute(
            "SELECT coalesce(sum(length(CAST(payload AS BLOB))+coalesce(length(raw),0)+coalesce(length(CAST(prepared AS BLOB)),0)),0) "
            "FROM jobs WHERE state IN ('complete','cancelled') AND EXISTS "
            "(SELECT 1 FROM deliveries WHERE job=jobs.id AND state NOT IN ('delivered','cancelled'))"
        ).fetchone()[0]
        reserved += db.execute(
            "SELECT coalesce(sum(length(CAST(payload AS BLOB))+length(CAST(command AS BLOB))),0) FROM deliveries "
            "WHERE state NOT IN ('delivered','cancelled') AND (job IS NULL OR job IN (SELECT id FROM jobs WHERE state IN ('complete','cancelled')))"
        ).fetchone()[0]
        pages = db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]
        occupied = pages * db.execute("PRAGMA page_size").fetchone()[0]
        if occupied + reserved + size > MAX_DATABASE * 3 // 4:
            raise Error(
                "assistant queue has insufficient reserved answer space; resolve its backlog before accepting more input"
            )

    @staticmethod
    def put(db: sqlite3.Connection, work: Work) -> None:
        if db.execute("SELECT 1 FROM jobs WHERE id=?", (work.id,)).fetchone():
            return
        required = work.settings.max_bytes * 6 * (3 + len(work.routes)) + len(work.model_dump_json().encode()) * 2
        Queue.reserve(db, 1 + len(work.routes), required)
        at = now()
        db.execute(
            "INSERT INTO jobs(id,assistant,kind,slot,state,created,updated,payload) VALUES (?,?,?,?,'pending',?,?,?)",
            (work.id, work.assistant, work.kind, work.slot, at, at, work.model_dump_json()),
        )

    def enqueue(self, work: Work) -> None:
        with self.connect() as db:
            self.put(db, work)

    def admit(
        self,
        name: str,
        messages: list[Message],
        jobs: list[Work],
        *,
        checkpoint: tuple[str, dict[str, JsonValue]] | None = None,
    ) -> None:
        """Persist messages, every consumer obligation and transport progress before acknowledging."""
        with self.connect() as db:
            accepted: set[str] = set()
            for message in messages:
                # A delivery receipt suppresses echoes even when an adapter omits own=true.
                echoed = db.execute(
                    "SELECT 1 FROM messages WHERE id=? AND role='assistant'", (f"{name}:{message.id}",)
                ).fetchone()
                if message.own or echoed:
                    continue
                accepted.add(message.id)
                payload = message.model_dump(mode="json", exclude={"cursor"})
                self.event(db, f"input:{name}:{message.id}", "channel.received", name, payload, message.time)
                db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?)",
                    (f"{name}:{message.id}", name, message.conversation, "user", message.time, message.text),
                )
            for work in jobs:
                origin = work.origin
                if origin is None or origin.message.id not in accepted:
                    continue
                work = work.model_copy(
                    update={"id": digest(f"{work.assistant}:{name}:{origin.message.id}".encode())[:32]}
                )
                self.put(db, work)
            if checkpoint:
                self.checkpoint(db, *checkpoint)
            self.trim(db)

    def admit_event(self, event: EventIntent) -> None:
        # A record journal is portable evidence, not an executable configuration or prompt carrier.
        from bf.assistants import prepare

        with self.connect() as db:
            if db.execute("SELECT 1 FROM events WHERE id=?", (event.id,)).fetchone():
                self.event(db, event.id, event.kind, event.source, event.data, event.at)
                return
        config = load(self.brain)
        jobs = []
        for target in event.targets:
            settings = config.assistants.get(target.assistant)
            trigger = settings.triggers.get(target.trigger) if settings else None
            if (
                settings is None
                or trigger is None
                or trigger.sensor.name != event.source
                or trigger.event != event.kind
            ):
                raise Error(
                    "pending sensor event no longer matches a configured assistant trigger; restore configuration before recovery"
                )
            if digest(encode(config.model_dump(mode="json"))) != target.configuration:
                raise Error(
                    "configuration changed before sensor event admission; restore it to recover pending dispatch"
                )
            jobs.append(
                prepare(
                    self.brain,
                    target.assistant,
                    "trigger",
                    str(event.data["start"]),
                    str(event.data["end"]),
                    config=config,
                    slot=f"trigger:{target.trigger}",
                    key=digest(f"{event.id}:{target.assistant}:{target.trigger}".encode())[:32],
                    event={"id": event.id, "type": event.kind, "sensor": event.source, **event.data},
                )
            )
        with self.connect() as db:
            existed = db.execute("SELECT 1 FROM events WHERE id=?", (event.id,)).fetchone() is not None
            self.event(db, event.id, event.kind, event.source, event.data, event.at)
            if existed:
                return
            for work in jobs:
                # Keep at most one pending successor to a running trigger, extending its evidence window.
                previous = db.execute(
                    "SELECT id,payload FROM jobs WHERE assistant=? AND slot=? AND state='pending' ORDER BY created LIMIT 1",
                    (work.assistant, work.slot),
                ).fetchone()
                if previous is not None:
                    combined = Work.model_validate_json(previous["payload"])
                    if (combined.settings, combined.routes, combined.template) != (
                        work.settings,
                        work.routes,
                        work.template,
                    ):
                        self.put(db, work)
                        continue
                    combined.start = min(combined.start, work.start)
                    combined.end = max(combined.end, work.end)
                    combined.event.update(start=combined.start, end=combined.end)
                    for key in ("added", "updated", "removed"):
                        combined.event[key] = int(str(combined.event.get(key, 0))) + int(str(work.event.get(key, 0)))
                    db.execute("UPDATE jobs SET payload=? WHERE id=?", (combined.model_dump_json(), previous["id"]))
                    continue
                self.put(db, work)

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
        return dict(row) if row is not None else self.get(key)

    def work(self, key: str) -> Work:
        try:
            row = self.get(key)
            if row["payload"] == "{}" and row["state"] in {"complete", "cancelled"}:
                raise Error("completed job body expired; read its assistant records instead")
            return Work.model_validate_json(str(row["payload"]))
        except ValidationError as error:
            raise Error("invalid queued assistant job; preserve the queue and repair it") from error

    def active(self, assistant: str, slot: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM jobs WHERE assistant=? AND slot=? AND state NOT IN ('complete','cancelled') LIMIT 1",
                    (assistant, slot),
                ).fetchone()
                is not None
            )

    def next(self, reactive: bool, names: set[str]) -> str | None:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,assistant FROM jobs WHERE state IN ('pending','answered','prepared') "
                "AND (kind IN ('input','manual'))=? AND NOT EXISTS (SELECT 1 FROM jobs earlier WHERE earlier.assistant=jobs.assistant "
                "AND earlier.slot=jobs.slot AND earlier.id!=jobs.id AND (earlier.state NOT IN ('complete','cancelled') "
                "OR EXISTS (SELECT 1 FROM deliveries WHERE job=earlier.id AND state NOT IN ('delivered','cancelled'))) "
                "AND (earlier.created<jobs.created OR (earlier.created=jobs.created AND earlier.id<jobs.id))) "
                "ORDER BY created,id LIMIT 10000",
                (reactive,),
            ).fetchall()
        return next((row[0] for row in rows if row[1] in names), None)

    def claim(self, key: str) -> bool:
        with self.connect() as db:
            return bool(
                db.execute(
                    "UPDATE jobs SET state='running',updated=? WHERE id=? AND state='pending'", (now(), key)
                ).rowcount
            )

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
                db.execute(
                    "INSERT OR IGNORE INTO deliveries(id,job,channel,state,payload,command,due) VALUES (?,?,?,'pending',?,?,?)",
                    (
                        message.id,
                        work.id,
                        route.channel,
                        message.model_dump_json(),
                        route.command.model_dump_json(),
                        now(),
                    ),
                )

    def finish(self, work: Work) -> None:
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='complete',updated=?,error='' WHERE id=?", (now(), work.id))
            if work.kind == "schedule":
                self.checkpoint(db, f"schedule:{work.assistant}:{work.slot}", {"end": work.end, "success": now()})
            self.event(db, f"completed:{work.id}", "assistant.completed", work.assistant, {"job": work.id}, now())
            self.trim(db)

    def fail(self, key: str, error: str, *, uncertain: bool = False) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state=?,error=?,updated=? WHERE id=? AND state NOT IN ('complete','cancelled')",
                ("uncertain" if uncertain else "failed", error, now(), key),
            )
            self.event(db, uuid4().hex, "assistant.failed", "runtime", {"job": key, "uncertain": uncertain}, now())

    def delivery(
        self, *, job: str = "", key: str = "", names: set[str] | None = None, excluding: set[str] | None = None
    ) -> dict[str, object] | None:
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
                if (names is None or row["assistant"] in names) and row["channel"] not in (excluding or set())
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
                "UPDATE deliveries SET state='delivered',receipt=?,error='' WHERE id=?",
                (receipt.model_dump_json(), key),
            )
            db.execute(
                "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?)",
                (f"{name}:{receipt.id}", name, receipt.conversation, "assistant", now(), message.text),
            )
            # A receipt can arrive after an adapter's echo. Cancel only work which has not started.
            for row in db.execute("SELECT id,payload FROM jobs WHERE kind='input' AND state='pending'").fetchall():
                work = Work.model_validate_json(row["payload"])
                if work.origin and work.origin.channel == name and work.origin.message.id == receipt.id:
                    db.execute("UPDATE jobs SET state='cancelled',error='own-message echo' WHERE id=?", (row["id"],))
            self.event(db, f"sent:{key}", "channel.sent", name, {"delivery": key}, now())
            self.trim(db)

    def delivery_failed(self, key: str, error: str, retry: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE deliveries SET state=?,error=?,due=? WHERE id=? AND state NOT IN ('delivered','cancelled')",
                ("pending" if retry else "uncertain", error, retry or now(), key),
            )

    def recover_attempts(self) -> None:
        """Only the exclusive dispatcher calls this; unresolved effects are never silently replayed."""
        with self.connect() as db:
            runs = [row[0] for row in db.execute("SELECT id FROM jobs WHERE state='running'")]
            sends = [row[0] for row in db.execute("SELECT id FROM deliveries WHERE state='sending'")]
        for table, prefix, keys in (("jobs", ".job-", runs), ("deliveries", ".delivery-", sends)):
            for key in keys:
                try:
                    with (
                        collecting(self.brain, prefix + (digest(key.encode()) if table == "deliveries" else key)),
                        self.connect() as db,
                    ):
                        query = (
                            "UPDATE jobs SET state='uncertain',error='interrupted harness attempt; inspect before retrying' WHERE id=? AND state='running'"
                            if table == "jobs"
                            else "UPDATE deliveries SET state='uncertain',error='interrupted send; inspect before retrying' WHERE id=? AND state='sending'"
                        )
                        db.execute(query, (key,))
                except BusyError:
                    pass

    def conversation(self, channel: str, conversation: str, before: str) -> list[dict[str, str]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT role,text,at FROM messages WHERE channel=? AND conversation=? AND at<=? ORDER BY at DESC,id DESC LIMIT 20",
                (channel, conversation, before),
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
            health = {
                row[0]: decode(row[1])
                for row in db.execute("SELECT key,value FROM checkpoints WHERE key LIKE 'health:%'")
            }
        return {
            "jobs": jobs,
            "deliveries": deliveries,
            "counts": counts,
            "delivery_counts": delivery_counts,
            "delivery_failures": failures,
            "transports": health,
        }

    def compact(self) -> None:
        """Bound retained bodies; small identity tombstones continue preventing duplicate work."""
        with self.connect() as db:
            self.trim(db)

    @staticmethod
    def trim(db: sqlite3.Connection) -> None:
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
            previous = db.execute("SELECT channel,payload,command FROM deliveries WHERE id=?", (message.id,)).fetchone()
            if previous is not None:
                if tuple(previous) != (name, message.model_dump_json(), command.model_dump_json()):
                    raise Error("delivery id already used; inspect its receipt or use a new id for a different message")
                return
            self.reserve(db, 1, len(message.model_dump_json().encode()) + len(command.model_dump_json().encode()))
            db.execute(
                "INSERT INTO deliveries(id,job,channel,state,payload,command,due) VALUES (?,NULL,?,'pending',?,?,?)",
                (message.id, name, message.model_dump_json(), command.model_dump_json(), now()),
            )

    def retry(self, key: str, *, cancel: bool = False) -> None:
        with self.connect() as db:
            delivery = db.execute("SELECT state FROM deliveries WHERE id=?", (key,)).fetchone()
            if delivery is not None:
                if delivery[0] not in {"uncertain", "failed", "pending"}:
                    raise Error("only pending or unresolved deliveries can be retried or cancelled")
                db.execute(
                    "UPDATE deliveries SET state=?,error='',due=? WHERE id=?",
                    ("cancelled" if cancel else "pending", now(), key),
                )
                return
            row = db.execute("SELECT state,raw,prepared,payload FROM jobs WHERE id=?", (key,)).fetchone()
            if row is None or row[0] not in {"failed", "uncertain", "pending"}:
                raise Error("only pending or unresolved jobs can be retried or cancelled")
            state = "cancelled" if cancel else "prepared" if row[2] else "answered" if row[1] is not None else "pending"
            db.execute("UPDATE jobs SET state=?,error='',updated=? WHERE id=?", (state, now(), key))
            if cancel:
                db.execute("UPDATE deliveries SET state='cancelled',error='' WHERE job=? AND state='pending'", (key,))
                work = Work.model_validate_json(row[3])
                if work.kind == "schedule":
                    self.checkpoint(db, f"schedule:{work.assistant}:{work.slot}", {"end": work.end, "success": now()})


def admit(store: Store, event: EventIntent) -> None:
    """Idempotent handoff called while the committed record journal is still durable."""
    Queue(store).admit_event(event)
