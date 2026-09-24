"""A disposable SQLite cache that refreshes changed files and answers lexical and time-window queries."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bf import records
from bf.markdown import authored, note
from bf.models import AUTHORED, Error, Query, Record, moment
from bf.storage import BusyError, Store, reader, writer

SCHEMA = 12
# Active projects whose note is older than this are listed for review; a reminder, never a failure.
REVIEW_DAYS = 14
CACHE = ".bf/index.sqlite"
_DDL = """
CREATE TABLE files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, ctime INTEGER, inode INTEGER,
                   error TEXT NOT NULL DEFAULT '');
CREATE TABLE items(id INTEGER PRIMARY KEY, ref TEXT NOT NULL UNIQUE, path TEXT NOT NULL, kind TEXT NOT NULL,
                   source TEXT NOT NULL, title TEXT NOT NULL, time TEXT NOT NULL, type TEXT NOT NULL,
                   status TEXT NOT NULL, lead TEXT NOT NULL, url TEXT NOT NULL,
                   updated TEXT NOT NULL, observed TEXT NOT NULL, partial INTEGER NOT NULL);
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
        "au",
        "aux",
        "avec",
        "ce",
        "ces",
        "cet",
        "cette",
        "comment",
        "d",
        "dans",
        "de",
        "des",
        "du",
        "elle",
        "elles",
        "en",
        "est",
        "et",
        "eux",
        "il",
        "ils",
        "je",
        "l",
        "la",
        "le",
        "les",
        "leur",
        "leurs",
        "lui",
        "mais",
        "mes",
        "mon",
        "nos",
        "notre",
        "nous",
        "où",
        "par",
        "pour",
        "pourquoi",
        "qu",
        "que",
        "quel",
        "quelle",
        "quelles",
        "quels",
        "qui",
        "sa",
        "se",
        "ses",
        "son",
        "sur",
        "tes",
        "toi",
        "ton",
        "tu",
        "un",
        "une",
        "vos",
        "votre",
        "vous",
        "à",
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
    directory = store.root / ".bf"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise Error(".bf must be a directory")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = store.root / CACHE
    for suffix in ("", "-wal", "-shm", "-journal"):
        with suppress(FileNotFoundError):
            store.fingerprint(CACHE + suffix)
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
            # Version alone does not establish that an interrupted or damaged cache is usable.
            for statement in (
                "SELECT path,size,mtime,ctime,inode,error FROM files LIMIT 0",
                "SELECT ref,path,kind,source,title,time,type,status,lead,url,updated,observed,partial FROM items LIMIT 0",
                "SELECT id,item,fragment,title FROM passages LIMIT 0",
                "SELECT name,item FROM names LIMIT 0",
                "SELECT item,target FROM links LIMIT 0",
                "SELECT title,text,names FROM search LIMIT 0",
            ):
                connection.execute(statement)
            return connection
    except sqlite3.DatabaseError:
        pass
    connection.close()
    return None


def _create(store: Store) -> sqlite3.Connection:
    path = _path(store)
    for suffix in ("", "-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    # Version zero withholds this generation until the ingestion transaction commits.
    connection.executescript(_DDL)
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
    values: dict[str, str | int],
    passages: list[tuple[str, str, str, str, str]],
    names: list[str],
    links: list[str],
) -> None:
    try:
        cursor = connection.execute(
            "INSERT INTO items(ref,path,kind,source,title,time,type,status,lead,url,updated,observed,partial) "
            "VALUES(:ref,:path,:kind,:source,:title,:time,:type,:status,:lead,:url,:updated,:observed,:partial)",
            values,
        )
    except sqlite3.IntegrityError:
        raise Error(f"duplicate reference {values['ref']}; run bf validate") from None
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
                "updated": f"{knowledge.updated}T00:00:00.000000Z" if knowledge.updated else "",
                "observed": "",
                "partial": 0,
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
                    "updated": record.updated,
                    "observed": record.observed,
                    "partial": int(record.attributes.get("partial") is True),
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
    try:
        return _refresh(store, full=full, wait=wait)
    except sqlite3.DatabaseError as error:
        raise Error("could not refresh the search cache; check free space and run bf build") from error


def _refresh(store: Store, *, full: bool, wait: float) -> dict[str, object]:
    with writer(store, wait):
        if full:
            records.recover(store)
        else:
            records.require_ready(store)
        connection = (None if full else _open(store)) or _create(store)
        with closing(connection):
            current = inputs(store)
            known = _known(connection)
            changed = sorted(path for path, info in current.items() if known.get(path) != info)
            removed = sorted(known.keys() - current.keys())
            if changed or removed:
                # A duplicate can disappear because another partition changed; its own bytes need not change.
                retry = {row[0] for row in connection.execute("SELECT path FROM files WHERE error!=''")}
                changed = sorted(set(changed) | (retry & current.keys()))
            with connection:
                connection.execute("BEGIN")
                for path in removed + changed:
                    _drop(connection, path)
                for path in changed:
                    connection.execute("SAVEPOINT file")
                    try:
                        problems = _index(connection, store, path)
                    except (Error, UnicodeError) as problem:
                        connection.execute("ROLLBACK TO file")
                        problems = [str(problem) or "invalid file"]
                    except OSError:
                        connection.execute("ROLLBACK TO file")
                        problems = ["inaccessible file; check permissions"]
                    connection.execute("RELEASE file")
                    error = (
                        problems[0] + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else "")
                        if problems
                        else ""
                    )
                    connection.execute("INSERT INTO files VALUES(?,?,?,?,?,?)", (path, *current[path], error))
                # PRAGMA has no bound-parameter form; SCHEMA is an internal integer constant.
                connection.execute(f"PRAGMA user_version={SCHEMA}")  # nosemgrep: formatted-sql-query
            skipped = connection.execute("SELECT count(*) FROM files WHERE error!=''").fetchone()[0]
        return {"files": len(current), "changed": len(changed), "removed": len(removed), "problems": skipped}


def fresh(store: Store) -> str:
    """Refresh a stale cache, or keep serving it as `stale` while another writer holds the brain."""
    connection = _open(store)
    try:
        with reader(store, wait=0):
            # A live writer may have a pending journal; only an abandoned one blocks cached reads.
            records.require_ready(store)
            if connection is not None and _known(connection) == inputs(store):
                return "ready"
    except BusyError:
        if connection is not None:
            return "stale"
    finally:
        if connection is not None:
            connection.close()
    try:
        # Without any usable generation there is nothing to serve, so wait for the other writer.
        refresh(store, wait=0 if connection is not None else 120)
    except BusyError:
        return "stale"
    return "ready"


@contextmanager
def database(store: Store) -> Iterator[tuple[sqlite3.Connection, str]]:
    # A full rebuild may replace the generation between the freshness check and open.
    # Retry once: fresh() waits for an in-progress generation instead of serving it.
    for _ in range(2):
        state = fresh(store)
        connection = _open(store)
        if connection is not None:
            break
    else:
        raise Error("the search cache is unavailable; run bf build")
    with closing(connection):
        connection.execute("PRAGMA query_only=ON")
        yield connection, state


def terms(text: str) -> list[str]:
    words = list(dict.fromkeys(re.findall(r"[^\W_]+", text.casefold())))
    return ([w for w in words if w not in _STOP] or words)[:32]


def identity(text: str) -> bool:
    return _IDENTITY.fullmatch(text.strip()) is not None


def identity_owners(connection: sqlite3.Connection, text: str) -> set[str]:
    """Explicit owners for cross-brain identity ordering, kept separate from public results."""
    return {
        row[0]
        for row in connection.execute(
            "SELECT ref FROM items WHERE ref=? OR id IN (SELECT item FROM names WHERE name=?)",
            (text.strip(), text.strip()),
        )
    }


def _note_time(value: str) -> str:
    # Notes carry dates, not instants. Resolve midnight on the reading machine, including DST,
    # at query time so changing timezone never requires rebuilding a shared/disposable cache.
    return moment(value[:10]) if value else ""


_TIME = "CASE WHEN i.kind='note' THEN note_time(i.time) ELSE i.time END"
_UPDATED = "CASE WHEN i.kind='note' THEN note_time(i.time) ELSE coalesce(nullif(i.updated,''),i.time) END"
_FIELDS = f"i.ref,i.kind,i.source,({_TIME}) AS time,i.type,i.status,i.url,i.updated,i.observed,i.partial"
_FILTERS = f"""(:source='' OR i.source=:source) AND (:type='' OR i.type=:type) AND (:status='' OR i.status=:status)
  AND ((:since='' AND :until='') OR i.time!='') AND (:since='' OR ({_TIME})>=:since) AND (:until='' OR ({_TIME})<:until)
  AND (:changed_since='' OR ({_UPDATED})>=:changed_since)
  AND (:current=0 OR i.kind='note' OR i.source IN (SELECT value FROM json_each(:active_sources)))"""  # noqa: S608 - fixed SQL fragments


def _lexical(connection: sqlite3.Connection, params: dict[str, object], match: str) -> list[dict[str, object]]:
    rows = connection.execute(
        f"""WITH matches AS MATERIALIZED (
           SELECT {_FIELDS},p.fragment,p.title,-bm25(search,10.0,1.0,0.5) AS score,
                  snippet(search,1,'','',' … ',48) AS excerpt
           FROM search JOIN passages p ON p.id=search.rowid JOIN items i ON i.id=p.item
           WHERE search MATCH :match AND {_FILTERS}),
           ranked AS (SELECT *,row_number() OVER (PARTITION BY ref ORDER BY score DESC,fragment) AS position
                      FROM matches)
           SELECT * FROM ranked WHERE position=1
           ORDER BY CASE WHEN :recent THEN time END DESC,CASE WHEN :recent THEN ref END DESC,
                    status IN ('deprecated','archived'),score * CASE WHEN kind='note' THEN 2 ELSE 1 END DESC,ref
           LIMIT :limit""",  # noqa: S608 - fixed SQL
        {**params, "match": match},
    ).fetchall()
    return [dict(row) for row in rows]


def _identity(connection: sqlite3.Connection, params: dict[str, object]) -> list[dict[str, object]]:
    rows = connection.execute(
        f"""WITH candidates AS (
              SELECT id AS item,2e6 AS score FROM items WHERE ref=:text
              UNION ALL SELECT item,1e6 FROM names WHERE name=:text
              UNION ALL SELECT item,1e3 FROM links WHERE target=:text),
              owners AS (SELECT item,max(score) AS score FROM candidates GROUP BY item)
           SELECT {_FIELDS},'' AS fragment,i.title,c.score,i.lead AS excerpt FROM owners c
           JOIN items i ON i.id=c.item WHERE {_FILTERS}
           ORDER BY c.score DESC,time DESC,i.ref LIMIT :limit""",  # noqa: S608 - fixed SQL
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def search(
    connection: sqlite3.Connection, query: Query, *, active_sources: set[str] | None = None
) -> list[dict[str, object]]:
    """Exact identities, lexical all-term then any-term matches, or a filtered listing."""
    connection.create_function("note_time", 1, _note_time, deterministic=True)
    params: dict[str, object] = query.model_dump()
    params["text"] = query.text.strip()
    params["active_sources"] = json.dumps(sorted(active_sources or ()))
    if not params["text"]:
        rows = connection.execute(
            f"""SELECT {_FIELDS},'' AS fragment,i.title,0 AS score,i.lead AS excerpt FROM items i
               WHERE {_FILTERS} ORDER BY time DESC,i.ref LIMIT :limit""",  # noqa: S608 - fixed SQL
            params,
        ).fetchall()
        return [_clean(dict(row)) for row in rows]
    if identity(str(params["text"])):
        return [_clean(row) for row in _identity(connection, params)]
    groups = []
    tokens = terms(str(params["text"]))
    if tokens:
        quoted = ['"' + t.replace('"', '""') + '"' for t in tokens]
        groups.append(_lexical(connection, params, " ".join(quoted)))
        if len(tokens) > 1:
            groups.append(_lexical(connection, params, " OR ".join(quoted)))
    selected: dict[str, dict[str, object]] = {}
    for group in groups:
        for row in group:
            selected.setdefault(str(row["ref"]), row)
    items = list(selected.values())
    if query.recent:
        items.sort(key=lambda r: (str(r["time"]), str(r["ref"])), reverse=True)
    return [_clean(row) for row in items[: query.limit]]


def _clean(row: dict[str, object]) -> dict[str, object]:
    fragment = row.pop("fragment", "")
    if fragment:
        row["ref"] = f"{row['ref']}#{fragment}"
    row.pop("score", None)
    row.pop("position", None)
    if row.get("partial"):
        row["partial"] = True
    else:
        row.pop("partial", None)
    if row["kind"] == "note":
        row.pop("source", None)
        row.pop("updated", None)
    return {key: value for key, value in row.items() if value not in ("", None)}


def problems(connection: sqlite3.Connection) -> list[str]:
    """Bound diagnostics shared by search and status; full validation remains available."""
    return [
        f"{row['path']}: {row['error']}"
        for row in connection.execute("SELECT path,error FROM files WHERE error!='' ORDER BY path LIMIT 200")
    ]


def status(store: Store, now: datetime | None = None) -> dict[str, object]:
    """Cache state, counts, skipped files, and active projects whose note has not been updated recently."""
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=REVIEW_DAYS)).strftime("%Y-%m-%d")
    with database(store) as (connection, state):
        sources = {
            row["source"]: {"records": row["records"], "latest": row["latest"] or ""}
            for row in connection.execute(
                "SELECT source,count(*) AS records,max(time) AS latest FROM items WHERE kind='record' GROUP BY source"
            )
        }
        notes = connection.execute("SELECT count(*) FROM items WHERE kind='note'").fetchone()[0]
        skipped = problems(connection)
        review = [
            {"ref": row["ref"], "title": row["title"], "updated": row["time"][:10]}
            for row in connection.execute(
                """SELECT ref,title,time FROM items WHERE kind='note' AND type='project'
                   AND status IN ('active','blocked') AND (time='' OR time<?) ORDER BY time,ref""",
                (cutoff,),
            )
        ]
    return {"index": state, "notes": notes, "sources": sources, "problems": skipped, "review": review}
