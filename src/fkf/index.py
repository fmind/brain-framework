"""One disposable SQLite generation for lexical retrieval and explicit links."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import quote

from pydantic import ValidationError

from fkf.config import load
from fkf.markdown import note, reference
from fkf.models import (
    AUTHORED,
    MAX_CORPUS,
    Collection,
    Error,
    Item,
    Passage,
    Query,
    decode,
    digest,
    encode,
    identity_uri,
    local_reference,
    qualify,
    record_uri,
)
from fkf.storage import Store, writer

VERSION = 16
CACHE = ".fkf/index.sqlite"
MANIFEST = ".fkf/index.json"
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
# Only function words are dropped: a subject such as "resume" or "active" stays literal.
# terms() falls back to all literal terms when cleanup would remove the query.
_SCHEMA = """
CREATE TABLE entries(uri TEXT PRIMARY KEY, kind TEXT, title TEXT, text TEXT, source TEXT,
                     time TEXT, captured TEXT, path TEXT, key TEXT, is_latest INTEGER NOT NULL DEFAULT 1,
                     note_type TEXT, status TEXT, reviewed TEXT, effective TEXT, signals TEXT);
CREATE INDEX record_versions ON entries(source,key,captured,uri);
CREATE TABLE captures(path TEXT PRIMARY KEY, source TEXT, captured TEXT, records INTEGER, mode TEXT);
CREATE INDEX capture_snapshots ON captures(source,mode,captured);
CREATE TABLE aliases(alias TEXT, uri TEXT, PRIMARY KEY(alias, uri));
CREATE INDEX alias_lookup ON aliases(alias);
CREATE INDEX alias_by_uri ON aliases(uri);
CREATE TABLE edges(src TEXT, dst TEXT, PRIMARY KEY(src, dst));
CREATE INDEX destination_lookup ON edges(dst);
CREATE TABLE memberships(uri TEXT, parent TEXT, PRIMARY KEY(uri,parent));
CREATE INDEX parent_lookup ON memberships(parent);
CREATE TABLE supersessions(uri TEXT, target TEXT, PRIMARY KEY(uri,target));
CREATE VIRTUAL TABLE search USING fts5(uri UNINDEXED, fragment UNINDEXED, status UNINDEXED, title, text, tokenize='unicode61 remove_diacritics 2');
"""


def fold(text: str) -> str:
    """Compare two Python-side strings the way the tokenizer does: case and diacritics never separate them."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def inputs(store: Store) -> list[str]:
    names = ["fkf.yaml"]
    try:
        store.fingerprint("fkf.local.yaml")
    except FileNotFoundError:
        pass
    else:
        names.append("fkf.local.yaml")
    for directory in AUTHORED:
        names.extend(n for n in store.files(directory) if n.endswith(".md"))
    names.extend(n for n in store.files("records") if n.endswith(".json"))
    return sorted(names)


def fingerprints(store: Store) -> dict[str, list[int]]:
    result = {name: list(store.fingerprint(name)) for name in inputs(store)}
    if sum(info[0] for info in result.values()) > MAX_CORPUS:
        raise Error("searchable corpus exceeds 512 MiB")
    return result


def document(store: Store, path: str, data: bytes | None = None) -> Collection:
    try:
        return Collection.model_validate(decode(store.read(path) if data is None else data))
    except ValidationError as error:
        raise Error(f"{path}: invalid evidence document") from error


def corpus(
    store: Store, names: list[str], captures: dict[str, tuple[str, str, int, str]] | None = None
) -> Iterator[Item]:
    """Searchable items plus capture metadata, including empty complete snapshots."""
    seen: set[str] = set()
    examined = 0
    for path in names:
        if path.split("/")[0] in AUTHORED:
            yield note(path, store.read(path, 4 << 20))
        elif path.startswith("records/"):
            data = store.read(path)
            capture = document(store, path, data)
            capture_hash = digest(data)
            if captures is not None:
                captures[path] = (capture.source, capture.captured, len(capture.records), capture.mode)
            for record in capture.records:
                examined += 1
                if examined > 100_000:
                    raise Error("corpus exceeds 100,000 records")
                uri = record_uri(capture_hash, record.id)
                if uri in seen:
                    raise Error(f"{path}: duplicate captured record identity")
                seen.add(uri)
                links = sorted(set(record.links + ([record.url] if record.url else [])))
                yield Item(
                    uri=uri,
                    kind=record.kind,
                    title=record.title,
                    text=record.text,
                    source=capture.source,
                    time=record.time,
                    captured=capture.captured,
                    path=path,
                    key=record.id,
                    aliases=sorted({identity_uri(capture.source, record.id), *record.aliases}),
                    links=links,
                    parents=record.parents,
                )


def populate(connection: sqlite3.Connection, store: Store, names: list[str]) -> int:
    config = load(store)
    connection.executescript(_SCHEMA)
    count = 0
    captures: dict[str, tuple[str, str, int, str]] = {}
    for item in corpus(store, names, captures):
        count += 1
        connection.execute(
            "INSERT INTO entries(uri,kind,title,text,source,time,captured,path,key,note_type,status,reviewed,effective,signals) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item.uri,
                item.kind,
                item.title,
                item.text,
                item.source,
                item.time,
                item.captured,
                item.path,
                item.key,
                item.knowledge.type,
                item.knowledge.status,
                item.knowledge.reviewed,
                item.knowledge.effective,
                encode(item.knowledge.signals()).decode(),
            ),
        )
        for passage in item.passages or [Passage(title=item.title, text=item.text)]:
            connection.execute(
                "INSERT INTO search VALUES(?,?,?,?,?)",
                (
                    item.uri,
                    passage.fragment,
                    passage.status,
                    passage.title,
                    passage.text + "\n" + item.source + "\n" + " ".join(item.aliases + item.links),
                ),
            )
        connection.executemany("INSERT OR IGNORE INTO aliases VALUES(?,?)", ((a, item.uri) for a in item.aliases))
        connection.executemany(
            "INSERT OR IGNORE INTO edges VALUES(?,?)",
            ((item.uri, t.removeprefix(f"fkf://{config.id}/")) for t in item.links),
        )
        connection.executemany("INSERT INTO memberships VALUES(?,?)", ((item.uri, p) for p in item.parents))
        connection.executemany(
            "INSERT INTO supersessions VALUES(?,?)",
            (
                (item.uri, local_reference(config.id, reference(item.path, target)))
                for target in item.knowledge.supersedes
            ),
        )
    connection.executemany(
        "INSERT INTO captures VALUES(?,?,?,?,?)", ((path, *values) for path, values in captures.items())
    )
    # Materialize snapshot recency once per generation: every query then reads a column instead of
    # re-deciding it per row, which a correlated subquery made O(corpus) on each exact read.
    connection.execute(
        """UPDATE entries SET is_latest = NOT EXISTS (
             SELECT 1 FROM entries newer WHERE entries.kind!='note' AND newer.kind!='note'
               AND newer.source=entries.source AND newer.key=entries.key
               AND (newer.captured>entries.captured
                    OR (newer.captured=entries.captured AND newer.uri<entries.uri)))
           WHERE kind!='note'""",
        (),
    )
    connection.commit()
    # Absence is evidence only for an explicitly complete source snapshot, never a time window.
    connection.execute(
        """UPDATE entries SET is_latest=0 WHERE kind!='note' AND captured <
        (SELECT max(captured) FROM captures WHERE source=entries.source AND mode='snapshot')""",
        (),
    )
    apply_supersessions(connection)
    connection.commit()
    return count


def apply_supersessions(connection: sqlite3.Connection) -> None:
    """An accepted replacement must name one existing note and cannot participate in a cycle."""
    graph: dict[str, set[str]] = {}
    for source, target in connection.execute(
        """SELECT s.uri,s.target FROM supersessions s
        JOIN entries e ON e.uri=s.uri WHERE e.status IN ('accepted','current')""",
        (),
    ):
        targets = connection.execute(
            """SELECT uri FROM entries WHERE kind='note' AND
            (uri=? OR uri IN (SELECT uri FROM aliases WHERE alias=?))""",
            (target, target),
        ).fetchall()
        if len(targets) != 1:
            raise Error("accepted supersession must resolve to one existing whole note")
        graph.setdefault(source, set()).add(targets[0][0])
    done: set[str] = set()
    for root in graph:
        pending = [(root, False)]
        active: set[str] = set()
        while pending:
            node, leaving = pending.pop()
            if leaving:
                active.remove(node)
                done.add(node)
            elif node in active:
                raise Error("accepted supersessions contain a cycle; resolve the conflicting decisions")
            elif node not in done:
                active.add(node)
                pending.append((node, True))
                pending.extend((target, False) for target in sorted(graph.get(node, ())))
    connection.executemany(
        "UPDATE entries SET status='superseded' WHERE uri=?",
        ((target,) for targets in graph.values() for target in targets),
    )


def build(store: Store, *, if_stale: bool = False) -> dict[str, object]:
    config = load(store)
    with writer(store):
        if if_stale and cache_state(store) == "ready":
            return {"index": "ready", "changed": False, "path": CACHE}
        before = fingerprints(store)
        hashes = {name: digest(store.read(name)) for name in before}
        connection = sqlite3.connect(":memory:")
        try:
            count = populate(connection, store, list(before))
            if connection.execute("PRAGMA integrity_check", ()).fetchone() != ("ok",):
                raise Error("SQLite integrity check failed")
            data = connection.serialize()
            structure = []
            for (
                uri,
                kind,
                title,
                source,
                captured,
            ) in connection.execute(
                """SELECT uri,kind,title,source,captured FROM entries
                WHERE is_latest AND (kind='container' OR uri IN (SELECT uri FROM memberships)) ORDER BY source,title,uri""",
                (),
            ):
                structure.append(
                    {
                        "ref": qualify(config.id, uri),
                        "kind": kind,
                        "title": title,
                        "source": source,
                        "captured": captured,
                        "identities": [
                            r[0]
                            for r in connection.execute("SELECT alias FROM aliases WHERE uri=? ORDER BY alias", (uri,))
                        ],
                        "parents": [
                            r[0]
                            for r in connection.execute(
                                "SELECT parent FROM memberships WHERE uri=? ORDER BY parent", (uri,)
                            )
                        ],
                    }
                )
            export = encode(
                {"base": {"id": config.id, "name": config.name}, "generation": digest(data), "items": structure}
            )
            if len(export) > MAX_CORPUS:
                raise Error("structure index exceeds 512 MiB")
        finally:
            connection.close()
        if fingerprints(store) != before or any(digest(store.read(n)) != h for n, h in hashes.items()):
            raise Error("evidence changed during indexing; retry the build")
        if len(data) > MAX_CORPUS:
            raise Error("index exceeds 512 MiB")
        # Two replacements may temporarily disagree; readers require repair instead of mixing generations.
        store.write(CACHE, data)
        manifest = {"version": VERSION, "cache": list(store.fingerprint(CACHE)), "files": before, "hashes": hashes}
        store.write(MANIFEST, encode(manifest))
        store.write("indexes/structures.json", export)

    return {"entries": count, "bytes": len(data), "path": CACHE, "changed": True}


def _input_state(store: Store, manifest: object, current: dict[str, list[int]]) -> str:
    if not isinstance(manifest, dict) or manifest.get("version") != VERSION:
        return "corrupt"
    files, hashes = manifest.get("files"), manifest.get("hashes")
    if not isinstance(files, dict) or not isinstance(hashes, dict) or files.keys() != hashes.keys():
        return "corrupt"
    if current.keys() != files.keys():
        return "stale"
    for path, info in current.items():
        if info != files[path] and digest(store.read(path)) != hashes.get(path):
            return "stale"
    return "ready"


def snapshot(store: Store) -> tuple[str, dict[str, list[int]]]:
    """Name the generation state from file fingerprints alone; no index bytes are read here."""
    current = fingerprints(store)
    try:
        manifest = decode(store.read(MANIFEST))
        state = _input_state(store, manifest, current)
        if state != "ready":
            return state, current
        # The manifest binds the exact cache file it was published with; a replaced file is a different generation.
        if not isinstance(manifest, dict) or list(store.fingerprint(CACHE)) != manifest.get("cache"):
            return "corrupt", current
        return "ready", current
    except FileNotFoundError:
        return "missing", current
    except Error, OSError, ValueError:
        return "corrupt", current


def cache_state(store: Store) -> str:
    try:
        return snapshot(store)[0]
    except Error, OSError:
        return "corrupt"


@contextmanager
def database(store: Store) -> Iterator[tuple[sqlite3.Connection, str]]:
    """Open a ready generation read-only; retrieval never builds an index."""
    load(store)
    state, _current = snapshot(store)
    if state != "ready":
        raise Error(f"index is {state}; run fkf build for the selected base")
    connection: sqlite3.Connection | None = None
    try:
        # Immutable read-only access keeps the opened inode even when a build replaces the file.
        connection = sqlite3.connect(f"file:{quote(str(store.root / CACHE))}?mode=ro&immutable=1", uri=True)
        connection.execute("PRAGMA trusted_schema=OFF", ())
        connection.execute("PRAGMA query_only=ON", ())
        connection.row_factory = sqlite3.Row
        yield connection, state
    except sqlite3.Error as error:
        raise Error("could not read the derived index; run fkf build") from error
    finally:
        if connection is not None:
            connection.close()


def status(store: Store) -> dict[str, object]:
    """Configured and retired sources with their capture counts and newest capture time."""
    config = load(store)
    sources: dict[str, dict[str, object]] = {
        name: {"enabled": source.enabled, "captures": 0, "records": 0, "latest": ""}
        for name, source in config.sources.items()
    }
    # Diagnostics remain available without an index and never construct an FTS database.
    state, current = snapshot(store)
    totals: dict[str, tuple[int, int, str]] = {}
    for path in current:
        if not path.startswith("records/"):
            continue
        capture = document(store, path)
        count, records, latest = totals.get(capture.source, (0, 0, ""))
        totals[capture.source] = (count + 1, records + len(capture.records), max(latest, capture.captured))
    for source, (captures, records, latest) in totals.items():
        entry = sources.setdefault(source, {"enabled": False, "captures": 0, "records": 0, "latest": ""})
        entry.update({"captures": captures, "records": records, "latest": latest})
    return {"name": config.name, "base": {"id": config.id, "name": config.name}, "index": state, "sources": sources}


def terms(query: str) -> list[str]:
    """Literal MATCH terms; FTS applies its own case and diacritic folding to both sides."""
    literal = set(re.findall(r"[^\W_]+", query.casefold()))
    return sorted(literal - _STOP or literal)[:32]


def search(connection: sqlite3.Connection, query: Query) -> list[dict[str, object]]:
    tokens = [] if re.fullmatch(r"[a-z][a-z0-9+.-]*:\S+", query.text) else terms(query.text)
    match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
    wanted = {fold(t) for t in tokens}
    connection.create_function(
        "title_overlap", 1, lambda title: len(wanted & set(re.findall(r"[^\W_]+", fold(title)))), deterministic=True
    )
    connection.create_function(
        "body_overlap", 1, lambda body: len(wanted & set(re.findall(r"[^\W_]+", fold(body or "")))), deterministic=True
    )
    params = {**query.model_dump(), "match": match or '""'}
    # UNION terminates membership cycles. The corpus bound also bounds the transitive set;
    # scopes follow explicit identities, never similar names or prose.
    if query.within:
        count = connection.execute(
            """WITH RECURSIVE members(uri) AS (
      SELECT m.uri FROM memberships m JOIN entries e ON e.uri=m.uri AND e.is_latest
      WHERE :within!='' AND (m.parent=:within OR m.parent IN (SELECT alias FROM aliases WHERE uri=:within))
      UNION SELECT m.uri FROM members p JOIN aliases a ON a.uri=p.uri
      JOIN memberships m ON m.parent=a.alias JOIN entries e ON e.uri=m.uri AND e.is_latest
    ) SELECT count(*) FROM (SELECT uri FROM members LIMIT 10001)""",
            params,
        ).fetchone()[0]
        if count > 10000:
            raise Error("container scope exceeds 10,000 items; select a narrower container")
    matches = connection.execute(
        """WITH RECURSIVE members(uri) AS (
      SELECT m.uri FROM memberships m JOIN entries e ON e.uri=m.uri AND e.is_latest
      WHERE :within!='' AND (m.parent=:within OR m.parent IN (SELECT alias FROM aliases WHERE uri=:within))
      UNION SELECT m.uri FROM members p JOIN aliases a ON a.uri=p.uri
      JOIN memberships m ON m.parent=a.alias JOIN entries e ON e.uri=m.uri AND e.is_latest
    ),
      lexical AS MATERIALIZED (
        SELECT uri,fragment,status,-bm25(search,0,0,0,8,1) AS score,
               CASE WHEN length(text)<=800 THEN text ELSE snippet(search,4,'','',' … ',32) END AS excerpt
        FROM search WHERE search MATCH :match
      ), candidates AS (
        SELECT * FROM lexical
        UNION ALL SELECT uri,'','',1000.0,NULL FROM aliases WHERE alias=:text
        UNION ALL SELECT uri,'','',1000.0,NULL FROM entries WHERE uri=:text
        UNION ALL SELECT src,'','',100.0,NULL FROM edges WHERE dst=:text
        UNION ALL SELECT uri,'','',0.0,NULL FROM entries WHERE :text='*' AND :within!=''
      ), scored AS (
        SELECT e.*,c.fragment,
          CASE WHEN e.status IN ('superseded','archived','deprecated') THEN e.status ELSE coalesce(nullif(c.status,''),e.status) END AS passage_status,
          coalesce(c.excerpt,substr(e.text,1,800)) AS excerpt,
          c.score*(CASE WHEN e.kind='note' THEN 3.0 ELSE 1.0 END)
            *(CASE WHEN e.status IN ('accepted','current') OR
                (e.status='stable' AND (e.reviewed!='' OR json_array_length(e.signals,'$.verified')>0))
                THEN 2.0 ELSE 1.0 END)
            +8*title_overlap(e.title)+2*body_overlap(c.excerpt) AS score
        FROM candidates c JOIN entries e ON e.uri=c.uri
        WHERE (:source='' OR e.source=:source) AND (:type='' OR e.note_type=:type)
          AND (:within='' OR e.uri IN members)
          AND (:history OR e.is_latest OR e.uri=:text)
          AND (:history OR :status IN ('superseded','archived','deprecated') OR
               (e.status NOT IN ('superseded','archived','deprecated') AND c.status NOT IN ('superseded','archived','deprecated')))
          AND (e.kind='note' OR (((:after='' AND :before='') OR e.time!='')
               AND (:after='' OR e.time>=:after) AND (:before='' OR e.time<:before)))
      ), ranked AS (
        SELECT *,row_number() OVER (PARTITION BY uri ORDER BY score DESC,fragment) AS position
        FROM scored WHERE :status='' OR passage_status=:status
      ) SELECT uri,kind,title,excerpt,source,time,captured,score,is_latest,fragment,
               note_type,passage_status AS status,reviewed,effective,signals,
               CASE WHEN kind='note' AND fragment='' AND passage_status NOT IN ('superseded','archived','deprecated') THEN substr(text,1,240) ELSE '' END AS lead,
               CASE WHEN kind!='note' THEN (SELECT min(alias) FROM aliases WHERE uri=ranked.uri) END AS alias
        FROM ranked WHERE position=1
        ORDER BY CASE WHEN :order='recent' THEN time ELSE '' END DESC,score DESC,captured DESC,uri LIMIT :limit
    """,
        params,
    ).fetchall()
    items = []
    for row in matches:
        item = dict(row)
        signals = decode(item.pop("signals").encode())
        if not isinstance(signals, dict):
            raise Error("invalid knowledge signals in index; rebuild it")
        item.update(signals)
        lead = item.pop("lead")
        passage = item["excerpt"]
        if lead and lead not in passage and passage not in lead:
            item["excerpt"] = lead + " … " + passage
        latest = item.pop("is_latest")
        if item["kind"] != "note":
            item["snapshot"] = "latest" if latest else "historical"
        item = {key: value for key, value in item.items() if value is not None and value != ""}
        items.append(item)
    return items
