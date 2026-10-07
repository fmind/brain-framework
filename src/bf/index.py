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
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from contextvars import ContextVar
from functools import cache, lru_cache
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from bf import links as bf_links
from bf import models, ontology, records
from bf.config import load
from bf.markdown import LEAD, authored, editor_lock, entry_note, note
from bf.models import (
    AUTHORED,
    IDENTITY,
    LINKS,
    MAX_FILES,
    MAX_NOTE,
    Config,
    Error,
    Query,
    Record,
    digest,
    encode,
    fold,
    moment,
)
from bf.storage import UNNAMED, BusyError, Store, building, generation, reader, unnamed, writer

SCHEMA = 34
CACHE = ".bf/index.sqlite"
# A full build fills this file beside the live cache, then renames it over CACHE.
BUILD = CACHE + ".new"
_SIDECARS = ("", "-wal", "-shm", "-journal")
# Seconds a finished build waits for connections to the generation it replaces to close.
_PUBLISH = 30
# Seconds a read that found a damaged generation waits for other connections to it before leaving it to the next one.
_DISCARD = 5
# `bf status` warns about a scanned tree once it holds more entries than this.
CROWDED = MAX_FILES * 4 // 5
# A skipped file's problem keeps at most this many bytes: a reply's 200 problems per brain stay far below MAX_REPLY.
_PROBLEM = 1024
# The oldest SQLite with every feature the cache uses; `AS MATERIALIZED` came last.
_SQLITE = (3, 35, 0)
# Query features that SQLite builds can leave out or predate: a materialized CTE, a JSON table and a named window.
_PROBE = (
    "WITH t AS MATERIALIZED (SELECT value FROM json_each('[1]')) "
    "SELECT row_number() OVER w FROM t WINDOW w AS (ORDER BY value)"
)
# Size, modification and change times in nanoseconds, and inode, of each brain-relative path.
Fingerprints = dict[str, tuple[int, int, int, int]]
# `file` binds a generation to the database file bf created: a cache copied, cloned or extracted from
# elsewhere has another inode, so it is rebuilt from the brain's files instead of trusted.
_TOKENIZER = "porter unicode61 remove_diacritics 2"
_DDL = f"""
CREATE TABLE ontology(signature TEXT NOT NULL, file TEXT NOT NULL);
-- A rowid table: secondary indexes of a WITHOUT ROWID table would copy its whole composite key.
CREATE TABLE edges(item INTEGER NOT NULL, subject TEXT NOT NULL, relation TEXT NOT NULL, target TEXT NOT NULL,
                   origin TEXT NOT NULL);
CREATE TABLE files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, ctime INTEGER, inode INTEGER,
                   error TEXT NOT NULL DEFAULT '');
CREATE TABLE items(id INTEGER PRIMARY KEY, ref TEXT NOT NULL UNIQUE, path TEXT NOT NULL, kind TEXT NOT NULL,
                   source TEXT NOT NULL, title TEXT NOT NULL, time TEXT NOT NULL, type TEXT NOT NULL,
                   status TEXT NOT NULL, lead TEXT NOT NULL, url TEXT NOT NULL,
                   updated TEXT NOT NULL, observed TEXT NOT NULL, partial INTEGER NOT NULL,
                   tasks_open INTEGER NOT NULL, tasks_done INTEGER NOT NULL, next TEXT NOT NULL,
                   weight INTEGER NOT NULL, stale_after TEXT NOT NULL,
                   fields TEXT NOT NULL);
CREATE TABLE tasks(item INTEGER NOT NULL, line INTEGER NOT NULL, fragment TEXT NOT NULL,
                   text TEXT NOT NULL, done INTEGER NOT NULL, PRIMARY KEY(item,line)) WITHOUT ROWID;
-- `search` holds folded words; `original` keeps a passage's text only when folding changed it, for excerpts.
-- `names` holds identity words; `context` the headings a passage ranks under: a note's tags, a section's parents.
CREATE TABLE passages(id INTEGER PRIMARY KEY, item INTEGER NOT NULL, fragment TEXT NOT NULL, title TEXT NOT NULL,
                      original TEXT NOT NULL);
CREATE VIRTUAL TABLE search USING fts5(title, text, names, context, tokenize='{_TOKENIZER}');
-- The indexed terms, read for spelling suggestions; it stores nothing itself.
CREATE VIRTUAL TABLE vocabulary USING fts5vocab(search, row);
CREATE TABLE names(name TEXT NOT NULL, item INTEGER NOT NULL, PRIMARY KEY(name, item)) WITHOUT ROWID;
CREATE TABLE links(item INTEGER NOT NULL, target TEXT NOT NULL, PRIMARY KEY(item, target)) WITHOUT ROWID;
CREATE TABLE tags(item INTEGER NOT NULL, target TEXT NOT NULL, PRIMARY KEY(item, target)) WITHOUT ROWID;
"""
# A full build creates secondary indexes after loading every file: sorting once beats growing each B-tree.
# Pages list notes by kind, records by time, and each source's records and totals without scanning every item.
_INDEXES = (
    "CREATE INDEX edges_item ON edges(item)",
    "CREATE INDEX edges_target ON edges(target)",
    "CREATE INDEX edges_subject ON edges(subject,relation)",
    "CREATE INDEX items_path ON items(path)",
    "CREATE INDEX items_kind_time ON items(kind,time)",
    "CREATE INDEX items_kind_source_time ON items(kind,source,time)",
    # The expression must stay identical to the record branch of MODIFIED for SQLite to use it.
    "CREATE INDEX items_kind_modified ON items(kind,coalesce(nullif(updated,''),time))",
    "CREATE INDEX passages_item ON passages(item)",
    "CREATE INDEX tasks_done ON tasks(done,item,line)",
    "CREATE INDEX names_item ON names(item)",
    "CREATE INDEX links_target ON links(target)",
    "CREATE INDEX tags_target ON tags(target)",
    # Every reply looks for skipped files, usually none: only their rows are indexed.
    "CREATE INDEX files_problems ON files(path) WHERE error!=''",
)
# Only English and French function words are dropped: a subject such as "resume", "active" or "comment" stays
# literal. English words that stem to one of them, such as "one" to "on", drop too: they would match nearly every text.
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
# Written in capitals, a function word is an acronym and stays a term (EU AI Act, IT budget); AND and OR are
# habitual operators, which plain words already imply.
_OPERATORS = frozenset({"AND", "OR"})
_IDENTITY = re.compile(IDENTITY)
# A quoted phrase, or a word with an optional trailing `*` for a prefix; other characters only separate terms.
_TERM = re.compile(r'["“”]([^"“”]*)["“”]|([^\W_]+)(\*?)')
# A word query matches any of its first 32 distinct terms; later terms are ignored.
WORDS = 32
# A lexical result names at most this many other matching records that share its URL.
ALSO = 5
# A lexical result names at most this many other matching sections of its note.
SECTIONS = 3
# A passage matching all of a query's several terms gains this fraction of its score, one matching some of them a
# proportional part: 2 of 3 terms gain half of it. A passage matching a single term gains nothing.
COVERAGE = 0.2
# The weight of a passage's context (a section's note title and parents, a note's tags) against a heading's 10: a
# section mentioning its note's title still ranks below the note, while "Atlas next actions" finds that section.
CONTEXT = 3.0
# Result and listing titles are previews of at most this many characters; exact reads keep the whole title.
TITLE = 200
# A listed field value longer than this stays in the exact read: listings show short facts only.
FACT = 200
# An item's listed fields, in name order, end before this many bytes of JSON: an item that declares hundreds of
# fields, as a shared brain may, cannot push a listing, home or a note's backlinks past the reply limit.
FACTS = 2048
# SQL twins of markdown.action_note and markdown.entry_note over `items i`, for page builders.
ACTION = (
    "i.kind='note' AND substr(i.path,1,8)='actions/' AND substr(i.path,-10)='/ACTION.md' "
    "AND length(i.path)-length(replace(i.path,'/',''))=2"
)
ENTRY = (
    f"i.kind='note' AND (substr(i.path,1,9) IN ('projects/','concepts/') OR ({ACTION})) "
    "AND i.path NOT GLOB '*/index.md' AND i.path NOT GLOB '*/log.md'"
)


def inputs(store: Store, counts: dict[str, int] | None = None) -> Fingerprints:
    """Authored notes and record files with their file fingerprints.

    Every symlink, special entry or unaddressable name stays an input so its exclusion is reported. A link can
    hide a whole directory regardless of its filename; never inspect its target to guess its contents. Editor
    locks beside authored files are neither: an open editor must not fail updates. The scan limit bounds each
    authored folder and each source of memories/ on its own; `counts` receives their entries.
    """
    found: Fingerprints = {}
    for directory in (*AUTHORED, "memories"):
        skipped: Fingerprints = {}
        scanned = store.scan(directory, skipped=skipped, split=directory == "memories", counts=counts)
        if directory == "memories":
            found.update({name: info for name, info in scanned.items() if records.stored(name)})
            found.update(skipped)
            continue
        found.update({name: info for name, info in scanned.items() if authored(name) and not editor_lock(name)})
        found.update({name: info for name, info in skipped.items() if not editor_lock(name)})
    return found


@contextmanager
def _writable() -> Iterator[None]:
    """Name the cache's write access, which retrieval needs even for a brain it only reads."""
    try:
        yield
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            raise Error(
                "search and read need write access to the brain's .bf cache directory; grant it or use a writable copy"
            ) from error
        raise


def _path(store: Store, name: str = CACHE) -> Path:
    # One no-follow descriptor checks and tightens .bf, so a swapped symlink cannot redirect either step;
    # Store.parent names a .bf that is a symlink or not a directory.
    with _writable(), store.parent(name, create=True) as (directory, _name):
        info = os.fstat(directory)
        if info.st_uid != os.getuid():
            raise Error(".bf belongs to another user; remove it so bf can rebuild its own cache")
        if stat.S_IMODE(info.st_mode) & 0o077:
            # A copied or extracted brain can bring a loose cache directory; the cache holds every record's text.
            os.fchmod(directory, stat.S_IMODE(info.st_mode) & 0o700)
    path = store.root / name
    for suffix in _SIDECARS:
        with suppress(FileNotFoundError):
            store.fingerprint(name + suffix)
    return path


def _remove(store: Store, name: str, suffixes: Sequence[str] = _SIDECARS) -> None:
    """Delete a database file and its SQLite sidecars without following links."""
    with _writable(), suppress(FileNotFoundError), store.parent(name) as (directory, leaf):
        for suffix in suffixes:
            with suppress(FileNotFoundError):
                os.unlink(leaf + suffix, dir_fd=directory)


def _open(store: Store, config: Config | None = None) -> sqlite3.Connection | None:
    """The current cache generation, or None when it is missing, outdated or unreadable.

    A refresh passes the configuration it indexes with, so the rows it adds match the generation's signature.
    """
    path = _path(store)
    signature = _signature(load(store) if config is None else config)
    try:
        # A concurrent full build may rename its generation over the file between this check and the connection:
        # that generation's identity then fails below, and the caller checks again.
        expected = (signature, _file(path))
    except FileNotFoundError:
        return None
    connection = _connect(path)
    # Queries shared with page builders place notes in time on the reading machine. A note's date resolves once per
    # connection: a page compares the same few dates on every row, and the next reply sees a changed timezone.
    connection.create_function("note_time", 1, lru_cache(maxsize=4096)(_note_time), deterministic=True)
    try:
        # The layout bf creates, and nothing more: a trigger or view would add rows no brain file holds, and a
        # missing or altered table would fail queries later. SQLite stores each statement's text as written.
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
    """A database's tables, indexes, triggers and views, without the statistics SQLite may add itself."""
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_stat%' ORDER BY type,name"
    )
    return [tuple(row) for row in rows]


@cache
def _layout() -> list[tuple[object, ...]]:
    """The layout of a complete generation, from the statements that build one; FTS5 adds its own tables."""
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(_DDL)
        for statement in _INDEXES:
            connection.execute(statement)
        return _schema(connection)


def _connect(path: Path | str) -> sqlite3.Connection:
    _supported()
    connection = sqlite3.connect(path, timeout=30)
    # The cache is data found on disk: its schema may not run functions or allow corrupting writes.
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    connection.row_factory = sqlite3.Row
    return connection


@cache
def _supported() -> None:
    """Name a Python whose SQLite cannot hold the cache, whose errors would otherwise read as damage or a full disk.

    Checked once per process when it passes: the library cannot change meanwhile.
    """
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            # The schema needs FTS5 with its tokenizer options; queries need the probed features.
            connection.executescript(_DDL)
            connection.execute(_PROBE).fetchall()
        capable = sqlite3.sqlite_version_info >= _SQLITE
    except sqlite3.Error:
        capable = False
    if not capable:
        raise Error(
            f"the search cache needs SQLite {'.'.join(map(str, _SQLITE))} or newer with FTS5 and JSON functions, which "
            f"this Python's SQLite {sqlite3.sqlite_version} lacks; reinstall bf on a uv-managed Python: "
            "uv tool install --reinstall --managed-python --python 3.14 brain-framework"
        )


def _signature(config: Config) -> str:
    """The configuration a generation's rows depend on. Documentation, such as a field's description, is not, nor
    are `broader`, which relation pages read at query time, and `targets`, which collection and validation check."""
    schema = {
        n: f.model_dump(exclude={"description", "examples", "broader", "targets"}) for n, f in config.ontology.items()
    }
    return digest(encode({"name": config.name, "fields": schema}))


def _file(path: Path) -> str:
    """The database file's inode, which no copy of it shares.

    Not its device number: a remount can assign another one, as for btrfs subvolumes, NFS or FUSE, while the file
    and its rows stay the same.
    """
    return str(path.lstat().st_ino)


def _create(store: Store, config: Config) -> sqlite3.Connection:
    """An empty generation beside the live cache in rollback-journal mode; the build switches it to WAL once filled.

    A new file needs almost no rollback journal, while WAL would write every page twice: once to the log
    and again when checkpointing it. Only the build lock holder uses BUILD: any file there is an abandoned build.
    """
    path = _path(store, BUILD)
    _remove(store, BUILD)
    with _writable():
        # Private from creation: SQLite gives its journal and WAL files the database file's mode.
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    connection = _connect(path)
    # Version zero withholds this generation until the ingestion transaction commits. One transaction creates the
    # schema: executescript adds none, and each statement would otherwise commit and sync on its own.
    connection.executescript("BEGIN;" + _DDL)
    connection.execute("INSERT INTO ontology VALUES(?,?)", (_signature(config), _file(path)))
    connection.commit()
    return connection


def _known(connection: sqlite3.Connection) -> Fingerprints:
    cursor = connection.cursor()
    # Plain tuples: every reply's freshness check reads the fingerprint of every indexed file.
    cursor.row_factory = None
    return {row[0]: row[1:] for row in cursor.execute("SELECT path,size,mtime,ctime,inode FROM files")}


def _drop(connection: sqlite3.Connection, path: str) -> None:
    """Delete one file's rows with one statement per table, however many items the file holds."""
    owned = "item IN (SELECT id FROM items WHERE path=:path)"
    values = {"path": path}
    connection.execute(f"DELETE FROM search WHERE rowid IN (SELECT id FROM passages WHERE {owned})", values)  # noqa: S608 - fixed SQL
    for table in ("passages", "names", "links", "edges", "tags", "tasks"):
        connection.execute(f"DELETE FROM {table} WHERE {owned}", values)  # noqa: S608 - fixed table names
    connection.execute("DELETE FROM items WHERE path=?", (path,))
    connection.execute("DELETE FROM files WHERE path=?", (path,))


class _Rows:
    """One file's rows, numbered up front so each table takes a single batched insert."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.item, self.passage = connection.execute(
            "SELECT (SELECT coalesce(max(id),0)+1 FROM items),(SELECT coalesce(max(id),0)+1 FROM passages)"
        ).fetchone()
        self.items: list[dict[str, str | int]] = []
        self.passages: list[tuple[int, int, str, str, str]] = []
        self.search: list[tuple[int, str, str, str, str]] = []
        self.names: list[tuple[str, int]] = []
        self.links: list[tuple[int, str]] = []
        self.edges: dict[tuple[int, str, str, str, str], None] = {}

    def add(
        self,
        values: dict[str, str | int],
        passages: list[tuple[str, str, str, str, str, str]],
        names: list[str],
        links: list[str],
        edges: Sequence[bf_links.Claim] = (),
    ) -> None:
        item = self.item
        self.item += 1
        self.items.append(
            {
                "id": item,
                "tasks_open": 0,
                "tasks_done": 0,
                "next": "",
                "weight": 1,
                "stale_after": "",
                "fields": "",
                **values,
            }
        )
        # `heading` is what ranks; `title` is what readers see, such as "Note — Section".
        for fragment, title, heading, text, identity, context in passages:
            folded = fold(text)
            self.passages.append((self.passage, item, fragment, title, "" if folded == text else text))
            self.search.append((self.passage, fold(heading), folded, fold(identity), fold(context)))
            self.passage += 1
        self.names.extend((name, item) for name in names)
        self.links.extend((item, link) for link in links)
        # Two identical claims from one file are one edge; support from other files stays separate.
        self.edges.update(dict.fromkeys((item, e.subject, e.relation, e.target, e.origin) for e in edges))

    def write(self, connection: sqlite3.Connection) -> None:
        connection.executemany(
            "INSERT INTO items(id,ref,path,kind,source,title,time,type,status,lead,url,updated,observed,partial,"
            "tasks_open,tasks_done,next,weight,stale_after,fields) VALUES(:id,:ref,:path,:kind,:source,:title,"
            ":time,:type,:status,:lead,:url,:updated,:observed,:partial,:tasks_open,:tasks_done,:next,:weight,"
            ":stale_after,:fields)",
            self.items,
        )
        connection.executemany("INSERT INTO passages(id,item,fragment,title,original) VALUES(?,?,?,?,?)", self.passages)
        connection.executemany("INSERT INTO search(rowid,title,text,names,context) VALUES(?,?,?,?,?)", self.search)
        connection.executemany("INSERT OR IGNORE INTO names VALUES(?,?)", self.names)
        connection.executemany("INSERT OR IGNORE INTO links VALUES(?,?)", self.links)
        connection.executemany("INSERT INTO edges VALUES(?,?,?,?,?)", self.edges)


def _facts(values: Mapping[str, JsonValue], config: Config) -> str:
    """Declared single-value fields, such as a status or an assignee, each at most FACT characters.

    Listings show them beside each item, so sources that disagree appear side by side without another read.
    """
    facts = {}
    size = 0
    for name, value in sorted(values.items()):
        definition = config.ontology.get(name)
        if definition is None or definition.cardinality == "many":
            continue
        with suppress(ValueError):
            normalized = definition.normalize(value)
            if isinstance(normalized, str | int | float | bool) and len(str(normalized)) <= FACT:
                # The listed JSON's size: each entry with its two-byte separator, or the braces for the first.
                size += len(json.dumps({name: normalized}, ensure_ascii=False).encode())
                if size > FACTS:
                    break
                facts[name] = normalized
    return json.dumps(facts, ensure_ascii=False, sort_keys=True) if facts else ""


def _index(connection: sqlite3.Connection, store: Store, path: str, config: Config) -> None:
    """Insert one file; a parse failure raises. Refs cannot repeat: a note ref is its path, and a record
    ref `source:id` names the one file whose SHA-256 filename matches that id.
    """
    if unnamed(path):
        # Only the scan's escaped form of the name exists: nothing can open it.
        raise Error(UNNAMED)
    # Validate skipped entries before interpreting their extension as a record file.
    if not authored(path) and not path.endswith(".json"):
        store.read(path, 0)
    if authored(path):
        projection = note(path, store.read(path, MAX_NOTE))
        knowledge = projection.knowledge
        edges = ontology.note_claims(projection, config)
        # Declared field values are searchable words, like a record's.
        values = [
            str(v) for value in knowledge.fields.values() for v in (value if isinstance(value, list) else [value])
        ]
        metadata = " ".join([knowledge.type, *knowledge.aliases, *projection.links, *values])
        rows = _Rows(connection)
        rows.add(
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
                "fields": _facts(knowledge.fields, config),
            },
            # The whole note ranks under its tags and each section under its parents, like headings (see _lexical).
            [
                (
                    p.fragment,
                    p.title,
                    p.heading,
                    p.text,
                    "" if p.fragment else metadata,
                    " ".join(p.parents or knowledge.tags),
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
        rows.write(connection)
        connection.executemany(
            "INSERT INTO tasks SELECT id,?,?,?,? FROM items WHERE ref=?",
            [(task.line, task.fragment, task.text, int(task.done), path) for task in projection.tasks],
        )
        connection.executemany(
            "INSERT INTO tags(item,target) SELECT id,? FROM items WHERE ref=?",
            [(ontology.qualify(config, f"tags/{tag}"), path) for tag in knowledge.tags],
        )
        return
    source = records.source_of(path)
    record = records.load(store, path)
    ref = f"{source}:{record.id}"
    edges = ontology.record_claims(record, config, ref)
    # Field values are searchable words; their JSON keys and punctuation would match every record.
    values = [v for value in record.fields.values() for v in (value if isinstance(value, list) else [value])]
    identity = " ".join([source, *record.aliases, *record.links, record.url, *map(str, values)])
    rows = _Rows(connection)
    rows.add(
        {
            "ref": ref,
            "path": path,
            "kind": "record",
            "source": source,
            "title": record.title,
            "time": record.time,
            "type": "record",
            "status": "",
            "lead": lead(record),
            "url": record.url,
            "updated": record.updated,
            "observed": record.observed,
            "partial": int(record.attributes.get("partial") is True),
            "fields": _facts(record.fields, config),
        },
        [("", record.title, record.title, record.text, identity, "")],
        sorted({ontology.qualify(config, ref), *(bf_links.target(v) for v in record.aliases)}),
        sorted({*(bf_links.target(v) for v in record.links), *(e.target for e in edges)}),
        edges,
    )
    rows.write(connection)


def lead(record: Record) -> str:
    """The first LEAD characters with whitespace collapsed, reading only a growing prefix of a long text.

    Collapsing a prefix always yields a prefix of the collapsed text, even when the cut splits a word.
    """
    size = 4 * LEAD
    while True:
        collapsed = " ".join(record.text[:size].split())
        if len(collapsed) >= LEAD or size >= len(record.text):
            return collapsed[:LEAD]
        size *= 4


def refresh(
    store: Store,
    *,
    full: bool = False,
    wait: float = 30,
    recover: bool = False,
    compared: tuple[Fingerprints, Fingerprints] | None = None,
) -> dict[str, object]:
    """Re-index only changed files; a file that fails to parse is skipped and reported, not fatal.

    `full`, or a cache that is missing or incompatible, builds a new generation. `full` and `recover` first roll
    back an interrupted record transaction, as build and update do; reads refuse to apply its journal instead.
    `recover` also checks every page of the live generation: a damaged one is discarded and built again.
    `compared` holds the cache's fingerprints and the brain's that a freshness check just compared: while the
    cache still holds the former, the refresh indexes from that scan instead of scanning the brain again.
    """
    try:
        try:
            return _refresh(store, full=full, wait=wait, recover=full or recover, compared=compared)
        except sqlite3.DatabaseError as error:
            if not _damaged(error):
                raise
        # SQLite never repairs a damaged file in place, and the cache holds nothing the brain's files do not.
        _discard(store, wait)
        return _refresh(store, full=full, wait=wait, recover=full or recover, compared=compared)
    except sqlite3.DatabaseError as error:
        if getattr(error, "sqlite_errorcode", 0) & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            # Another program, such as a sqlite3 shell, holds the cache: a rebuild would wait on it too.
            raise Error("another process holds the search cache; close it and retry") from error
        raise Error("could not refresh the search cache; check free space and run bf build") from error


class _DamagedError(sqlite3.DatabaseError):
    """A page check found damage that no query has read yet."""


def _damaged(error: sqlite3.DatabaseError) -> bool:
    """Whether SQLite found the cache file damaged or not a database, rather than busy, full or unreadable."""
    code = getattr(error, "sqlite_errorcode", None)
    # Extended codes, such as SQLITE_CORRUPT_VTAB for FTS5 damage, keep the primary code in their low byte.
    return isinstance(error, _DamagedError) or (
        isinstance(code, int) and code & 0xFF in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
    )


def _discard(store: Store, wait: float) -> None:
    """Delete a damaged generation so the next freshness check builds another, once its connections close.

    Only a generation whose own page check fails goes: the error may have come from another brain's cache in the
    same reply. A build in progress replaces it anyway; a busy writer or reader leaves it to the next one to fail.
    """
    with (
        suppress(BusyError),
        building(store, 0),
        writer(store, wait),
        generation(store, shared=False, wait=wait),
    ):
        connection = _open(store)
        if connection is not None:
            # No other connection can be open here: any database error means this file cannot serve.
            with closing(connection), suppress(sqlite3.DatabaseError):
                if connection.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok":
                    return
        _remove(store, CACHE)


def _refresh(
    store: Store, *, full: bool, wait: float, recover: bool, compared: tuple[Fingerprints, Fingerprints] | None
) -> dict[str, object]:
    if not full and (result := _incremental(store, wait=wait, recover=recover, compared=compared)) is not None:
        return result
    with building(store, wait):
        if full:
            with writer(store, wait):
                records.recover(store)
        # Another build may have published a compatible generation while this one waited.
        elif (result := _incremental(store, wait=wait, recover=recover, compared=compared)) is not None:
            return result
        return _build(store, wait)


def _incremental(
    store: Store, *, wait: float, recover: bool, compared: tuple[Fingerprints, Fingerprints] | None
) -> dict[str, object] | None:
    """Update the live generation in place; None when there is no compatible one."""
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
            # Refreshes read only the pages of changed files: damage elsewhere would fail every later search.
            if recover and connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise _DamagedError("the search cache is damaged")
            return _fill(store, connection, config, new=False, compared=compared)


def _build(store: Store, wait: float) -> dict[str, object]:
    """Fill a new generation beside the live cache, then publish it; a failed build leaves the live cache.

    The build lock keeps one build at a time, but the brain writer lock is held only to publish: searches keep
    reading the live cache, incremental refreshes keep updating it, and collections keep committing. A file that
    changes after the build fingerprinted it no longer matches its fingerprint, so the next refresh indexes it again.
    """
    config = load(store)
    try:
        with closing(_create(store, config)) as connection:
            result = _fill(store, connection, config, new=True)
            # Incremental refreshes then commit beside readers instead of blocking them.
            connection.execute("PRAGMA journal_mode=WAL")
        # Rather than discard a finished build, wait out a collection's commit.
        with writer(store, max(wait, _PUBLISH)):
            _publish(store)
    finally:
        # After a publish, only sidecars an unclean close left can remain.
        _remove(store, BUILD)
    return result


def _publish(store: Store) -> None:
    """Rename the finished build over the live cache, under the brain writer lock.

    SQLite finds a database's -wal and -shm files by name, never checking which database they belong to: a
    connection to the old file would attach the new generation's log. Readers therefore hold the generation lock
    while connected, and the publish takes it exclusively. The old generation leaves WAL mode, which removes both
    files, so connections to the new file can neither replay nor share them. The rename keeps the inode `file` records.
    """
    with generation(store, shared=False, wait=_PUBLISH):
        _replace(store)


def _replace(store: Store) -> None:
    path = _path(store)
    deadline = time.monotonic() + _PUBLISH
    with closing(_connect(path)) as live:
        while True:
            try:
                live.execute("PRAGMA journal_mode=DELETE")
                break
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                if time.monotonic() >= deadline:
                    raise BusyError("readers kept the search cache open; retry bf build") from error
                time.sleep(0.05)
            except sqlite3.DatabaseError:
                # Not a database: no connection can use a log beside it.
                break
    _remove(store, CACHE, _SIDECARS[1:])
    with _writable(), store.parent(CACHE) as (directory, leaf):
        os.replace(Path(BUILD).name, leaf, src_dir_fd=directory, dst_dir_fd=directory)


def _fill(
    store: Store,
    connection: sqlite3.Connection,
    config: Config,
    *,
    new: bool,
    compared: tuple[Fingerprints, Fingerprints] | None = None,
) -> dict[str, object]:
    """Index changed files in one transaction; a new generation has nothing to drop and then gains its indexes.

    Every file is indexed with `config`, whose signature the generation carries: a bf.yaml edit during the fill
    makes the next refresh rebuild instead of failing the files indexed meanwhile.
    """
    # Keep the index B-trees being written in memory: up to 64 MiB, released when the refresh closes.
    connection.execute("PRAGMA cache_size=-65536")
    known = _known(connection)
    # A freshness check's scan still applies while the cache holds the fingerprints it was compared with, so no
    # other refresh has committed since; as during a build, a file changed after the scan no longer matches.
    current = compared[1] if compared is not None and compared[0] == known else inputs(store)
    # A file's problem depends on its own bytes and the configuration signature: an unchanged
    # fingerprint keeps its result.
    changed = sorted(path for path, info in current.items() if known.get(path) != info)
    removed = sorted(known.keys() - current.keys())
    with connection:
        connection.execute("BEGIN")
        for path in () if new else (removed + changed):
            _drop(connection, path)
        for path in changed:
            connection.execute("SAVEPOINT file")
            error = ""
            try:
                _index(connection, store, path, config)
            except FileNotFoundError:
                # Removed after the scan, as by a commit during a build or an editor's atomic save: without a
                # fingerprint, the next refresh compares it again.
                connection.execute("ROLLBACK TO file")
                connection.execute("RELEASE file")
                continue
            except (Error, UnicodeError) as problem:
                connection.execute("ROLLBACK TO file")
                # Most parse errors name their file first, which `path` already holds: a long path would leave no
                # room for the reason.
                error = _capped(str(problem).removeprefix(f"{path}: ") or "invalid file")
            except PermissionError:
                # Restoring access changes the file's ctime, and so its fingerprint.
                connection.execute("ROLLBACK TO file")
                error = "inaccessible file or folder; check permissions"
            except OSError:
                # A transient failure, such as EIO on a network mount, leaves the file unchanged: a fingerprint that
                # matches nothing makes the next refresh read it again instead of keeping the failure.
                connection.execute("ROLLBACK TO file")
                connection.execute("RELEASE file")
                connection.execute(
                    "INSERT INTO files VALUES(?,?,?,?,?,?)",
                    (path, -1, -1, -1, -1, "unreadable file; retried next time"),
                )
                continue
            connection.execute("RELEASE file")
            connection.execute("INSERT INTO files VALUES(?,?,?,?,?,?)", (path, *current[path], error))
        for statement in _INDEXES if new else ():
            connection.execute(statement)
        # PRAGMA has no bound-parameter form; SCHEMA is an internal integer constant.
        connection.execute(f"PRAGMA user_version={SCHEMA}")
    skipped = connection.execute("SELECT count(*) FROM files WHERE error!=''").fetchone()[0]
    return {"files": len(current), "changed": len(changed), "removed": len(removed), "skipped": skipped}


def _capped(text: str) -> str:
    """At most _PROBLEM bytes of a problem, cut with an ellipsis: a validation error names every invalid field."""
    data = text.encode()
    return text if len(data) <= _PROBLEM else data[: _PROBLEM - 3].decode(errors="ignore") + "…"


def _sweep(store: Store) -> None:
    """Delete the file a killed build left beside the cache, which can be as large as the cache.

    Only the build lock holder fills BUILD, so holding the lock proves a file there abandoned; a running build
    keeps it. The cleanup never fails a read: the next build replaces what remains.
    """
    with suppress(Error, OSError):
        # Checked first, so a read takes the build lock only when there is something to delete.
        store.lstat(BUILD)
        with building(store, 0):
            _remove(store, BUILD)


def fresh(store: Store, counts: dict[str, int] | None = None) -> str:
    """Refresh a changed cache, or keep serving it as `busy` while another writer or build holds the brain.

    `counts` receives the entries of each scanned tree, as from `inputs`, when the check scans the brain.
    Never call it while holding the generation lock: its refresh may publish a rebuilt generation.
    """
    # The cache folder's own checks come first: they name the problem more precisely than a lock would.
    _path(store)
    _sweep(store)
    connection = None
    compared: tuple[Fingerprints, Fingerprints] | None = None
    try:
        with generation(store, shared=True, wait=_PUBLISH):
            connection = _open(store)
            try:
                with reader(store, wait=0):
                    # A live writer may have a pending journal; only an abandoned one blocks cached reads.
                    records.require_ready(store)
                    if connection is not None:
                        if counts is not None:
                            counts.clear()
                        compared = (_known(connection), inputs(store, counts))
                        if compared[0] == compared[1]:
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
        # Nothing to serve: the refresh below discards the damaged generation and builds another.
        connection = None
    try:
        # Without any usable generation there is nothing to serve, so wait for the other writer or build.
        refresh(store, wait=0 if connection is not None else 120, compared=compared)
    except BusyError:
        return "busy"
    return "ready"


# Freshness already established for each brain root within the current reply, when one is being built.
_CHECKED: ContextVar[dict[Path, str] | None] = ContextVar("checked", default=None)


@contextmanager
def session() -> Iterator[None]:
    """Compare each brain's files with its cache once per reply, however many views the reply opens."""
    token = _CHECKED.set({})
    try:
        yield
    finally:
        _CHECKED.reset(token)


@contextmanager
def database(store: Store, counts: dict[str, int] | None = None) -> Iterator[tuple[sqlite3.Connection, str]]:
    """The fresh cache generation and its state, within one read snapshot; `counts` as for `fresh`."""
    checked = _CHECKED.get()
    state = checked.get(store.root, "") if checked is not None else ""
    # A full rebuild may replace the generation between the freshness check and open: check again, twice at most.
    # fresh() waits for an in-progress generation instead of serving it, so it runs outside the generation lock.
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
                        # fresh() already waited for the other writer: another wait would only delay this answer.
                        raise BusyError("another writer is building the search cache; retry when it finishes")
                    continue
                with closing(connection):
                    connection.execute("PRAGMA query_only=ON")
                    # Counts, rows, snippets and claims must see one generation even while a WAL writer
                    # commits between their queries. Closing the connection releases this read snapshot.
                    connection.execute("BEGIN")
                    yield connection, state
        except Exception as error:
            # This reply fails; without the damaged generation, the next freshness check builds another. Callers
            # may already have named the cache in an Error raised from the database error.
            cause = error if isinstance(error, sqlite3.DatabaseError) else error.__cause__
            if isinstance(cause, sqlite3.DatabaseError) and _damaged(cause):
                _discard(store, _DISCARD)
            raise
        return
    # The shared message: this module's CACHE is the cache's path.
    raise Error(models.CACHE)


def terms(text: str) -> list[str]:
    """Distinct query terms in the indexed form: words, `word*` prefixes and phrases as space-separated words.

    Function words drop unless they are the whole query, quoted or written as an acronym; a phrase keeps its own.
    FTS5 folds case itself. Python's casefold would turn ß into ss, a spelling the index never holds.
    """
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
    # Notes carry dates, not instants. Resolve midnight on the reading machine, including DST,
    # at query time so changing timezone never requires rebuilding a shared/disposable cache.
    try:
        return moment(value[:10]) if value else ""
    except Error:
        # SQLite would abort the whole query; an unplaceable date only leaves the note undated.
        return ""


# Fixed SQL expressions over `items i`, shared with page builders: event time and last modification.
TIME = "CASE WHEN i.kind='note' THEN note_time(i.time) ELSE i.time END"
# A note's date as written: replies state it instead of the local midnight that orders it.
DATE = "CASE WHEN i.kind='note' THEN substr(i.time,1,10) ELSE '' END"
UPDATED = "CASE WHEN i.kind='note' THEN note_time(i.time) ELSE coalesce(nullif(i.updated,''),i.time) END"
# Event time within [:since, :until), both given. Records compare their stored instants as a range on
# items(kind,time); only the few notes resolve their dates one by one.
WITHIN = (
    "((i.kind='record' AND i.time>=:since AND i.time<:until) "
    "OR (i.kind='note' AND i.time!='' AND note_time(i.time)>=:since AND note_time(i.time)<:until))"
)
# UPDATED within [:since, :until), both given, with the same split over items_kind_modified.
MODIFIED = (
    "((i.kind='record' AND coalesce(nullif(i.updated,''),i.time)>=:since "
    "AND coalesce(nullif(i.updated,''),i.time)<:until) "
    "OR (i.kind='note' AND i.time!='' AND note_time(i.time)>=:since AND note_time(i.time)<:until))"
)
_FIELDS = (
    f"i.ref,i.kind,i.source,({TIME}) AS time,({DATE}) AS date,i.type,i.status,i.url,i.updated,i.observed,i.partial,"
    "i.fields"
)
_ROW = f"{_FIELDS},'' AS fragment,i.title,i.lead AS excerpt,i.tasks_open,i.tasks_done,i.next"
# An item links to a target when it names it exactly, or names a section of a target note.
# Two index lookups: exact targets, then the sections of targets. One OR across both would scan every link,
# and CROSS JOIN keeps the few section prefixes as the outer loop of each range search.
_LINKING = """SELECT l.item FROM links l WHERE l.target IN (SELECT value FROM json_each(:targets))
  UNION SELECT l.item FROM json_each(:sections) s CROSS JOIN links l ON l.target>s.value||'#' AND l.target<s.value||'$'"""
_FILTERS = f"""((:since='' AND :until='') OR i.time!='') AND (:since='' OR ({TIME})>=:since) AND (:until='' OR ({TIME})<:until)
  AND (NOT :undated OR i.time='') AND (:prefix='' OR i.path=:prefix OR substr(i.path,1,length(:prefix)+1)=:prefix||'/')
  AND (:target='' OR (:tag_scope AND i.id IN (SELECT item FROM tags WHERE target=:target))
       OR (NOT :tag_scope AND (i.ref IN (SELECT value FROM json_each(:targets)) OR i.id IN ({_LINKING}))))"""  # noqa: S608


def sections(targets: set[str]) -> list[str]:
    """Identities whose #section links also count as links to them: BF addresses and note paths.

    A BF address encodes a literal `#` in its path, so only a real fragment follows it; a bare
    `source:id` may contain `#` in its id and never takes a section.
    """
    result = []
    for value in targets:
        with suppress(Error):
            parsed = bf_links.parse(value)
            if not parsed.fragment if parsed else authored(value):
                result.append(value)
    return sorted(result)


def _parameters(targets: set[str]) -> dict[str, object]:
    return {"targets": json.dumps(sorted(targets)), "sections": json.dumps(sections(targets))}


def _string(term: str) -> str:
    """One quoted FTS5 string per term, so no query text is FTS5 syntax; only a prefix adds its `*` outside."""
    words = term.removesuffix("*")
    return '"' + words.replace('"', '""') + '"' + ("*" if words != term else "")


def _match(text: str) -> str:
    return " OR ".join(map(_string, terms(text)))


def unmatched(connection: sqlite3.Connection, text: str) -> list[str]:
    """The query's terms that match nothing in this brain, whatever the scope; a phrase is shown quoted."""
    return [
        f'"{term}"' if " " in term else term
        for term in terms(text)
        if connection.execute("SELECT 1 FROM search WHERE search MATCH ? LIMIT 1", (_string(term),)).fetchone() is None
    ]


# An unmatched word gets up to SUGGESTIONS close indexed terms, chosen among CANDIDATES sharing its first two letters.
SUGGESTIONS = 3
CANDIDATES = 5000


def suggestions(connection: sqlite3.Connection, term: str) -> list[str]:
    """Words of this brain close to an unmatched word, compared as indexed stems; none for phrases and prefixes."""
    if " " in term or term.endswith("*") or len(term) < 4:
        return []
    stem = _stem(term)
    rows = connection.execute(
        "SELECT term FROM vocabulary WHERE term>=? AND term<? LIMIT ?", (stem[:2], stem[:2] + "\U0010ffff", CANDIDATES)
    )
    close = difflib.get_close_matches(stem, [row[0] for row in rows], n=SUGGESTIONS, cutoff=0.75)
    # The index holds stems, such as `polici`: show a word one of its passages spells, such as `policy`.
    return list(dict.fromkeys(word for found in close if (word := _spelled(connection, found))))


@lru_cache(maxsize=256)
def _stem(word: str) -> str:
    """A word as the search table indexes it, from the same tokenizer."""
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize={_TOKENIZER!r})")
        connection.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, row)")
        connection.execute("INSERT INTO t VALUES(?)", (word,))
        row = connection.execute("SELECT term FROM v LIMIT 1").fetchone()
    return row[0] if row else word.lower()


def _spelled(connection: sqlite3.Connection, stem: str) -> str:
    """The first indexed spelling of a stem: a one-token snippet of a passage holding it."""
    row = connection.execute(
        "SELECT snippet(search,-1,char(1),char(2),'',1) FROM search WHERE search MATCH ? LIMIT 1", (_string(stem),)
    ).fetchone()
    found = re.search("\x01([^\x01\x02]+)\x02", row[0]) if row else None
    return found[1].lower() if found else ""


def _lexical(connection: sqlite3.Connection, params: dict[str, object]) -> Iterator[dict[str, object]]:
    """One ranked any-term query: BM25 adds the weight of each matched term, so fuller matches rank higher.

    `hits` counts the distinct `:terms` each passage matches, when there are several: matching them all adds
    COVERAGE to its score. That tips near ties; a long record holding every word still ranks below a note whose
    headings hold most of them.

    Every match is ranked on narrow rows; only the returned items read their display fields. Each row
    names its best passage: snippets cost far more than ranking, so only a reply's items get an excerpt.

    `context` ranks below a heading but above body text: a note's tags, and a section's note title and enclosing
    headings (see _index). "Atlas" and "Next actions" outrank another note's "Next actions" that merely mentions
    Atlas in its text, while "Atlas" alone finds the note before a section mentioning it. A section matching through
    its context alone repeats its parent, which answers instead. Equal scores list the newest item first, then by ref.

    One window keeps each note's best passage and, for each URL, the records of its best-ranked record's source,
    before the window is cut: a record has one passage and a note no URL, and an item number never equals a URL
    string. Another source's record of that URL, such as a catalog entry for a collected file, becomes the kept
    record's `also`; one source's records stay distinct, such as several highlights of one document. A note's
    next-best sections become its `sections`. A ROWS frame spares first_value the default frame's scan of tied rows.
    """
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
             WHERE search MATCH :match AND {_FILTERS} AND (p.fragment='' OR bm25(search,10.0,1.0,0.0,0.0)<0)),
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


# FTS5 marks a snippet cut short at either end with this text.
_ELLIPSIS = " … "


def excerpt(connection: sqlite3.Connection, text: str, passage: int) -> str:
    """The query's words in context within one ranked passage, as a one-line preview in the evidence's characters.

    A whole note without introduction text, such as one matching by its title, previews its lead instead.
    """
    row = connection.execute(
        "SELECT snippet(search,1,'','',?,48),p.original,CASE WHEN p.fragment='' THEN i.lead ELSE '' END "
        "FROM search JOIN passages p ON p.id=search.rowid JOIN items i ON i.id=p.item "
        "WHERE search MATCH ? AND search.rowid=?",
        (_ELLIPSIS, _match(text), passage),
    ).fetchone()
    if row is None:
        return ""
    return re.sub(r"\s+", " ", _verbatim(row[0], row[1]) if row[1] else row[0]).strip() or row[2]


def _verbatim(snippet: str, original: str) -> str:
    """The original words behind a snippet of a folded passage, so `m²` or `…` never reads as `m2` or `...`.

    Folding each whitespace-separated run yields the folded passage, so every snippet position maps to one run.
    ASCII is already folded, which keeps a large passage with a single `²` or no-break space fast.
    """
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


def _identity(connection: sqlite3.Connection, params: dict[str, object]) -> Iterator[dict[str, object]]:
    if bf_links.tag(str(params["text"])) is not None:
        return (
            dict(row)
            for row in connection.execute(
                f"SELECT {_FIELDS},'' AS fragment,i.title,1e3 AS score,i.lead AS excerpt FROM items i "  # noqa: S608 - fixed SQL
                f"WHERE i.id IN (SELECT item FROM tags WHERE target=:text) AND {_FILTERS} "
                "ORDER BY time DESC,i.ref DESC LIMIT :limit",
                params,
            )
        )
    rows = connection.execute(
        f"""WITH candidates AS (
              SELECT id AS item,2e6 AS score FROM items WHERE ref=:text
              UNION ALL SELECT item,1e6 FROM names WHERE name IN (SELECT value FROM json_each(:identities))
              UNION ALL SELECT item,1e3 FROM links WHERE target IN (SELECT value FROM json_each(:identities))
              UNION ALL SELECT l.item,1e3 FROM json_each(:identity_sections) s
                CROSS JOIN links l ON l.target>s.value||'#' AND l.target<s.value||'$'),
              owners AS (SELECT item,max(score) AS score FROM candidates GROUP BY item)
           SELECT {_FIELDS},'' AS fragment,i.title,c.score,i.lead AS excerpt FROM owners c
           JOIN items i ON i.id=c.item WHERE {_FILTERS}
           ORDER BY c.score DESC,time DESC,i.ref DESC LIMIT :limit""",  # noqa: S608 - fixed SQL
        params,
    )
    return (dict(row) for row in rows)


def known(connection: sqlite3.Connection, identities: set[str]) -> bool:
    """Whether an item is, names or links to one of these identities, whatever the query's scope."""
    values = {"values": json.dumps(sorted(identities)), "sections": json.dumps(sections(identities))}
    return bool(
        connection.execute(
            """SELECT EXISTS (SELECT 1 FROM items WHERE ref IN (SELECT value FROM json_each(:values)))
               OR EXISTS (SELECT 1 FROM names WHERE name IN (SELECT value FROM json_each(:values)))
               OR EXISTS (SELECT 1 FROM links WHERE target IN (SELECT value FROM json_each(:values)))
               OR EXISTS (SELECT 1 FROM json_each(:sections) s
                          CROSS JOIN links l ON l.target>s.value||'#' AND l.target<s.value||'$')""",
            values,
        ).fetchone()[0]
    )


def search(
    connection: sqlite3.Connection,
    query: Query,
    *,
    identities: set[str],
    targets: set[str],
    exact: bool,
    limit: int,
    low: Collection[str] = (),
) -> Iterator[dict[str, object]]:
    """An exact identity or tag returns its owners, then what links to it; other text ranks lexically, within the scope.

    `limit` bounds this brain's rows, such as enough to fill a continued multi-brain window.
    Lexical rows name their `_passage` for `excerpt`, `also` other matching records of their URL and `sections`
    other matching sections of their note; records of `low` sources rank at half weight there. Identity rows carry
    their owner `_rank`.
    """
    text = query.text.strip()
    # Only an identity-shaped query can be a tag address: "bf: how to configure sensors" stays words.
    if identity(text) and bf_links.tag(text) is not None:
        text = bf_links.identity(text)
    params: dict[str, object] = {
        **query.model_dump(),
        **_parameters(targets),
        "tag_scope": bf_links.tag(query.target) is not None,
        "limit": limit,
        "text": text,
        "identities": json.dumps(sorted(identities or {text})),
        "identity_sections": json.dumps(sections(identities or {text})),
    }
    if exact:
        return ({**_clean(dict(row)), "_rank": row["score"]} for row in _identity(connection, params))
    strings = [_string(term) for term in terms(text)]
    if not strings:
        return iter(())
    # One term has nothing to count: its query need not run twice.
    counted = json.dumps(strings if len(strings) > 1 else [])
    values = {**params, "match": " OR ".join(strings), "terms": counted, "low": json.dumps(sorted(low))}
    rows = _lexical(connection, values)
    return (
        _clean(
            {
                **row,
                "also": sorted(json.loads(str(row["also"]))) if row["also"] else None,
                # Best first: each entry pairs a section's position within its note with its fragment.
                "sections": [f"{row['ref']}#{f}" for _, f in sorted(json.loads(str(row["sections"])))]
                if row["sections"]
                else None,
            }
        )
        for row in rows
    )


def listing(
    connection: sqlite3.Connection, where: str, params: Mapping[str, object], order: str, limit: int = -1
) -> tuple[Iterator[dict[str, object]], int]:
    """Stream items matching fixed SQL written by the page builders, with the total before the limit."""
    values = {
        "since": "",
        "until": "",
        "undated": False,
        "prefix": "",
        "target": "",
        **_parameters(set()),
        **params,
        "limit": limit,
    }
    # `where` and `order` are fixed SQL written by the page builders; values stay bound parameters.
    total = connection.execute(f"SELECT count(*) FROM items i WHERE {where}", values).fetchone()[0]  # noqa: S608
    rows = connection.execute(f"SELECT {_ROW} FROM items i WHERE {where} ORDER BY {order} LIMIT :limit", values)  # noqa: S608
    return (_clean(dict(row)) for row in rows), total


_INCOMING = f"""WITH typed AS (
    SELECT e.item,e.relation FROM edges e WHERE e.relation!='' AND e.target IN (SELECT value FROM json_each(:targets))
    UNION SELECT e.item,e.relation FROM json_each(:sections) s
      CROSS JOIN edges e ON e.target>s.value||'#' AND e.target<s.value||'$' WHERE e.relation!=''),
  plain AS (SELECT item,'' AS relation FROM ({_LINKING}) WHERE item NOT IN (SELECT item FROM typed)),
  linked AS (SELECT * FROM typed UNION ALL SELECT * FROM plain)"""  # noqa: S608 - fixed SQL fragments
_GROUPS = f"""{_INCOMING} SELECT k.relation,count(*) AS total FROM linked k JOIN items i ON i.id=k.item
  WHERE i.ref!=:exclude GROUP BY k.relation ORDER BY k.relation='',k.relation LIMIT 64"""  # noqa: S608
_GROUP = f"""{_INCOMING} SELECT {_ROW} FROM linked k JOIN items i ON i.id=k.item
  WHERE k.relation=:relation AND i.ref!=:exclude ORDER BY time DESC,i.ref DESC LIMIT :limit"""  # noqa: S608
_RELATION = "k.relation IN (SELECT value FROM json_each(:relations)) AND i.ref!=:exclude"
_RELATION_TOTAL = f"{_INCOMING} SELECT count(*) FROM linked k JOIN items i ON i.id=k.item WHERE {_RELATION}"  # noqa: S608
_RELATION_ROWS = f"""{_INCOMING} SELECT {_ROW},k.relation FROM linked k JOIN items i ON i.id=k.item
  WHERE {_RELATION} ORDER BY time DESC,i.ref DESC,k.relation"""  # noqa: S608


def incoming(
    connection: sqlite3.Connection, targets: set[str], *, exclude: str = "", limit: int = 20
) -> list[dict[str, object]]:
    """Items linking to any target, grouped by explicit relation; links without one come last as LINKS."""
    values = {**_parameters(targets), "exclude": exclude, "limit": limit}
    groups = []
    for relation, total in connection.execute(_GROUPS, values).fetchall():
        rows = connection.execute(_GROUP, {**values, "relation": relation}).fetchall()
        groups.append({"relation": relation or LINKS, "total": total, "items": [_clean(dict(row)) for row in rows]})
    return groups


def linking(
    connection: sqlite3.Connection, targets: set[str], relations: list[str], *, exclude: str = ""
) -> tuple[Iterator[dict[str, object]], int]:
    """Stream the items linking to any target through these relations, newest first, with their total.

    Each row names its `relation`, LINKS for an untyped link; an item linking through two of the relations
    appears once per relation.
    """
    values = {
        **_parameters(targets),
        "exclude": exclude,
        "relations": json.dumps(["" if relation == LINKS else relation for relation in relations]),
    }
    total = connection.execute(_RELATION_TOTAL, values).fetchone()[0]
    rows = connection.execute(_RELATION_ROWS, values)
    return ({**_clean(dict(row)), "relation": row["relation"] or LINKS} for row in rows), total


def _shared(name: str, item: str) -> str:
    """Fixed SQL: whether an item other than `item` also holds `name`, as a name or its ref.

    Like graph.local_refs, a name that several items claim identifies none of them.
    """
    return (
        f"(EXISTS (SELECT 1 FROM names o WHERE o.name={name} AND o.item!={item}) "  # noqa: S608 - fixed SQL
        f"OR EXISTS (SELECT 1 FROM items o WHERE o.ref={name} AND o.id!={item}))"
    )


# For each reviewed note, given as [id, last edit] in :reviewed and [id, name, takes sections] in :names: the items
# linking to it by a name or a section of one, as _LINKING finds them, and the records it links to by ref or name.
# As for backlinks, a name that another item also holds identifies neither: :names omits it, and a link by it names
# no record, unless it is that record's own ref, which reads resolve exactly.
# CROSS JOIN keeps the few reviewed notes as the outer loop of each index search, never every record or link.
# Each newer item counts once, ranked by the instant that makes it newer; a note keeps its total and :limit refs.
_NEWER = f"""WITH reviewed(note,after) AS MATERIALIZED (
    SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(:reviewed)),
  named(note,name,sections) AS MATERIALIZED (
    SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_extract(value,'$[2]') FROM json_each(:names)),
  linked(note,item) AS (
    SELECT m.note,l.item FROM named m CROSS JOIN links l ON l.target=m.name
    UNION SELECT m.note,l.item FROM named m CROSS JOIN links l
      ON m.sections AND l.target>m.name||'#' AND l.target<m.name||'$'
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
    """Per note id, how many linked items hold evidence newer than its last edit in `edits`, and the newest refs.

    Linked items link to the note or are records it links to. One counts when its event time or, for a record, its
    upstream `updated` falls between the edit and `now`: a future event counts once it happens, so an edit clears
    the count until newer evidence arrives. `observed` never counts: a first collection is not a change. As for
    backlinks, a name that another item also holds, such as a shared alias, identifies neither: links by it never count,
    except a link by a record's own ref, which reads resolve exactly.
    """
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
        # A one-line preview; `bf read` returns the exact text.
        row["excerpt"] = re.sub(r"\s+", " ", excerpt).strip()
    if isinstance(title := row.get("title"), str) and len(title) > TITLE:
        # Some sources title records with a whole message, such as a commit; cut at a word when one is near.
        cut = title[: TITLE - 1]
        space = cut.rfind(" ")
        row["title"] = (cut[:space] if space >= TITLE // 2 else cut).rstrip() + "…"
    row.pop("score", None)
    row.pop("position", None)
    opened, done = int(cast("int", row.pop("tasks_open", 0) or 0)), int(cast("int", row.pop("tasks_done", 0) or 0))
    if opened or done:
        row["tasks"] = {"open": opened, "done": done}
    if row.get("partial"):
        row["partial"] = True
    else:
        row.pop("partial", None)
    if isinstance(facts := row.get("fields"), str) and facts:
        row["fields"] = json.loads(facts)
    if row["kind"] == "note":
        row.pop("source", None)
        row.pop("updated", None)
    else:
        # Every record's type is `record`, which its kind already states.
        row.pop("type", None)
    return {key: value for key, value in row.items() if value not in ("", None)}


def problems(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Skipped files shared by search, pages and status, up to 200 and a count of the rest; bf validate lists all.

    Each error keeps at most _PROBLEM bytes, so a few files with long errors cannot push a reply past its limit.
    """
    rows = connection.execute("SELECT path,error FROM files WHERE error!='' ORDER BY path LIMIT 201").fetchall()
    result: list[dict[str, object]] = [{"file": row["path"], "error": _capped(row["error"])} for row in rows[:200]]
    if len(rows) > 200:
        more = connection.execute("SELECT count(*) FROM files WHERE error!=''").fetchone()[0] - 200
        result.append({"error": f"{more} more files were skipped; run bf validate"})
    return result


def sources(connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Indexed record totals and latest event time per source; a source without dated records has no `latest`."""
    return {
        row["source"]: {"records": row["records"], **({"latest": row["latest"]} if row["latest"] else {})}
        for row in connection.execute(
            "SELECT source,count(*) AS records,max(time) AS latest FROM items WHERE kind='record' GROUP BY source"
        )
    }


def activity(connection: sqlite3.Connection, since: str, until: str) -> list[tuple[str, int]]:
    """Records per source whose event time falls in [since, until), busiest first."""
    return [
        (row[0], row[1])
        for row in connection.execute(
            "SELECT source,count(*) FROM items WHERE kind='record' AND time!='' AND time>=? AND time<? "
            "GROUP BY source ORDER BY count(*) DESC,source",
            (since, until),
        )
    ]


def capacity(store: Store, counts: dict[str, int] | None = None) -> list[dict[str, object]]:
    """Scanned trees, each authored folder and each source of memories/, holding more than CROWDED entries.

    `counts`, the entries per tree of a scan this reply already made, spares scanning the brain again.
    """
    if not counts:
        counts = {}
        inputs(store, counts)
    return [
        {"warning": "directory nears the scan limit", "directory": tree, "entries": entries, "limit": MAX_FILES}
        for tree, entries in sorted(counts.items())
        if entries > CROWDED
    ]


def status(store: Store) -> dict[str, object]:
    """Cache state, note and record counts, bytes per source, skipped files, and trees nearing the scan limit.

    An interrupted record transaction keeps the cache from refreshing until a writer recovers it: status then
    describes the last generation as `stale`, or an empty `missing` one, instead of failing.
    """
    pending = _abandoned(store)
    # The freshness check's scan of the brain also counts the entries that capacity warns about.
    entries: dict[str, int] = {}
    with _last(store) if pending else database(store, entries) as (connection, state):
        counts = sources(connection)
        notes = connection.execute("SELECT count(*) FROM items WHERE kind='note'").fetchone()[0]
        skipped = problems(connection)
        # The sizes the last refresh fingerprinted: bytes on disk below each source, invalid files included.
        sizes = dict(
            connection.execute(
                "SELECT substr(path,10,instr(substr(path,10),'/')-1),sum(size) FROM files "
                "WHERE path GLOB 'memories/?*/*' GROUP BY 1"
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
    """Whether an interrupted record transaction waits for recovery; a live writer's own journal does not."""
    try:
        with reader(store, wait=0):
            return records.interrupted(store)
    except BusyError:
        return False


@contextmanager
def _last(store: Store) -> Iterator[tuple[sqlite3.Connection, str]]:
    """The last generation as it is, never refreshed, since that would index half-changed records."""
    _path(store)
    with generation(store, shared=True, wait=_PUBLISH):
        connection, state = _open(store), "stale"
        if connection is None:
            connection, state = _connect(":memory:"), "missing"
            connection.executescript(_DDL)
        with closing(connection):
            yield connection, state
