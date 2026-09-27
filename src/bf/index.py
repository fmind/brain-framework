"""A disposable SQLite cache that refreshes changed files and answers lexical and time-window queries."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import cast

from bf import links as bf_links
from bf import ontology, records
from bf.config import load
from bf.markdown import LEAD, authored, note
from bf.models import AUTHORED, MAX_NOTE, Error, Query, Record, digest, encode, moment
from bf.storage import BusyError, Store, reader, writer

SCHEMA = 21
CACHE = ".bf/index.sqlite"
_DDL = """
CREATE TABLE ontology(signature TEXT NOT NULL);
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
                   weight INTEGER NOT NULL);
CREATE TABLE passages(id INTEGER PRIMARY KEY, item INTEGER NOT NULL, fragment TEXT NOT NULL, title TEXT NOT NULL);
CREATE VIRTUAL TABLE search USING fts5(title, text, names, tokenize='porter unicode61 remove_diacritics 2');
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
    "CREATE INDEX names_item ON names(item)",
    "CREATE INDEX links_target ON links(target)",
    "CREATE INDEX tags_target ON tags(target)",
)
# Only English and French function words are dropped: a subject such as "resume" or "active" stays literal.
_STOP = frozenset(
    """
    a an and are as at be by can could did do does for from how i in is it me my of on or please that the their
    this to was we were what when where which who why with would you your
    au aux avec ce ces cet cette comment d dans de des du elle elles en est et eux il ils je l la le les leur leurs
    lui mais mes mon nos notre nous où par pour pourquoi qu que quel quelle quelles quels qui sa se ses son sur tes
    toi ton tu un une vos votre vous à
    """.split()  # noqa: SIM905 - one readable word list, grouped by language
)
_IDENTITY = re.compile(r"[a-z][a-z0-9+.-]*:\S+")
# Only folders directly below actions/ hold an ACTION.md; inputs/ and outputs/ may hold other notes.
ACTION = (
    "i.kind='note' AND substr(i.path,1,8)='actions/' AND substr(i.path,-10)='/ACTION.md' "
    "AND length(i.path)-length(replace(i.path,'/',''))=2"
)


def distilled(path: str) -> bool:
    """Notes that distill an answer rank above evidence: projects, concepts and each action's ACTION.md.

    OKF indexes and update logs navigate or record history; action inputs and outputs are working files.
    """
    parts = path.split("/")
    if parts[0] == "projects":
        return True
    if parts[0] == "concepts":
        return parts[-1] not in {"index.md", "log.md"}
    return len(parts) == 3 and parts[0] == "actions" and parts[2] == "ACTION.md"


def inputs(store: Store) -> dict[str, tuple[int, int, int, int]]:
    """Authored notes and record partitions with their file fingerprints.

    Every symlink or special entry stays an input so its exclusion is reported. A link can hide
    a whole directory regardless of its filename; never inspect its target to guess its contents.
    """
    found: dict[str, tuple[int, int, int, int]] = {}
    for directory in (*AUTHORED, "memories"):
        skipped: dict[str, tuple[int, int, int, int]] = {}
        scanned = store.scan(directory, skipped=skipped)
        found.update(
            {
                name: info
                for name, info in scanned.items()
                if (name.endswith(".jsonl") if directory == "memories" else authored(name))
            }
        )
        found.update(skipped)
    return found


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
    expected_signature = _signature(store)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    # Queries shared with page builders place notes in time on the reading machine.
    connection.create_function("note_time", 1, _note_time, deterministic=True)
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA:
            # Version alone does not establish that an interrupted or damaged cache is usable.
            for statement in (
                "SELECT path,size,mtime,ctime,inode,error FROM files LIMIT 0",
                (
                    "SELECT ref,path,kind,source,title,time,type,status,lead,url,updated,observed,partial,"
                    "tasks_open,tasks_done,next,weight FROM items LIMIT 0"
                ),
                "SELECT id,item,fragment,title FROM passages LIMIT 0",
                "SELECT name,item FROM names LIMIT 0",
                "SELECT item,target FROM links LIMIT 0",
                "SELECT item,target FROM tags LIMIT 0",
                "SELECT item,subject,relation,target,origin FROM edges LIMIT 0",
                "SELECT title,text,names FROM search LIMIT 0",
            ):
                connection.execute(statement)
            signature = connection.execute("SELECT signature FROM ontology").fetchone()
            if signature and signature[0] == expected_signature:
                return connection
    except sqlite3.DatabaseError:
        pass
    connection.close()
    return None


def _signature(store: Store) -> str:
    config = load(store)
    return digest(encode({"name": config.name, "schema": {n: f.model_dump() for n, f in config.ontology.items()}}))


def _create(store: Store) -> sqlite3.Connection:
    """An empty generation in rollback-journal mode; the caller switches it to WAL once it is filled.

    A new file needs almost no rollback journal, while WAL would write every page twice: once to the log
    and again when checkpointing it.
    """
    path = _path(store)
    for suffix in ("", "-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    # Version zero withholds this generation until the ingestion transaction commits.
    connection.executescript(_DDL)
    connection.execute("INSERT INTO ontology VALUES(?)", (_signature(store),))
    connection.commit()
    connection.row_factory = sqlite3.Row
    return connection


def _known(connection: sqlite3.Connection) -> dict[str, tuple[int, int, int, int]]:
    return {
        row["path"]: (row["size"], row["mtime"], row["ctime"], row["inode"])
        for row in connection.execute("SELECT * FROM files")
    }


def _drop(connection: sqlite3.Connection, path: str) -> None:
    """Delete one file's rows with one statement per table, however many items the file holds."""
    owned = "item IN (SELECT id FROM items WHERE path=:path)"
    values = {"path": path}
    connection.execute(f"DELETE FROM search WHERE rowid IN (SELECT id FROM passages WHERE {owned})", values)  # noqa: S608 - fixed SQL
    for table in ("passages", "names", "links", "edges", "tags"):
        connection.execute(f"DELETE FROM {table} WHERE {owned}", values)  # noqa: S608 - fixed table names
    connection.execute("DELETE FROM items WHERE path=?", (path,))
    connection.execute("DELETE FROM files WHERE path=?", (path,))


class _Rows:
    """One file's rows, numbered up front so each table takes a single batched insert."""

    def __init__(self, connection: sqlite3.Connection, refs: list[str]) -> None:
        self.item, self.passage = connection.execute(
            "SELECT (SELECT coalesce(max(id),0)+1 FROM items),(SELECT coalesce(max(id),0)+1 FROM passages)"
        ).fetchone()
        # Refs that other files already hold; the first file to claim a ref keeps it, as the order of indexing.
        self.taken = {
            row[0]
            for row in connection.execute(
                "SELECT ref FROM items WHERE ref IN (SELECT value FROM json_each(?))", (json.dumps(refs),)
            )
        }
        self.items: list[dict[str, str | int]] = []
        self.passages: list[tuple[int, int, str, str]] = []
        self.search: list[tuple[int, str, str, str]] = []
        self.names: list[tuple[str, int]] = []
        self.links: list[tuple[int, str]] = []
        self.edges: dict[tuple[int, str, str, str, str], None] = {}

    def add(
        self,
        values: dict[str, str | int],
        passages: list[tuple[str, str, str, str, str]],
        names: list[str],
        links: list[str],
        edges: Sequence[bf_links.Claim] = (),
    ) -> None:
        ref = str(values["ref"])
        if ref in self.taken:
            raise Error(f"duplicate reference {ref}; run bf validate")
        self.taken.add(ref)
        item = self.item
        self.item += 1
        self.items.append({"id": item, "tasks_open": 0, "tasks_done": 0, "next": "", "weight": 1, **values})
        # `heading` is what ranks; `title` is what readers see, such as "Note — Section".
        for fragment, title, heading, text, extra in passages:
            self.passages.append((self.passage, item, fragment, title))
            self.search.append((self.passage, heading, text, extra))
            self.passage += 1
        self.names.extend((name, item) for name in names)
        self.links.extend((item, link) for link in links)
        # Two identical claims from one file are one edge; support from other files stays separate.
        self.edges.update(dict.fromkeys((item, e.subject, e.relation, e.target, e.origin) for e in edges))

    def write(self, connection: sqlite3.Connection) -> None:
        connection.executemany(
            "INSERT INTO items(id,ref,path,kind,source,title,time,type,status,lead,url,updated,observed,partial,"
            "tasks_open,tasks_done,next,weight) VALUES(:id,:ref,:path,:kind,:source,:title,:time,:type,:status,"
            ":lead,:url,:updated,:observed,:partial,:tasks_open,:tasks_done,:next,:weight)",
            self.items,
        )
        connection.executemany("INSERT INTO passages(id,item,fragment,title) VALUES(?,?,?,?)", self.passages)
        connection.executemany("INSERT INTO search(rowid,title,text,names) VALUES(?,?,?,?)", self.search)
        connection.executemany("INSERT OR IGNORE INTO names VALUES(?,?)", self.names)
        connection.executemany("INSERT OR IGNORE INTO links VALUES(?,?)", self.links)
        connection.executemany("INSERT INTO edges VALUES(?,?,?,?,?)", self.edges)


def _index(connection: sqlite3.Connection, store: Store, path: str) -> list[str]:
    """Insert one file; parse failures raise, while duplicate record ids are skipped and reported."""
    config = load(store)
    # Validate skipped entries before interpreting their extension as a partition.
    if not authored(path) and not path.endswith(".jsonl"):
        store.read(path, 0)
    if authored(path):
        projection = note(path, store.read(path, MAX_NOTE))
        knowledge = projection.knowledge
        edges = ontology.note_claims(projection, config)
        metadata = " ".join([knowledge.type, *knowledge.tags, *knowledge.aliases, *projection.links])
        rows = _Rows(connection, [path])
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
                "weight": 2 if distilled(path) else 1,
                "tasks_open": sum(not done for done, _ in projection.tasks),
                "tasks_done": sum(done for done, _ in projection.tasks),
                "next": next((text for done, text in projection.tasks if not done), ""),
            },
            [
                (p.fragment, p.title, p.heading, p.text, projection.title if p.fragment else metadata)
                for p in projection.passages
            ],
            sorted(
                {
                    ontology.qualify(config, path),
                    *(bf_links.target(v) for v in knowledge.aliases),
                    *([bf_links.identity(knowledge.entity)] if knowledge.entity else []),
                }
            ),
            sorted({*projection.links, *(e.target for e in edges)}),
            edges,
        )
        rows.write(connection)
        connection.executemany(
            "INSERT INTO tags(item,target) SELECT id,? FROM items WHERE ref=?",
            [(ontology.qualify(config, f"tags/{tag}"), path) for tag in knowledge.tags],
        )
        return []
    source = records.source_of(path)
    problems = []
    loaded = records.load(store, path)
    rows = _Rows(connection, [f"{source}:{record.id}" for record in loaded])
    for record in loaded:
        edges = ontology.record_claims(record, config, f"{source}:{record.id}")
        # Field values are searchable words; their JSON keys and punctuation would match every record.
        values = [v for value in record.fields.values() for v in (value if isinstance(value, list) else [value])]
        identity = " ".join([source, *record.aliases, *record.links, record.url, *map(str, values)])
        try:
            rows.add(
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
                sorted(
                    {ontology.qualify(config, f"{source}:{record.id}"), *(bf_links.target(v) for v in record.aliases)}
                ),
                sorted({*(bf_links.target(v) for v in record.links), *(e.target for e in edges)}),
                edges,
            )
        except Error as error:
            problems.append(str(error))
    rows.write(connection)
    return problems


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
        existing = None if full else _open(store)
        connection = existing or _create(store)
        with closing(connection):
            # Keep the index B-trees being written in memory: up to 64 MiB, released when the refresh closes.
            connection.execute("PRAGMA cache_size=-65536")
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
                # A new generation has nothing to drop.
                for path in (removed + changed) if existing else ():
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
                if existing is None:
                    for statement in _INDEXES:
                        connection.execute(statement)
                # PRAGMA has no bound-parameter form; SCHEMA is an internal integer constant.
                connection.execute(f"PRAGMA user_version={SCHEMA}")
            if existing is None:
                # Incremental refreshes then commit beside readers instead of blocking them.
                connection.execute("PRAGMA journal_mode=WAL")
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
def database(store: Store) -> Iterator[tuple[sqlite3.Connection, str]]:
    checked = _CHECKED.get()
    state = checked.get(store.root, "") if checked is not None else ""
    connection = _open(store) if state else None
    if connection is None:
        # A full rebuild may replace the generation between the freshness check and open.
        # Retry once: fresh() waits for an in-progress generation instead of serving it.
        for _ in range(2):
            state = fresh(store)
            connection = _open(store)
            if connection is not None:
                break
        else:
            raise Error("the search cache is unavailable; run bf build")
        if checked is not None:
            checked[store.root] = state
    with closing(connection):
        connection.execute("PRAGMA query_only=ON")
        yield connection, state


def terms(text: str) -> list[str]:
    words = list(dict.fromkeys(re.findall(r"[^\W_]+", text.casefold())))
    return ([w for w in words if w not in _STOP] or words)[:32]


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
_FIELDS = f"i.ref,i.kind,i.source,({TIME}) AS time,i.type,i.status,i.url,i.updated,i.observed,i.partial"
_ROW = f"{_FIELDS},'' AS fragment,i.title,i.lead AS excerpt,i.tasks_open,i.tasks_done,i.next"
# An item links to a target when it names it exactly, or names a section of a target note.
# Two index lookups: exact targets, then the sections of targets. One OR across both would scan every link,
# and CROSS JOIN keeps the few section prefixes as the outer loop of each range search.
_LINKING = """SELECT l.item FROM links l WHERE l.target IN (SELECT value FROM json_each(:targets))
  UNION SELECT l.item FROM json_each(:sections) s CROSS JOIN links l ON l.target>s.value||'#' AND l.target<s.value||'$'"""
_FILTERS = f"""((:since='' AND :until='') OR i.time!='') AND (:since='' OR ({TIME})>=:since) AND (:until='' OR ({TIME})<:until)
  AND (:prefix='' OR i.path=:prefix OR substr(i.path,1,length(:prefix)+1)=:prefix||'/')
  AND (:target='' OR (:tag_scope AND i.id IN (SELECT item FROM tags WHERE target=:target))
       OR (NOT :tag_scope AND i.id IN ({_LINKING})))"""  # noqa: S608 - fixed SQL


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


def _lexical(connection: sqlite3.Connection, params: dict[str, object], match: str) -> Iterator[dict[str, object]]:
    """One ranked any-word query: BM25 adds the weight of each matched term, so fuller matches rank higher.

    Every match is ranked on narrow rows; only the returned items read their display fields.
    Snippets cost far more than ranking, so only returned passages get an excerpt.
    """
    rows = connection.execute(
        f"""WITH matches AS MATERIALIZED (
             SELECT p.item,p.id AS passage,p.fragment,i.ref,i.status IN ('deprecated','archived') AS closed,
                    -bm25(search,10.0,1.0,0.5)*i.weight AS score
             FROM search JOIN passages p ON p.id=search.rowid JOIN items i ON i.id=p.item
             WHERE search MATCH :match AND {_FILTERS}),
           top AS (
             SELECT * FROM (SELECT *,row_number() OVER (PARTITION BY item ORDER BY score DESC,fragment) AS position
                            FROM matches)
             WHERE position=1 ORDER BY closed,score DESC,ref LIMIT :limit)
           SELECT {_FIELDS},t.fragment,p.title,t.passage FROM top t
           JOIN items i ON i.id=t.item JOIN passages p ON p.id=t.passage
           ORDER BY t.closed,t.score DESC,t.ref""",  # noqa: S608 - fixed SQL
        {**params, "match": match},
    )
    for row in rows:
        item = dict(row)
        excerpt = connection.execute(
            "SELECT snippet(search,1,'','',' … ',48) FROM search WHERE search MATCH ? AND rowid=?",
            (match, item.pop("passage")),
        ).fetchone()
        yield {**item, "excerpt": excerpt[0] if excerpt else ""}


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
    identities: set[str] | None = None,
    targets: set[str] | None = None,
    exact: bool | None = None,
    limit: int = 0,
) -> Iterator[dict[str, object]]:
    """A known identity returns its owners, then what links to it; other text ranks lexically, within the scope.

    Words shaped like an identity, such as re:invent, rank lexically when no item is, names or links to them.
    `limit` overrides the query's own, such as one more row to tell whether further results exist.
    """
    params: dict[str, object] = query.model_dump()
    params["tag_scope"] = bf_links.tag(query.target) is not None
    params["limit"] = limit or query.limit
    text = query.text.strip()
    if bf_links.tag(text) is not None:
        text = bf_links.identity(text)
        exact = True
    params["text"] = text
    exact_identities = identities or {text}
    params["identities"] = json.dumps(sorted(exact_identities))
    params["identity_sections"] = json.dumps(sections(exact_identities))
    params.update(_parameters(targets or ({query.target} if query.target else set())))
    if exact is None:
        exact = identity(text) and known(connection, exact_identities | {text})
    if exact:
        return ({**_clean(dict(row)), "_rank": row["score"]} for row in _identity(connection, params))
    tokens = terms(text)
    if not tokens:
        return iter(())
    match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
    return (_clean(row) for row in _lexical(connection, params, match))


def listing_rows(
    connection: sqlite3.Connection, where: str, params: Mapping[str, object], order: str
) -> tuple[Iterator[dict[str, object]], int]:
    """Stream fixed page SQL in order without retaining skipped rows in Python."""
    total = connection.execute(f"SELECT count(*) FROM items i WHERE {where}", params).fetchone()[0]  # noqa: S608
    rows = connection.execute(f"SELECT {_ROW} FROM items i WHERE {where} ORDER BY {order}", params)  # noqa: S608
    return (_clean(dict(row)) for row in rows), total


def listing(
    connection: sqlite3.Connection, where: str, params: Mapping[str, object], order: str, limit: int
) -> tuple[list[dict[str, object]], int]:
    """Items matching fixed SQL written by the page builders, with the total before the limit."""
    values = {"since": "", "until": "", "prefix": "", "target": "", **_parameters(set()), **params}
    # `where` and `order` are fixed SQL written by the page builders; values stay bound parameters.
    total = connection.execute(f"SELECT count(*) FROM items i WHERE {where}", values).fetchone()[0]  # noqa: S608
    rows = connection.execute(
        f"SELECT {_ROW} FROM items i WHERE {where} ORDER BY {order} LIMIT :limit",  # noqa: S608
        {**values, "limit": limit},
    ).fetchall()
    return [_clean(dict(row)) for row in rows], total


_INCOMING = f"""WITH typed AS (
    SELECT e.item,e.relation FROM edges e WHERE e.relation!='' AND e.target IN (SELECT value FROM json_each(:targets))
    UNION SELECT e.item,e.relation FROM json_each(:sections) s
      CROSS JOIN edges e ON e.target>s.value||'#' AND e.target<s.value||'$' WHERE e.relation!=''),
  plain AS (SELECT item,'' AS relation FROM ({_LINKING}) WHERE item NOT IN (SELECT item FROM typed)),
  linked AS (SELECT * FROM typed UNION ALL SELECT * FROM plain)"""  # noqa: S608 - fixed SQL fragments
_GROUPS = f"""{_INCOMING} SELECT k.relation,count(*) AS total FROM linked k JOIN items i ON i.id=k.item
  WHERE i.ref!=:exclude GROUP BY k.relation ORDER BY k.relation='',k.relation LIMIT 64"""  # noqa: S608
_GROUP = f"""{_INCOMING} SELECT {_ROW} FROM linked k JOIN items i ON i.id=k.item
  WHERE k.relation=:relation AND i.ref!=:exclude ORDER BY time DESC,i.ref LIMIT :limit"""  # noqa: S608


def incoming(
    connection: sqlite3.Connection, targets: set[str], *, exclude: str = "", limit: int = 20
) -> list[dict[str, object]]:
    """Items linking to any target, grouped by explicit relationship; links without a role come last."""
    values = {**_parameters(targets), "exclude": exclude, "limit": limit}
    groups = []
    for relation, total in connection.execute(_GROUPS, values).fetchall():
        rows = connection.execute(_GROUP, {**values, "relation": relation}).fetchall()
        group: dict[str, object] = {"total": total, "items": [_clean(dict(row)) for row in rows]}
        groups.append({"relation": relation, **group} if relation else group)
    return groups


def newer_links(connection: sqlite3.Connection, ref: str, after: str) -> int:
    """How many other items link to a note and are dated after `after`: evidence the note may not reflect."""
    row = connection.execute("SELECT id FROM items WHERE ref=?", (ref,)).fetchone()
    if row is None:
        return 0
    names = {name for (name,) in connection.execute("SELECT name FROM names WHERE item=?", (row[0],))} | {ref}
    return int(
        connection.execute(
            f"""SELECT count(*) FROM items i
                WHERE i.id IN ({_LINKING}) AND i.id!=:item AND i.time!='' AND ({TIME})>=:after""",  # noqa: S608
            {**_parameters(names), "item": row[0], "after": after},
        ).fetchone()[0]
    )


def _clean(row: dict[str, object]) -> dict[str, object]:
    fragment = row.pop("fragment", "")
    if fragment:
        row["ref"] = f"{row['ref']}#{fragment}"
    if isinstance(excerpt := row.get("excerpt"), str):
        # A one-line preview; `bf read` returns the exact text.
        row["excerpt"] = re.sub(r"\s+", " ", excerpt).strip()
    row.pop("score", None)
    row.pop("position", None)
    opened, done = int(cast("int", row.pop("tasks_open", 0) or 0)), int(cast("int", row.pop("tasks_done", 0) or 0))
    if opened or done:
        row["tasks"] = {"open": opened, "done": done}
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
        # Most parse errors already name their file; name it once.
        row["error"] if row["error"].startswith(row["path"] + ":") else f"{row['path']}: {row['error']}"
        for row in connection.execute("SELECT path,error FROM files WHERE error!='' ORDER BY path LIMIT 200")
    ]


def sources(connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Indexed record totals and latest event time per source."""
    return {
        row["source"]: {"records": row["records"], "latest": row["latest"] or ""}
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


def partitions(connection: sqlite3.Connection, source: str) -> list[tuple[str, int]]:
    """Indexed partition files of one source with their record counts, newest first."""
    return [
        (row[0], row[1])
        for row in connection.execute(
            "SELECT path,count(*) FROM items WHERE kind='record' AND source=? GROUP BY path ORDER BY path DESC LIMIT 400",
            (source,),
        )
    ]


def status(store: Store) -> dict[str, object]:
    """Cache state, note and record counts, and skipped files."""
    with database(store) as (connection, state):
        counts = sources(connection)
        notes = connection.execute("SELECT count(*) FROM items WHERE kind='note'").fetchone()[0]
        skipped = problems(connection)
    return {"index": state, "notes": notes, "sources": counts, "problems": skipped}
