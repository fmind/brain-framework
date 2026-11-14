"""Brain-local runtime queue, durable job state, active runs, wakes, messages and environment helpers."""

from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from collections.abc import Collection, Generator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal, cast
from unicodedata import category

from pydantic import AfterValidator, Field, JsonValue, ValidationError, field_validator

from bf.config import load
from bf.models import Error, Model, RunKind, decode, digest, encode, explain, timestamp
from bf.storage import QUEUE, BusyError, Store, appending, collecting, lock_file, private_dir

VERSION = 3
MAX_PENDING = 10_000
MAX_DATABASE = 256 << 20
BODY_BUDGET = 8 << 20
RETENTION = timedelta(days=35)
MAX_ATTEMPTS = 20
MAX_WAKES = 16
MAX_WAKE_DEPTH = 8
MAX_BATCH = 1000
LOG_LIMIT = 1 << 20
_ENTRY = b"\n== "
_CREATE = Lock()

# Variables/prefixes causing shells or runtimes to inject startup code; Git vars stay for hooks.
_S1 = ("BASH_ENV", "BASHOPTS", "ENV", "GCONV_PATH", "DOTNET_STARTUP_HOOKS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS")
_S2 = ("_JAVA_OPTIONS", "NODE_OPTIONS", "NODE_PATH", "RUBYOPT", "RUBYLIB", "PERL5OPT", "PERL5LIB", "PERLLIB")
_S3 = ("PHP_INI_SCAN_DIR", "PHPRC", "PS4", "SHELLOPTS", "ZDOTDIR")
_STARTUP = frozenset((*_S1, *_S2, *_S3))
_P1 = ("LD_", "DYLD_", "BASH_FUNC_", "PYTHON", "LUA_INIT", "LUA_PATH")
_P2 = ("LUA_CPATH", "R_PROFILE", "R_ENVIRON", "CORECLR_")
_PREFIXES = (*_P1, *_P2)


def now() -> str:
    return timestamp(datetime.now(UTC).isoformat())


class QueueFullError(Error):
    """Queue bounds reached; unresolved work must finish or clear before admitting more."""


def environment() -> dict[str, str]:
    """Caller environment without loader injection or relative PATH entries; never opens runtime DB."""
    env = {k: v for k, v in os.environ.items() if k not in _STARTUP and not k.startswith(_PREFIXES)}
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(entry for entry in env["PATH"].split(os.pathsep) if Path(entry).is_absolute())
    return env


def log_path(name: str) -> str:
    """A job's bounded local log path inside the brain; excluded from retrieval."""
    return f"logs/{name}.log"


def append_log(store: Store, name: str, heading: str, body: bytes = b"") -> None:
    """Append one `== heading ==` entry and drop oldest whole entries beyond LOG_LIMIT."""
    path = log_path(name)
    entry = f"== {heading} ==\n".encode() + body + (b"" if not body or body.endswith(b"\n") else b"\n")
    with appending(store, name):
        try:
            previous = store.read(path, LOG_LIMIT)
        except Error, OSError:
            previous = b""
        data = previous + entry
        if len(data) > LOG_LIMIT:
            cut = data.find(_ENTRY, len(data) - LOG_LIMIT)
            data = data[cut + 1 :] if cut >= 0 else entry[-LOG_LIMIT:]
        store.write(path, data, durable=False)


class Message(Model):
    """Validated inbound channel message; adapter owns sender authorization."""

    id: Annotated[str, Field(min_length=1, max_length=4096)]
    sender: Annotated[str, Field(min_length=1, max_length=4096)]
    conversation: Annotated[str, Field(min_length=1, max_length=4096)]
    time: str
    text: Annotated[str, Field(min_length=1, max_length=32_768)]
    reply_to: Annotated[str, Field(max_length=4096)] = ""
    cursor: Annotated[str, Field(max_length=4096)] = ""
    own: bool = False
    _time = field_validator("time")(timestamp)

    @field_validator("id", "sender", "conversation", "reply_to", "cursor")
    @classmethod
    def identity(cls, value: str) -> str:
        if any(category(c) in {"Cc", "Cf", "Cs"} for c in value) or (value and not value.strip()):
            raise ValueError("message identifiers cannot contain control characters")
        return value


class Batch(Model):
    messages: Annotated[list[Message], Field(max_length=MAX_BATCH)]


class Outgoing(Model):
    id: Annotated[str, Field(min_length=1, max_length=128)]
    text: Annotated[str, Field(min_length=1, max_length=4 << 20)]
    conversation: Annotated[str, Field(max_length=4096)] = ""
    reply_to: Annotated[str, Field(max_length=4096)] = ""
    _identity = field_validator("id", "conversation", "reply_to")(Message.identity)


class Receipt(Model):
    id: Annotated[str, Field(min_length=1, max_length=4096)]
    conversation: Annotated[str, Field(min_length=1, max_length=4096)]
    _identity = field_validator("id", "conversation")(Message.identity)


_TS = Annotated[str, AfterValidator(lambda v: timestamp(v) if v else "")]
_NN = Annotated[int, Field(ge=0)]


class JobState(Model):
    """Durable execution state and latest metrics of one configured job."""

    last_run: _TS = ""
    last_success: _TS = ""
    last_error: str = ""
    error_count: _NN = 0
    cursor: str = ""
    window_start: _TS = ""
    window_end: _TS = ""
    last_reconcile: _TS = ""
    reconcile: bool = False
    runs: _NN = 0
    missed: _NN = 0
    of: _NN = 0
    incomplete_runs: _NN = 0
    records: _NN = 0
    added: _NN = 0
    updated: _NN = 0
    unchanged: _NN = 0
    removed: _NN = 0
    forgotten: _NN = 0
    expired: _NN = 0
    requested_start: _TS = ""
    requested_end: _TS = ""
    elapsed_seconds: float = Field(default=0.0, ge=0)
    output_bytes: _NN = 0
    action: str = ""
    record: str = ""
    when_cursor: _TS = ""
    reports: dict[str, str] = Field(default_factory=dict)


_SCHEMA = """
CREATE TABLE job_state (job TEXT PRIMARY KEY, last_success TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '', error_count INTEGER NOT NULL DEFAULT 0, cursor TEXT NOT NULL DEFAULT '', window_end TEXT NOT NULL DEFAULT '', last_reconcile TEXT NOT NULL DEFAULT '', runs INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE active_runs (run_id TEXT PRIMARY KEY, job TEXT NOT NULL, kind TEXT NOT NULL, channel TEXT NOT NULL DEFAULT '', thread TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '', wake_depth INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running');
CREATE INDEX active_runs_job ON active_runs(job,status);
CREATE TABLE wakes (id TEXT PRIMARY KEY, job TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL DEFAULT '', due TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', wake_depth INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending', created TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX wake_idempotency ON wakes(run_id,idempotency_key) WHERE run_id!='' AND idempotency_key!='';
CREATE INDEX due_wakes ON wakes(state,due);
CREATE TABLE messages (id TEXT PRIMARY KEY, channel TEXT NOT NULL, conversation TEXT NOT NULL, sender TEXT NOT NULL DEFAULT '', role TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL, digest TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'done');
CREATE INDEX conversation_messages ON messages(channel,conversation,at);
CREATE TABLE receipts (id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL, channel TEXT NOT NULL, receipt_id TEXT NOT NULL, conversation TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL);
CREATE UNIQUE INDEX receipt_channel ON receipts(channel,receipt_id);
CREATE TABLE deliveries (id TEXT PRIMARY KEY, run_id TEXT NOT NULL DEFAULT '', job TEXT NOT NULL DEFAULT '', send_job TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, command TEXT NOT NULL, digest TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, idempotent INTEGER NOT NULL DEFAULT 0, due TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX ready_deliveries ON deliveries(state,due);
CREATE TABLE created_actions (run_id TEXT NOT NULL, slug TEXT NOT NULL, path TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY (run_id,slug));
CREATE TABLE events (id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX kind_events ON events(kind,source,at);
CREATE TABLE checkpoints (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_OBJECTS = "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"


@cache
def _expected() -> frozenset[tuple[str, str, str]]:
    """Exact current schema objects; foreign databases are preserved, never adapted."""
    db = sqlite3.connect(":memory:")
    try:
        for stmt in _SCHEMA.split(";"):
            if stmt.strip():
                db.execute(stmt)
        return frozenset(db.execute(_OBJECTS).fetchall())
    finally:
        db.close()


def check_version(version: int) -> None:
    """Refuse a runtime DB written by another release without migrating it."""
    if 0 < version < VERSION:
        raise Error("runtime database predates this release; preserve it and follow upgrade steps in the changelog")
    if version != VERSION:
        raise Error("unsupported runtime database version; preserve it and use its matching BF release")


def check_schema(connection: sqlite3.Connection) -> None:
    """Refuse a runtime DB whose schema differs from this release."""
    if frozenset(tuple(row) for row in connection.execute(_OBJECTS)) != _expected():
        raise Error("unexpected runtime database schema; preserve it and repair it")


def _identity(send_job: str, payload: str, command: str) -> str:
    return digest(encode([send_job, payload, command]))


def retry_policy(
    idempotent: bool, owned: bool, attempts: int, reason: str, *, not_started: bool = False
) -> tuple[str, str, str]:
    """Return `(state, error, due)` after a failed or interrupted delivery attempt."""
    if not idempotent and not not_started:
        return "uncertain", reason, ""
    if not idempotent and not owned:
        return "failed", f"{reason}; retry it with bf queue ID --retry", ""
    if attempts >= MAX_ATTEMPTS:
        return "failed", f"{reason}; stopped after {attempts} attempts; retry or cancel with bf queue", ""
    delay = min(300, 10 * 2 ** min(max(0, attempts - 1), 5))
    return "pending", reason, timestamp((datetime.now(UTC) + timedelta(seconds=delay)).isoformat())


def forget(store: Store, name: str) -> None:
    """Remove a finished run/delivery lock file while holding that lock."""
    state_store, leaf = lock_file(store, f"collect-{name}.lock")
    with suppress(Error, OSError):
        state_store.delete(leaf, durable=False)


def _active_run(db: sqlite3.Connection, run_id: str, msg: str) -> sqlite3.Row:
    sql = "SELECT job,wake_depth,status FROM active_runs WHERE run_id=?"
    if (run := db.execute(sql, (run_id,)).fetchone()) is None or run["status"] != "running":
        raise Error(msg)
    return run


class Queue:
    """Short SQLite transactions inside `<brain>/.bf/runtime.sqlite3`; never held across subprocess calls."""

    def __init__(self, store: Store) -> None:
        self.brain = self.store = store

    def exists(self) -> bool:
        try:
            return self.store.lstat(QUEUE).st_size > 0
        except FileNotFoundError:
            return False

    @contextmanager
    def _open(self, *, readonly: bool) -> Generator[sqlite3.Connection]:
        if not readonly:
            private_dir(self.store, ".bf")
            with _CREATE, self.store.parent(QUEUE) as (parent, name), suppress(FileExistsError):
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                os.close(fd)
                os.fsync(parent)
        info = self.store.lstat(QUEUE)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Error("runtime database must be a private regular file owned by you")
        for suffix in ("-wal", "-shm", "-journal"):
            with suppress(FileNotFoundError):
                self.store.fingerprint(QUEUE + suffix)
        target = (self.store.root / QUEUE).as_uri() + "?mode=ro" if readonly else self.store.root / QUEUE
        conn = sqlite3.connect(target, uri=readonly, timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA trusted_schema=OFF")
            if not readonly:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA synchronous=EXTRA")
            conn.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            if not readonly:
                ps = conn.execute("PRAGMA page_size").fetchone()[0]
                conn.execute(f"PRAGMA max_page_count={MAX_DATABASE // ps}").fetchall()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if not readonly and version == 0:
                if conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise Error("unknown runtime database format; preserve it and repair it")
                for stmt in _SCHEMA.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
                conn.execute(f"PRAGMA user_version={VERSION}")
            else:
                check_version(version)
            check_schema(conn)
            self._check_binding(conn, write=not readonly)
            yield conn
            if not readonly:
                conn.commit()
        except BaseException as error:
            with suppress(sqlite3.DatabaseError):
                conn.rollback()
            if isinstance(error, sqlite3.DatabaseError):
                if readonly and getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_READONLY_ROLLBACK:
                    raise Error("runtime database holds an interrupted write; run bf queue to recover it") from error
                raise Error(
                    "runtime database is unavailable; preserve files and check permissions or corruption"
                ) from error
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Generator[sqlite3.Connection]:
        """Read-only snapshot over `.bf/runtime.sqlite3`; never creates the file or takes write locks."""
        with self._open(readonly=True) as db:
            yield db

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        """Immediate write transaction; creates `.bf/runtime.sqlite3` privately if missing."""
        with self._open(readonly=False) as db:
            yield db

    def _rows(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        if not self.exists():
            return []
        with self.read() as db:
            return db.execute(sql, params).fetchall()

    def _check_binding(self, db: sqlite3.Connection, *, write: bool) -> None:
        try:
            name: str | None = load(self.brain).name
        except Error:
            name = None
        binding: dict[str, JsonValue] = {"path": str(self.brain.root), "name": name}
        row = db.execute("SELECT value FROM checkpoints WHERE key='binding'").fetchone()
        prev = decode(row[0]) if row is not None else None
        if isinstance(prev, dict) and (prev == binding or (name is None and prev.get("path") == binding["path"])):
            return
        if prev is not None and self.unresolved(db):
            raise Error("runtime queue holds unresolved work under a previous path or name; resolve via bf queue first")
        if write and name is not None:
            self.checkpoint(db, "binding", binding)

    @staticmethod
    def unresolved(db: sqlite3.Connection) -> bool:
        sql = "SELECT 1 FROM active_runs WHERE status IN ('running','uncertain') UNION ALL SELECT 1 FROM wakes WHERE state='pending' UNION ALL SELECT 1 FROM deliveries WHERE state NOT IN ('delivered','cancelled') LIMIT 1"
        return db.execute(sql).fetchone() is not None

    @staticmethod
    def checkpoint(db: sqlite3.Connection, key: str, value: dict[str, JsonValue]) -> None:
        db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?)", (key, encode(value).decode()))

    def progress(self, key: str) -> dict[str, JsonValue]:
        if not (rows := self._rows("SELECT value FROM checkpoints WHERE key=?", (key,))):
            return {}
        if not isinstance(value := decode(rows[0][0]), dict):
            raise Error("invalid runtime checkpoint; preserve the database and repair it")
        return cast("dict[str, JsonValue]", value)

    def set_progress(self, key: str, value: dict[str, JsonValue]) -> None:
        with self.connect() as db:
            self.checkpoint(db, key, value)

    @staticmethod
    def used(db: sqlite3.Connection) -> int:
        pages = db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]
        return pages * db.execute("PRAGMA page_size").fetchone()[0]

    @staticmethod
    def reserve(db: sqlite3.Connection, slots: int, size: int, *, limit: int = 0, reason: str = "") -> None:
        if slots:
            p = db.execute(
                "SELECT (SELECT count(*) FROM deliveries WHERE state NOT IN ('delivered','cancelled'))+(SELECT count(*) FROM wakes WHERE state='pending')"
            ).fetchone()[0]
            if p + slots > MAX_PENDING:
                raise QueueFullError("runtime queue has no unresolved-work slots; clear pending work first")
        ceiling = limit or (MAX_DATABASE * 3 // 8)
        if Queue.used(db) + size > ceiling:
            Queue.trim(db)
            if Queue.used(db) + size > ceiling:
                raise QueueFullError(reason or "runtime queue reached its size limit; resolve or clear pending work")

    def compact(self) -> None:
        with self.connect() as db:
            self.trim(db)

    @staticmethod
    def trim(db: sqlite3.Connection) -> None:
        horizon = timestamp((datetime.now(UTC) - RETENTION).isoformat())
        for sql in (
            "DELETE FROM deliveries WHERE state IN ('delivered','cancelled') AND due<?",
            "DELETE FROM receipts WHERE at<?",
            "DELETE FROM wakes WHERE state IN ('done','cancelled') AND created<?",
            "DELETE FROM active_runs WHERE status!='running' AND started_at<?",
            "DELETE FROM events WHERE at<?",
            "DELETE FROM created_actions WHERE created<? AND NOT EXISTS (SELECT 1 FROM active_runs WHERE run_id=created_actions.run_id AND status='running')",
        ):
            db.execute(sql, (horizon,))
        sql_ev = "UPDATE events SET data='{}' WHERE id IN (SELECT id FROM (SELECT id,sum(length(CAST(data AS BLOB))) OVER (ORDER BY at DESC,id DESC) AS b FROM events) WHERE b>?)"
        sql_del = "UPDATE deliveries SET payload='{}',command='{}' WHERE state IN ('delivered','cancelled') AND id IN (SELECT id FROM (SELECT id,sum(length(CAST(payload AS BLOB))+length(CAST(command AS BLOB))) OVER (ORDER BY due DESC,id DESC) AS b FROM deliveries WHERE state IN ('delivered','cancelled')) WHERE b>?)"
        sql_msg = "DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY at DESC LIMIT 10000) OR id IN (SELECT id FROM (SELECT id,sum(length(CAST(text AS BLOB))) OVER (ORDER BY at DESC,id DESC) AS b FROM messages) WHERE b>?)"
        for sql in (sql_ev, sql_del, sql_msg):
            db.execute(sql, (BODY_BUDGET,))

    def all_states(self, job: str = "") -> dict[str, dict[str, object]]:
        """Read stored job state(s) without creating `.bf/runtime.sqlite3` if absent."""
        out: dict[str, dict[str, object]] = {}
        for row in self._rows("SELECT job,data FROM job_state WHERE (?='' OR job=?) ORDER BY job", (job, job)):
            with suppress(ValidationError, Error):
                out[str(row["job"])] = JobState.model_validate(decode(row["data"])).model_dump(exclude_defaults=True)
        return out

    def state_of(self, job: str) -> dict[str, object]:
        return self.all_states(job).get(job, {})

    def remember(self, job: str, /, **values: object) -> dict[str, object]:
        """Validate and upsert one job's execution state inside `job_state`."""
        with self.connect() as db:
            row = db.execute("SELECT data FROM job_state WHERE job=?", (job,)).fetchone()
            cur: object = {}
            if row is not None:
                with suppress(Error):
                    cur = decode(row["data"])
            try:
                e = JobState.model_validate({**(cur if isinstance(cur, dict) else {}), **values})
            except ValidationError as error:
                raise Error(f"{job}: invalid job state: {explain(error)}") from error
            dumped = e.model_dump(exclude_defaults=True)
            head = (job, e.last_success, e.last_error, e.error_count, e.cursor)
            args = (*head, e.window_end, e.last_reconcile, e.runs, encode(dumped).decode())
            db.execute("INSERT OR REPLACE INTO job_state VALUES (?,?,?,?,?,?,?,?,?)", args)
        return dumped

    def start_run(
        self,
        job: str,
        kind: RunKind,
        *,
        run_id: str = "",
        channel: str = "",
        thread: str = "",
        target: str = "",
        wake_depth: int = 0,
    ) -> str:
        """Record a running invocation so `--run {{run}}` binds CLI verbs safely."""
        if run_id and (any(category(c) in {"Cc", "Cf", "Cs"} for c in run_id) or len(run_id) > 128):
            raise Error("invalid run id")
        if not 0 <= wake_depth <= MAX_WAKE_DEPTH:
            raise Error("invalid wake depth")
        rid = run_id or uuid.uuid4().hex
        args = (rid, job, kind, channel, thread, target, wake_depth, now())
        with self.connect() as db:
            db.execute("INSERT INTO active_runs VALUES (?,?,?,?,?,?,?,?,'running')", args)
        return rid

    def get_run(self, run_id: str, *, require_active: bool = False) -> dict[str, object]:
        if run_id and (rows := self._rows("SELECT * FROM active_runs WHERE run_id=?", (run_id,))):
            if require_active and rows[0]["status"] != "running":
                raise Error(f"run {run_id} is not active ({rows[0]['status']})")
            return dict(rows[0])
        raise Error("unknown run id")

    def finish_run(
        self, run_id: str, *, status: Literal["completed", "failed", "uncertain", "cancelled"] = "completed"
    ) -> None:
        if self.exists():
            with self.connect() as db:
                db.execute("UPDATE active_runs SET status=? WHERE run_id=?", (status, run_id))

    def record_action(self, run_id: str, slug: str, path: str) -> tuple[str, bool]:
        """Bind `bf action new --run` idempotently to an active run; returns `(path, created)`."""
        sql = "SELECT path FROM created_actions WHERE run_id=? AND slug=?"
        with self.connect() as db:
            _active_run(db, run_id, "bf action new --run requires an active run")
            if (existing := db.execute(sql, (run_id, slug)).fetchone()) is not None:
                return str(existing[0]), False
            db.execute("INSERT INTO created_actions VALUES (?,?,?,?)", (run_id, slug, path, now()))
            return path, True

    def request_wake(
        self,
        job: str,
        *,
        reason: str = "",
        due: str = "",
        run_id: str = "",
        idempotency_key: str = "",
        wake_depth: int | None = None,
    ) -> tuple[str, bool]:
        """Schedule a bounded wake with chain-depth cap and `(run_id, idempotency_key)` deduplication."""
        when, idem_sql = timestamp(due) if due else now(), "SELECT id FROM wakes WHERE run_id=? AND idempotency_key=?"
        with self.connect() as db:
            run = _active_run(db, run_id, "bf wake --run requires an active run") if run_id else None
            depth = wake_depth if wake_depth is not None else (int(run["wake_depth"]) + 1 if run else 0)
            if depth >= MAX_WAKE_DEPTH:
                raise Error(f"wake chain depth limit ({MAX_WAKE_DEPTH}) reached; refusing wake for {job}")
            if run_id and idempotency_key and (existing := db.execute(idem_sql, (run_id, idempotency_key)).fetchone()):
                return str(existing[0]), False
            if db.execute("SELECT count(*) FROM wakes WHERE state='pending'").fetchone()[0] >= MAX_WAKES:
                raise QueueFullError(f"wake limit ({MAX_WAKES}) reached; resolve pending wakes first")
            self.reserve(db, 1, len(reason.encode()) + 512)
            wake_id = uuid.uuid4().hex
            args = (wake_id, job, run_id, idempotency_key, when, reason, depth, now())
            db.execute("INSERT INTO wakes VALUES (?,?,?,?,?,?,?,'pending',?,'')", args)
            return wake_id, True

    def due_wakes(self, job: str = "", *, before: str = "") -> list[dict[str, object]]:
        sql = "SELECT * FROM wakes WHERE state='pending' AND due<=? AND (?='' OR job=?) ORDER BY due,created"
        return [dict(r) for r in self._rows(sql, (before or now(), job, job))]

    def claim_wake(self, wake_id: str) -> bool:
        with self.connect() as db:
            return (
                db.execute("UPDATE wakes SET state='claimed' WHERE id=? AND state='pending'", (wake_id,)).rowcount > 0
            )

    def finish_wake(
        self, wake_id: str, *, state: Literal["done", "failed", "cancelled"] = "done", error: str = ""
    ) -> None:
        with self.connect() as db:
            db.execute("UPDATE wakes SET state=?,error=? WHERE id=?", (state, error, wake_id))

    @staticmethod
    def event(db: sqlite3.Connection, key: str, kind: str, source: str, data: dict[str, JsonValue], at: str) -> None:
        encoded = encode(data).decode()
        checksum = digest(encoded.encode())
        if (prev := db.execute("SELECT digest FROM events WHERE id=?", (key,)).fetchone()) is not None and prev[
            0
        ] != checksum:
            raise Error("event id reused with different content; preserve runtime queue and check source")
        db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)", (key, kind, source, at, checksum, encoded))

    def record_event(self, kind: str, source: str, data: dict[str, JsonValue], *, key: str = "", at: str = "") -> str:
        eid, when = key or uuid.uuid4().hex, timestamp(at) if at else now()
        with self.connect() as db:
            self.reserve(db, 0, len(encode(data)) + 256)
            self.event(db, eid, kind, source, data, when)
        return eid

    def recent_events(
        self, kind: str, source: str = "", *, since: str = "", limit: int = 200
    ) -> list[dict[str, object]]:
        sql = "SELECT id,kind,source,at,data FROM events WHERE kind=? AND (?='' OR source=?) AND (?='' OR at>?) ORDER BY at ASC LIMIT ?"
        return [
            {**dict(r), "data": decode(r["data"])} for r in self._rows(sql, (kind, source, source, since, since, limit))
        ]

    def admit(
        self, channel: str, messages: list[Message], *, checkpoint: tuple[str, dict[str, JsonValue]] | None = None
    ) -> list[Message]:
        """Deduplicate and suppress echoes before committing inbound `emit: messages` reads."""
        admitted: list[Message] = []
        rcpt_sql = "SELECT 1 FROM receipts WHERE channel=? AND receipt_id=?"
        with self.connect() as db:
            for m in messages:
                key = f"{channel}:{m.id}"
                echoed = (
                    m.own
                    or db.execute("SELECT 1 FROM messages WHERE id=? AND role='assistant'", (key,)).fetchone()
                    or db.execute(rcpt_sql, (channel, m.id)).fetchone()
                )
                if echoed or db.execute("SELECT 1 FROM events WHERE id=?", (f"input:{key}",)).fetchone():
                    continue
                payload = m.model_dump(mode="json", exclude={"cursor"})
                encoded = encode(payload)
                self.reserve(db, 0, 2 * (len(encoded) + len(m.text.encode())))
                self.event(db, f"input:{key}", "channel.received", channel, payload, now())
                row_h = (key, channel, m.conversation, m.sender, "user")
                db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
                    (*row_h, m.time, m.text, digest(encoded), "pending"),
                )
                admitted.append(m)
            if checkpoint:
                self.checkpoint(db, *checkpoint)
            self.trim(db)
        return admitted

    def mark_message(self, channel: str, message_id: str, status: Literal["pending", "done", "echo"] = "done") -> None:
        with self.connect() as db:
            db.execute("UPDATE messages SET status=? WHERE id=?", (status, f"{channel}:{message_id}"))

    def pending_messages(self, channel: str = "") -> list[dict[str, object]]:
        sql = "SELECT * FROM messages WHERE role='user' AND status='pending' AND (?='' OR channel=?) ORDER BY at,id"
        return [dict(r) for r in self._rows(sql, (channel, channel))]

    def conversation(
        self, channel: str, conversation: str, before: str = "", *, exclude: str = ""
    ) -> list[dict[str, str]]:
        """Up to 20 earlier messages of one conversation within 32 KiB, oldest first."""
        sql = "SELECT role,sender,text,at FROM messages WHERE channel=? AND conversation=? AND at<=? AND id!=? ORDER BY at DESC,id DESC LIMIT 20"
        result, size = [], 0
        for row in self._rows(sql, (channel, conversation, before or now(), exclude)):
            if (size := size + len(row["text"].encode())) > 32_768:
                break
            result.append(dict(row))
        return list(reversed(result))

    def enqueue_delivery(
        self,
        send_job: str,
        message: Outgoing,
        command: list[str],
        *,
        idempotent: bool = False,
        run_id: str = "",
        job: str = "",
    ) -> tuple[str, bool]:
        """Queue an outbound message via a `send:` job cleanly and idempotently by `message.id`."""
        with self.connect() as db:
            if run_id:
                job = job or str(_active_run(db, run_id, "bf post --run requires an active run")["job"])
            payload, encoded = message.model_dump_json(), encode(command).decode()
            checksum = _identity(send_job, payload, encoded)
            if (prev := db.execute("SELECT digest FROM deliveries WHERE id=?", (message.id,)).fetchone()) is not None:
                if prev[0] != checksum:
                    raise Error("delivery id already used; inspect its receipt or use a new id for a different message")
                return message.id, False
            self.reserve(db, 1, 2 * (len(payload.encode()) + len(encoded.encode())))
            ts = now()
            head = (message.id, run_id, job, send_job, "pending", payload, encoded)
            sql = "INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            db.execute(sql, (*head, checksum, "", 0, int(idempotent), ts, ts, ts))
            return message.id, True

    def due_delivery(
        self, *, job: str = "", key: str = "", excluding: set[str] | None = None, skip: Collection[str] = ()
    ) -> dict[str, object] | None:
        sql = "SELECT * FROM deliveries WHERE state='pending' AND due<=? AND (?='' OR job=?) AND (?='' OR id=?) ORDER BY due,id LIMIT 10000"
        rows = self._rows(sql, (now(), job, job, key, key))
        row = next((r for r in rows if r["send_job"] not in (excluding or set()) and r["id"] not in skip), None)
        return dict(row) if row is not None else None

    def sending(self, key: str) -> bool:
        sql = "UPDATE deliveries SET state='sending',attempts=attempts+1,updated=? WHERE id=? AND state='pending'"
        with self.connect() as db:
            return db.execute(sql, (now(), key)).rowcount > 0

    def delivered(self, key: str, send_job: str, message: Outgoing, receipt: Receipt) -> None:
        ts, msg_key = now(), f"{send_job}:{receipt.id}"
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state='delivered',error='',due=?,updated=? WHERE id=?", (ts, ts, key))
            r_row = (msg_key, key, send_job, receipt.id, receipt.conversation, receipt.model_dump_json(), ts)
            m_head = (msg_key, send_job, receipt.conversation, send_job, "assistant", ts)
            db.execute("INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?)", r_row)
            db.execute(
                "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
                (*m_head, message.text, digest(message.text.encode()), "done"),
            )
            db.execute("UPDATE messages SET status='echo' WHERE id=? AND role='user' AND status='pending'", (msg_key,))
            self.trim(db)

    def delivery_failed(self, key: str, error: str, *, outcome: str = "uncertain", due: str = "") -> None:
        ts = now()
        sql = "UPDATE deliveries SET state=?,error=?,due=?,updated=? WHERE id=? AND state NOT IN ('delivered','cancelled')"
        with self.connect() as db:
            db.execute(sql, (outcome, error, due or ts, ts, key))

    def recover_attempts(self, key: str = "") -> None:
        """Mark killed `active_runs` and stale `sending` deliveries safely without replaying non-idempotent sends."""
        for (run_id,) in self._rows(
            "SELECT run_id FROM active_runs WHERE status='running' AND ? IN ('',run_id)", (key,)
        ):
            with suppress(BusyError), collecting(self.brain, f".run-{run_id}"):
                self.interrupted(run_id=run_id)
        for (delivery,) in self._rows("SELECT id FROM deliveries WHERE state='sending' AND ? IN ('',id)", (key,)):
            with suppress(BusyError), collecting(self.brain, f".delivery-{digest(delivery.encode())}"):
                self.interrupted(delivery=delivery)

    def interrupted(self, *, run_id: str = "", delivery: str = "") -> None:
        q = "SELECT job!='',idempotent,attempts FROM deliveries WHERE id=? AND state='sending'"
        with self.connect() as db:
            if run_id:
                db.execute("UPDATE active_runs SET status='uncertain' WHERE run_id=? AND status='running'", (run_id,))
            if (row := db.execute(q, (delivery,)).fetchone()) is not None:
                reason = "interrupted send" if row[1] else "interrupted send; inspect before retrying"
                outcome, error, due = retry_policy(bool(row[1]), bool(row[0]), int(row[2]), reason)
                ts = now()
                sql = "UPDATE deliveries SET state=?,error=?,due=?,updated=? WHERE id=? AND state='sending'"
                db.execute(sql, (outcome, error, due or ts, ts, delivery))

    def inspect(self, key: str) -> dict[str, object]:
        if self.exists():
            with self.read() as db:
                for sql in (
                    "SELECT * FROM deliveries WHERE id=?",
                    "SELECT * FROM wakes WHERE id=?",
                    "SELECT * FROM active_runs WHERE run_id=?",
                ):
                    if (row := db.execute(sql, (key,)).fetchone()) is not None:
                        out = dict(row)
                        if (
                            "send_job" in out
                            and (
                                rcpt := db.execute(
                                    "SELECT payload FROM receipts WHERE delivery_id=?", (key,)
                                ).fetchone()
                            )
                            is not None
                        ):
                            out["receipt"] = rcpt[0]
                        return out
        raise Error("unknown queue entry; use bf queue to list work")

    def retry(self, key: str, *, cancel: bool = False) -> None:
        """Retry or cancel an unresolved delivery, wake or interrupted run."""
        self.recover_attempts(key)
        lock = ""
        sql_w = "UPDATE wakes SET state=?,error='',due=? WHERE id=?"
        with self.connect() as db:
            if (row := db.execute("SELECT state FROM deliveries WHERE id=?", (key,)).fetchone()) is not None:
                if row[0] not in {"uncertain", "failed", "pending"}:
                    raise Error("delivery cannot be retried or cancelled in its current state")
                ts = now()
                sql = "UPDATE deliveries SET state=?,error='',due=?,attempts=0,updated=? WHERE id=?"
                db.execute(sql, ("cancelled" if cancel else "pending", ts, ts, key))
                lock = f".delivery-{digest(key.encode())}"
            elif (wake := db.execute("SELECT state FROM wakes WHERE id=?", (key,)).fetchone()) is not None:
                if wake[0] not in {"failed", "pending"}:
                    raise Error("wake cannot be retried or cancelled in its current state")
                db.execute(sql_w, ("cancelled" if cancel else "pending", now(), key))
            elif (run := db.execute("SELECT status FROM active_runs WHERE run_id=?", (key,)).fetchone()) is not None:
                if not cancel or run[0] != "uncertain":
                    raise Error("only uncertain runs can be cleared with --cancel")
                db.execute("UPDATE active_runs SET status='cancelled' WHERE run_id=?", (key,))
                lock = f".run-{key}"
            else:
                raise Error("unknown queue entry; use bf queue to list work")
        if cancel and lock:
            with suppress(BusyError), collecting(self.brain, lock):
                forget(self.brain, lock)

    @staticmethod
    def _status_counts(db: sqlite3.Connection) -> dict[str, int | str | None]:
        d = dict(db.execute("SELECT state,count(*) FROM deliveries GROUP BY state").fetchall())
        r = dict(db.execute("SELECT status,count(*) FROM active_runs GROUP BY status").fetchall())
        w = dict(db.execute("SELECT state,count(*) FROM wakes GROUP BY state").fetchall())
        sql_old = "SELECT min(created) FROM (SELECT created FROM deliveries WHERE state IN ('pending','sending') UNION ALL SELECT created FROM wakes WHERE state='pending')"
        oldest = db.execute(sql_old).fetchone()[0]
        return {
            "pending": int(d.get("pending", 0)) + int(d.get("sending", 0)),
            "failed": int(d.get("failed", 0)) + int(w.get("failed", 0)),
            "uncertain": int(d.get("uncertain", 0)) + int(r.get("uncertain", 0)),
            "wakes": int(w.get("pending", 0)),
            "active_runs": int(r.get("running", 0)),
            "oldest_pending": str(oldest) if oldest else None,
        }

    def status(self) -> dict[str, int | str | None]:
        """Queue summary matching `replies.STATUS["queue"]`; never creates `.bf/runtime.sqlite3`."""
        if not self.exists():
            return {"pending": 0, "failed": 0, "uncertain": 0, "wakes": 0, "active_runs": 0, "oldest_pending": None}
        with self.read() as db:
            return self._status_counts(db)

    def failing(self) -> tuple[list[str], list[str]]:
        """Return `(failing_jobs, failing_transports)` without creating the runtime DB."""
        if not self.exists():
            return [], []
        q1 = "SELECT DISTINCT job FROM active_runs WHERE status='uncertain' AND job!=''"
        q2 = "SELECT DISTINCT job FROM deliveries WHERE state IN ('failed','uncertain') AND job!=''"
        qt = "SELECT DISTINCT send_job FROM deliveries WHERE state NOT IN ('delivered','cancelled') AND (state='uncertain' OR error!='')"
        with self.read() as db:
            jobs = {r[0] for r in db.execute(q1)} | {r[0] for r in db.execute(q2)}
            transports = [r[0] for r in db.execute(qt)]
        return sorted(jobs), sorted(transports)

    def report(self, *, limit: int = 50) -> dict[str, object]:
        """Inspectable queue snapshot for `bf queue`."""
        if not self.exists():
            return {"status": self.status(), "active_runs": [], "wakes": [], "deliveries": [], "transports": {}}
        q_w = "SELECT * FROM wakes WHERE state IN ('pending','claimed','failed') ORDER BY due LIMIT ?"
        q_d = "SELECT id,run_id,job,send_job,state,attempts,due,error FROM deliveries WHERE state NOT IN ('delivered','cancelled') ORDER BY due LIMIT ?"
        q_h = "SELECT key,value FROM checkpoints WHERE key LIKE 'health:%'"
        with self.read() as db:
            runs = [dict(r) for r in db.execute("SELECT * FROM active_runs ORDER BY started_at DESC LIMIT ?", (limit,))]
            wakes = [dict(r) for r in db.execute(q_w, (limit,))]
            deliveries = [dict(r) for r in db.execute(q_d, (limit,))]
            health = {k: v for k, raw in db.execute(q_h) if isinstance(v := decode(raw), dict) and v.get("error")}
            summary = self._status_counts(db)
        return {"status": summary, "active_runs": runs, "wakes": wakes, "deliveries": deliveries, "transports": health}
