"""A disposable SQLite cache that refreshes changed files and answers lexical and time-window queries."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from typing import cast

from fkf import records
from fkf.markdown import authored, note
from fkf.models import AUTHORED, DEMOTED, Error, Query, Record
from fkf.storage import BusyError, Store, writer

SCHEMA = 9
CACHE = ".fkf/index.sqlite"
_DDL = """
CREATE TABLE files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, ctime INTEGER, inode INTEGER,
                   error TEXT NOT NULL DEFAULT '');
CREATE TABLE items(id INTEGER PRIMARY KEY, ref TEXT NOT NULL UNIQUE, path TEXT NOT NULL, kind TEXT NOT NULL,
                   source TEXT NOT NULL, title TEXT NOT NULL, time TEXT NOT NULL, type TEXT NOT NULL,
                   status TEXT NOT NULL, lead TEXT NOT NULL, url TEXT NOT NULL);
CREATE INDEX items_path ON items(path);
CREATE INDEX items_time ON items(time);
CREATE TABLE passages(id INTEGER PRIMARY KEY, item INTEGER NOT NULL, fragment TEXT NOT NULL, title TEXT NOT NULL);
CREATE INDEX passages_item ON passages(item);
CREATE VIRTUAL TABLE search USING fts5(title, text, names, tokenize='porter unicode61 remove_diacritics 2');
CREATE TABLE names(name TEXT NOT NULL, item INTEGER NOT NULL, PRIMARY KEY(name, item)) WITHOUT ROWID;
CREATE INDEX names_item ON names(item);
CREATE TABLE links(item INTEGER NOT NULL, target TEXT NOT NULL, PRIMARY KEY(item, target)) WITHOUT ROWID;
CREATE INDEX links_target ON links(target);
"""
# Only function words are dropped: a subject such as "resume" or "active" stays literal.
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "that",
        "the",
        "their",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "would",
        "you",
        "your",
    ]
)
_IDENTITY = re.compile(r"[a-z][a-z0-9+.-]*:\S+")


def inputs(store: Store) -> dict[str, tuple[int, int, int, int]]:
    """Authored notes and record partitions with their file fingerprints."""
    names = [n for d in AUTHORED for n in store.files(d) if authored(n)] + records.partitions(store)
    current = {}
    for name in names:
        with suppress(FileNotFoundError):
            current[name] = store.fingerprint(name)
    return current


def _path(store: Store) -> Path:
    directory = store.root / ".fkf"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise Error(".fkf must be a directory")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = store.root / CACHE
    if path.is_symlink():
        raise Error(f"{CACHE} must be a regular file")
    return path


def _open(store: Store) -> sqlite3.Connection | None:
    """The current cache generation, or None when it is missing, outdated or unreadable."""
    path = _path(store)
    if not path.exists():
        return None
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA:
            return connection
    except sqlite3.DatabaseError:
        pass
    connection.close()
    return None


def _create(store: Store) -> sqlite3.Connection:
    path = _path(store)
    for suffix in ("", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.executescript(_DDL + f"PRAGMA user_version={SCHEMA};")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.row_factory = sqlite3.Row
    return connection


def _known(connection: sqlite3.Connection) -> dict[str, tuple[int, int, int, int]]:
    return {
        row["path"]: (row["size"], row["mtime"], row["ctime"], row["inode"])
        for row in connection.execute("SELECT * FROM files")
    }


def _drop(connection: sqlite3.Connection, path: str) -> None:
    items = [row[0] for row in connection.execute("SELECT id FROM items WHERE path=?", (path,))]
    for item in items:
        connection.execute("DELETE FROM search WHERE rowid IN (SELECT id FROM passages WHERE item=?)", (item,))
        for table in ("passages", "names", "links"):
            connection.execute(f"DELETE FROM {table} WHERE item=?", (item,))  # noqa: S608 - fixed table names
    connection.execute("DELETE FROM items WHERE path=?", (path,))
    connection.execute("DELETE FROM files WHERE path=?", (path,))


def _insert(
    connection: sqlite3.Connection,
    values: dict[str, str],
    passages: list[tuple[str, str, str, str, str]],
    names: list[str],
    links: list[str],
) -> None:
    try:
        cursor = connection.execute(
            "INSERT INTO items(ref,path,kind,source,title,time,type,status,lead,url) "
            "VALUES(:ref,:path,:kind,:source,:title,:time,:type,:status,:lead,:url)",
            values,
        )
    except sqlite3.IntegrityError:
        raise Error(f"duplicate reference {values['ref']}; run fkf validate") from None
    item = cursor.lastrowid
    # `heading` is what ranks; `title` is what readers see, such as "Note — Section".
    for fragment, title, heading, text, extra in passages:
        passage = connection.execute(
            "INSERT INTO passages(item,fragment,title) VALUES(?,?,?)", (item, fragment, title)
        ).lastrowid
        connection.execute(
            "INSERT INTO search(rowid,title,text,names) VALUES(?,?,?,?)", (passage, heading, text, extra)
        )
    connection.executemany("INSERT OR IGNORE INTO names VALUES(?,?)", ((name, item) for name in names))
    connection.executemany("INSERT OR IGNORE INTO links VALUES(?,?)", ((item, link) for link in links))


def _index(connection: sqlite3.Connection, store: Store, path: str) -> list[str]:
    """Insert one file; parse failures raise, while duplicate record ids are skipped and reported."""
    if authored(path):
        projection = note(path, store.read(path, 4 << 20))
        knowledge = projection.knowledge
        metadata = " ".join([knowledge.type, *knowledge.tags, *knowledge.aliases, *projection.links])
        _insert(
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
            },
            [
                (p.fragment, p.title, p.heading, p.text, projection.title if p.fragment else metadata)
                for p in projection.passages
            ],
            knowledge.aliases,
            projection.links,
        )
        return []
    source = records.source_of(path)
    problems = []
    for record in records.load(store, path):
        identity = " ".join([source, *record.aliases, *record.links, record.url])
        try:
            _insert(
                connection,
                {
                    "ref": f"{source}:{record.id}",
                    "path": path,
                    "kind": "record",
                    "source": source,
                    "title": record.title,
                    "time": record.time,
                    "type": "record",
                    "status": "",
                    "lead": lead(record),
                    "url": record.url,
                },
                [("", record.title, record.title, record.text, identity)],
                record.aliases,
                sorted({*record.links, *([record.url] if record.url else [])}),
            )
        except Error as error:
            problems.append(str(error))
    return problems


def lead(record: Record) -> str:
    return re.sub(r"\s+", " ", record.text).strip()[:320]


def refresh(store: Store, *, full: bool = False, wait: float = 30) -> dict[str, object]:
    """Re-index only changed files; a file that fails to parse is skipped and reported, not fatal."""
    with writer(store, wait):
        connection = (None if full else _open(store)) or _create(store)
        with closing(connection):
            current = inputs(store)
            known = _known(connection)
            changed = sorted(path for path, info in current.items() if known.get(path) != info)
            removed = sorted(known.keys() - current.keys())
            with connection:
                for path in removed + changed:
                    _drop(connection, path)
                for path in changed:
                    connection.execute("SAVEPOINT file")
                    try:
                        problems = _index(connection, store, path)
                    except (Error, UnicodeError) as problem:
                        connection.execute("ROLLBACK TO file")
                        problems = [str(problem) or "invalid file"]
                    connection.execute("RELEASE file")
                    error = (
                        problems[0] + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else "")
                        if problems
                        else ""
                    )
                    connection.execute("INSERT INTO files VALUES(?,?,?,?,?,?)", (path, *current[path], error))
            skipped = connection.execute("SELECT count(*) FROM files WHERE error!=''").fetchone()[0]
        return {"files": len(current), "changed": len(changed), "removed": len(removed), "problems": skipped}


def fresh(store: Store) -> str:
    """Refresh a stale cache, or keep serving it as `stale` while another writer holds the base."""
    connection = _open(store)
    if connection is not None:
        with closing(connection):
            if _known(connection) == inputs(store):
                return "ready"
    try:
        # Without any usable generation there is nothing to serve, so wait for the other writer.
        refresh(store, wait=0 if connection is not None else 120)
    except BusyError:
        return "stale"
    return "ready"


@contextmanager
def database(store: Store) -> Iterator[tuple[sqlite3.Connection, str]]:
    state = fresh(store)
    connection = _open(store)
    if connection is None:
        raise Error("the search cache is unavailable; run fkf build")
    with closing(connection):
        connection.execute("PRAGMA query_only=ON")
        yield connection, state


def terms(text: str) -> list[str]:
    words = list(dict.fromkeys(re.findall(r"[^\W_]+", text.casefold())))
    return ([w for w in words if w not in _STOP] or words)[:32]


_FIELDS = "i.ref,i.kind,i.source,i.time,i.type,i.status,i.url"
_FILTERS = """(:source='' OR i.source=:source) AND (:type='' OR i.type=:type) AND (:status='' OR i.status=:status)
  AND ((:since='' AND :until='') OR i.time!='') AND (:since='' OR i.time>=:since) AND (:until='' OR i.time<:until)"""


def _lexical(connection: sqlite3.Connection, params: dict[str, object], match: str) -> list[dict[str, object]]:
    rows = connection.execute(
        f"""SELECT {_FIELDS},p.fragment,p.title,-bm25(search,10.0,1.0,0.5) AS score,
                  snippet(search,1,'','',' … ',48) AS excerpt
           FROM search JOIN passages p ON p.id=search.rowid JOIN items i ON i.id=p.item
           WHERE search MATCH :match AND {_FILTERS} ORDER BY score DESC LIMIT 2000""",  # noqa: S608 - fixed SQL
        {**params, "match": match},
    ).fetchall()
    return [dict(row) for row in rows]


def _identity(connection: sqlite3.Connection, params: dict[str, object]) -> list[dict[str, object]]:
    rows = connection.execute(
        f"""SELECT {_FIELDS},'' AS fragment,i.title,c.score,i.lead AS excerpt FROM (
              SELECT id AS item,1e6 AS score FROM items WHERE ref=:text
              UNION ALL SELECT item,1e6 FROM names WHERE name=:text
              UNION ALL SELECT item,1e3 FROM links WHERE target=:text) c
           JOIN items i ON i.id=c.item WHERE {_FILTERS}
           ORDER BY c.score DESC,i.time DESC LIMIT 2000""",  # noqa: S608 - fixed SQL
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _rank(row: dict[str, object]) -> float:
    score = float(cast("float", row["score"]))
    # Authored knowledge is the distilled answer; records are its supporting evidence.
    return score * 2 if row["kind"] == "note" else score


def _order(row: dict[str, object]) -> tuple[bool, float, str]:
    """Deprecated and archived notes stay visible but rank after current matches of the same kind."""
    return row["status"] in DEMOTED, -_rank(row), str(row["ref"])


def search(connection: sqlite3.Connection, query: Query) -> list[dict[str, object]]:
    """Exact identities, then notes and records matching every term, then any term; or a filtered listing."""
    params: dict[str, object] = query.model_dump()
    params["text"] = query.text.strip()
    if not params["text"]:
        rows = connection.execute(
            f"""SELECT {_FIELDS},'' AS fragment,i.title,0 AS score,i.lead AS excerpt FROM items i
               WHERE {_FILTERS} ORDER BY i.time DESC,i.ref LIMIT :limit""",  # noqa: S608 - fixed SQL
            params,
        ).fetchall()
        return [_clean(dict(row)) for row in rows]
    groups = []
    identity = bool(_IDENTITY.fullmatch(str(params["text"])))
    if identity:
        groups.append(_identity(connection, params))
    tokens = terms(str(params["text"]))
    if tokens:
        quoted = ['"' + t.replace('"', '""') + '"' for t in tokens]
        groups.append(_lexical(connection, params, " ".join(quoted)))
        # An identity is precise: its parts matching anywhere would only add noise.
        if len(tokens) > 1 and not identity:
            groups.append(_lexical(connection, params, " OR ".join(quoted)))
    selected: dict[str, dict[str, object]] = {}
    for group in groups:
        best: dict[str, dict[str, object]] = {}
        for row in group:
            ref = str(row["ref"])
            if ref not in selected and (ref not in best or _rank(row) > _rank(best[ref])):
                best[ref] = row
        for row in sorted(best.values(), key=_order):
            if len(selected) < query.limit:
                selected[str(row["ref"])] = row
    items = list(selected.values())
    if query.recent:
        items.sort(key=lambda r: (str(r["time"]), str(r["ref"])), reverse=True)
    return [_clean(row) for row in items]


def _clean(row: dict[str, object]) -> dict[str, object]:
    fragment = row.pop("fragment", "")
    if fragment:
        row["ref"] = f"{row['ref']}#{fragment}"
    row.pop("score", None)
    if row["kind"] == "note":
        row.pop("source", None)
    return {key: value for key, value in row.items() if value not in ("", None)}


def status(store: Store) -> dict[str, object]:
    with database(store) as (connection, state):
        sources = {
            row["source"]: {"records": row["records"], "latest": row["latest"] or ""}
            for row in connection.execute(
                "SELECT source,count(*) AS records,max(time) AS latest FROM items WHERE kind='record' GROUP BY source"
            )
        }
        notes = connection.execute("SELECT count(*) FROM items WHERE kind='note'").fetchone()[0]
        problems = [
            f"{row['path']}: {row['error']}" for row in connection.execute("SELECT * FROM files WHERE error!=''")
        ]
    return {"index": state, "notes": notes, "sources": sources, "problems": problems}
