"""Per-source sharded metadata index and validation cache under `.bf/cache/`."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from contextlib import suppress
from importlib import resources
from typing import Any, TypeGuard

from bf import markdown, models
from bf.models import AUTHORED, FORMAT, MAX_FILE, MAX_TOTAL_FILES, Error, decode, encode
from bf.storage import LINKED, Store, private_dir, relative, writer

_IDX_OPT = ("title", "status", "updated", "tags", "aliases", "entity", "stale_after", "summary")
_NOTE_KINDS = frozenset(("project", "concept", "action"))
_LINK_KINDS = frozenset(("note", "record", "brain", "url"))
_REQ_ENTRY_KEYS = ("path", "kind", "ref", "source")


def _is_sig(val: object) -> bool:
    return isinstance(val, list) and len(val) == 4 and all(isinstance(x, int) and not isinstance(x, bool) for x in val)


def valid_cached_note_entry(cn: object, md_sig: list[int] | None, mem_sig: list[int] | None) -> TypeGuard[list[Any]]:
    """Validate one cached authored-note validate entry before trusting its anchors and links."""
    if not (isinstance(cn, list) and len(cn) == 4 and md_sig == cn[0] and mem_sig is not None and mem_sig == cn[1]):
        return False
    if not (isinstance(cn[2], list) and all(isinstance(a, str) for a in cn[2])):
        return False
    if not isinstance(cn[3], list):
        return False
    for lk in cn[3]:
        if not isinstance(lk, dict) or lk.get("kind") not in _LINK_KINDS or not isinstance(to := lk.get("to"), str):
            return False
        if lk["kind"] == "note":
            try:
                if markdown.is_private_path("/".join(relative(to))):
                    return False
            except Error:
                return False
        elif lk["kind"] == "record" and not markdown.is_memory_record_path(to):
            return False
        elif lk["kind"] == "brain":
            sib, sep, rest = to.removeprefix("bf://").partition("/")
            if not (to.startswith("bf://") and sib and sep and rest):
                return False
            try:
                if ":" not in rest and markdown.is_private_path("/".join(relative(rest))):
                    return False
            except Error:
                return False
    return True


def skill_is_outdated(store: Store) -> bool:
    """Return whether `skills/bf/SKILL.md` exists in `store` and differs from the installed `bf` package."""
    if not store.exists("skills/bf/SKILL.md"):
        return False
    try:
        local = store.read("skills/bf/SKILL.md", models.MAX_NOTE)
        bundled = resources.files("bf").joinpath("skills/bf/SKILL.md").read_bytes()
    except Error, OSError:
        return False
    return local != bundled


def stat_sig(info: os.stat_result) -> list[int]:
    """Return a compact stat signature `[ino, ctime_ns, mtime_ns, size]`."""
    return [info.st_ino, info.st_ctime_ns, info.st_mtime_ns, info.st_size]


def file_sig(store: Store, rel: str) -> list[int] | None:
    """Return `stat_sig` for regular file `rel`, or `None` when absent or inaccessible."""
    with suppress(Error, OSError):
        info = store.stat(rel)
        if stat.S_ISREG(info.st_mode):
            return stat_sig(info)
    return None


def authored_notes(store: Store, *, skipped: list[str] | None = None) -> list[str]:
    """Return sorted OKF note paths without recursing into `actions/*/inputs/` or `outputs/`."""
    paths: list[str] = []
    for folder in ("projects", "concepts"):
        paths.extend(rel for rel in store.files(folder, skipped=skipped) if markdown.okf_role(rel) is not None)
    action_dirs, _ = store.subdirs("actions", skipped=skipped)
    for slug in action_dirs:
        rel = f"actions/{slug}/ACTION.md"
        try:
            info = store.stat(rel)
        except FileNotFoundError:
            continue
        except PermissionError as err:
            if skipped is None:
                raise Error(f"{rel}: permission denied") from err
            skipped.append(f"{rel}: permission denied")
            continue
        except Error as err:
            if skipped is None:
                raise
            skipped.append(str(err))
            continue
        if stat.S_ISREG(info.st_mode):
            paths.append(rel)
        else:
            msg = LINKED if stat.S_ISLNK(info.st_mode) else "expected a regular file"
            if skipped is None:
                raise Error(f"{rel}: {msg}")
            skipped.append(f"{rel}: {msg}")
    return sorted(paths)


def index_entry(mp: str, source: str, rec: dict[str, Any], *, is_note: bool) -> dict[str, Any]:
    """Build a compact metadata entry for `.bf/cache/index/<source>.json`."""
    rid = str(rec.get("id") or "")
    ref = rec["ref"] if is_note else str(rec.get("ref") or (f"{source}:{rid}" if rid else mp))
    kind = rec["kind"] if is_note else str(rec.get("kind") or "memory")
    src_val = rec["source"] if is_note else source
    out: dict[str, Any] = {"path": mp, "kind": kind, "ref": ref, "source": src_val}
    for key in (*_IDX_OPT, "sha256" if is_note else "id"):
        if val := rec.get(key):
            out[key] = val
    if not is_note and not out.get("updated") and (tm := str(rec.get("time") or "")):
        out["updated"] = tm
    if is_note:
        open_tasks = sum(1 for t in (rec.get("tasks") or ()) if isinstance(t, dict) and not t.get("done"))
        if open_tasks:
            out["tasks_open"] = open_tasks
        if not rec.get("updated"):
            retrieved: list[str] = []
            for src_item in rec["sources"] if isinstance(rec.get("sources"), list) else ():
                if isinstance(src_item, dict) and isinstance(r := src_item.get("retrieved"), str) and r:
                    with suppress(Error):
                        norm_fn = models.normalize_date if len(r) == 10 else models.normalize_time
                        retrieved.append(norm_fn(r, "retrieved"))
            if latest_ev := max(retrieved, key=lambda v: f"{v}T23:59:59Z" if len(v) == 10 else v, default=""):
                out["evidence_updated"] = latest_ev
    raw_links = [e for e in (rec.get("links") or ()) if isinstance(e, dict) and isinstance(e.get("to"), str)]
    if raw_links:
        out["links"] = [{"to": str(e["to"]), "kind": str(e.get("kind") or "")} for e in raw_links]
    return out


def valid_index_entry(source: str, key: object, val: object) -> bool:
    if not (isinstance(key, str) and isinstance(val, dict) and val.get("path") == key):
        return False
    if not (key.startswith(f"memories/{source}/") and key.endswith(".json")):
        return False
    if (source not in AUTHORED and key.count("/") != 2) or any(part.startswith(".") for part in key.split("/")):
        return False
    if val.get("source") != source or not all(isinstance(val.get(k), str) for k in _REQ_ENTRY_KEYS):
        return False
    if "tasks_open" in val and (isinstance(val["tasks_open"], bool) or not isinstance(val["tasks_open"], int)):
        return False
    return "links" not in val or (
        isinstance(val["links"], list)
        and all(
            isinstance(e, dict) and isinstance(e.get("to"), str) and isinstance(e.get("kind"), str)
            for e in val["links"]
        )
    )


def _load_index_shard(store: Store, source: str) -> tuple[dict[str, dict[str, Any]], dict[str, list[int]], bool]:
    with suppress(FileNotFoundError, Error):
        shard_dir = private_dir(store, ".bf", "cache", "index", create=False)
        raw = decode(
            shard_dir.read(f"{source}.json", MAX_FILE), f".bf/cache/index/{source}.json", check_duplicates=False
        )
        if not isinstance(raw, dict) or raw.get("version") != FORMAT:
            return {}, {}, False
        entries = raw.get("entries")
        stats = raw.get("stats")
        if not isinstance(entries, dict) or not isinstance(stats, dict):
            return {}, {}, False
        if all(valid_index_entry(source, k, v) for k, v in entries.items()) and all(
            isinstance(k, str) and _is_sig(v) for k, v in stats.items()
        ):
            return dict(entries), dict(stats), True
    return {}, {}, False


def _save_index_shards(
    store: Store,
    updates: dict[str, tuple[dict[str, dict[str, Any]], dict[str, list[int]]]],
    deletions: Sequence[str] = (),
    *,
    locked: bool = False,
) -> None:
    if not updates and not deletions:
        return

    def _write() -> None:
        shard_dir = private_dir(store, ".bf", "cache", "index")
        for src, (entries, stats) in sorted(updates.items()):
            payload = encode({"version": FORMAT, "entries": entries, "stats": stats})
            shard_dir.write(f"{src}.json", payload, mode=0o600, durable=False)
        for src in sorted(deletions):
            with suppress(FileNotFoundError):
                shard_dir.delete(f"{src}.json", durable=False)
        store.sync_dirs([".bf/cache/index"])

    if locked:
        _write()
    else:
        with suppress(Error, PermissionError, OSError), writer(store):
            _write()


def sync_authored_index(
    store: Store,
    compiled_notes: dict[str, dict[str, Any]],
    *,
    locked: bool = False,
) -> None:
    """Update only the three authored shards (`projects`, `concepts`, `actions`) without scanning sensor sources."""
    by_source: dict[str, dict[str, dict[str, Any]]] = {folder: {} for folder in AUTHORED}
    for mp, rec in compiled_notes.items():
        parts = mp.split("/")
        if len(parts) >= 3 and parts[1] in by_source:
            by_source[parts[1]][mp] = rec
    updates: dict[str, tuple[dict[str, dict[str, Any]], dict[str, list[int]]]] = {}
    for folder, recs in by_source.items():
        old_entries, old_stats, ok = _load_index_shard(store, folder)
        new_entries: dict[str, dict[str, Any]] = {}
        new_stats: dict[str, list[int]] = {}
        for mp in sorted(recs):
            new_entries[mp] = index_entry(mp, folder, recs[mp], is_note=True)
            if (sig := file_sig(store, mp)) is not None:
                new_stats[mp] = sig
        if not ok or new_entries != old_entries or new_stats != old_stats:
            updates[folder] = (new_entries, new_stats)
    _save_index_shards(store, updates, locked=locked)


def _sync_source_shard(
    store: Store,
    source: str,
    *,
    problems: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[int]], bool]:
    is_auth = source in AUTHORED
    old_entries, old_stats, shard_ok = _load_index_shard(store, source)
    new_entries: dict[str, dict[str, Any]] = {}
    new_stats: dict[str, list[int]] = {}
    dirty = not shard_ok
    for mp, st_info in store.walk_stats(f"memories/{source}", skipped=problems):
        parts = mp.split("/")
        if not mp.endswith(".json") or (not is_auth and len(parts) != 3):
            continue
        sig = stat_sig(st_info)
        if sig == old_stats.get(mp) and mp in old_entries:
            new_entries[mp] = old_entries[mp]
            new_stats[mp] = sig
            continue
        try:
            rec = decode(store.read(mp, MAX_FILE), mp, check_duplicates=False)
        except FileNotFoundError:
            continue
        except PermissionError as err:
            if problems is None:
                raise Error(f"{mp}: permission denied") from err
            problems.append(f"{mp}: permission denied")
            continue
        except Error as err:
            if not is_auth and problems is not None:
                problems.append(str(err))
            continue
        if not isinstance(rec, dict):
            continue
        if is_auth and (
            rec.get("kind") not in _NOTE_KINDS
            or not isinstance(rec.get("ref"), str)
            or not isinstance(rec.get("source"), str)
        ):
            continue
        entry = index_entry(mp, source, rec, is_note=is_auth)
        new_entries[mp] = entry
        new_stats[mp] = sig
        dirty = dirty or old_entries.get(mp) != entry
    dirty = dirty or len(new_entries) != len(old_entries) or new_stats != old_stats
    return new_entries, new_stats, dirty


def valid_memory_sources(store: Store, *, skipped: list[str] | None = None) -> list[str]:
    """Return valid `memories/<source>` directory names (authored plus sensor sources)."""
    dirs, _ = store.subdirs("memories", skipped=skipped)
    return [
        src for src in dirs if src in AUTHORED or (models.NAME_RE.fullmatch(src) and src not in models.RESERVED_SOURCES)
    ]


def load_index(
    store: Store,
    *,
    problems: list[str] | None = None,
    read_only: bool = False,
) -> list[dict[str, Any]]:
    """Return sorted metadata entries across all `memories/` sources, syncing only dirty per-source shards."""
    active_sources = valid_memory_sources(store, skipped=problems)
    all_entries: dict[str, dict[str, Any]] = {}
    updates: dict[str, tuple[dict[str, dict[str, Any]], dict[str, list[int]]]] = {}
    total = 0
    for src in active_sources:
        entries, stats, dirty = _sync_source_shard(store, src, problems=problems)
        total += len(entries)
        if total > MAX_TOTAL_FILES:
            raise Error(f"memories exceeds the {MAX_TOTAL_FILES:,}-entry total scan limit")
        all_entries.update(entries)
        if dirty:
            updates[src] = (entries, stats)
    stale_shards: list[str] = []
    if not read_only:
        active_set = set(active_sources) | set(AUTHORED)
        with suppress(FileNotFoundError, Error):
            private_dir(store, ".bf", "cache", "index", create=False)
            _, top_files = store.subdirs(".bf/cache/index", skipped=[])
            for rel in top_files:
                leaf = rel.rsplit("/", 1)[-1]
                if leaf.endswith(".json") and (stem := leaf[:-5]) not in active_set:
                    stale_shards.append(stem)
        _save_index_shards(store, updates, stale_shards, locked=False)
    return [all_entries[p] for p in sorted(all_entries)]


def load_validate_shard(store: Store, source: str) -> dict[str, Any]:
    """Load one per-source validation cache shard from `.bf/cache/validate/<source>.json`."""
    with suppress(FileNotFoundError, Error):
        shard_dir = private_dir(store, ".bf", "cache", "validate", create=False)
        raw = decode(
            shard_dir.read(f"{source}.json", MAX_FILE), f".bf/cache/validate/{source}.json", check_duplicates=False
        )
        if isinstance(raw, dict) and raw.get("version") == FORMAT:
            return dict(raw)
    return {}


def save_validate_shards(store: Store, updates: dict[str, dict[str, Any]]) -> None:
    """Persist dirty per-source validation cache shards under `.bf/cache/validate/`."""
    if not updates:
        return
    with suppress(Error, PermissionError, OSError), writer(store):
        shard_dir = private_dir(store, ".bf", "cache", "validate")
        for src, doc in sorted(updates.items()):
            shard_dir.write(f"{src}.json", encode({"version": FORMAT, **doc}), mode=0o600, durable=False)
        store.sync_dirs([".bf/cache/validate"])


def prune_cache_shards(store: Store, active_sources: set[str], *, locked: bool = False) -> list[str]:
    """Remove stale `.bf/cache/{index,validate}/*.json` shards and retired monolithic cache files."""
    removed: list[str] = []

    def _clean() -> None:
        touched: set[str] = set()
        for bucket in ("index", "validate"):
            sub = f".bf/cache/{bucket}"
            with suppress(FileNotFoundError, Error):
                shard_dir = private_dir(store, ".bf", "cache", bucket, create=False)
                _, top_files = store.subdirs(sub, skipped=[])
                for rel in top_files:
                    leaf = rel.rsplit("/", 1)[-1]
                    if leaf.endswith(".json") and (stem := leaf[:-5]) not in active_sources:
                        with suppress(FileNotFoundError):
                            shard_dir.delete(leaf, durable=False)
                            removed.append(f"cache/{bucket}/{stem}")
                            touched.add(sub)
        for legacy_dir, legacy_leaf in ((".bf", "index.jsonl"), (".bf/cache", "state.json")):
            with suppress(FileNotFoundError, Error):
                parent_store = private_dir(store, *legacy_dir.split("/"), create=False)
                if parent_store.exists(legacy_leaf):
                    parent_store.delete(legacy_leaf, durable=False)
                    removed.append(f"{legacy_dir}/{legacy_leaf}")
                    touched.add(legacy_dir)
        if touched:
            store.sync_dirs(sorted(touched))

    if locked:
        _clean()
    else:
        with suppress(Error, PermissionError, OSError), writer(store):
            _clean()
    return sorted(removed)
