"""Brain-local runtime queue, durable job state, active runs, wakes, messages and environment helpers."""

from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from collections.abc import Collection, Generator
from contextlib import contextmanager, nullcontext, suppress
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal, cast
from unicodedata import category

from pydantic import AfterValidator, Field, JsonValue, TypeAdapter, ValidationError, field_validator

from bf.config import load
from bf.models import CACHE, Error, Model, RunKind, decode, digest, encode, explain, timestamp
from bf.storage import QUEUE, BusyError, Store, appending, collecting, lock_file, ping_wakeup, private_dir

VERSION, MAX_PENDING, MAX_DATABASE, BODY_BUDGET, INCIDENT_REMIND = 4, 10_000, 256 << 20, 8 << 20, 21_600
MAX_ATTEMPTS, MAX_WAKES, MAX_WAKE_DEPTH, MAX_BATCH, LOG_LIMIT = 20, 16, 8, 1000, 1 << 20
RETENTION, _ENTRY, _CREATE = timedelta(days=35), b"\n== ", Lock()

# Variables/prefixes causing shells or runtimes to inject startup code; Git vars stay for hooks.
_S1 = "BASH_ENV BASHOPTS ENV GCONV_PATH DOTNET_STARTUP_HOOKS JAVA_TOOL_OPTIONS PHPRC JDK_JAVA_OPTIONS _JAVA_OPTIONS"
_S2 = "NODE_OPTIONS NODE_PATH RUBYOPT RUBYLIB PERL5OPT PERL5LIB PERLLIB PHP_INI_SCAN_DIR PS4 SHELLOPTS ZDOTDIR"
_STARTUP = frozenset(f"{_S1} {_S2}".split())
_P1 = ("LD_", "DYLD_", "BASH_FUNC_", "PYTHON", "LUA_INIT", "LUA_PATH", "LUA_CPATH")
_PREFIXES = (*_P1, "R_PROFILE", "R_ENVIRON", "CORECLR_")


def now(offset: timedelta = timedelta()) -> str:
    return timestamp((datetime.now(UTC) + offset).isoformat())


class QueueFullError(Error):
    """Queue bounds reached; unresolved work must finish or clear before admitting more."""


def environment() -> dict[str, str]:
    """Caller environment without loader injection or relative PATH entries; never opens runtime DB."""
    env = {k: v for k, v in os.environ.items() if k not in _STARTUP and not k.startswith(_PREFIXES)}
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if Path(p).is_absolute())
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


_S4 = Annotated[str, Field(max_length=4096)]
_R4 = Annotated[str, Field(min_length=1, max_length=4096)]
_NN = Annotated[int, Field(ge=0)]
_TS = Annotated[str, AfterValidator(lambda v: timestamp(v) if v else "")]


class Message(Model):
    """Validated inbound channel message; adapter owns sender authorization."""

    id: _R4
    sender: _R4
    conversation: _R4
    time: str
    text: Annotated[str, Field(min_length=1, max_length=32_768)]
    reply_to: _S4 = ""
    cursor: _S4 = ""
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
    conversation: _S4 = ""
    reply_to: _S4 = ""
    _identity = field_validator("id", "conversation", "reply_to")(Message.identity)


class Receipt(Model):
    id: _R4
    conversation: _R4
    _identity = field_validator("id", "conversation")(Message.identity)


def parse_messages(decoded: object, label: str, *, allow_single: bool = False) -> tuple[list[Message], str]:
    """Validate a JSON message list, `{messages, cursor?}` batch, or optional single message."""
    if isinstance(decoded, list):
        msgs = TypeAdapter(list[Message]).validate_python(decoded[: MAX_BATCH + 1])
        if len(msgs) > MAX_BATCH:
            raise Error(f"{label} exceeds {MAX_BATCH} messages")
        return msgs, ""
    if isinstance(decoded, dict) and (not allow_single or "messages" in decoded):
        batch = Batch.model_validate({k: v for k, v in decoded.items() if k != "cursor"})
        return batch.messages, str(decoded.get("cursor") or "")
    if allow_single:
        return [Message.model_validate(decoded)], ""
    raise Error(f"{label}: emit: messages expects JSON array or {{messages: [...]}}")


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
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, at TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}');
CREATE INDEX kind_events ON events(kind,source,seq);
CREATE TABLE item_checkpoints (ref TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', observed TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL DEFAULT '');
CREATE TABLE event_cursors (job TEXT PRIMARY KEY, cursor_seq INTEGER NOT NULL DEFAULT 0, firings INTEGER NOT NULL DEFAULT 0, last_fired_at TEXT NOT NULL DEFAULT '');
CREATE TABLE incidents (job TEXT PRIMARY KEY, opened_at TEXT NOT NULL, last_alert_at TEXT NOT NULL, last_error TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1);
CREATE TABLE checkpoints (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_OBJECTS = "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
_ZERO_KEYS = ("pending", "failed", "uncertain", "wakes", "active_runs", "incidents")
_ZERO_STATUS: dict[str, int | str | None] = {**dict.fromkeys(_ZERO_KEYS, 0), "oldest_pending": None}


@cache
def _expected() -> frozenset[tuple[str, str, str]]:
    """Exact current schema objects; foreign databases are preserved, never adapted."""
    db = sqlite3.connect(":memory:")
    try:
        for stmt in filter(str.strip, _SCHEMA.split(";")):
            db.execute(stmt)
        return frozenset(db.execute(_OBJECTS).fetchall())
    finally:
        db.close()


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
    return "pending", reason, now(timedelta(seconds=min(300, 10 * 2 ** min(max(0, attempts - 1), 5))))


def forget(store: Store, name: str) -> None:
    """Remove a finished run/delivery lock file while holding that lock."""
    state_store, leaf = lock_file(store, f"collect-{name}.lock")
    with suppress(Error, OSError):
        state_store.delete(leaf, durable=False)


def _active_run(db: sqlite3.Connection, run_id: str, msg: str) -> sqlite3.Row:
    row = db.execute("SELECT job,wake_depth,status FROM active_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None or row["status"] != "running":
        raise Error(msg)
    return row


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
                os.close(os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent))
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
                ps = conn.execute("PRAGMA page_size").fetchone()[0]
                conn.execute(f"PRAGMA max_page_count={MAX_DATABASE // ps}").fetchall()
            conn.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if not readonly and version == 0:
                if conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise Error("unknown runtime database format; preserve it and repair it")
                for stmt in filter(str.strip, _SCHEMA.split(";")):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version={VERSION}")
                self.checkpoint(conn, "created", {"at": now()})
            elif 0 < version < VERSION:
                raise Error(
                    "runtime database predates this release; preserve it and follow upgrade steps in the changelog"
                )
            elif version != VERSION:
                raise Error("unsupported runtime database version; preserve it and use its matching BF release")
            if frozenset(tuple(row) for row in conn.execute(_OBJECTS)) != _expected():
                raise Error("unexpected runtime database schema; preserve it and repair it")
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

    def _dicts(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        return [dict(r) for r in self._rows(sql, params)]

    def _exec(self, sql: str, params: tuple[object, ...] = ()) -> int:
        with self.connect() as db:
            return db.execute(sql, params).rowcount

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
        sql = (
            "SELECT 1 FROM active_runs WHERE status IN ('running','uncertain') "
            "UNION ALL SELECT 1 FROM wakes WHERE state='pending' "
            "UNION ALL SELECT 1 FROM deliveries WHERE state NOT IN ('delivered','cancelled') LIMIT 1"
        )
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
        p = db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]
        return p * db.execute("PRAGMA page_size").fetchone()[0]

    @staticmethod
    def reserve(db: sqlite3.Connection, slots: int, size: int, *, limit: int = 0, reason: str = "") -> None:
        q = "SELECT (SELECT count(*) FROM deliveries WHERE state NOT IN ('delivered','cancelled'))+(SELECT count(*) FROM wakes WHERE state='pending')"
        if slots and db.execute(q).fetchone()[0] + slots > MAX_PENDING:
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
        horizon = now(-RETENTION)
        for sql in (
            "DELETE FROM deliveries WHERE state IN ('delivered','cancelled') AND due<?",
            "DELETE FROM receipts WHERE at<?",
            "DELETE FROM wakes WHERE state IN ('done','cancelled') AND created<?",
            "DELETE FROM active_runs WHERE status!='running' AND started_at<?",
            "DELETE FROM events WHERE at<?",
            "DELETE FROM created_actions WHERE created<? AND NOT EXISTS (SELECT 1 FROM active_runs WHERE run_id=created_actions.run_id AND status='running')",
        ):
            db.execute(sql, (horizon,))
        for sql in (
            "UPDATE events SET data='{}' WHERE seq IN (SELECT seq FROM (SELECT seq,sum(length(CAST(data AS BLOB))) OVER (ORDER BY seq DESC) AS b FROM events) WHERE b>?)",
            "UPDATE deliveries SET payload='{}',command='{}' WHERE state IN ('delivered','cancelled') AND id IN (SELECT id FROM (SELECT id,sum(length(CAST(payload AS BLOB))+length(CAST(command AS BLOB))) OVER (ORDER BY due DESC,id DESC) AS b FROM deliveries WHERE state IN ('delivered','cancelled')) WHERE b>?)",
            "DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY at DESC LIMIT 10000) OR id IN (SELECT id FROM (SELECT id,sum(length(CAST(text AS BLOB))) OVER (ORDER BY at DESC,id DESC) AS b FROM messages) WHERE b>?)",
        ):
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
            cur = decode(row["data"]) if row is not None else {}
            try:
                e = JobState.model_validate({**(cur if isinstance(cur, dict) else {}), **values})
            except ValidationError as error:
                raise Error(f"{job}: invalid job state: {explain(error)}") from error
            dumped = e.model_dump(exclude_defaults=True)
            vals = (job, e.last_success, e.last_error, e.error_count, e.cursor, e.window_end, e.last_reconcile)
            sql = "INSERT OR REPLACE INTO job_state VALUES (?,?,?,?,?,?,?,?,?)"
            db.execute(sql, (*vals, e.runs, encode(dumped).decode()))
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
        rid, sql = run_id or uuid.uuid4().hex, "INSERT INTO active_runs VALUES (?,?,?,?,?,?,?,?,'running')"
        self._exec(sql, (rid, job, kind, channel, thread, target, wake_depth, now()))
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
            self._exec("UPDATE active_runs SET status=? WHERE run_id=?", (status, run_id))

    def record_action(self, run_id: str, slug: str, path: str) -> tuple[str, bool]:
        """Bind `bf action new --run` idempotently to an active run; returns `(path, created)`."""
        with self.connect() as db:
            _active_run(db, run_id, "bf action new --run requires an active run")
            if row := db.execute(
                "SELECT path FROM created_actions WHERE run_id=? AND slug=?", (run_id, slug)
            ).fetchone():
                return str(row[0]), False
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
        when = timestamp(due) if due else now()
        with self.connect() as db:
            run = _active_run(db, run_id, "bf wake --run requires an active run") if run_id else None
            depth = wake_depth if wake_depth is not None else (int(run["wake_depth"]) + 1 if run else 0)
            if depth >= MAX_WAKE_DEPTH:
                raise Error(f"wake chain depth limit ({MAX_WAKE_DEPTH}) reached; refusing wake for {job}")
            iq = "SELECT id FROM wakes WHERE run_id=? AND idempotency_key=?"
            if run_id and idempotency_key and (row := db.execute(iq, (run_id, idempotency_key)).fetchone()):
                return str(row[0]), False
            if db.execute("SELECT count(*) FROM wakes WHERE state='pending'").fetchone()[0] >= MAX_WAKES:
                raise QueueFullError(f"wake limit ({MAX_WAKES}) reached; resolve pending wakes first")
            self.reserve(db, 1, len(reason.encode()) + 512)
            wid, sql = uuid.uuid4().hex, "INSERT INTO wakes VALUES (?,?,?,?,?,?,?,'pending',?,'')"
            db.execute(sql, (wid, job, run_id, idempotency_key, when, reason, depth, now()))
            return wid, True

    def due_wakes(self, job: str = "", *, before: str = "") -> list[dict[str, object]]:
        sql = "SELECT * FROM wakes WHERE state='pending' AND due<=? AND (?='' OR job=?) ORDER BY due,created"
        return self._dicts(sql, (before or now(), job, job))

    def claim_wake(self, wake_id: str) -> bool:
        return self._exec("UPDATE wakes SET state='claimed' WHERE id=? AND state='pending'", (wake_id,)) > 0

    def finish_wake(
        self, wake_id: str, *, state: Literal["done", "failed", "cancelled"] = "done", error: str = ""
    ) -> None:
        self._exec("UPDATE wakes SET state=?,error=? WHERE id=?", (state, error, wake_id))

    @staticmethod
    def event(db: sqlite3.Connection, meta: tuple[str, str, str, str, str, str], data: dict[str, JsonValue]) -> None:
        key, at, kind, source, ref, summary = meta
        encoded = encode(data).decode()
        if (prev := db.execute("SELECT data FROM events WHERE id=?", (key,)).fetchone()) is not None:
            if prev[0] != encoded:
                raise Error("event id reused with different content; preserve runtime queue and check source")
            return
        db.execute(
            "INSERT INTO events (id,at,kind,source,ref,summary,data) VALUES (?,?,?,?,?,?,?)",
            (key, at, kind, source, ref[:1024], summary[:512], encoded),
        )

    def record_event(
        self,
        kind: str,
        source: str,
        data: dict[str, JsonValue],
        *,
        key: str = "",
        at: str = "",
        ref: str = "",
        summary: str = "",
    ) -> str:
        eid, when = key or uuid.uuid4().hex, timestamp(at) if at else now()
        with self.connect() as db:
            self.reserve(db, 0, len(encode(data)) + 256)
            self.event(db, (eid, when, kind, source, ref, summary), data)
        return eid

    def recent_events(
        self, kind: str, source: str = "", *, since: str = "", after_seq: int = 0, limit: int = 200
    ) -> list[dict[str, object]]:
        sql = "SELECT seq,id,at,kind,source,ref,summary,data FROM events WHERE kind=? AND (?='' OR source=?) AND seq>? AND (?='' OR at>?) ORDER BY seq ASC LIMIT ?"
        rows = self._rows(sql, (kind, source, source, after_seq, since, since, limit))
        return [{**dict(r), "data": decode(r["data"])} for r in rows]

    def events_after(
        self, after_seq: int, kinds: Collection[str], sources: Collection[str] = (), *, limit: int = 200
    ) -> list[dict[str, object]]:
        if not (klist := sorted(set(kinds))):
            return []
        slist, kq = sorted(set(sources)), ",".join("?" * len(klist))
        extra = f" AND source IN ({','.join('?' * len(slist))})" if slist else ""
        sql = f"SELECT seq,id,at,kind,source,ref,summary,data FROM events WHERE seq>? AND kind IN ({kq}){extra} ORDER BY seq ASC LIMIT ?"  # noqa: S608
        return [{**dict(r), "data": decode(r["data"])} for r in self._rows(sql, (after_seq, *klist, *slist, limit))]

    def event_cursor(self, job: str) -> tuple[int, int, str]:
        rows = self._rows("SELECT cursor_seq,firings,last_fired_at FROM event_cursors WHERE job=?", (job,))
        return (int(rows[0][0]), int(rows[0][1]), str(rows[0][2])) if rows else (0, 0, "")

    def set_event_cursor(self, job: str, cursor_seq: int, firings: int, last_fired_at: str) -> None:
        self._exec("INSERT OR REPLACE INTO event_cursors VALUES (?,?,?,?)", (job, cursor_seq, firings, last_fired_at))

    def admit(
        self, channel: str, messages: list[Message], *, checkpoint: tuple[str, dict[str, JsonValue]] | None = None
    ) -> list[Message]:
        """Deduplicate and suppress echoes before committing inbound `emit: messages` reads."""
        admitted: list[Message] = []
        dup = "SELECT 1 FROM messages WHERE id=? AND role='assistant' UNION ALL "
        dup += (
            "SELECT 1 FROM receipts WHERE channel=? AND receipt_id=? UNION ALL SELECT 1 FROM events WHERE id=? LIMIT 1"
        )
        with self.connect() as db:
            for m in messages:
                key = f"{channel}:{m.id}"
                if m.own or db.execute(dup, (key, channel, m.id, f"input:{key}")).fetchone():
                    continue
                payload = m.model_dump(mode="json", exclude={"cursor"})
                encoded, summary = encode(payload), " ".join(f"{m.sender}: {m.text}".split())[:512]
                self.reserve(db, 0, 2 * (len(encoded) + len(m.text.encode())))
                self.event(db, (f"input:{key}", now(), "message.received", channel, key, summary), payload)
                row = (key, channel, m.conversation, m.sender, "user", m.time, m.text, digest(encoded), "pending")
                db.execute("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?)", row)
                admitted.append(m)
            if checkpoint:
                self.checkpoint(db, *checkpoint)
            self.trim(db)
        if admitted:
            ping_wakeup(self.brain)
        return admitted

    def mark_message(self, channel: str, message_id: str, status: Literal["pending", "done", "echo"] = "done") -> None:
        self._exec("UPDATE messages SET status=? WHERE id=?", (status, f"{channel}:{message_id}"))

    def pending_messages(self, channel: str = "") -> list[dict[str, object]]:
        sql = "SELECT * FROM messages WHERE role='user' AND status='pending' AND (?='' OR channel=?) ORDER BY at,id"
        return self._dicts(sql, (channel, channel))

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
        db: sqlite3.Connection | None = None,
    ) -> tuple[str, bool]:
        """Queue an outbound message via a `send:` job cleanly and idempotently by `message.id`."""
        with nullcontext(db) if db is not None else self.connect() as conn:
            if run_id:
                job = job or str(_active_run(conn, run_id, "bf post --run requires an active run")["job"])
            payload, encoded = message.model_dump_json(), encode(command).decode()
            checksum = digest(encode([send_job, payload, encoded]))
            if (prev := conn.execute("SELECT digest FROM deliveries WHERE id=?", (message.id,)).fetchone()) is not None:
                if prev[0] != checksum:
                    raise Error("delivery id already used; inspect its receipt or use a new id for a different message")
                return message.id, False
            self.reserve(conn, 1, 2 * (len(payload.encode()) + len(encoded.encode())))
            ts, head = now(), (message.id, run_id, job, send_job, "pending", payload, encoded, checksum)
            sql = "INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            conn.execute(sql, (*head, "", 0, int(idempotent), ts, ts, ts))
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
        return self._exec(sql, (now(), key)) > 0

    def delivered(self, key: str, send_job: str, message: Outgoing, receipt: Receipt) -> None:
        ts, msg_key, conv, txt = now(), f"{send_job}:{receipt.id}", receipt.conversation, message.text
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state='delivered',error='',due=?,updated=? WHERE id=?", (ts, ts, key))
            r_row = (msg_key, key, send_job, receipt.id, conv, receipt.model_dump_json(), ts)
            m_row = (msg_key, send_job, conv, send_job, "assistant", ts, txt, digest(txt.encode()), "done")
            db.execute("INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?)", r_row)
            db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?)", m_row)
            db.execute("UPDATE messages SET status='echo' WHERE id=? AND role='user' AND status='pending'", (msg_key,))
            self.trim(db)

    def delivery_failed(self, key: str, error: str, *, outcome: str = "uncertain", due: str = "") -> None:
        ts = now()
        sql = "UPDATE deliveries SET state=?,error=?,due=?,updated=? WHERE id=? AND state NOT IN ('delivered','cancelled')"
        self._exec(sql, (outcome, error, due or ts, ts, key))

    def recover_attempts(self, key: str = "") -> None:
        """Mark killed `active_runs` and stale `sending` deliveries safely without replaying non-idempotent sends."""
        rq = "SELECT run_id FROM active_runs WHERE status='running' AND ? IN ('',run_id)"
        for (run_id,) in self._rows(rq, (key,)):
            with suppress(BusyError), collecting(self.brain, f".run-{run_id}"):
                self.interrupted(run_id=run_id)
        for (delivery,) in self._rows("SELECT id FROM deliveries WHERE state='sending' AND ? IN ('',id)", (key,)):
            with suppress(BusyError), collecting(self.brain, f".delivery-{digest(delivery.encode())}"):
                self.interrupted(delivery=delivery)

    def interrupted(self, *, run_id: str = "", delivery: str = "") -> None:
        with self.connect() as db:
            if run_id:
                db.execute("UPDATE active_runs SET status='uncertain' WHERE run_id=? AND status='running'", (run_id,))
            q = "SELECT job!='',idempotent,attempts FROM deliveries WHERE id=? AND state='sending'"
            if (row := db.execute(q, (delivery,)).fetchone()) is not None:
                reason = "interrupted send" if row[1] else "interrupted send; inspect before retrying"
                outcome, error, due = retry_policy(bool(row[1]), bool(row[0]), int(row[2]), reason)
                ts = now()
                sql = "UPDATE deliveries SET state=?,error=?,due=?,updated=? WHERE id=? AND state='sending'"
                db.execute(sql, (outcome, error, due or ts, ts, delivery))

    def inspect(self, key: str) -> dict[str, object]:
        if self.exists():
            with self.read() as db:
                for tbl, col in (("deliveries", "id"), ("wakes", "id"), ("active_runs", "run_id")):
                    if (row := db.execute(f"SELECT * FROM {tbl} WHERE {col}=?", (key,)).fetchone()) is not None:  # noqa: S608
                        out = dict(row)
                        rq = "SELECT payload FROM receipts WHERE delivery_id=?"
                        if "send_job" in out and (rcpt := db.execute(rq, (key,)).fetchone()):
                            out["receipt"] = rcpt[0]
                        return out
        raise Error("unknown queue entry; use bf queue to list work")

    def retry(self, key: str, *, cancel: bool = False) -> None:
        """Retry or cancel an unresolved delivery, wake or interrupted run."""
        self.recover_attempts(key)
        lock, target = "", "cancelled" if cancel else "pending"
        with self.connect() as db:
            if (row := db.execute("SELECT state FROM deliveries WHERE id=?", (key,)).fetchone()) is not None:
                if row[0] not in {"uncertain", "failed", "pending"}:
                    raise Error("delivery cannot be retried or cancelled in its current state")
                ts = now()
                sql = "UPDATE deliveries SET state=?,error='',due=?,attempts=0,updated=? WHERE id=?"
                db.execute(sql, (target, ts, ts, key))
                lock = f".delivery-{digest(key.encode())}"
            elif (wake := db.execute("SELECT state FROM wakes WHERE id=?", (key,)).fetchone()) is not None:
                if wake[0] not in {"failed", "pending"}:
                    raise Error("wake cannot be retried or cancelled in its current state")
                db.execute("UPDATE wakes SET state=?,error='',due=? WHERE id=?", (target, now(), key))
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
        qs = (
            "SELECT state,count(*) FROM deliveries GROUP BY state",
            "SELECT status,count(*) FROM active_runs GROUP BY status",
            "SELECT state,count(*) FROM wakes GROUP BY state",
        )
        d, r, w = ({k: int(v) for k, v in db.execute(q).fetchall()} for q in qs)
        inc = int(db.execute("SELECT count(*) FROM incidents").fetchone()[0])
        oq = "SELECT min(created) FROM (SELECT created FROM deliveries WHERE state IN ('pending','sending') UNION ALL SELECT created FROM wakes WHERE state='pending')"
        oldest = db.execute(oq).fetchone()[0]
        p, f = d.get("pending", 0) + d.get("sending", 0), d.get("failed", 0) + w.get("failed", 0)
        u = d.get("uncertain", 0) + r.get("uncertain", 0)
        vals = (p, f, u, w.get("pending", 0), r.get("running", 0), inc)
        return {**dict(zip(_ZERO_KEYS, vals, strict=True)), "oldest_pending": str(oldest) if oldest else None}

    def status(self) -> dict[str, int | str | None]:
        """Queue summary matching `replies.STATUS["queue"]`; never creates `.bf/runtime.sqlite3`."""
        if not self.exists():
            return dict(_ZERO_STATUS)
        with self.read() as db:
            return self._status_counts(db)

    def _alert_incident(self, db: sqlite3.Connection, job: str, text_tail: str) -> bool:
        try:
            config = load(self.brain)
        except Error, OSError, UnicodeError:
            return False
        post = config.defaults.post
        if not (post and post != job and (target := config.jobs.get(post)) and target.enabled and target.send):
            return False
        msg = Outgoing(id=f"incident:{uuid.uuid4().hex}", text=f"[{config.name}] {job} {text_tail}")
        with suppress(QueueFullError):
            self.enqueue_delivery(post, msg, target.send, idempotent=target.idempotent, job=job, db=db)
            return True
        return False

    def open_incident(self, job: str, error: str, *, at: str = "") -> None:
        """Record a job failure incident and enqueue open/6h reminder alerts when defaults.post is enabled."""
        ts, summary = timestamp(at) if at else now(), " ".join(error.split())[:512] or f"job {job} failed"
        with self.connect() as db:
            row = db.execute("SELECT opened_at,last_alert_at,count FROM incidents WHERE job=?", (job,)).fetchone()
            count, last_alert = (int(row[2]) + 1, str(row[1])) if row else (1, "")
            gap = (datetime.fromisoformat(ts) - datetime.fromisoformat(last_alert)).total_seconds() if last_alert else 0
            alert_at = last_alert
            tail = f"{'failed' if row is None else f'still failing ({count} failures)'}: {summary}"
            if (row is None or not last_alert or gap >= INCIDENT_REMIND) and self._alert_incident(db, job, tail):
                alert_at = ts
            sql = "INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?)"
            db.execute(sql, (job, row[0] if row else ts, alert_at, summary, count))

    def resolve_incident(self, job: str) -> None:
        """Close any active incident for `job` and enqueue one recovery alert if alerted."""
        if not self.exists() or not self._rows("SELECT 1 FROM incidents WHERE job=?", (job,)):
            return
        with self.connect() as db:
            if (row := db.execute("SELECT last_alert_at FROM incidents WHERE job=?", (job,)).fetchone()) is None:
                return
            db.execute("DELETE FROM incidents WHERE job=?", (job,))
            if row[0]:
                self._alert_incident(db, job, "recovered")

    def incidents(self) -> list[dict[str, object]]:
        return self._dicts("SELECT job,opened_at,last_alert_at,last_error,count FROM incidents ORDER BY opened_at,job")

    def failing(self) -> tuple[list[str], list[str]]:
        """Return `(failing_jobs, failing_transports)` without creating the runtime DB."""
        if not self.exists():
            return [], []
        qj = "SELECT job FROM active_runs WHERE status='uncertain' AND job!='' UNION SELECT job FROM deliveries WHERE state IN ('failed','uncertain') AND job!=''"
        qt = "SELECT DISTINCT send_job FROM deliveries WHERE state NOT IN ('delivered','cancelled') AND (state='uncertain' OR error!='')"
        with self.read() as db:
            return sorted(r[0] for r in db.execute(qj)), sorted(r[0] for r in db.execute(qt))

    def report(self, *, limit: int = 50) -> dict[str, object]:
        """Inspectable queue snapshot for `bf queue`."""
        if not self.exists():
            return {"status": dict(_ZERO_STATUS), "active_runs": [], "wakes": [], "deliveries": [], "transports": {}}
        q_w = "SELECT * FROM wakes WHERE state IN ('pending','claimed','failed') ORDER BY due LIMIT ?"
        q_d = "SELECT id,run_id,job,send_job,state,attempts,due,error FROM deliveries WHERE state NOT IN ('delivered','cancelled') ORDER BY due LIMIT ?"
        with self.read() as db:
            rows_tr = db.execute("SELECT key,value FROM checkpoints WHERE key LIKE 'health:%'")
            tr = {k: v for k, raw in rows_tr if isinstance(v := decode(raw), dict) and v.get("error")}
            runs = [dict(r) for r in db.execute("SELECT * FROM active_runs ORDER BY started_at DESC LIMIT ?", (limit,))]
            wakes, dels = [dict(r) for r in db.execute(q_w, (limit,))], [dict(r) for r in db.execute(q_d, (limit,))]
            st = self._status_counts(db)
            return {"status": st, "active_runs": runs, "wakes": wakes, "deliveries": dels, "transports": tr}


_ItemMeta = tuple[str, str, str]


def sync_item_events(store: Store) -> int:
    """Diff indexed items against `item_checkpoints` and emit item lifecycle events."""
    from bf import index

    try:
        if not store.lstat(CACHE).st_size:
            return 0
    except FileNotFoundError:
        return 0
    wm, queue = index.watermarks(store), Queue(store)
    job_for = {src: jname for src, (jname, _) in load(store).record_sources().items()}
    emitted: list[tuple[str, str, str, str, dict[str, JsonValue]]] = []
    with queue.connect() as db:
        ts = now()
        seeded = db.execute("SELECT 1 FROM checkpoints WHERE key='items:seeded'").fetchone() is not None
        pq = "SELECT ref,kind,source,observed,sha256 FROM item_checkpoints"
        prev = {r[0]: (r[1], r[2], r[3], r[4]) for r in db.execute(pq)}
        cutoff, tracked = "", set[str]()
        if not seeded:
            row = db.execute("SELECT value FROM checkpoints WHERE key='created'").fetchone()
            val = decode(row[0]) if row else None
            c_at = val["at"] if isinstance(val, dict) and isinstance(val.get("at"), str) else ""
            c_at = c_at or timestamp(datetime.fromtimestamp(store.lstat(QUEUE).st_ctime, UTC).isoformat())
            cutoff = timestamp((datetime.fromisoformat(c_at) - timedelta(minutes=5)).isoformat())
            tracked = {str(r[0]) for r in db.execute("SELECT job FROM job_state")}
        upserts: list[tuple[str, str, str, str, str]] = []

        def emit(ev: str, src: str, ref: str, summary: str, meta: _ItemMeta, title: str | None = None) -> None:
            kind, obs, sha = meta
            d = dict[str, JsonValue](ref=ref, kind=kind, source=src, observed=obs, sha256=sha)
            if title is not None:
                d["title"] = title
            emitted.append((ev, src, ref, summary[:512], d))

        for ref in sorted(wm):
            kind, src, obs, sha, title = wm[ref]
            if (old := prev.get(ref)) == (kind, src, obs, sha):
                continue
            upserts.append((ref, kind, src, obs, sha))
            untracked = old is None and job_for.get(src, src) not in tracked and src not in tracked
            if old is not None or seeded or (kind != "note" and not (untracked and (not obs or obs < cutoff))):
                ev = "note.changed" if kind == "note" else ("record.added" if old is None else "record.changed")
                emit(ev, src, ref, title, (kind, obs, sha), title)
        if deletes := sorted(prev.keys() - wm.keys()):
            for ref in deletes:
                pk, psrc, pobs, psha = prev[ref]
                if pk == "record":
                    emit("record.removed", psrc, ref, f"removed {psrc} record {ref}", (pk, pobs, psha))
            db.executemany("DELETE FROM item_checkpoints WHERE ref=?", [(r,) for r in deletes])
        if upserts:
            db.executemany("INSERT OR REPLACE INTO item_checkpoints VALUES (?,?,?,?,?)", upserts)
        if not seeded:
            queue.checkpoint(db, "items:seeded", {"at": ts})
        if emitted:
            queue.reserve(db, 0, sum(len(ref) + 256 for _, _, ref, *_ in emitted))
            for ev_k, src, ref, summary, d in emitted:
                queue.event(db, (f"item:{ev_k}:{uuid.uuid4().hex}", ts, ev_k, src, ref, summary), d)
            queue.trim(db)
    if emitted:
        ping_wakeup(store)
    return len(emitted)
