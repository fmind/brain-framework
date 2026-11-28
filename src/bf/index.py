"""Strictly disposable offset-indexed SQLite FTS5 + ref/alias/link cache under `.bf/cache/index.sqlite3`.

Flat JSONL (`memories/_notes.jsonl` and `memories/<source>.jsonl`) stays canonical on disk.
Write commands (`bf build`, `bf ingest`) update `.bf/cache/index.sqlite3` incrementally under `.bf/corpus.lock`.
Read-only commands (`bf query`, `bf read`, `bf review`, `bf status`) open it strictly read-only (`mode=ro`),
hydrate matched records via `os.pread` from canonical JSONL, and fall back gracefully to streaming JSONL.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import unicodedata
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import closing, nullcontext, suppress
from typing import Any

from bf import models, refs
from bf.models import NOTES_FILE, Error, InputError, decode, now_utc
from bf.storage import Store, open_regular, private_dir, valid_memory_sources, writer

CACHE_PARTS = (".bf", "cache")
INDEX_NAME = "index.sqlite3"
CORPUS_LOCK = "corpus.lock"
SCHEMA_VERSION = 2
NOTE_BOOST = 2.5
_MAX_QUERY_CHARS = 1024
_MAX_QUERY_CLAUSES = 64
_CHUNK_SIZE = 2000
_CORRUPT_CODES = frozenset({11, 24, 26})  # SQLITE_CORRUPT, SQLITE_FORMAT, SQLITE_NOTADB
_EXPECTED_TABLES = frozenset({"meta", "sources", "records", "aliases", "links", "fts"})
_ALLOWED_PLAIN_TABLES = frozenset(
    {
        "meta",
        "sources",
        "records",
        "aliases",
        "links",
        "fts_data",
        "fts_idx",
        "fts_content",
        "fts_docsize",
        "fts_config",
    }
)
_CONTENTLESS_DELETE = sqlite3.sqlite_version_info >= (3, 43, 0)
_FTS_TOKEN_RE = re.compile(r'"([^"]+)"|(\S+)')
_FTS_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

_FTS_OPTS = "content='', contentless_delete=1, " if _CONTENTLESS_DELETE else ""
_DDL = f"""
PRAGMA user_version = {SCHEMA_VERSION};
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    source TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    ino INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    rowid INTEGER PRIMARY KEY,
    ref TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    sort_ts TEXT NOT NULL,
    tags_text TEXT NOT NULL,
    file_offset INTEGER NOT NULL,
    byte_len INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_source ON records(source);
CREATE INDEX IF NOT EXISTS idx_records_kind ON records(kind);
CREATE INDEX IF NOT EXISTS idx_records_type ON records(type) WHERE type <> '';
CREATE INDEX IF NOT EXISTS idx_records_status ON records(status) WHERE status <> '';
CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT NOT NULL,
    ref TEXT NOT NULL,
    PRIMARY KEY (alias, ref)
);
CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
CREATE INDEX IF NOT EXISTS idx_aliases_ref ON aliases(ref);
CREATE TABLE IF NOT EXISTS links (
    src_ref TEXT NOT NULL,
    to_ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    anchor TEXT NOT NULL,
    rel TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_ref);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_ref);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    title,
    description,
    tags,
    aliases,
    ref,
    text,
    {_FTS_OPTS}tokenize="unicode61 remove_diacritics 2"
);
"""


class IndexStaleError(Error):
    """Raised when a canonical JSONL file changed between SQLite lookup and `os.pread` hydration."""


def _cache_dir(store: Store, *, create: bool = True) -> Store:
    return private_dir(store, *CACHE_PARTS, create=create)


def _is_corrupt(err: sqlite3.Error) -> bool:
    if not isinstance(err, sqlite3.DatabaseError):
        return False
    code = getattr(err, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in _CORRUPT_CODES:
        return True
    msg = str(err).lower()
    return any(tok in msg for tok in ("untrusted schema", "malformed", "not a database", "corrupt"))


_DROP_ALL = (
    "DROP TABLE IF EXISTS fts; DROP TABLE IF EXISTS links; DROP TABLE IF EXISTS aliases; "
    "DROP TABLE IF EXISTS records; DROP TABLE IF EXISTS sources; DROP TABLE IF EXISTS meta;"
)


def _check_db_files(cache_dir: Store, *, reject_sidecars: bool = False) -> bool:
    """Verify `.bf/cache/index.sqlite3` (user-owned 0600) and sidecars (user-owned regular files, no symlinks)."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        leaf = f"{INDEX_NAME}{suffix}"
        with cache_dir.parent(leaf) as (parent, name):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                if reject_sidecars:
                    return False
                raise Error(f".bf/cache/{leaf}: expected a regular file without symlinks")
            if info.st_uid != os.geteuid() or (not suffix and (info.st_mode & 0o077)):
                if reject_sidecars:
                    return False
                raise Error(f".bf/cache/{leaf}: must be owned by you with mode 600")
            if reject_sidecars and suffix:
                return False
    return True


def _unlink_db_files(cache_dir: Store) -> None:
    """Unlink `.bf/cache/index.sqlite3` and sidecars safely via `dir_fd` without following symlinks."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        leaf = f"{INDEX_NAME}{suffix}"
        with suppress(Error, OSError), cache_dir.parent(leaf) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode):
                os.unlink(name, dir_fd=parent)


def _verify_schema_safe(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA trusted_schema = OFF")
    bad = con.execute("SELECT type FROM sqlite_master WHERE type IN ('trigger', 'view') LIMIT 1").fetchone()
    if bad is not None:
        raise sqlite3.DatabaseError("untrusted schema objects in index")
    for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"):
        norm = " ".join(str(sql or "").upper().split())
        if name == "fts":
            if not norm.startswith("CREATE VIRTUAL TABLE FTS USING FTS5("):
                raise sqlite3.DatabaseError("untrusted schema: unexpected fts table in index")
        elif name not in _ALLOWED_PLAIN_TABLES or not norm.startswith("CREATE TABLE "):
            raise sqlite3.DatabaseError("untrusted schema: unexpected table in index")


def _connect_rw(store: Store, brain_name: str) -> tuple[sqlite3.Connection, bool]:
    cache_dir = _cache_dir(store)
    db_path = cache_dir.root / INDEX_NAME

    def _open_and_init() -> tuple[sqlite3.Connection, bool]:
        _check_db_files(cache_dir, reject_sidecars=False)
        with cache_dir.parent(INDEX_NAME) as (parent, leaf):
            fd = open_regular(parent, leaf, f".bf/cache/{INDEX_NAME}", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or (info.st_mode & 0o077):
                raise Error(f".bf/cache/{INDEX_NAME}: must be owned by you with mode 600")
        finally:
            os.close(fd)
        con = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            _verify_schema_safe(con)
            con.execute("PRAGMA journal_mode = DELETE")
            con.execute("PRAGMA synchronous = NORMAL")
            con.execute("PRAGMA temp_store = MEMORY")
            con.execute("PRAGMA cache_size = -16000")
            ver = con.execute("PRAGMA user_version").fetchone()[0]
            rebuilt = ver != SCHEMA_VERSION
            if not rebuilt:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if not _EXPECTED_TABLES.issubset(tables):
                    raise sqlite3.DatabaseError("corrupt index missing expected tables")
                row = con.execute("SELECT value FROM meta WHERE key = 'brain'").fetchone()
                rebuilt = row is None or str(row[0]) != brain_name
            if rebuilt:
                con.executescript(_DROP_ALL)
                con.executescript(_DDL)
                with con:
                    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('brain', ?)", (brain_name,))
            return con, rebuilt
        except BaseException:
            con.close()
            raise

    try:
        return _open_and_init()
    except sqlite3.Error as err:
        if not _is_corrupt(err):
            raise
        _unlink_db_files(cache_dir)
        con, _ = _open_and_init()
        return con, True


def open_readonly(store: Store) -> sqlite3.Connection | None:
    """Open `.bf/cache/index.sqlite3` strictly read-only (`mode=ro`) after checking `O_NOFOLLOW`, `0600`, non-WAL, and schema."""
    con: sqlite3.Connection | None = None
    try:
        cache_dir = _cache_dir(store, create=False)
        if not _check_db_files(cache_dir, reject_sidecars=True):
            return None
        with cache_dir.parent(INDEX_NAME) as (parent, leaf):
            fd = open_regular(parent, leaf, f".bf/cache/{INDEX_NAME}", os.O_RDONLY)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or (info.st_mode & 0o077):
                return None
            header = os.pread(fd, 20, 0)
            # Fail closed unless legacy rollback-journal SQLite 3 header (0x01 0x01) so `?mode=ro` never creates `-shm`.
            if len(header) < 20 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
                return None
        finally:
            os.close(fd)
        db_path = cache_dir.root / INDEX_NAME
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=1.0)
        _verify_schema_safe(con)
        con.execute("PRAGMA query_only = ON")
        if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            con.close()
            return None
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not _EXPECTED_TABLES.issubset(tables):
            con.close()
            return None
        return con
    except (Error, OSError, sqlite3.Error):
        if con is not None:
            con.close()
        return None
    except BaseException:
        if con is not None:
            con.close()
        raise


def _delete_bucket_rows(con: sqlite3.Connection, bucket_key: str) -> None:
    if bucket_key == "_notes":
        con.execute("DELETE FROM fts WHERE rowid IN (SELECT rowid FROM records WHERE kind = 'note')")
        con.execute("DELETE FROM aliases WHERE ref IN (SELECT ref FROM records WHERE kind = 'note')")
        con.execute("DELETE FROM links WHERE src_ref IN (SELECT ref FROM records WHERE kind = 'note')")
        con.execute("DELETE FROM records WHERE kind = 'note'")
    else:
        con.execute(
            "DELETE FROM fts WHERE rowid IN (SELECT rowid FROM records WHERE kind = 'memory' AND source = ?)",
            (bucket_key,),
        )
        con.execute(
            "DELETE FROM aliases WHERE ref IN (SELECT ref FROM records WHERE kind = 'memory' AND source = ?)",
            (bucket_key,),
        )
        con.execute(
            "DELETE FROM links WHERE src_ref IN (SELECT ref FROM records WHERE kind = 'memory' AND source = ?)",
            (bucket_key,),
        )
        con.execute("DELETE FROM records WHERE kind = 'memory' AND source = ?", (bucket_key,))
    con.execute("DELETE FROM sources WHERE source = ?", (bucket_key,))


def _delete_refs(con: sqlite3.Connection, ref_list: Sequence[str]) -> None:
    for i in range(0, len(ref_list), 500):
        chunk = list(ref_list[i : i + 500])
        ph = ", ".join("?" for _ in chunk)
        con.execute(f"DELETE FROM fts WHERE rowid IN (SELECT rowid FROM records WHERE ref IN ({ph}))", chunk)  # noqa: S608
        con.execute(f"DELETE FROM aliases WHERE ref IN ({ph})", chunk)  # noqa: S608
        con.execute(f"DELETE FROM links WHERE src_ref IN ({ph})", chunk)  # noqa: S608
        con.execute(f"DELETE FROM records WHERE ref IN ({ph})", chunk)  # noqa: S608


def _batch_insert(
    con: sqlite3.Connection,
    rows: Iterable[tuple[int, int, Mapping[str, Any]]],
    brain_name: str,
    *,
    bucket_key: str,
) -> int:
    """Stream offset-indexed rows into SQLite in bounded chunks while keeping `records` and `fts` rowids strictly aligned."""
    forced_kind = "note" if bucket_key == "_notes" else "memory"
    next_rid = int(con.execute("SELECT COALESCE(MAX(rowid), 0) FROM records").fetchone()[0] or 0) + 1
    seen_total = 0

    def _flush(chunk_map: dict[str, tuple[int, int, Mapping[str, Any]]]) -> None:
        nonlocal next_rid
        if not chunk_map:
            return
        # Remove any pre-existing colliding refs first so `fts` rowids never desync with `records`.
        _delete_refs(con, list(chunk_map))
        rec_rows: list[tuple[Any, ...]] = []
        fts_rows: list[tuple[Any, ...]] = []
        alias_rows: list[tuple[str, str]] = []
        link_rows: list[tuple[str, str, str, str, str]] = []
        for ref, (offset, byte_len, obj) in chunk_map.items():
            rid = next_rid
            next_rid += 1
            title = str(obj.get("title") or "")
            desc = str(obj.get("description") or "")
            entity = str(obj.get("entity") or "")
            rec_source = str(obj.get("source") or "") if forced_kind == "note" else bucket_key
            tags_text = " ".join(str(t) for t in (obj.get("tags") or ()) if isinstance(t, str))
            sort_ts = models.ts_key(str(obj.get("updated") or obj.get("created") or ""))
            text = str(obj.get("text") or "")
            alias_vals: set[str] = {entity} if entity else set()
            for al in obj.get("aliases") or ():
                if isinstance(al, str) and al:
                    alias_vals.add(al)
            aliases_text = " ".join(sorted(alias_vals))
            rec_rows.append(
                (
                    rid,
                    ref,
                    rec_source,
                    forced_kind,
                    str(obj.get("type") or ""),
                    str(obj.get("status") or ""),
                    sort_ts,
                    tags_text,
                    offset,
                    byte_len,
                )
            )
            fts_rows.append((rid, title, desc, tags_text, aliases_text, ref, text))
            if alias_vals:
                alias_rows.extend((a, ref) for a in alias_vals)
            for lk in obj.get("links") or ():
                if isinstance(lk, Mapping) and isinstance(lk.get("to"), str):
                    to_ref = refs.strip_local_uri(str(lk["to"]), brain_name)
                    meta = lk.get("meta") if isinstance(lk.get("meta"), Mapping) else {}
                    link_rows.append(
                        (
                            ref,
                            to_ref,
                            str(lk.get("kind") or ""),
                            str(lk.get("anchor") or ""),
                            str(meta.get("rel") or ""),
                        )
                    )
        con.executemany(
            "INSERT INTO records(rowid, ref, source, kind, type, status, sort_ts, tags_text, file_offset, byte_len) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rec_rows,
        )
        con.executemany(
            "INSERT INTO fts(rowid, title, description, tags, aliases, ref, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
            fts_rows,
        )
        if alias_rows:
            con.executemany("INSERT OR IGNORE INTO aliases(alias, ref) VALUES (?, ?)", alias_rows)
        if link_rows:
            con.executemany("INSERT INTO links(src_ref, to_ref, kind, anchor, rel) VALUES (?, ?, ?, ?, ?)", link_rows)

    chunk: dict[str, tuple[int, int, Mapping[str, Any]]] = {}
    for offset, byte_len, obj in rows:
        if not (ref := str(obj.get("ref") or "")):
            continue
        if ref not in chunk:
            seen_total += 1
        chunk[ref] = (offset, byte_len, obj)
        if len(chunk) >= _CHUNK_SIZE:
            _flush(chunk)
            chunk.clear()
    _flush(chunk)
    return seen_total


def _iter_jsonl_offsets(store: Store, rel_path: str) -> Generator[tuple[int, int, dict[str, Any]]]:
    with closing(store.iter_lines_with_offsets(rel_path, models.MAX_FILE)) as lines:
        for idx, (offset, raw_ln) in enumerate(lines, 1):
            stripped = raw_ln.strip()
            if not stripped:
                continue
            parsed = decode(stripped, f"{rel_path}:{idx}", check_duplicates=False)
            if isinstance(parsed, dict):
                yield offset, len(raw_ln), parsed


def _insert_records(
    con: sqlite3.Connection,
    bucket_key: str,
    rel_path: str,
    st: os.stat_result,
    rows: Iterable[tuple[int, int, Mapping[str, Any]]],
    brain_name: str,
) -> int:
    _delete_bucket_rows(con, bucket_key)
    count = _batch_insert(con, rows, brain_name, bucket_key=bucket_key)
    con.execute(
        "INSERT OR REPLACE INTO sources(source, path, mtime_ns, size_bytes, ino, record_count, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bucket_key, rel_path, st.st_mtime_ns, st.st_size, st.st_ino, count, now_utc()),
    )
    return count


def _sync_all_in_tx(
    con: sqlite3.Connection,
    store: Store,
    brain_name: str,
    *,
    sources: Iterable[str] | None = None,
    reserved: frozenset[str] | None = None,
    meta_updates: Mapping[str, str] | None = None,
) -> dict[str, int]:
    src_list = list(sources) if sources is not None else valid_memory_sources(store, reserved=reserved)
    reindexed = removed = 0
    desired: dict[str, tuple[str, os.stat_result]] = {}
    with suppress(FileNotFoundError):
        desired["_notes"] = (NOTES_FILE, store.stat(NOTES_FILE))
    for src in src_list:
        rel = f"memories/{src}.jsonl"
        with suppress(FileNotFoundError):
            desired[src] = (rel, store.stat(rel))

    existing = {
        row[0]: (int(row[1]), int(row[2]), int(row[3]))
        for row in con.execute("SELECT source, mtime_ns, size_bytes, ino FROM sources").fetchall()
    }
    stale_keys = set(existing) - set(desired)
    to_sync = [
        (key, rel_path, st)
        for key, (rel_path, st) in sorted(desired.items())
        if existing.get(key) != (st.st_mtime_ns, st.st_size, st.st_ino)
    ]
    pending_meta: list[tuple[str, str]] = []
    if meta_updates:
        cur_meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        pending_meta = [(k, v) for k, v in meta_updates.items() if cur_meta.get(k) != v]
    if stale_keys or to_sync or pending_meta:
        with con:
            if not con.in_transaction:
                con.execute("BEGIN")
            for old_src in stale_keys:
                _delete_bucket_rows(con, old_src)
                removed += 1
            for key, rel_path, st in to_sync:
                con.execute("SAVEPOINT sp_src")
                try:
                    with closing(_iter_jsonl_offsets(store, rel_path)) as rows:
                        _insert_records(con, key, rel_path, st, rows, brain_name)
                except (Error, OSError):
                    con.execute("ROLLBACK TO sp_src")
                    con.execute("RELEASE sp_src")
                    _delete_bucket_rows(con, key)
                else:
                    con.execute("RELEASE sp_src")
                    reindexed += 1
            if pending_meta:
                con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", pending_meta)
    return {"reindexed": reindexed, "removed": removed}


def _with_rw_retry[T](
    store: Store,
    brain_name: str,
    action: Callable[[sqlite3.Connection, bool], T],
    *,
    locked: bool = False,
) -> T:
    """Run `action(con, rebuilt)` under `.bf/corpus.lock`, recreating the DB once ONLY on verified SQLite corruption."""
    with nullcontext() if locked else writer(store, name=CORPUS_LOCK):
        for attempt in range(2):
            con: sqlite3.Connection | None = None
            try:
                con, rebuilt = _connect_rw(store, brain_name)
                return action(con, rebuilt)
            except sqlite3.Error as err:
                if con is not None:
                    with suppress(sqlite3.Error):
                        con.close()
                if _is_corrupt(err) and attempt == 0:
                    _unlink_db_files(_cache_dir(store))
                    continue
                raise Error(f".bf/cache/{INDEX_NAME}: SQLite index error ({type(err).__name__})") from err
            finally:
                if con is not None:
                    with suppress(sqlite3.Error):
                        con.close()
    raise AssertionError("unreachable")  # pragma: no cover


def sync_source_delta(
    store: Store,
    brain_name: str,
    bucket_key: str,
    rel_path: str,
    *,
    pre_stat: tuple[int, int, int] | None,
    changed_rows: Iterable[tuple[int, int, Mapping[str, Any]]],
    row_offsets: Mapping[str, tuple[int, int]],
    removed_refs: Iterable[str],
    total_count: int,
    reserved: frozenset[str] | None = None,
    meta_updates: Mapping[str, str] | None = None,
    locked: bool = False,
) -> None:
    """Delta-update changed/removed rows and shifted file offsets without re-tokenizing unchanged rows in FTS5."""
    changed_list = list(changed_rows)
    rem_list = list(removed_refs)

    def _run(con: sqlite3.Connection, rebuilt: bool) -> None:
        if rebuilt:
            _sync_all_in_tx(con, store, brain_name, reserved=reserved, meta_updates=meta_updates)
            return
        with con:
            try:
                st = store.stat(rel_path)
            except FileNotFoundError:
                _delete_bucket_rows(con, bucket_key)
                if meta_updates:
                    con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", list(meta_updates.items()))
                return
            row = con.execute(
                "SELECT mtime_ns, size_bytes, ino FROM sources WHERE source = ?", (bucket_key,)
            ).fetchone()
            in_sync = (pre_stat is None and row is None and len(changed_list) == total_count) or (
                pre_stat is not None and row is not None and (int(row[0]), int(row[1]), int(row[2])) == pre_stat
            )
            if not in_sync:
                with closing(_iter_jsonl_offsets(store, rel_path)) as rows:
                    _insert_records(con, bucket_key, rel_path, st, rows, brain_name)
                if meta_updates:
                    con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", list(meta_updates.items()))
                return
            changed_refs = {str(obj.get("ref") or "") for _, _, obj in changed_list if obj.get("ref")}
            del_refs = sorted({*rem_list, *changed_refs})
            if del_refs:
                _delete_refs(con, del_refs)
            if changed_list:
                _batch_insert(con, changed_list, brain_name, bucket_key=bucket_key)
            # Update shifted byte offsets for surviving unchanged rows without touching FTS5/aliases/links.
            if bucket_key == "_notes":
                cur_rows = con.execute("SELECT ref, file_offset, byte_len FROM records WHERE kind = 'note'").fetchall()
            else:
                cur_rows = con.execute(
                    "SELECT ref, file_offset, byte_len FROM records WHERE kind = 'memory' AND source = ?",
                    (bucket_key,),
                ).fetchall()
            shifted: list[tuple[int, int, str]] = []
            stray_refs: list[str] = []
            for r_ref, old_off, old_len in cur_rows:
                ref_s = str(r_ref)
                if ref_s in changed_refs:
                    continue
                if (new_pos := row_offsets.get(ref_s)) is None:
                    stray_refs.append(ref_s)
                elif new_pos != (int(old_off), int(old_len)):
                    shifted.append((new_pos[0], new_pos[1], ref_s))
            if stray_refs:
                _delete_refs(con, stray_refs)
            if shifted:
                con.executemany("UPDATE records SET file_offset = ?, byte_len = ? WHERE ref = ?", shifted)
            con.execute(
                "INSERT OR REPLACE INTO sources(source, path, mtime_ns, size_bytes, ino, record_count, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (bucket_key, rel_path, st.st_mtime_ns, st.st_size, st.st_ino, total_count, now_utc()),
            )
            if meta_updates:
                con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", list(meta_updates.items()))

    _with_rw_retry(store, brain_name, _run, locked=locked)


def get_meta(store: Store, key: str) -> str:
    """Read a metadata value from `.bf/cache/index.sqlite3` strictly read-only, returning `''` if absent or corrupt."""
    con = open_readonly(store)
    if con is None:
        return ""
    try:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""
    finally:
        con.close()


def sync_all(
    store: Store,
    brain_name: str,
    sources: Iterable[str] | None = None,
    *,
    reserved: frozenset[str] | None = None,
    meta_updates: Mapping[str, str] | None = None,
    locked: bool = False,
) -> dict[str, int]:
    """Incrementally sync `.bf/cache/index.sqlite3`, discovering memory sources inside `.bf/corpus.lock` when omitted."""

    def _run(con: sqlite3.Connection, _rebuilt: bool) -> dict[str, int]:
        return _sync_all_in_tx(con, store, brain_name, sources=sources, reserved=reserved, meta_updates=meta_updates)

    return _with_rw_retry(store, brain_name, _run, locked=locked)


def is_source_fresh(con: sqlite3.Connection, store: Store, bucket_key: str, rel_path: str) -> bool:
    """Return True if `bucket_key` in open read-only `con` matches `(mtime_ns, size_bytes, ino)` of `rel_path`."""
    try:
        st = store.stat(rel_path)
    except FileNotFoundError:
        row = con.execute("SELECT 1 FROM sources WHERE source = ?", (bucket_key,)).fetchone()
        return row is None
    row = con.execute("SELECT mtime_ns, size_bytes, ino FROM sources WHERE source = ?", (bucket_key,)).fetchone()
    return row is not None and (int(row[0]), int(row[1]), int(row[2])) == (st.st_mtime_ns, st.st_size, st.st_ino)


def is_stale(
    store: Store,
    sources: Iterable[str],
    *,
    include_notes: bool = True,
    exact_sources: bool = True,
    ignore_sources: Iterable[str] = (),
) -> bool:
    """Read-only O(sources) staleness check comparing `.bf/cache/index.sqlite3` against requested disk files."""
    desired: dict[str, tuple[int, int, int]] = {}
    if include_notes:
        with suppress(FileNotFoundError):
            st = store.stat(NOTES_FILE)
            desired["_notes"] = (st.st_mtime_ns, st.st_size, st.st_ino)
    ignored = set(ignore_sources)
    src_set: set[str] = set()
    for src in sources:
        if src in ignored:
            continue
        src_set.add(src)
        rel = f"memories/{src}.jsonl"
        with suppress(FileNotFoundError):
            st = store.stat(rel)
            desired[src] = (st.st_mtime_ns, st.st_size, st.st_ino)
    con = open_readonly(store)
    if con is None:
        return bool(desired)
    try:
        all_indexed = {
            str(row[0]): (int(row[1]), int(row[2]), int(row[3]))
            for row in con.execute("SELECT source, mtime_ns, size_bytes, ino FROM sources").fetchall()
        }
        if exact_sources:
            scoped = {
                k: v
                for k, v in all_indexed.items()
                if (k == "_notes" and include_notes) or (k != "_notes" and k not in ignored)
            }
            return scoped != desired
        checked_keys = ({"_notes"} if include_notes else set()) | src_set
        return any(all_indexed.get(key) != desired.get(key) for key in checked_keys)
    except sqlite3.Error:
        return True
    finally:
        con.close()


def _hydrate_rows(
    con: sqlite3.Connection, store: Store, rows: Sequence[tuple[str, str, str, int, int]]
) -> dict[str, dict[str, Any]]:
    """Batch-hydrate matched `(ref, source, kind, file_offset, byte_len)` rows via `os.pread` with stat verification."""
    if not rows:
        return {}
    by_bucket: dict[str, list[tuple[str, int, int]]] = {}
    for ref, source, kind, offset, byte_len in rows:
        bucket = "_notes" if kind == "note" else source
        by_bucket.setdefault(bucket, []).append((ref, offset, byte_len))

    ph = ", ".join("?" for _ in by_bucket)
    meta_rows = con.execute(
        f"SELECT source, path, mtime_ns, size_bytes, ino FROM sources WHERE source IN ({ph})",  # noqa: S608
        list(by_bucket),
    ).fetchall()
    bucket_meta = {str(r[0]): (str(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in meta_rows}

    out: dict[str, dict[str, Any]] = {}
    for bucket, entries in by_bucket.items():
        if bucket not in bucket_meta:
            raise IndexStaleError(f"index missing source metadata for {bucket}")
        if bucket == "_notes":
            expected_path = NOTES_FILE
        elif models.NAME_RE.fullmatch(bucket):
            expected_path = f"memories/{bucket}.jsonl"
        else:
            raise IndexStaleError(f"index contains invalid bucket name {bucket!r}")
        rel_path, exp_mtime, exp_size, exp_ino = bucket_meta[bucket]
        if rel_path != expected_path:
            raise IndexStaleError(f"index source path mismatch for {bucket}")
        try:
            with store.open_for_reads(rel_path) as (fd, info):
                if info.st_size > models.MAX_FILE or (info.st_mtime_ns, info.st_size, info.st_ino) != (
                    exp_mtime,
                    exp_size,
                    exp_ino,
                ):
                    raise IndexStaleError(f"{rel_path} changed since index sync")
                for ref, offset, byte_len in entries:
                    if offset < 0 or byte_len <= 0 or byte_len > models.MAX_FILE or offset + byte_len > info.st_size:
                        raise IndexStaleError(f"{rel_path}: out-of-range offset for {ref}")
                    raw = os.pread(fd, byte_len, offset).strip()
                    parsed = decode(raw, rel_path, check_duplicates=False)
                    if not isinstance(parsed, dict) or str(parsed.get("ref") or "") != ref:
                        raise IndexStaleError(f"{rel_path}: ref mismatch at offset {offset}")
                    out[ref] = parsed
        except (FileNotFoundError, OSError) as err:
            raise IndexStaleError(f"{rel_path}: unreadable during hydration") from err
    return out


def _fold_marks(value: str) -> str:
    """Compose to NFC and drop residual combining marks so Latin NFD, Hangul, and kana reach FTS5 intact."""
    return "".join(ch for ch in unicodedata.normalize("NFC", value) if not unicodedata.combining(ch))


def parse_query_clauses(raw: str, *, fold: Callable[[str], str] = _fold_marks) -> list[tuple[str, str]]:
    """Tokenize free-text query into `("phrase"|"prefix"|"word", folded)` clauses enforcing length & term bounds."""
    stripped = raw.strip()
    if len(stripped) > _MAX_QUERY_CHARS:
        raise InputError(f"query exceeds {_MAX_QUERY_CHARS} characters")
    clauses: list[tuple[str, str]] = []
    for m in _FTS_TOKEN_RE.finditer(stripped):
        phrase, word = m.group(1), m.group(2)
        if phrase is not None:
            words = _FTS_WORD_RE.findall(fold(phrase))
            if words:
                clauses.append(("phrase", " ".join(words)))
        elif word:
            prefix = word.endswith("*") and len(word) > 1
            core = word[:-1] if prefix else word
            parts = _FTS_WORD_RE.findall(fold(core))
            if not parts:
                continue
            if prefix:
                clauses.extend(("word", p) for p in parts[:-1])
                clauses.append(("prefix", parts[-1]))
            else:
                clauses.extend(("word", p) for p in parts)
        if len(clauses) > _MAX_QUERY_CLAUSES:
            raise InputError(f"query exceeds {_MAX_QUERY_CLAUSES} terms")
    return clauses


def build_fts_query(raw: str) -> str:
    """Convert free-text query into a safe SQLite FTS5 MATCH expression (multi-term AND, phrases, prefix `*`)."""
    clauses = parse_query_clauses(raw, fold=_fold_marks)
    out: list[str] = []
    for kind, val in clauses:
        escaped = val.replace('"', '""')
        out.append(f'"{escaped}"*' if kind == "prefix" else f'"{escaped}"')
    return " AND ".join(out)


def search_fts(
    con: sqlite3.Connection,
    store: Store,
    *,
    query_text: str = "",
    kind: str = "",
    source: str = "",
    ref_prefix: str = "",
    rtype: str = "",
    status: str = "",
    tag: str = "",
    since_key: str = "",
    links_to: Sequence[str] = (),
    exclude: Iterable[str] = (),
    limit: int | None = 20,
) -> tuple[list[tuple[dict[str, Any], float]], bool]:
    """Execute ranked BM25 (note-boosted) or structured filter query over `.bf/cache/index.sqlite3` and hydrate hits."""
    where: list[str] = []
    params: list[Any] = []
    if kind:
        where.append("r.kind = ?")
        params.append(kind)
    if source:
        where.append("r.source = ?")
        params.append(source)
    if ref_prefix:
        pfx_slash = f"{ref_prefix}/"
        where.append("(r.ref = ? OR r.ref = ? OR r.ref = ? OR substr(r.ref, 1, ?) = ?)")
        params.extend((ref_prefix, f"{ref_prefix}.md", f"{ref_prefix}/index.md", len(pfx_slash), pfx_slash))
    if rtype:
        where.append("r.type = ?")
        params.append(rtype)
    if status:
        where.append("r.status = ?")
        params.append(status)
    if tag:
        if any(ch.isspace() for ch in tag):
            return [], False
        where.append("instr(' ' || r.tags_text || ' ', ?) > 0")
        params.append(f" {tag} ")
    if since_key:
        where.append("r.sort_ts >= ?")
        params.append(since_key)
    if links_to:
        lt_list = list(dict.fromkeys(links_to))
        ph = ", ".join("?" for _ in lt_list)
        where.append(f"EXISTS (SELECT 1 FROM links l WHERE l.src_ref = r.ref AND l.to_ref IN ({ph}))")  # noqa: S608
        params.extend(lt_list)
    excl = sorted({s for s in exclude if s})
    if excl:
        placeholders = ", ".join("?" for _ in excl)
        where.append(f"r.source NOT IN ({placeholders})")
        params.extend(excl)

    lim_sql = "" if limit is None else " LIMIT ?"
    fts_expr = build_fts_query(query_text) if query_text else ""

    if query_text and not fts_expr:
        return [], False

    if fts_expr:
        where.insert(0, "fts MATCH ?")
        exec_params: list[Any] = [NOTE_BOOST, fts_expr, *params]
        where_sql = " AND ".join(where)
        sql = (
            "SELECT r.ref, r.source, r.kind, r.file_offset, r.byte_len, "  # noqa: S608
            "(bm25(fts, 10.0, 5.0, 3.0, 4.0, 2.0, 1.0) * CASE WHEN r.kind = 'note' THEN ? ELSE 1.0 END) AS score "
            f"FROM fts JOIN records r ON r.rowid = fts.rowid WHERE {where_sql} "
            f"ORDER BY score ASC, r.sort_ts DESC, r.ref ASC{lim_sql}"
        )
    else:
        exec_params = list(params)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            f"SELECT r.ref, r.source, r.kind, r.file_offset, r.byte_len, 0.0 AS score FROM records r{where_sql} "  # noqa: S608
            "ORDER BY CASE WHEN r.kind = 'note' THEN 0 ELSE 1 END ASC, "
            f"r.sort_ts DESC, r.ref ASC{lim_sql}"
        )
    if limit is not None:
        exec_params.append(limit + 1)

    rows = con.execute(sql, exec_params).fetchall()
    truncated = limit is not None and len(rows) > limit
    picked = rows[:limit] if limit is not None else rows
    hydrated = _hydrate_rows(con, store, [(str(r[0]), str(r[1]), str(r[2]), int(r[3]), int(r[4])) for r in picked])
    out: list[tuple[dict[str, Any], float]] = []
    for ref_val, _, _, _, _, score in picked:
        if (rec := hydrated.get(str(ref_val))) is not None:
            out.append((rec, round(-float(score), 4) if fts_expr else 0.0))
    return out, truncated


def lookup_ref(con: sqlite3.Connection, store: Store, ref: str) -> dict[str, Any] | None:
    row = con.execute(
        "SELECT ref, source, kind, file_offset, byte_len FROM records WHERE ref = ? LIMIT 1", (ref,)
    ).fetchone()
    if row is None:
        return None
    hydrated = _hydrate_rows(con, store, [(str(row[0]), str(row[1]), str(row[2]), int(row[3]), int(row[4]))])
    return hydrated.get(str(row[0]))


def lookup_alias(con: sqlite3.Connection, store: Store, identity: str) -> dict[str, Any] | None:
    """Resolve `identity` via `aliases` / `entity` preferring authored notes, then alphabetical ref."""
    candidates = list(refs.alias_variants(identity))
    if not candidates:
        return None
    placeholders = ", ".join("?" for _ in candidates)
    sql = (
        "SELECT r.ref, r.source, r.kind, r.file_offset, r.byte_len "  # noqa: S608
        "FROM aliases a JOIN records r ON r.ref = a.ref "
        f"WHERE a.alias IN ({placeholders}) "
        "ORDER BY CASE WHEN r.kind = 'note' THEN 0 ELSE 1 END ASC, r.ref ASC LIMIT 1"
    )
    row = con.execute(sql, candidates).fetchone()
    if row is None:
        return None
    hydrated = _hydrate_rows(con, store, [(str(row[0]), str(row[1]), str(row[2]), int(row[3]), int(row[4]))])
    return hydrated.get(str(row[0]))


def indexed_counts(con: sqlite3.Connection) -> tuple[int, int, int]:
    """Return `(notes_count, memories_count, memory_sources_count)` in O(sources) SQL."""
    row = con.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN source = '_notes' THEN record_count ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN source <> '_notes' THEN record_count ELSE 0 END), 0), "
        "SUM(CASE WHEN source <> '_notes' AND record_count > 0 THEN 1 ELSE 0 END) "
        "FROM sources"
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
