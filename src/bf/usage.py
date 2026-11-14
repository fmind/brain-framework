"""Local usage counts inside `<brain>/.bf/usage.sqlite3`: searches, empty searches, reads, and unmatched tokens.

Advisory counts live in a separate SQLite database from `.bf/runtime.sqlite3` so retrieval never opens or locks the
runtime queue. Neither query text, refs, paths nor identities are stored.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, NotRequired, TypedDict

from bf import links
from bf.markdown import authored, split_ref
from bf.models import Error
from bf.storage import Store, private_dir

if TYPE_CHECKING:
    from bf.models import Query

USAGE = ".bf/usage.sqlite3"
VERSION = 1
LIMIT = 1 << 20
WINDOWS = {"7d": 7, "30d": 30}
MAX_UNMATCHED = 8
MAX_TOKEN = 64
TOP_TERMS = 10
DAY_TERMS = 32
OTHER = "other"
_CREATE = Lock()
_SCHEMA = """
CREATE TABLE usage_days(day TEXT PRIMARY KEY, search INTEGER NOT NULL DEFAULT 0,
    empty INTEGER NOT NULL DEFAULT 0, read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE usage_kinds(day TEXT NOT NULL, op TEXT NOT NULL, kind TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, empty_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, op, kind));
CREATE TABLE usage_terms(day TEXT NOT NULL, term TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, term));
"""
KINDS: dict[str, frozenset[str]] = {
    "search": frozenset(
        {"none", "period", "folder", "note", "memories", "source", "source-period", "record", "identity", "tag"}
        | {"combined", OTHER}
    ),
    "read": frozenset(
        {"home", "review", "period", "tasks", "tags", "tag", "memories", "source", "source-period", "record", "folder"}
        | {"note", "section", "action", "identity", "relation", OTHER}
    ),
}


class Counts(TypedDict):
    """One window's searches, searches that found nothing and reads."""

    search: int
    empty: int
    read: int
    kinds: NotRequired[dict[str, dict[str, int]]]
    empty_kinds: NotRequired[dict[str, int]]
    empty_terms: NotRequired[list[dict[str, object]]]
    since: NotRequired[str]


def scope(query: Query) -> str:
    """A search's scope kind; a period with any other scope is `combined`, except a source's own period pages."""
    timed = bool(query.since or query.until or query.undated)
    parts = query.prefix.split("/") if query.prefix else []
    if query.target and parts:
        return "combined"
    if query.target:
        try:
            shape = "tag" if links.tag(query.target) else "identity"
        except Error:
            shape = "identity"
    elif not parts:
        shape = ""
    elif parts[0] == "memories":
        shape = ("memories", "source")[len(parts) - 1] if len(parts) <= 2 else "record"
    else:
        shape = "note" if authored(query.prefix) else "folder"
    if not timed:
        return shape or "none"
    if not shape:
        return "period"
    return "source-period" if shape == "source" else "combined"


def exact(value: Mapping[str, object]) -> str:
    """An exact read's kind: a record, a note's section, an action's ACTION.md or another note."""
    if "record" in value:
        return "record"
    path, fragment = split_ref(str(value.get("ref", "")))
    if fragment:
        return "section"
    return "action" if re.fullmatch(r"actions/[^/]+/ACTION\.md", path) else "note"


def _top(counter: Counter[str], count: int) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:count]


def _first(today: date, days: int) -> date:
    """A window of `days` counts today and the UTC days before it, whole."""
    return today - timedelta(days=days - 1)


def _check_sidecars(store: Store) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        with suppress(FileNotFoundError):
            store.lstat(USAGE + suffix)


def _ensure_db(store: Store) -> None:
    private_dir(store, ".bf")
    with _CREATE, store.parent(USAGE) as (parent, name):
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            try:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                os.fsync(parent)
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Error("usage database must be a private regular file owned by you")
    _check_sidecars(store)


def note(store: Store, operation: str, results: int, *, kind: str, unmatched: Sequence[str] = ()) -> None:
    """Record one search or read event in `.bf/usage.sqlite3`; counting never breaks retrieval."""
    if operation not in KINDS:
        return
    try:
        now = datetime.now(UTC)
        today = now.astimezone(UTC).date()
        day = today.isoformat()
        oldest = _first(today, max(WINDOWS.values())).isoformat()
        known = kind if kind in KINDS[operation] else OTHER
        is_empty = int(operation == "search" and results == 0)
        tokens = (
            [str(term).strip().lower()[:MAX_TOKEN] for term in unmatched if str(term).strip()][:MAX_UNMATCHED]
            if is_empty and unmatched
            else []
        )
        _ensure_db(store)
        connection = sqlite3.connect(store.root / USAGE, timeout=1, isolation_level=None)
        try:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=NORMAL")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            connection.execute(f"PRAGMA max_page_count={LIMIT // page_size}").fetchall()
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise Error("unknown usage database format")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(f"PRAGMA user_version={VERSION}")
            elif version != VERSION:
                raise Error("unsupported usage database version")
            is_search = int(operation == "search")
            connection.execute(
                "INSERT INTO usage_days(day,search,empty,read) VALUES(?,?,?,?) ON CONFLICT(day) DO UPDATE "
                "SET search=search+excluded.search, empty=empty+excluded.empty, read=read+excluded.read",
                (day, is_search, is_empty, 1 - is_search),
            )
            connection.execute(
                "INSERT INTO usage_kinds(day,op,kind,count,empty_count) VALUES(?,?,?,1,?) "
                "ON CONFLICT(day,op,kind) DO UPDATE SET count=count+1, empty_count=empty_count+excluded.empty_count",
                (day, operation, known, is_empty),
            )
            connection.executemany(
                "INSERT INTO usage_terms(day,term,count) VALUES(?,?,1) ON CONFLICT(day,term) DO UPDATE SET count=count+1",
                [(day, term) for term in tokens],
            )
            for table in ("usage_days", "usage_kinds", "usage_terms"):
                connection.execute(f"DELETE FROM {table} WHERE day < ?", (oldest,))  # noqa: S608 - fixed table names
            if tokens:
                connection.execute(
                    "DELETE FROM usage_terms WHERE day = ? AND term NOT IN ("
                    "SELECT term FROM usage_terms WHERE day = ? ORDER BY count DESC, term ASC LIMIT ?)",
                    (day, day, DAY_TERMS * 4),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    except OSError, sqlite3.DatabaseError, Error:
        pass


def summary(store: Store, now: datetime | None = None) -> dict[str, Counts]:
    """Searches, searches that found nothing, and reads over today and the 6 or 29 UTC days before it."""
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    counts = {window: Counts(search=0, empty=0, read=0) for window in WINDOWS}
    starts = {window: _first(today, days) for window, days in WINDOWS.items()}
    oldest, today_iso = min(starts.values()).isoformat(), today.isoformat()
    try:
        info = store.lstat(USAGE)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            return counts
        _check_sidecars(store)
        connection = sqlite3.connect(
            (store.root / USAGE).as_uri() + "?mode=ro", uri=True, timeout=1, isolation_level=None
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN")
            if connection.execute("PRAGMA user_version").fetchone()[0] != VERSION:
                return counts
            day_rows = connection.execute(
                "SELECT day,search,empty,read FROM usage_days WHERE day>=? AND day<=?", (oldest, today_iso)
            ).fetchall()
            kind_rows = connection.execute(
                "SELECT day,op,kind,count,empty_count FROM usage_kinds WHERE day>=? AND day<=?", (oldest, today_iso)
            ).fetchall()
            term_rows = connection.execute(
                "SELECT day,term,count FROM usage_terms WHERE day>=? AND day<=?", (oldest, today_iso)
            ).fetchall()
        finally:
            connection.close()
    except OSError, sqlite3.DatabaseError, Error:
        return counts

    def _windows(raw: object) -> list[str]:
        try:
            row_day = date.fromisoformat(str(raw))
        except ValueError:
            return []
        return [] if row_day > today else [w for w, start in starts.items() if row_day >= start]

    totals: dict[str, dict[str, int]] = {w: {"search": 0, "empty": 0, "read": 0} for w in WINDOWS}
    kinds: dict[str, dict[str, Counter[str]]] = {w: {op: Counter() for op in KINDS} for w in WINDOWS}
    empty_kinds: dict[str, Counter[str]] = {w: Counter() for w in WINDOWS}
    unmatched: dict[str, Counter[str]] = {w: Counter() for w in WINDOWS}
    for row in day_rows:
        s, e, r = max(0, int(row["search"])), max(0, int(row["empty"])), max(0, int(row["read"]))
        for w in _windows(row["day"]):
            totals[w]["search"] += s
            totals[w]["empty"] += min(e, s)
            totals[w]["read"] += r
    for row in kind_rows:
        op, kind = str(row["op"]), str(row["kind"])
        if op in KINDS and kind in KINDS[op]:
            c, ec = max(0, int(row["count"])), max(0, int(row["empty_count"]))
            for w in _windows(row["day"]):
                if c:
                    kinds[w][op][kind] += c
                if op == "search" and ec:
                    empty_kinds[w][kind] += min(ec, c)
    for row in term_rows:
        term, c = str(row["term"]), max(0, int(row["count"]))
        if term and len(term) <= MAX_TOKEN and c:
            for w in _windows(row["day"]):
                unmatched[w][term] += c
    for w in WINDOWS:
        counts[w].update(search=totals[w]["search"], empty=totals[w]["empty"], read=totals[w]["read"])
        if any(kinds[w].values()):
            counts[w]["kinds"] = {op: dict(sorted(counter.items())) for op, counter in kinds[w].items()}
        if empty_kinds[w]:
            counts[w]["empty_kinds"] = dict(sorted(empty_kinds[w].items()))
        if unmatched[w]:
            counts[w]["empty_terms"] = [{"term": t, "count": c} for t, c in _top(unmatched[w], TOP_TERMS)]
    return counts
