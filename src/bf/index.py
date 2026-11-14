"""A disposable SQLite cache that refreshes changed files and answers lexical and time-window queries."""

from __future__ import annotations

import difflib
import errno
import json
import os
import re
import sqlite3
import stat
import time
from collections import Counter
from collections.abc import Callable, Collection, Generator, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path, PurePosixPath
from typing import cast

from bf import links as bf_links
from bf import models, ontology, records
from bf.config import load
from bf.markdown import LEAD, authored, editor_lock, entry_note, note, preview
from bf.models import (
    AUTHORED,
    IDENTITY,
    LINKS,
    MAX_FILES,
    MAX_NOTE,
    Bounds,
    Config,
    Error,
    Query,
    Record,
    digest,
    encode,
    fold,
    moment,
)
from bf.storage import UNNAMED, BusyError, Store, building, generation, reader, unnamed, unread, writer

SCHEMA = 43
CACHE = models.CACHE
BUILD = CACHE + ".new"
_SIDECARS = ("", "-wal", "-shm", "-journal")
_PUBLISH = 30
# Short wait when a serving cache exists so writers leave it `busy` instead of stalling readers.
_CONTENDED = 2.0
_DISCARD = 5
CROWDED = MAX_FILES * 4 // 5
_PROBLEM = 1024
_SQLITE = (3, 35, 0)
_PROBE = (
    "WITH t AS MATERIALIZED (SELECT value FROM json_each('[1]')) "
    "SELECT row_number() OVER w FROM t WINDOW w AS (ORDER BY value)"
)
Fingerprints = dict[str, tuple[int, int, int, int]]
Tree = tuple[int, int, int, int, int, int]
# Wait past coarse filesystem timestamp ticks (e.g. FAT 2s) before trusting a source folder fingerprint.
_SETTLE = 5_000_000_000
_TOKENIZER = "porter unicode61 remove_diacritics 2"
_FOLDED = _TOKENIZER.removeprefix("porter ")
_DDL = f"""
CREATE TABLE ontology(signature TEXT NOT NULL, file TEXT NOT NULL);
CREATE TABLE edges(item INTEGER NOT NULL, subject TEXT NOT NULL, relation TEXT NOT NULL, target TEXT NOT NULL,
                   origin TEXT NOT NULL);
CREATE TABLE files(path TEXT PRIMARY KEY, tree TEXT NOT NULL, size INTEGER, mtime INTEGER, ctime INTEGER,
                   inode INTEGER, error TEXT NOT NULL DEFAULT '');
CREATE TABLE trees(path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime INTEGER NOT NULL, ctime INTEGER NOT NULL,
                   inode INTEGER NOT NULL, entries INTEGER NOT NULL, trusted INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE items(id INTEGER PRIMARY KEY, ref TEXT NOT NULL UNIQUE, path TEXT NOT NULL, kind TEXT NOT NULL,
                   source TEXT NOT NULL, title TEXT NOT NULL, time TEXT NOT NULL, type TEXT NOT NULL,
                   item_type TEXT NOT NULL, status TEXT NOT NULL, lead TEXT NOT NULL, url TEXT NOT NULL,
                   updated TEXT NOT NULL, observed TEXT NOT NULL, partial INTEGER NOT NULL,
                   tasks_open INTEGER NOT NULL, tasks_done INTEGER NOT NULL, next TEXT NOT NULL,
                   weight INTEGER NOT NULL, stale_after TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE tasks(item INTEGER NOT NULL, line INTEGER NOT NULL, fragment TEXT NOT NULL,
                   text TEXT NOT NULL, done INTEGER NOT NULL, PRIMARY KEY(item,line)) WITHOUT ROWID;
CREATE TABLE passages(id INTEGER PRIMARY KEY, item INTEGER NOT NULL, fragment TEXT NOT NULL, title TEXT NOT NULL,
                      original TEXT NOT NULL);
CREATE VIRTUAL TABLE search USING fts5(title, text, names, context, tokenize='{_TOKENIZER}');
CREATE VIRTUAL TABLE vocabulary USING fts5vocab(search, row);
CREATE TABLE words(word TEXT PRIMARY KEY, stem TEXT NOT NULL, held INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE names(name TEXT NOT NULL, item INTEGER NOT NULL, PRIMARY KEY(name, item)) WITHOUT ROWID;
CREATE TABLE links(item INTEGER NOT NULL, target TEXT NOT NULL, PRIMARY KEY(item, target)) WITHOUT ROWID;
CREATE TABLE tags(item INTEGER NOT NULL, target TEXT NOT NULL, PRIMARY KEY(item, target)) WITHOUT ROWID;
"""
_INDEXES = (
    "CREATE INDEX edges_item ON edges(item)",
    "CREATE INDEX edges_target ON edges(target)",
    "CREATE INDEX edges_subject ON edges(subject,relation)",
    "CREATE INDEX items_path ON items(path)",
    "CREATE INDEX items_kind_time ON items(kind,time)",
    "CREATE INDEX items_kind_source_time ON items(kind,source,time,observed)",
    "CREATE INDEX items_kind_source_observed ON items(kind,source,observed)",
    # Expression must stay identical to MODIFIED's record branch for SQLite index use.
    "CREATE INDEX items_kind_modified ON items(kind,coalesce(nullif(updated,''),time))",
    "CREATE INDEX passages_item ON passages(item)",
    "CREATE INDEX tasks_done ON tasks(done,item,line)",
    "CREATE INDEX names_item ON names(item)",
    "CREATE INDEX links_target ON links(target)",
    "CREATE INDEX tags_target ON tags(target)",
    "CREATE INDEX files_problems ON files(path) WHERE error!=''",
    "CREATE INDEX files_tree ON files(tree)",
)
# English and French stopwords dropped unless quoted or uppercase acronyms.
_STOP = frozenset(
    """
    a about am an and another any are as at be been being but by can could did do does doing for from had has have
    having he her hers him his how i if in into is it its me might must my not of on one ones or other others our ours
    please shall she should so than that the their theirs them there these they this those to us was we were what when
    where which who whom whose why will with would yet you your yours
    ai au autre autres aux avec c ce ces cet cette d dans de des donc du elle elles en est et eu eux il ils j je l la le
    les leur leurs lui m ma mais me mes moi mon n ne nos notre nous on ont ou où par pas pour pourquoi qu quand que quel
    quelle quelles quels qui quoi s sa se ses si son sont sur t ta te tes toi ton tu un une vos votre vous y à été être
    """.split()  # noqa: SIM905 - one readable word list, grouped by language
)
_OPERATORS = frozenset({"AND", "OR"})
_IDENTITY = re.compile(IDENTITY)
_TERM = re.compile(r'["“”]([^"“”]*)["“”]|([^\W_]+)(\*?)')
WORDS = 32
SPELLED, EXPANSIONS = 3, 64
_WORD = re.compile(rf"[^\W_]{{{SPELLED + 1},}}")
_ASCII_WORD = re.compile(rf"[A-Za-z0-9]{{{SPELLED + 1},}}")
_SPELLINGS = 20_000
_APART = "\ue000"
ALSO = 5
SECTIONS = 3
COVERAGE = 0.2
CONTEXT = 3.0
TITLE = 200
# SQL twins of markdown.action_note and markdown.entry_note over `items i`.
ACTION = (
    "i.kind='note' AND substr(i.path,1,8)='actions/' AND substr(i.path,-10)='/ACTION.md' "
    "AND length(i.path)-length(replace(i.path,'/',''))=2"
)
ENTRY = (
    f"i.kind='note' AND (substr(i.path,1,9) IN ('projects/','concepts/') OR ({ACTION})) "
    "AND i.path NOT GLOB '*/index.md' AND i.path NOT GLOB '*/log.md'"
)


def inputs(
    store: Store,
    counts: dict[str, int] | None = None,
    reuse: Callable[[str, tuple[int, int, int, int]], bool] | None = None,
) -> tuple[Fingerprints, dict[str, Tree]]:
    """Collect fingerprints for authored notes, stored records, and skipped entries across brain roots."""
    started = time.time_ns()
    found: Fingerprints = {}
    tally: dict[str, int] = {}
    folders: dict[str, tuple[int, int, int, int]] = {}

    def listing(tree: str, stamp: tuple[int, int, int, int]) -> bool:
        if reuse is not None and reuse(tree, stamp):
            return True
        folders[tree] = stamp
        return False

    held: dict[str, int] = {}
    unlisted: Fingerprints = {}
    for directory in (*AUTHORED, "memories"):
        skipped: Fingerprints = {}
        if directory == "memories":
            scanned = store.scan(directory, skipped=skipped, split=True, counts=tally, reuse=listing)
            for tree in map(_tree, (*scanned, *skipped)):
                held[tree] = held.get(tree, 0) + 1
            unlisted = skipped
            found.update({name: info for name, info in scanned.items() if records.stored(name)})
            found.update(skipped)
            continue
        scanned = store.scan(directory, skipped=skipped, counts=tally)
        found.update({name: info for name, info in scanned.items() if authored(name) and not editor_lock(name)})
        found.update({name: info for name, info in skipped.items() if not editor_lock(name)})
    if counts is not None:
        counts.update(tally)
    trees: dict[str, Tree] = {}
    for tree, stamp in folders.items():
        if tree not in unlisted:
            entries = tally.get(tree, 0)
            settled = max(stamp[1], stamp[2]) < started - _SETTLE
            trees[tree] = (*stamp, entries, int(settled and entries == held.get(tree, 0)))
    return found, trees


def _tree(path: str) -> str:
    parts = path.split("/", 2)
    return f"memories/{parts[1]}" if parts[0] == "memories" and len(parts) == 3 else parts[0]


@dataclass(frozen=True)
class Scan:
    """Comparison between a brain's files/trees and one cache generation."""

    scope: frozenset[str]
    stored: dict[str, Tree]
    known: Fingerprints
    current: Fingerprints
    trees: dict[str, Tree]

    def matches(self) -> bool:
        return self.known == self.current

    def unchanged(self) -> bool:
        return self.matches() and self.trees == {tree: row for tree, row in self.stored.items() if tree in self.scope}


def _scan(store: Store, connection: sqlite3.Connection | None, counts: dict[str, int] | None = None) -> Scan:
    """Scan brain files, reusing unchanged trusted `memories/` source folders."""
    stored = _trees(connection) if connection is not None else {}
    dirty = _dirty(store, connection, stored) if connection is not None else set()
    reused: set[str] = set()

    def reuse(tree: str, stamp: tuple[int, int, int, int]) -> bool:
        row = stored.get(tree)
        if row is None or not row[5] or row[:4] != stamp or tree in dirty:
            return False
        reused.add(tree)
        return True

    tally: dict[str, int] = {}
    current, trees = inputs(store, tally, reuse)
    if counts is not None:
        counts.update(tally)
        counts.update({tree: stored[tree][4] for tree in reused if stored[tree][4]})
    scope = frozenset({*AUTHORED, "memories", *trees, *(stored.keys() - reused)})
    known = _known(connection, scope) if connection is not None else {}
    return Scan(scope, stored, known, current, trees)


def _dirty(store: Store, connection: sqlite3.Connection, stored: Mapping[str, Tree]) -> set[str]:
    """Trusted source folders holding a skipped entry whose in-place stat changed."""
    found: set[str] = set()
    cursor = connection.cursor()
    cursor.row_factory = None
    for path, *stamp in cursor.execute("SELECT path,size,mtime,ctime,inode FROM files WHERE error!=''"):
        tree = _tree(path)
        if tree in found or tree not in stored or not stored[tree][5]:
            continue
        try:
            same = store.fingerprint(path, regular=False) == tuple(stamp)
        except Error:
            continue
        except OSError:
            same = False
        if not same:
            found.add(tree)
    return found


def _trees(connection: sqlite3.Connection) -> dict[str, Tree]:
    cursor = connection.cursor()
    cursor.row_factory = None
    return {row[0]: row[1:] for row in cursor.execute("SELECT path,size,mtime,ctime,inode,entries,trusted FROM trees")}


@contextmanager
def _writable() -> Generator[None]:
    try:
        yield
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            raise Error(
                "the search cache needs write access to the brain's .bf directory; grant it or use a writable copy"
            ) from error
        raise


def _path(store: Store, name: str = CACHE) -> Path:
    with _writable(), store.parent(name, create=True) as (directory, _name):
        info = os.fstat(directory)
        if info.st_uid != os.getuid():
            raise Error(".bf belongs to another user; remove it so bf can rebuild its own cache")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(directory, stat.S_IMODE(info.st_mode) & 0o700)
    path = store.root / name
    for suffix in _SIDECARS:
        with suppress(FileNotFoundError):
            store.fingerprint(name + suffix)
    return path


def _remove(store: Store, name: str, suffixes: Sequence[str] = _SIDECARS) -> None:
    with _writable(), suppress(FileNotFoundError), store.parent(name) as (directory, leaf):
        for suffix in suffixes:
            with suppress(FileNotFoundError):
                os.unlink(leaf + suffix, dir_fd=directory)


def _open(store: Store, config: Config | None = None) -> sqlite3.Connection | None:
    """Open the current cache generation if its schema, signature, and inode match."""
    path = _path(store)
    signature = _signature(load(store) if config is None else config)
    try:
        expected = (signature, _file(path))
    except FileNotFoundError:
        return None
    connection = _connect(path)
    connection.create_function("note_time", 1, lru_cache(maxsize=4096)(_note_time), deterministic=True)
    try:
        if (
            connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
            and _schema(connection) == _layout()
            and tuple(connection.execute("SELECT signature,file FROM ontology").fetchone() or ()) == expected
        ):
            return connection
    except sqlite3.DatabaseError:
        pass
    connection.close()
    return None


def _schema(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_stat%' ORDER BY type,name"
    )
    return [tuple(row) for row in rows]


@cache
def _layout() -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(_DDL)
        for statement in _INDEXES:
            connection.execute(statement)
        return _schema(connection)


def _connect(path: Path | str) -> sqlite3.Connection:
    _supported()
    connection = sqlite3.connect(path, timeout=30)
    # Untrusted schema hardening: forbid SQLite triggers/views from altering queries or files.
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    connection.row_factory = sqlite3.Row
    return connection


@cache
def _supported() -> None:
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.executescript(_DDL)
            connection.execute(_PROBE).fetchall()
        capable = sqlite3.sqlite_version_info >= _SQLITE
    except sqlite3.Error:
        capable = False
    if not capable:
        raise Error(
            f"the search cache needs SQLite {'.'.join(map(str, _SQLITE))} or newer with FTS5 and JSON functions, which "
            f"this Python's SQLite {sqlite3.sqlite_version} lacks; reinstall bf on a uv-managed Python: "
            "uv tool install --reinstall --managed-python --python 3.14 brain-framework, or 'brain-framework[mcp]' with MCP"
        )


def _signature(config: Config) -> str:
    return digest(encode({"name": config.name}))


def _file(path: Path) -> str:
    return str(path.lstat().st_ino)


def _create(store: Store, config: Config) -> sqlite3.Connection:
    """Create a fresh private build DB in rollback-journal mode before publishing."""
    path = _path(store, BUILD)
    _remove(store, BUILD)
    with _writable():
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    connection = _connect(path)
    connection.executescript("BEGIN;" + _DDL)
    connection.execute("INSERT INTO ontology VALUES(?,?)", (_signature(config), _file(path)))
    connection.commit()
    return connection


def _known(connection: sqlite3.Connection, scope: Collection[str]) -> Fingerprints:
    cursor = connection.cursor()
    cursor.row_factory = None
    return {
        row[0]: row[1:]
        for row in cursor.execute(
            "SELECT path,size,mtime,ctime,inode FROM files WHERE tree IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(scope)),),
        )
    }


def _holds(connection: sqlite3.Connection, scan: Scan) -> bool:
    return _trees(connection) == scan.stored and _known(connection, scan.scope) == scan.known


def _drop(connection: sqlite3.Connection, path: str) -> None:
    owned = "item IN (SELECT id FROM items WHERE path=:path)"
    values = {"path": path}
    connection.execute(f"DELETE FROM search WHERE rowid IN (SELECT id FROM passages WHERE {owned})", values)  # noqa: S608 - fixed SQL
    for table in ("passages", "names", "links", "edges", "tags", "tasks"):
        connection.execute(f"DELETE FROM {table} WHERE {owned}", values)  # noqa: S608 - fixed table names
    connection.execute("DELETE FROM items WHERE path=?", (path,))
    connection.execute("DELETE FROM files WHERE path=?", (path,))


def _insert(
    connection: sqlite3.Connection,
    values: dict[str, str | int],
    passages: list[tuple[str, str, str, str, str, str]],
    names: list[str],
    links: list[str],
    edges: Sequence[bf_links.Claim],
) -> int:
    defaults: dict[str, str | int] = {
        "item_type": "",
        "tasks_open": 0,
        "tasks_done": 0,
        "next": "",
        "weight": 1,
        "stale_after": "",
        "sha256": "",
    }
    item = connection.execute(
        "INSERT INTO items(ref,path,kind,source,title,time,type,item_type,status,lead,url,updated,observed,partial,"
        "tasks_open,tasks_done,next,weight,stale_after,sha256) VALUES(:ref,:path,:kind,:source,:title,:time,:type,"
        ":item_type,:status,:lead,:url,:updated,:observed,:partial,:tasks_open,:tasks_done,:next,:weight,:stale_after,"
        ":sha256)",
        {**defaults, **values},
    ).lastrowid
    for fragment, title, heading, text, identity, context in passages:
        folded = fold(text)
        passage = connection.execute(
            "INSERT INTO passages(item,fragment,title,original) VALUES(?,?,?,?)",
            (item, fragment, title, "" if folded == text else text),
        ).lastrowid
        connection.execute(
            "INSERT INTO search(rowid,title,text,names,context) VALUES(?,?,?,?,?)",
            (passage, fold(heading), folded, fold(identity), fold(context)),
        )
    connection.executemany("INSERT OR IGNORE INTO names VALUES(?,?)", [(name, item) for name in names])
    connection.executemany("INSERT OR IGNORE INTO links VALUES(?,?)", [(item, link) for link in links])
    claims = dict.fromkeys((item, e.subject, e.relation, e.target, e.origin) for e in edges)
    connection.executemany("INSERT INTO edges VALUES(?,?,?,?,?)", claims)
    return cast("int", item)


_PASSAGES = (
    "SELECT title,text,names,context FROM search WHERE rowid IN (SELECT p.id FROM items i "
    "JOIN passages p ON p.item=i.id WHERE i.path IN (SELECT value FROM json_each(?)))"
)


def _spell(connection: sqlite3.Connection, paths: Sequence[str] | None, change: int = 1) -> None:
    """Update `words` occurrence counts (+1/-1) for passages of `paths` or the whole table."""
    rows = connection.execute(
        "SELECT title,text,names,context FROM search" if paths is None else _PASSAGES,
        () if paths is None else (json.dumps(paths),),
    )
    words: Counter[str] = Counter()
    for row in rows:
        for column in row:
            if column.isascii():
                words.update(word for word in _ASCII_WORD.findall(column) if not word[-1].isdigit())
            else:
                words.update(_WORD.findall(column))
        if len(words) >= _SPELLINGS:
            _respell(connection, words, change)
            words.clear()
    _respell(connection, words, change)


def _respell(connection: sqlite3.Connection, words: Counter[str], change: int) -> None:
    candidates = sorted(words)
    held: Counter[tuple[str, str]] = Counter()
    for candidate, tokens in zip(candidates, _spellings(candidates) if candidates else [], strict=True):
        for pair in tokens:
            held[pair] += words[candidate]
    rows = [
        (word, stem, count * change) for (word, stem), count in held.items() if word != stem and len(word) > SPELLED
    ]
    connection.executemany(
        "INSERT INTO words VALUES(?,?,?) ON CONFLICT(word) DO UPDATE SET held=held+excluded.held", rows
    )
    if change < 0:
        connection.executemany("DELETE FROM words WHERE word=? AND held<=0", [(word,) for word, _, _ in rows])


def _spellings(words: Sequence[str]) -> list[list[tuple[str, str]]]:
    """Tokenize `words` through in-memory FTS5 folded and stemmed tables, split by `_APART`."""
    with closing(sqlite3.connect(":memory:")) as memory:
        for table, tokenizer in (("folded", _FOLDED), ("stemmed", _TOKENIZER)):
            memory.execute(f"CREATE VIRTUAL TABLE {table} USING fts5(x, tokenize={tokenizer!r})")
            memory.execute(f"CREATE VIRTUAL TABLE {table}_tokens USING fts5vocab({table}, instance)")
            memory.execute(f"INSERT INTO {table}(rowid,x) VALUES(1,?)", (f" {_APART} ".join(words),))
        stems = dict(memory.execute("SELECT offset,term FROM stemmed_tokens"))
        spelled: list[list[tuple[str, str]]] = [[]]
        for offset, term in memory.execute("SELECT offset,term FROM folded_tokens ORDER BY offset"):
            if term == _APART:
                spelled.append([])
            else:
                spelled[-1].append((term, stems[offset]))
        return spelled


def _scalars(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, int | float):
        return [str(value)]
    if isinstance(value, list):
        return [item for element in value for item in _scalars(element)]
    if isinstance(value, dict):
        return [item for element in value.values() for item in _scalars(element)]
    return []


def _index(connection: sqlite3.Connection, store: Store, path: str, config: Config) -> None:
    """Index one note or stored record; parse or safety errors raise."""
    if unnamed(path):
        raise Error(UNNAMED)
    if authored(path):
        raw = store.read(path, MAX_NOTE)
        projection = note(path, raw)
        knowledge = projection.knowledge
        edges = ontology.note_claims(projection, config)
        values = [*_scalars(knowledge.relations), *_scalars(knowledge.attributes)]
        stem = PurePosixPath(path).stem
        title_folded = fold(projection.title).lower()
        extra_stem = (
            [stem]
            if stem not in {"ACTION", "index", "log"} and fold(stem.replace("-", " ")).lower() != title_folded
            else []
        )
        extra_aliases = [
            alias
            for alias in knowledge.aliases
            if fold(alias.partition(":")[2].replace("-", " ")).lower() != title_folded
        ]
        metadata = " ".join([knowledge.type, *extra_stem, *knowledge.aliases, *projection.links, *values])
        item = _insert(
            connection,
            {
                "ref": path,
                "path": path,
                "kind": "note",
                "source": "",
                "title": projection.title,
                "time": f"{knowledge.updated}T00:00:00.000000Z" if knowledge.updated else "",
                "type": knowledge.type,
                "status": knowledge.status,
                "lead": projection.lead,
                "url": "",
                "updated": f"{knowledge.updated}T00:00:00.000000Z" if knowledge.updated else "",
                "observed": "",
                "partial": 0,
                "weight": 2 if entry_note(path) else 1,
                "tasks_open": sum(not task.done for task in projection.tasks),
                "tasks_done": sum(task.done for task in projection.tasks),
                "next": next((task.text for task in projection.tasks if not task.done), ""),
                "stale_after": knowledge.stale_after,
                "sha256": digest(raw),
            },
            [
                (
                    p.fragment,
                    p.title,
                    p.heading,
                    p.text,
                    "" if p.fragment else metadata,
                    " ".join([*extra_stem, *extra_aliases, *p.parents] if p.fragment else knowledge.tags),
                )
                for p in projection.passages
            ],
            sorted(
                {
                    ontology.qualify(config, path),
                    *(bf_links.target(v) for v in knowledge.names),
                    *([bf_links.identity(knowledge.entity)] if knowledge.entity else []),
                }
            ),
            sorted({*projection.links, *(e.target for e in edges)}),
            edges,
        )
        connection.executemany(
            "INSERT INTO tasks VALUES(?,?,?,?,?)",
            [(item, task.line, task.fragment, task.text, int(task.done)) for task in projection.tasks],
        )
        connection.executemany(
            "INSERT INTO tags(item,target) VALUES(?,?)",
            [(item, ontology.qualify(config, f"tags/{tag}")) for tag in knowledge.tags],
        )
        return
    if "/" not in path:
        raise Error(unread(store, path))
    try:
        source = records.source_of(path)
    except Error:
        if not stat.S_ISREG(store.mode(path)):
            store.read(path, 0)
        raise
    raw = store.read(path, models.MAX_RECORD)
    record = records.parse(path, raw)
    ref = f"{source}:{record.id}"
    edges = ontology.record_claims(record, config, ref)
    values = [*_scalars(record.relations), *_scalars(record.attributes)]
    identity = " ".join(
        [source, *([record.type] if record.type else []), *record.aliases, *record.links, record.url, *values]
    )
    _insert(
        connection,
        {
            "ref": ref,
            "path": path,
            "kind": "record",
            "source": source,
            "title": record.title,
            "time": record.time,
            "type": "record",
            "item_type": record.type,
            "status": "",
            "lead": lead(record),
            "url": record.url,
            "updated": record.updated,
            "observed": record.observed,
            "partial": int(record.attributes.get("partial") is True),
            "sha256": digest(raw),
        },
        [("", record.title, record.title, record.text, identity, "")],
        sorted({ontology.qualify(config, ref), *(bf_links.target(v) for v in record.aliases)}),
        sorted({*(bf_links.target(v) for v in record.links), *(e.target for e in edges)}),
        edges,
    )


def lead(record: Record) -> str:
    """Preview of collapsed text within LEAD chars."""
    size = 4 * LEAD
    while True:
        collapsed = " ".join(record.text[:size].split())
        if len(collapsed) > LEAD or size >= len(record.text):
            return preview(collapsed)
        size *= 4


def refresh(
    store: Store, *, full: bool = False, wait: float = 30, recover: bool = False, compared: Scan | None = None
) -> dict[str, object]:
    """Re-index changed files or build a new generation, skipping invalid files with diagnostics."""
    try:
        try:
            return _refresh(store, full=full, wait=wait, recover=full or recover, compared=compared)
        except sqlite3.DatabaseError as error:
            if not _damaged(error):
                raise
        _discard(store, wait)
        return _refresh(store, full=full, wait=wait, recover=full or recover, compared=compared)
    except sqlite3.DatabaseError as error:
        if getattr(error, "sqlite_errorcode", 0) & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise Error("another process holds the search cache; close it and retry") from error
        raise Error("could not refresh the search cache; check free space and run bf build") from error


class _DamagedError(sqlite3.DatabaseError):
    """Page check detected damage before a query ran."""


def _damaged(error: sqlite3.DatabaseError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(error, _DamagedError) or (
        isinstance(code, int) and code & 0xFF in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
    )


def _discard(store: Store, wait: float) -> None:
    with suppress(BusyError), building(store, 0), writer(store, wait), generation(store, shared=False, wait=wait):
        connection = _open(store)
        if connection is not None:
            with closing(connection), suppress(sqlite3.DatabaseError):
                if connection.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok":
                    return
        _remove(store, CACHE)


def _refresh(store: Store, *, full: bool, wait: float, recover: bool, compared: Scan | None) -> dict[str, object]:
    if not full and (result := _incremental(store, wait=wait, recover=recover, compared=compared)) is not None:
        return result
    with building(store, wait):
        if full:
            with writer(store, wait):
                records.recover(store)
        elif (result := _incremental(store, wait=wait, recover=recover, compared=compared)) is not None:
            return result
        return _build(store, wait)


def _incremental(store: Store, *, wait: float, recover: bool, compared: Scan | None) -> dict[str, object] | None:
    """Update the live generation in place; None when no compatible generation exists."""
    checked = ""
    if recover or compared is None or compared.unchanged():
        with reader(store, wait):
            if not (recover and records.pending(store)):
                if not recover:
                    records.require_ready(store)
                config = load(store)
                connection = _open(store, config)
                if connection is None:
                    return None
                with closing(connection):
                    connection.execute("PRAGMA cache_size=-65536")
                    if recover:
                        if connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                            raise _DamagedError("the search cache is damaged")
                        checked = _file(_path(store))
                    if compared is None or not _holds(connection, compared):
                        compared = _scan(store, connection)
                    if compared.unchanged():
                        return _counts(connection, compared, 0, 0)
    with writer(store, wait):
        if recover:
            records.recover(store)
        else:
            records.require_ready(store)
        config = load(store)
        connection = _open(store, config)
        if connection is None:
            return None
        with closing(connection):
            connection.execute("PRAGMA cache_size=-65536")
            if compared is not None and not _holds(connection, compared):
                compared = None
            if (
                recover
                and (compared is None or _file(_path(store)) != checked)
                and connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok"
            ):
                raise _DamagedError("the search cache is damaged")
            return _fill(store, connection, config, new=False, compared=compared)


def _build(store: Store, wait: float) -> dict[str, object]:
    """Fill a new generation beside the live cache and publish it atomically."""
    config = load(store)
    try:
        with closing(_create(store, config)) as connection:
            result = _fill(store, connection, config, new=True)
            connection.execute("PRAGMA journal_mode=WAL")
        with writer(store, max(wait, _PUBLISH)):
            _publish(store)
    finally:
        _remove(store, BUILD)
    return result


def _publish(store: Store) -> None:
    with generation(store, shared=False, wait=_PUBLISH):
        _replace(store)


def _replace(store: Store) -> None:
    path = _path(store)
    try:
        store.lstat(CACHE)
    except FileNotFoundError:
        pass
    else:
        deadline = time.monotonic() + _PUBLISH
        with closing(_connect(path)) as live:
            while True:
                try:
                    # Leave WAL mode before replacing so new generation never attaches old -wal/-shm sidecars.
                    live.execute("PRAGMA journal_mode=DELETE")
                    break
                except sqlite3.OperationalError as error:
                    if error.sqlite_errorcode & 0xFF not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                        raise
                    if time.monotonic() >= deadline:
                        raise BusyError("readers kept the search cache open; retry bf build") from error
                    time.sleep(0.05)
                except sqlite3.DatabaseError:
                    break
    _remove(store, CACHE, _SIDECARS[1:])
    with _writable(), store.parent(CACHE) as (directory, leaf):
        os.replace(Path(BUILD).name, leaf, src_dir_fd=directory, dst_dir_fd=directory)


def _fill(
    store: Store, connection: sqlite3.Connection, config: Config, *, new: bool, compared: Scan | None = None
) -> dict[str, object]:
    """Index changed files in one transaction; a new build creates secondary indexes after loading."""
    connection.execute("PRAGMA cache_size=-65536")
    if compared is None or new:
        compared = _scan(store, None if new else connection)
    if not new and compared.unchanged():
        return _counts(connection, compared, 0, 0)
    known, current = compared.known, compared.current
    changed = sorted(path for path, info in current.items() if known.get(path) != info)
    removed = sorted(known.keys() - current.keys())
    vanished = sorted((compared.stored.keys() & compared.scope) - compared.trees.keys())
    with connection:
        connection.execute("BEGIN")
        if not new:
            _spell(connection, removed + changed, -1)
        for path in () if new else (removed + changed):
            _drop(connection, path)
        for path in changed:
            connection.execute("SAVEPOINT file")
            error = ""
            try:
                _index(connection, store, path, config)
            except FileNotFoundError:
                connection.execute("ROLLBACK TO file")
                connection.execute("RELEASE file")
                continue
            except (Error, UnicodeError) as problem:
                connection.execute("ROLLBACK TO file")
                error = _capped(str(problem).removeprefix(f"{path}: ") or "invalid file")
            except PermissionError:
                connection.execute("ROLLBACK TO file")
                error = "inaccessible file or folder; check permissions"
            except OSError:
                connection.execute("ROLLBACK TO file")
                connection.execute("RELEASE file")
                connection.execute(
                    "INSERT INTO files VALUES(?,?,?,?,?,?,?)",
                    (path, _tree(path), -1, -1, -1, -1, "unreadable file; retried next time"),
                )
                continue
            connection.execute("RELEASE file")
            connection.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)", (path, _tree(path), *current[path], error))
        _spell(connection, None if new else changed)
        connection.executemany("DELETE FROM trees WHERE path=?", [(tree,) for tree in vanished])
        connection.executemany(
            "INSERT OR REPLACE INTO trees VALUES(?,?,?,?,?,?,?)",
            [(tree, *row) for tree, row in sorted(compared.trees.items()) if compared.stored.get(tree) != row],
        )
        for statement in _INDEXES if new else ():
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version={SCHEMA}")
    return _counts(connection, compared, len(changed), len(removed))


def _counts(connection: sqlite3.Connection, compared: Scan, changed: int, removed: int) -> dict[str, object]:
    kept = connection.execute(
        "SELECT count(*) FROM files WHERE tree NOT IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(compared.scope)),),
    ).fetchone()[0]
    skipped = connection.execute("SELECT count(*) FROM files WHERE error!=''").fetchone()[0]
    return {"files": len(compared.current) + kept, "changed": changed, "removed": removed, "skipped": skipped}


def _capped(text: str) -> str:
    data = text.encode()
    return text if len(data) <= _PROBLEM else data[: _PROBLEM - 3].decode(errors="ignore") + "…"


def _sweep(store: Store) -> None:
    with suppress(Error, OSError):
        store.lstat(BUILD)
        with building(store, 0):
            _remove(store, BUILD)


def fresh(store: Store, counts: dict[str, int] | None = None) -> str:
    """Refresh changed files or return `busy` while another writer holds the brain past `_CONTENDED`."""
    _path(store)
    _sweep(store)
    for _ in range(2):
        connection = None
        compared: Scan | None = None
        try:
            with generation(store, shared=True, wait=_PUBLISH):
                connection = _open(store)
                try:
                    with reader(store, wait=_CONTENDED):
                        records.require_ready(store)
                        if connection is not None:
                            if counts is not None:
                                counts.clear()
                            compared = _scan(store, connection, counts)
                            if compared.unchanged():
                                return "ready"
                finally:
                    if connection is not None:
                        connection.close()
        except BusyError:
            if connection is not None:
                return "busy"
        except sqlite3.DatabaseError as error:
            if not _damaged(error):
                raise
            connection, compared = None, None
        try:
            refresh(store, wait=_CONTENDED if connection is not None else 120, compared=compared)
        except BusyError:
            return "ready" if compared is not None and compared.matches() else "busy"
        if connection is not None:
            break
    return "ready"


_CHECKED: ContextVar[dict[Path, str] | None] = ContextVar("checked", default=None)


@contextmanager
def session() -> Generator[None]:
    """Compare each brain once per reply across multiple view opens."""
    token = _CHECKED.set({})
    try:
        yield
    finally:
        _CHECKED.reset(token)


@contextmanager
def database(store: Store, counts: dict[str, int] | None = None) -> Generator[tuple[sqlite3.Connection, str]]:
    """Open the fresh cache generation and state inside one read snapshot."""
    checked = _CHECKED.get()
    state = checked.get(store.root, "") if checked is not None else ""
    for attempt in range(3):
        if attempt or not state:
            state = fresh(store, counts)
            if checked is not None:
                checked[store.root] = state
        try:
            with generation(store, shared=True, wait=_PUBLISH):
                connection = _open(store)
                if connection is None:
                    if state == "busy":
                        raise BusyError("another writer is building the search cache; retry when it finishes")
                    continue
                with closing(connection):
                    connection.execute("PRAGMA query_only=ON")
                    connection.execute("BEGIN")
                    yield connection, state
        except Exception as error:
            cause = error if isinstance(error, sqlite3.DatabaseError) else error.__cause__
            if isinstance(cause, sqlite3.DatabaseError) and _damaged(cause):
                _discard(store, _DISCARD)
            raise
        return
    raise Error(models.CACHE_ERROR)


def terms(text: str) -> list[str]:
    """Folded terms (words, `word*` prefixes, phrases), keeping acronyms/quoted words and capping at `WORDS`."""
    found: dict[str, str] = {}
    literal: set[str] = set()
    for match in _TERM.finditer(fold(text)):
        term = match[2] + match[3] if match[2] else " ".join(re.findall(r"[^\W_]+", match[1]))
        if term:
            found.setdefault(term.lower(), term)
        if match[1] is not None or (len(match[2]) > 1 and match[2].isupper() and match[2] not in _OPERATORS):
            literal.add(term.lower())
    kept = [term for key, term in found.items() if key not in _STOP or key in literal]
    return (kept or list(found.values()))[:WORDS]


def identity(text: str) -> bool:
    return _IDENTITY.fullmatch(text.strip()) is not None


def _note_time(value: str) -> str:
    try:
        return moment(value[:10]) if value else ""
    except Error:
        return ""


TIME = "CASE WHEN i.kind='note' THEN note_time(i.time) ELSE i.time END"
PERIOD_TIME = "CASE WHEN i.kind='note' THEN note_time(i.time) ELSE coalesce(nullif(i.time,''),i.observed) END"
DATE = "CASE WHEN i.kind='note' THEN substr(i.time,1,10) ELSE '' END"
UPDATED = "CASE WHEN i.kind='note' THEN note_time(i.time) ELSE coalesce(nullif(i.updated,''),i.time) END"
WITHIN = (
    "((i.kind='record' AND i.time>=:since AND i.time<:until) "
    "OR (i.kind='note' AND i.time!='' AND note_time(i.time)>=:since AND note_time(i.time)<:until))"
)
PERIOD_WITHIN = f"({WITHIN} OR (i.kind='record' AND i.time='' AND i.observed>=:since AND i.observed<:until))"
MODIFIED = (
    "((i.kind='record' AND coalesce(nullif(i.updated,''),i.time)>=:since "
    "AND coalesce(nullif(i.updated,''),i.time)<:until) "
    "OR (i.kind='note' AND i.time!='' AND note_time(i.time)>=:since AND note_time(i.time)<:until))"
)
_FIELDS = (
    f"i.ref,i.kind,i.source,({TIME}) AS time,({DATE}) AS date,i.type,i.item_type,i.status,i.url,i.updated,"
    "i.observed,i.partial"
)
_ROW = f"{_FIELDS},'' AS fragment,i.title,i.lead AS excerpt,i.tasks_open,i.tasks_done,i.next"


def within(column: str, note: str) -> str:
    """Fixed SQL matching sections of `note` (`note#...` before `$`, never `.md`)."""
    return f"{column}>{note}||'#' AND {column}<{note}||'$' AND substr({column},-3)!='.md'"


OWNED = (
    "SELECT name,min(item) AS item FROM names WHERE name IN (SELECT value FROM json_each(:names)) "
    "GROUP BY name HAVING count(*)=1"
)
_LINKING = f"""SELECT l.item FROM links l WHERE l.target IN (SELECT value FROM json_each(:targets))
  UNION SELECT l.item FROM json_each(:sections) s CROSS JOIN links l ON {within("l.target", "s.value")}"""  # noqa: S608 - fixed SQL
FILTERS = f"""((:since='' AND :until='') OR ({PERIOD_TIME})!='')
  AND (:since='' OR ({PERIOD_TIME})>=:since) AND (:until='' OR ({PERIOD_TIME})<:until)
  AND (:observed_since='' OR (i.observed!='' AND i.observed>=:observed_since))
  AND (NOT :undated OR i.time='') AND (:prefix='' OR i.path=:prefix OR substr(i.path,1,length(:prefix)+1)=:prefix||'/')
  AND (:target='' OR (:tag_scope AND i.id IN (SELECT item FROM tags WHERE target=:target))
       OR (NOT :tag_scope AND (i.ref IN (SELECT value FROM json_each(:targets)) OR i.id IN ({_LINKING}))))"""  # noqa: S608


def sections(targets: set[str]) -> list[str]:
    """Targets eligible for #section inheritance: whole note paths and unfragmented BF addresses."""
    result = []
    for value in targets:
        with suppress(Error):
            parsed = bf_links.parse(value)
            if not parsed.fragment if parsed else (authored(value) or bool(re.fullmatch(r"actions/[^/#]+", value))):
                result.append(value)
    return sorted(result)


def _parameters(targets: set[str]) -> dict[str, object]:
    return {"targets": json.dumps(sorted(targets)), "sections": json.dumps(sections(targets))}


def scoped(bounds: Bounds, targets: set[str]) -> dict[str, object]:
    """Build SQL parameter dict for `FILTERS` from `bounds` and expanded `targets`."""
    return {
        **bounds.model_dump(include=set(Bounds.model_fields)),
        **_parameters(targets),
        "tag_scope": bf_links.tag(bounds.target) is not None,
    }


def _string(term: str) -> str:
    words = term.removesuffix("*")
    return '"' + words.replace('"', '""') + '"' + ("*" if words != term else "")


@lru_cache(maxsize=256)
def _forms(word: str) -> tuple[str, str] | None:
    pairs = set(_spellings([word])[0])
    return pairs.pop() if len(pairs) == 1 else None


_CHANGED = (
    "SELECT stem,min(word) FROM words WHERE word>=:folded AND word<:end AND substr(stem,1,:length)!=:stem "
    "GROUP BY stem ORDER BY stem LIMIT :limit"
)
_UNCHANGED = (
    "SELECT term FROM vocabulary WHERE term>=:folded AND term<:end AND substr(term,1,:length)!=:stem "
    "ORDER BY term LIMIT :limit"
)
_MATCHES = "SELECT 1 FROM search WHERE search MATCH ? LIMIT 1"


def _expanded(connection: sqlite3.Connection, term: str) -> str:
    string = _string(term)
    forms = _forms(term[:-1]) if term.endswith("*") and " " not in term else None
    if forms is None or len(forms[0]) < SPELLED:
        return string
    folded, stem = forms
    values = {"folded": folded, "end": folded + "\U0010ffff", "length": len(stem), "stem": stem, "limit": EXPANSIONS}
    spellings: dict[str, str] = dict(connection.execute(_CHANGED, values))
    if folded != stem:
        candidates = [row[0] for row in connection.execute(_UNCHANGED, values) if row[0] not in spellings]
        own = _spellings(candidates) if candidates else []
        spellings.update((word, word) for tokens in own for word, stemmed in tokens if word == stemmed)
    spelled = [_string(spellings[found]) for found in sorted(spellings)[:EXPANSIONS]]
    return f"({' OR '.join([string, *spelled])})" if spelled else string


def expand(connection: sqlite3.Connection, text: str) -> dict[str, str]:
    """Map each extracted term in `text` to its brain-expanded FTS5 query string."""
    return {term: _expanded(connection, term) for term in terms(text)}


def unmatched(connection: sqlite3.Connection, expanded: Mapping[str, str]) -> list[str]:
    """Expanded terms that match nothing anywhere in the brain."""
    return [
        f'"{term}"' if " " in term else term
        for term, string in expanded.items()
        if connection.execute(_MATCHES, (string,)).fetchone() is None
    ]


SUGGESTIONS = 3
CANDIDATES = 5000
SPREAD, WALK = 1000, 64
_SORTED = (
    "SELECT p.id FROM search s JOIN passages p ON p.id=s.rowid JOIN items i ON i.id=p.item "
    "WHERE s.search MATCH :stem ORDER BY i.ref,p.fragment LIMIT 1"
)
# CROSS JOIN keeps leading items outermost for deterministic spelling selection on common stems.
_WALKED = (
    "SELECT p.id FROM items i CROSS JOIN passages p ON p.item=i.id "
    "WHERE i.ref<=(SELECT ref FROM items ORDER BY ref LIMIT 1 OFFSET :walk) "
    "AND EXISTS(SELECT 1 FROM search WHERE search MATCH :stem AND rowid=p.id) ORDER BY i.ref,p.fragment LIMIT 1"
)


def suggestions(connection: sqlite3.Connection, term: str) -> list[str]:
    """Close indexed spellings for an unmatched plain word."""
    if " " in term or term.endswith("*") or len(term) < 4:
        return []
    stem = _stem(term)
    rows = connection.execute(
        "SELECT term,doc FROM vocabulary WHERE term>=? AND term<? LIMIT ?",
        (stem[:2], stem[:2] + "\U0010ffff", CANDIDATES),
    )
    passages = {row[0]: row[1] for row in rows}
    close = difflib.get_close_matches(stem, list(passages), n=SUGGESTIONS, cutoff=0.75)
    return list(dict.fromkeys(word for found in close if (word := _spelled(connection, found, passages[found]))))


@lru_cache(maxsize=256)
def _stem(word: str, tokenizer: str = _TOKENIZER) -> str:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize={tokenizer!r})")
        connection.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, row)")
        connection.execute("INSERT INTO t VALUES(?)", (word,))
        row = connection.execute("SELECT term FROM v LIMIT 1").fetchone()
    return row[0] if row else word.lower()


def _spelled(connection: sqlite3.Connection, stem: str, passages: int) -> str:
    parameters = {"stem": _string(stem), "walk": WALK - 1}
    passage = connection.execute(_WALKED, parameters).fetchone() if passages > SPREAD else None
    if not (passage := passage or connection.execute(_SORTED, parameters).fetchone()):
        return ""
    # Separate lookup by rowid because FTS5 ignores rowid=NULL constraints.
    row = connection.execute(
        "SELECT snippet(search,-1,char(1),char(2),'',1) FROM search WHERE search MATCH ? AND rowid=?",
        (parameters["stem"], passage[0]),
    ).fetchone()
    found = re.search("\x01([^\x01\x02]+)\x02", row[0]) if row else None
    return _stem(found[1], _FOLDED) if found else ""


def _lexical(connection: sqlite3.Connection, params: dict[str, object]) -> Iterator[dict[str, object]]:
    """Rank lexical passage matches by BM25, term coverage, and source priority, deduplicating by note/URL."""
    rows = connection.execute(
        f"""WITH hits AS MATERIALIZED (
             SELECT s.rowid AS passage,count(*) AS n
             FROM json_each(:terms) t CROSS JOIN search s ON s.search MATCH t.value GROUP BY s.rowid),
           matches AS MATERIALIZED (
             SELECT p.item,p.id AS passage,p.fragment,i.ref,i.url,i.source,i.status='deprecated' AS closed,
                    ({TIME}) AS time,-bm25(search,10.0,1.0,0.5,{CONTEXT})*i.weight
                    *(CASE WHEN i.source IN (SELECT value FROM json_each(:low)) THEN 0.5 ELSE 1.0 END)
                    *(1+{COVERAGE}*(coalesce(h.n,1)-1)/max(json_array_length(:terms)-1,1)) AS score
             FROM search JOIN passages p ON p.id=search.rowid JOIN items i ON i.id=p.item
             LEFT JOIN hits h ON h.passage=p.id
             WHERE search MATCH :match AND {FILTERS} AND (p.fragment='' OR bm25(search,10.0,1.0,0.0,0.0)<0)),
           ranked AS MATERIALIZED (
             SELECT *,row_number() OVER copy AS position,first_value(source) OVER copy AS lead FROM matches
             WINDOW copy AS (PARTITION BY CASE WHEN url='' THEN item ELSE url END
                             ORDER BY closed,score DESC,time DESC,fragment,ref ROWS UNBOUNDED PRECEDING)),
           top AS (SELECT * FROM ranked WHERE position=1 OR (url!='' AND source=lead)
                   ORDER BY closed,score DESC,time DESC,ref LIMIT :limit),
           copies AS (
             SELECT url,json_group_array(ref) AS refs
             FROM (SELECT url,ref,row_number() OVER (PARTITION BY url ORDER BY ref) AS n FROM ranked
                   WHERE source!=lead AND url IN (SELECT url FROM top WHERE position=1 AND url!=''))
             WHERE n<={ALSO} GROUP BY url),
           others AS (
             SELECT item,json_group_array(json_array(position,fragment)) AS fragments
             FROM (SELECT item,position,fragment,row_number() OVER (PARTITION BY item ORDER BY position) AS n
                   FROM ranked WHERE position>1 AND fragment!='' AND item IN (SELECT item FROM top WHERE url=''))
             WHERE n<={SECTIONS} GROUP BY item)
           SELECT {_FIELDS},t.fragment,p.title,t.passage AS _passage,c.refs AS also,o.fragments AS sections FROM top t
           JOIN items i ON i.id=t.item JOIN passages p ON p.id=t.passage
           LEFT JOIN copies c ON c.url=t.url AND t.position=1 LEFT JOIN others o ON o.item=t.item
           ORDER BY t.closed,t.score DESC,t.time DESC,t.ref""",  # noqa: S608 - fixed SQL
        params,
    )
    return (dict(row) for row in rows)


_ELLIPSIS = " … "


def excerpt(connection: sqlite3.Connection, expression: str, passage: int) -> str:
    """One-line snippet for one passage matching `expression`, falling back to its lead."""
    row = connection.execute(
        "SELECT snippet(search,1,'','',?,48),p.original,CASE WHEN p.fragment='' THEN i.lead ELSE '' END "
        "FROM search JOIN passages p ON p.id=search.rowid JOIN items i ON i.id=p.item "
        "WHERE search MATCH ? AND search.rowid=?",
        (_ELLIPSIS, expression, passage),
    ).fetchone()
    if row is None:
        return ""
    return re.sub(r"\s+", " ", _verbatim(row[0], row[1]) if row[1] else row[0]).strip() or row[2]


def _verbatim(snippet: str, original: str) -> str:
    """Map folded snippet spans back to whitespace runs of `original`."""
    runs = re.findall(r"\s+|\S+", original)
    forms = [run if run.isascii() else fold(run) for run in runs]
    head = _ELLIPSIS if snippet.startswith(_ELLIPSIS) else ""
    tail = _ELLIPSIS if snippet.endswith(_ELLIPSIS) else ""
    window = snippet.removeprefix(head).removesuffix(tail)
    start = "".join(forms).find(window)
    if start < 0:
        return snippet
    end, position, kept = start + len(window), 0, []
    for run, form in zip(runs, forms, strict=True):
        if position >= end:
            break
        if start < position + len(form):
            kept.append(run)
        position += len(form)
    return head + "".join(kept) + tail


_CANDIDATES = f"""SELECT id AS item,2e6 AS score FROM items WHERE ref=:text
  UNION ALL SELECT item,1e6 FROM names WHERE name IN (SELECT value FROM json_each(:identities))
  UNION ALL SELECT item,1e3 FROM links WHERE target IN (SELECT value FROM json_each(:identities))
  UNION ALL SELECT l.item,1e3 FROM json_each(:identity_sections) s CROSS JOIN links l ON {within("l.target", "s.value")}"""  # noqa: S608 - fixed SQL


def _identity(connection: sqlite3.Connection, params: dict[str, object]) -> Iterator[dict[str, object]]:
    if bf_links.tag(str(params["text"])) is not None:
        return (
            dict(row)
            for row in connection.execute(
                f"SELECT {_FIELDS},'' AS fragment,i.title,1e3 AS score,i.lead AS excerpt FROM items i "  # noqa: S608 - fixed SQL
                f"WHERE i.id IN (SELECT item FROM tags WHERE target=:text) AND {FILTERS} "
                "ORDER BY time DESC,i.ref DESC LIMIT :limit",
                params,
            )
        )
    rows = connection.execute(
        f"""WITH candidates AS ({_CANDIDATES}),
              owners AS (SELECT item,max(score) AS score FROM candidates GROUP BY item)
           SELECT {_FIELDS},'' AS fragment,i.title,c.score,i.lead AS excerpt FROM owners c
           JOIN items i ON i.id=c.item WHERE {FILTERS}
           ORDER BY c.score DESC,time DESC,i.ref DESC LIMIT :limit""",  # noqa: S608 - fixed SQL
        params,
    )
    return (dict(row) for row in rows)


def known(connection: sqlite3.Connection, text: str, identities: set[str]) -> bool:
    """Whether an identity search matches an owner or incoming link."""
    sql = f"SELECT EXISTS (SELECT 1 FROM ({_CANDIDATES}))"  # noqa: S608 - fixed SQL
    return bool(connection.execute(sql, _identities(text, identities)).fetchone()[0])


def _identities(text: str, identities: set[str]) -> dict[str, object]:
    expanded = identities or {text}
    return {
        "text": text,
        "identities": json.dumps(sorted(expanded)),
        "identity_sections": json.dumps(sections(expanded)),
    }


def holders(connection: sqlite3.Connection, name: str) -> list[str]:
    """At most two item refs claiming `name` (two indicates ambiguity)."""
    rows = connection.execute(
        "SELECT i.ref FROM names n JOIN items i ON i.id=n.item WHERE n.name=? ORDER BY i.ref LIMIT 2", (name,)
    )
    return [row[0] for row in rows]


def search(
    connection: sqlite3.Connection,
    query: Query,
    *,
    identities: set[str],
    targets: set[str],
    exact: bool,
    limit: int,
    expanded: Mapping[str, str],
    low: Collection[str] = (),
) -> Iterator[dict[str, object]]:
    """Search for an identity/tag exactly or rank words lexically within scope."""
    text = query.text.strip()
    # Only an identity-shaped query can be a tag address ("bf: how to..." stays words).
    if identity(text) and bf_links.tag(text) is not None:
        text = bf_links.identity(text)
    params: dict[str, object] = {
        **query.model_dump(),
        **scoped(query, targets),
        "limit": limit,
        **_identities(text, identities),
    }
    if exact:
        return ({**_clean(dict(row)), "_rank": row["score"]} for row in _identity(connection, params))
    strings = list(expanded.values())
    if not strings:
        return iter(())
    counted = json.dumps(strings if len(strings) > 1 else [])
    values = {**params, "match": " OR ".join(strings), "terms": counted, "low": json.dumps(sorted(low))}
    rows = _lexical(connection, values)
    return (
        _clean(
            {
                **row,
                "also": sorted(json.loads(str(row["also"]))) if row["also"] else None,
                "sections": [f"{row['ref']}#{f}" for _, f in sorted(json.loads(str(row["sections"])))]
                if row["sections"]
                else None,
            }
        )
        for row in rows
    )


def listing(
    connection: sqlite3.Connection,
    where: str,
    params: Mapping[str, object],
    order: str,
    limit: int = -1,
    offset: int = 0,
    *,
    counted: bool = True,
) -> tuple[Iterator[dict[str, object]], int]:
    """Stream items matching fixed page-builder SQL with optional total count."""
    values = {
        "since": "",
        "until": "",
        "observed_since": "",
        "undated": False,
        "prefix": "",
        "target": "",
        "tag_scope": False,
        **_parameters(set()),
        **params,
        "limit": limit,
        "offset": offset,
    }
    total = (
        connection.execute(f"SELECT count(*) FROM items i WHERE {where}", values).fetchone()[0]  # noqa: S608
        if counted
        else 0
    )
    rows = connection.execute(
        f"SELECT {_ROW} FROM items i WHERE {where} ORDER BY {order} LIMIT :limit OFFSET :offset",  # noqa: S608
        values,
    )
    return (_clean(dict(row)) for row in rows), total


_INCOMING = f"""WITH typed AS (
    SELECT e.item,e.relation FROM edges e WHERE e.relation!='' AND e.target IN (SELECT value FROM json_each(:targets))
    UNION SELECT e.item,e.relation FROM json_each(:sections) s
      CROSS JOIN edges e ON {within("e.target", "s.value")} WHERE e.relation!=''),
  plain AS (SELECT item,'' AS relation FROM ({_LINKING}) WHERE item NOT IN (SELECT item FROM typed)),
  linked AS (SELECT * FROM typed UNION ALL SELECT * FROM plain)"""  # noqa: S608 - fixed SQL fragments
GROUPS = 64
_GROUPS = f"""{_INCOMING} SELECT k.relation,count(*) AS total FROM linked k JOIN items i ON i.id=k.item
  WHERE i.ref!=:exclude GROUP BY k.relation ORDER BY k.relation='',k.relation"""  # noqa: S608
_GROUP = f"""{_INCOMING} SELECT {_ROW} FROM linked k JOIN items i ON i.id=k.item
  WHERE k.relation=:relation AND i.ref!=:exclude ORDER BY time DESC,i.ref DESC LIMIT :limit"""  # noqa: S608
_RELATION = "k.relation IN (SELECT value FROM json_each(:relations)) AND i.ref!=:exclude"
_RELATION_TOTAL = f"{_INCOMING} SELECT count(*) FROM linked k JOIN items i ON i.id=k.item WHERE {_RELATION}"  # noqa: S608
_RELATION_ROWS = f"""{_INCOMING} SELECT {_ROW},k.relation FROM linked k JOIN items i ON i.id=k.item
  WHERE {_RELATION} ORDER BY time DESC,i.ref DESC,k.relation LIMIT :limit OFFSET :offset"""  # noqa: S608


def incoming(
    connection: sqlite3.Connection, targets: set[str], *, exclude: str = "", limit: int
) -> list[dict[str, object]]:
    """Backlinks grouped by explicit relation (untyped LINKS last), previewing the first GROUPS."""
    values = {**_parameters(targets), "exclude": exclude, "limit": limit}
    groups = []
    for number, (relation, total) in enumerate(connection.execute(_GROUPS, values).fetchall()):
        rows = connection.execute(_GROUP, {**values, "relation": relation}).fetchall() if number < GROUPS else []
        groups.append({"relation": relation or LINKS, "total": total, "items": [_clean(dict(row)) for row in rows]})
    return groups


def linking(
    connection: sqlite3.Connection,
    targets: set[str],
    relations: list[str],
    *,
    exclude: str = "",
    limit: int = -1,
    offset: int = 0,
) -> tuple[Iterator[dict[str, object]], int]:
    """Stream items linking to `targets` through `relations`, newest first, with total count."""
    values = {
        **_parameters(targets),
        "exclude": exclude,
        "relations": json.dumps(["" if relation == LINKS else relation for relation in relations]),
        "limit": limit,
        "offset": offset,
    }
    total = connection.execute(_RELATION_TOTAL, values).fetchone()[0]
    rows = connection.execute(_RELATION_ROWS, values)
    return ({**_clean(dict(row)), "relation": row["relation"] or LINKS} for row in rows), total


def _shared(name: str, item: str) -> str:
    return (
        f"(EXISTS (SELECT 1 FROM names o WHERE o.name={name} AND o.item!={item}) "  # noqa: S608 - fixed SQL
        f"OR EXISTS (SELECT 1 FROM items o WHERE o.ref={name} AND o.id!={item}))"
    )


# CROSS JOIN keeps reviewed notes as the outer loop instead of scanning every link/record.
_NEWER = f"""WITH reviewed(note,after) AS MATERIALIZED (
    SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(:reviewed)),
  named(note,name,sections) AS MATERIALIZED (
    SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_extract(value,'$[2]') FROM json_each(:names)),
  linked(note,item) AS (
    SELECT m.note,l.item FROM named m CROSS JOIN links l ON l.target=m.name
    UNION SELECT m.note,l.item FROM named m CROSS JOIN links l ON m.sections AND {within("l.target", "m.name")}
    UNION SELECT r.note,i.id FROM reviewed r CROSS JOIN links l ON l.item=r.note
      CROSS JOIN items i ON i.ref=l.target WHERE i.kind='record'
    UNION SELECT r.note,n.item FROM reviewed r CROSS JOIN links l ON l.item=r.note
      CROSS JOIN names n ON n.name=l.target CROSS JOIN items i ON i.id=n.item
      WHERE i.kind='record' AND NOT {_shared("l.target", "n.item")}),
  newer AS (
    SELECT k.note,i.ref,max(
      CASE WHEN ({TIME})>=r.after AND ({TIME})<=:now THEN ({TIME}) ELSE '' END,
      CASE WHEN i.kind='record' AND i.updated>=r.after AND i.updated<=:now THEN i.updated ELSE '' END) AS at
    FROM linked k JOIN reviewed r ON r.note=k.note JOIN items i ON i.id=k.item WHERE i.id!=k.note)
SELECT note,ref,total FROM (
  SELECT note,ref,count(*) OVER (PARTITION BY note) AS total,
    row_number() OVER (PARTITION BY note ORDER BY at DESC,ref) AS n FROM newer WHERE at!='')
WHERE n<=:limit ORDER BY note,n"""  # noqa: S608 - fixed SQL fragments


def newer_links(
    connection: sqlite3.Connection, edits: Mapping[int, str], now: str, limit: int
) -> dict[int, tuple[int, list[str]]]:
    """Per note id, count and newest refs of linked items dated or updated between its edit and `now`."""
    if not edits:
        return {}
    found: dict[int, set[str]] = {}
    for item, name in connection.execute(
        "SELECT item,name FROM (SELECT id AS item,ref AS name FROM items "  # noqa: S608 - fixed SQL
        "WHERE id IN (SELECT value FROM json_each(:ids)) "
        "UNION ALL SELECT item,name FROM names WHERE item IN (SELECT value FROM json_each(:ids))) m "
        f"WHERE NOT {_shared('m.name', 'm.item')}",
        {"ids": json.dumps(sorted(edits))},
    ):
        found.setdefault(item, set()).add(name)
        if re.fullmatch(r"actions/[^/#]+/ACTION\.md", name):
            found[item].add(name.removesuffix("/ACTION.md"))
        with suppress(Error):
            if (parsed := bf_links.parse(name)) and re.fullmatch(r"actions/[^/#]+/ACTION\.md", parsed.path):
                found[item].add(bf_links.address(parsed.brain, parsed.path.removesuffix("/ACTION.md")))
    names = []
    for item, known in found.items():
        taking = set(sections(known))
        names.extend([item, name, name in taking] for name in sorted(known))
    values = {"reviewed": json.dumps(sorted(edits.items())), "names": json.dumps(names), "now": now, "limit": limit}
    result: dict[int, tuple[int, list[str]]] = {}
    for item, ref, total in connection.execute(_NEWER, values):
        result.setdefault(item, (total, []))[1].append(ref)
    return result


def _clean(row: dict[str, object]) -> dict[str, object]:
    fragment = row.pop("fragment", "")
    if fragment:
        row["ref"] = f"{row['ref']}#{fragment}"
    if isinstance(excerpt := row.get("excerpt"), str):
        row["excerpt"] = re.sub(r"\s+", " ", excerpt).strip()
    if isinstance(title := row.get("title"), str):
        row["title"] = preview(title, TITLE)
    row.pop("score", None)
    row.pop("position", None)
    opened, done = int(cast("int", row.pop("tasks_open", 0) or 0)), int(cast("int", row.pop("tasks_done", 0) or 0))
    if opened or done:
        row["tasks"] = {"open": opened, "done": done}
    if row.get("partial"):
        row["partial"] = True
    else:
        row.pop("partial", None)
    item_type = row.pop("item_type", "")
    if row["kind"] == "note":
        row.pop("source", None)
        row.pop("updated", None)
    else:
        row.pop("type", None)
        if item_type:
            row["@type"] = item_type
    return {key: value for key, value in row.items() if value not in ("", None)}


def problems(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Skipped files shared across search, pages, and status, capped at 200 entries."""
    rows = connection.execute("SELECT path,error FROM files WHERE error!='' ORDER BY path LIMIT 201").fetchall()
    result: list[dict[str, object]] = [{"file": row["path"], "error": _capped(row["error"])} for row in rows[:200]]
    if len(rows) > 200:
        more = connection.execute("SELECT count(*) FROM files WHERE error!=''").fetchone()[0] - 200
        rest = "1 more file was" if more == 1 else f"{more} more files were"
        result.append({"error": f"{rest} skipped; run bf validate"})
    return result


def sources(connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Indexed record totals, latest event time, and latest `observed` change per source."""
    return {
        row["source"]: {
            "records": row["records"],
            **({"latest": row["latest"]} if row["latest"] else {}),
            **({"changed": row["changed"]} if row["changed"] else {}),
        }
        for row in connection.execute(
            "SELECT source,count(*) AS records,max(time) AS latest,max(observed) AS changed FROM items "
            "WHERE kind='record' GROUP BY source"
        )
    }


def activity(connection: sqlite3.Connection, since: str, until: str) -> list[tuple[str, int]]:
    """Records per source placed in [since, until) by event time or observation, busiest first."""
    return [
        (row[0], row[1])
        for row in connection.execute(
            f"SELECT source,count(*) FROM items i WHERE kind='record' AND {PERIOD_WITHIN} "  # noqa: S608 - fixed SQL; bounds are parameters
            "GROUP BY source ORDER BY count(*) DESC,source",
            {"since": since, "until": until},
        )
    ]


def capacity(store: Store, counts: dict[str, int] | None = None) -> list[dict[str, object]]:
    """Scanned trees exceeding `CROWDED` entries, reusing `counts` when already scanned."""
    if not counts:
        counts = {}
        inputs(store, counts)
    return [
        {"warning": "directory nears the scan limit", "directory": tree, "entries": entries, "limit": MAX_FILES}
        for tree, entries in sorted(counts.items())
        if entries > CROWDED
    ]


def status(store: Store) -> dict[str, object]:
    """Cache state, counts, per-source sizes, skipped files, and trees nearing `MAX_FILES`."""
    pending = _abandoned(store)
    entries: dict[str, int] = {}
    with _last(store) if pending else database(store, entries) as (connection, state):
        counts = sources(connection)
        notes = connection.execute("SELECT count(*) FROM items WHERE kind='note'").fetchone()[0]
        skipped = problems(connection)
        sizes = dict(
            connection.execute(
                "SELECT substr(path,10,instr(substr(path,10),'/')-1),sum(size) FROM files "
                "WHERE path GLOB 'memories/?*/*' AND size>=0 GROUP BY 1"
            ).fetchall()
        )
    for source, entry in counts.items():
        entry["bytes"] = sizes.get(source, 0)
    if pending:
        issue = "memories/.pending holds an interrupted record transaction; run bf update or bf build to recover it"
        skipped.insert(0, {"error": issue})
    crowded = capacity(store, entries)
    return {
        "cache": state,
        **({"pending_transaction": True} if pending else {}),
        "notes": notes,
        "sources": counts,
        "problems": skipped,
        **({"warnings": crowded} if crowded else {}),
    }


def _abandoned(store: Store) -> bool:
    try:
        with reader(store, wait=0):
            return records.interrupted(store)
    except BusyError:
        return False


@contextmanager
def _last(store: Store) -> Generator[tuple[sqlite3.Connection, str]]:
    """Last cache generation without refreshing when a pending transaction exists."""
    _path(store)
    with generation(store, shared=True, wait=_PUBLISH):
        connection, state = _open(store), "stale"
        if connection is None:
            connection, state = _connect(":memory:"), "missing"
            connection.executescript(_DDL)
        with closing(connection):
            yield connection, state


@contextmanager
def _readonly(store: Store) -> Generator[sqlite3.Connection | None]:
    """Read-only snapshot of an existing cache without creating or touching `.bf/`, or None when missing/older."""
    try:
        store.fingerprint(CACHE)
    except FileNotFoundError:
        yield None
        return
    except OSError as error:
        raise Error(f"the search cache is unreadable: {error}") from error
    with generation(store, shared=True, wait=_PUBLISH):
        try:
            connection = _connect(store.root / CACHE)
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0 or (version == SCHEMA and _schema(connection) != _layout()):
                    raise Error("the search cache is unreadable; run bf build")
                if version != SCHEMA:
                    connection.close()
                    yield None
                    return
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
            except Exception:
                connection.close()
                raise
        except sqlite3.DatabaseError as error:
            raise Error("the search cache is unreadable; run bf build") from error
        with closing(connection):
            try:
                yield connection
            except sqlite3.DatabaseError as error:
                raise Error("the search cache is unreadable; run bf build") from error


def watermarks(store: Store) -> dict[str, tuple[str, str, str, str, str]]:
    """Indexed items keyed by ref: (kind, source, observed, sha256, title), or {} when the cache is absent."""
    with _readonly(store) as connection:
        if connection is None:
            return {}
        cursor = connection.cursor()
        cursor.row_factory = None
        return {
            row[0]: (row[1], row[2], row[3], row[4], row[5])
            for row in cursor.execute("SELECT ref,kind,source,observed,sha256,title FROM items")
        }


def match_refs(store: Store, query: str, refs: Collection[str]) -> set[str]:
    """Refs among `refs` currently indexed whose passages match `query`, or set() when absent."""
    if not refs or not terms(query):
        return set()
    with _readonly(store) as connection:
        if connection is None or not (strings := list(expand(connection, query).values())):
            return set()
        return {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT i.ref FROM search JOIN passages p ON p.id=search.rowid "
                "JOIN items i ON i.id=p.item WHERE search MATCH ? AND i.ref IN (SELECT value FROM json_each(?))",
                (" OR ".join(strings), json.dumps(sorted(set(refs)))),
            )
        }


def text_matches(query: str, text: str) -> bool:
    """Whether plain `text` matches `query` under FTS5 word folding, stemming, prefixes and phrases."""
    if not (found := terms(query)) or not text.strip():
        return False
    _supported()
    match = " OR ".join(_string(term) for term in found)
    folded = fold(text)
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(f"CREATE VIRTUAL TABLE s USING fts5(x, tokenize={_TOKENIZER!r})")
        connection.execute(f"CREATE VIRTUAL TABLE f USING fts5(x, tokenize={_FOLDED!r})")
        connection.execute("INSERT INTO s VALUES(?)", (folded,))
        connection.execute("INSERT INTO f VALUES(?)", (folded,))
        return bool(
            connection.execute(
                "SELECT EXISTS(SELECT 1 FROM s WHERE s MATCH ?) OR EXISTS(SELECT 1 FROM f WHERE f MATCH ?)",
                (match, match),
            ).fetchone()[0]
        )
