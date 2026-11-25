"""Brain lifecycle operations: init, incremental build, fast native+optional validate, and indexed review."""

from __future__ import annotations

import errno
import json
import os
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing, nullcontext, suppress
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from bf import __version__, config, index, markdown, models, records, refs, schemas, state
from bf.markdown import authored_notes
from bf.models import DEFAULT_NOTES, FORMAT, MAX_TOTAL_FILES, NOTES_FILE, Error, InputError, digest, encode, now_utc
from bf.storage import Store, expand, private_dir, writer

_WARN_FORGOTTEN = "snapshot_or_forgotten"
_TERMINAL_STATUSES = frozenset(("deprecated", "done", "archived", "cancelled"))
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _starter_bf_yaml(name: str, description: str) -> bytes:
    header = "# yaml-language-server: $schema=https://fmind.github.io/brain-framework/bf.schema.json\n"
    name_json = json.dumps(name, ensure_ascii=False)
    desc_json = json.dumps(description, ensure_ascii=False)
    return f"{header}version: {FORMAT}\nname: {name_json}\ndescription: {desc_json}\nloops: {{}}\n".encode()


def init_brain(target: Path, *, name: str = "", description: str = "") -> dict[str, Any]:
    """Initialize a new brain directory safely via `O_NOFOLLOW` descriptors, starter schemas, and FTS5 cache."""
    exp = expand(target)
    resolved_name = exp.resolve().name if exp.name in {"", ".", ".."} else exp.name
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", resolved_name.lower()).strip("-"))
    default_name = (f"brain-{slug}" if slug and not slug[0].isalpha() else (slug or "brain"))[:64].rstrip("-")
    brain_name = name or default_name
    if not models.NAME_RE.fullmatch(brain_name):
        raise InputError("brain name must start with a lowercase letter and use lowercase letters, digits, hyphens")
    try:
        stripped = description.strip()
        desc = models.clean_text(stripped, "description") if stripped else "Local-first knowledge brain."
        if len(desc) > 1024:
            raise Error("description exceeds 1024 characters")
    except Error as err:
        raise InputError(str(err)) from err

    if exp.parent != exp:
        exp.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    try:
        exp.mkdir(mode=0o755, exist_ok=False)
    except FileExistsError:
        pass
    except OSError as err:
        raise Error(f"{target}: cannot create directory ({err.strerror or type(err).__name__})") from err

    try:
        dir_fd = os.open(exp, _DIR_FLAGS)
    except OSError as err:
        if err.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise Error(f"{target}: destination exists and is not a real directory") from err
        raise Error(f"{target}: cannot open destination directory") from err
    try:
        info = os.fstat(dir_fd)
        if info.st_uid != os.geteuid():
            raise Error(f"{target}: directory must be owned by you")
        with os.scandir(dir_fd) as it:
            if any(True for _ in it):
                raise Error(f"{target}: directory is not empty; pass a new or empty folder")
        store = Store(exp)
        post = store.root.lstat()
        if (post.st_dev, post.st_ino) != (info.st_dev, info.st_ino):
            raise Error(f"{target}: destination changed during initialization")
    finally:
        os.close(dir_fd)

    starter = resources.files("bf").joinpath("starter")
    for rel in (".gitignore", "AGENTS.md", "concepts/welcome.md", "evals/smoke.py"):
        src_name = "gitignore" if rel == ".gitignore" else rel
        store.write(rel, starter.joinpath(src_name).read_bytes(), durable=False)
    for folder in DEFAULT_NOTES:
        store.write(f"schemas/{folder}.json", encode(schemas.starter_note_schema(folder)), durable=False)
    store.write("bf.yaml", _starter_bf_yaml(brain_name, desc), durable=False)
    for folder in ("projects", "actions", "sensors", "routines", "agents", "memories"):
        store.mkdir(folder, durable=False)
    private_dir(store, "logs")
    store.sync_dirs(["", "concepts", "evals", "schemas", "memories"])
    build(store)
    return {"ok": True, "brain": brain_name, "path": str(store.root)}


def unbuilt_note_dirs(
    store: Store,
    folders: Iterable[str],
    authored: Iterable[str] | None = None,
    *,
    fast: bool = False,
) -> list[str]:
    """Return sorted note folders whose compiled entries in `memories/_notes.jsonl` are missing or out of sync."""
    dirs = tuple(dict.fromkeys(folders))
    all_authored = authored_notes(store, dirs, skipped=[]) if authored is None else sorted(authored)

    by_dir: dict[str, list[str]] = {d: [] for d in dirs}
    for path in all_authored:
        if (d := path.split("/", 1)[0]) in by_dir:
            by_dir[d].append(path)

    has_file = store.exists(NOTES_FILE)
    if not all_authored:
        return list(dirs) if has_file else []
    if not has_file:
        return sorted(d for d, ps in by_dir.items() if ps)

    if fast:
        try:
            mem_mtime_ns = store.stat(NOTES_FILE).st_mtime_ns
            # Check both mtime and ctime so `mv` renames (which preserve mtime but bump ctime) never false-negative.
            all_older = True
            for p in all_authored:
                st = store.stat(p)
                if max(st.st_mtime_ns, st.st_ctime_ns) >= mem_mtime_ns:
                    all_older = False
                    break
            if all_older:
                with closing(store.iter_lines(NOTES_FILE, models.MAX_FILE)) as lines:
                    line_count = sum(1 for ln in lines if ln.strip())
                if line_count == len(all_authored):
                    return []
        except (Error, OSError):
            return sorted(dirs)

    built: dict[str, str] = {}
    try:
        with closing(records.iter_jsonl(store, NOTES_FILE, check_duplicates=False)) as rows:
            for _, obj in rows:
                if not isinstance(ref := obj.get("ref"), str) or ref in built:
                    return sorted(dirs)
                built[ref] = str(obj.get("sha256") or "")
    except (Error, OSError):
        return sorted(dirs)

    dirty: set[str] = set()
    authored_set = set(all_authored)
    for ref in built:
        if ref not in authored_set:
            if (d := ref.split("/", 1)[0]) in by_dir:
                dirty.add(d)
            else:
                return sorted(dirs)
    for folder, notes in by_dir.items():
        for path in notes:
            try:
                if path not in built or digest(store.read(path, models.MAX_NOTE)) != built[path]:
                    dirty.add(folder)
                    break
            except (Error, OSError):
                dirty.add(folder)
                break
    return sorted(dirty)


def _schema_fingerprint(store: Store, cfg: config.Config) -> str:
    parts: list[str] = [__version__, cfg.name, ",".join(cfg.notes)]
    for path in (f"schemas/{f}.json" for f in cfg.notes):
        try:
            parts.append(f"{path}:{digest(store.read(path, models.MAX_SCHEMA))}")
        except (FileNotFoundError, Error, OSError):
            parts.append(f"{path}:-")
    return digest("|".join(parts).encode("utf-8"))


def build(store: Store, *, dry_run: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Incrementally compile authored notes into `memories/_notes.jsonl`, best-effort sync FTS5, and emit `notes.built`."""
    cfg = config.load(store)
    problems: list[str] = []

    dir_vals: dict[str, Any] = {}
    bad_schema_dirs: set[str] = set()
    for folder in cfg.notes:
        try:
            dir_vals[folder] = schemas.load_validator(store, f"schemas/{folder}.json")
        except Error as err:
            bad_schema_dirs.add(folder)
            problems.append(str(err))

    cur_sig = _schema_fingerprint(store, cfg)
    built_event_data: dict[str, Any] | None = None
    index_warning = ""
    with nullcontext() if dry_run else writer(store, name=index.CORPUS_LOCK):
        notes_index_fresh = not index.is_stale(store, (), include_notes=True, exact_sources=False)
        can_reuse = (not bad_schema_dirs) and notes_index_fresh and (index.get_meta(store, "build_sig") == cur_sig)

        existing: dict[str, tuple[bytes, str]] = {}
        existing_order: list[str] = []
        existing_clean = True
        pre_notes_stat: tuple[int, int, int] | None = None
        with suppress(FileNotFoundError):
            st_pre = store.stat(NOTES_FILE)
            pre_notes_stat = (st_pre.st_mtime_ns, st_pre.st_size, st_pre.st_ino)
        if pre_notes_stat is not None:
            try:
                with closing(records.iter_jsonl(store, NOTES_FILE, check_duplicates=False)) as rows:
                    for raw_ln, obj in rows:
                        if isinstance(ref := obj.get("ref"), str) and ref and ref not in existing:
                            existing[ref] = (raw_ln, str(obj.get("sha256") or ""))
                            existing_order.append(ref)
                        else:
                            existing_clean = False
            except (Error, OSError):
                existing_clean = False

        desired: dict[str, bytes] = {}
        changed_by_ref: dict[str, dict[str, Any]] = {}
        failed_mtimes: list[int] = []
        note_count = written = reused = 0

        for folder in cfg.notes:
            folder_skips: list[str] = []
            folder_paths = authored_notes(store, (folder,), skipped=folder_skips)
            if folder_skips:
                problems.extend(folder_skips)
                for p in folder_paths:
                    with suppress(Error, OSError):
                        st_f = store.stat(p)
                        failed_mtimes.append(min(st_f.st_mtime_ns, st_f.st_ctime_ns))
                for ref, (prev_raw, _) in existing.items():
                    if ref.startswith(f"{folder}/"):
                        desired[ref] = prev_raw
                continue
            if folder in bad_schema_dirs:
                for p in folder_paths:
                    with suppress(Error, OSError):
                        st_f = store.stat(p)
                        failed_mtimes.append(min(st_f.st_mtime_ns, st_f.st_ctime_ns))
                for ref, (prev_raw, _) in existing.items():
                    if ref.startswith(f"{folder}/"):
                        desired[ref] = prev_raw
                continue
            user_val = dir_vals.get(folder)
            for path in folder_paths:
                try:
                    raw = store.read(path, models.MAX_NOTE)
                    raw_sha = digest(raw)
                    prev = existing.get(path)
                    if can_reuse and prev is not None and prev[1] == raw_sha:
                        desired[path] = prev[0]
                        note_count += 1
                        reused += 1
                        continue
                    compiled = markdown.compile_note(path, raw, brain_name=cfg.name, note_dirs=cfg.notes)
                    note_errs = schemas.validate_core_record(compiled, path)
                    if user_val is not None:
                        note_errs.extend(schemas.validate_instance(user_val, compiled, path))
                    if note_errs:
                        problems.extend(note_errs)
                        if prev is not None:
                            desired[path] = prev[0]
                        with suppress(Error, OSError):
                            st_f = store.stat(path)
                            failed_mtimes.append(min(st_f.st_mtime_ns, st_f.st_ctime_ns))
                        continue
                    line_bytes = encode(compiled).rstrip(b"\n")
                    desired[path] = line_bytes
                    note_count += 1
                    if prev is None or prev[0] != line_bytes:
                        written += 1
                        changed_by_ref[path] = compiled
                except (Error, OSError) as err:
                    problems.append(str(err) if isinstance(err, Error) else f"{path}: unreadable file")
                    if (prev := existing.get(path)) is not None:
                        desired[path] = prev[0]
                    with suppress(Error, OSError):
                        st_f = store.stat(path)
                        failed_mtimes.append(min(st_f.st_mtime_ns, st_f.st_ctime_ns))

        removed_refs = [ref for ref in existing if ref not in desired]
        removed = len(removed_refs)
        if not dry_run:
            has_notes_file = pre_notes_stat is not None
            sorted_refs = sorted(desired)
            already_synced = (
                existing_clean
                and written == 0
                and removed == 0
                and not failed_mtimes
                and existing_order == sorted_refs
                and (bool(desired) == has_notes_file)
            )
            touched = False
            row_offsets: dict[str, tuple[int, int]] = {}
            if not already_synced:
                if desired:
                    chunks: list[bytes] = []
                    pos = 0
                    for ref in sorted_refs:
                        line_b = desired[ref]
                        row_offsets[ref] = (pos, len(line_b) + 1)
                        chunks.append(line_b)
                        chunks.append(b"\n")
                        pos += len(line_b) + 1
                    payload = b"".join(chunks)
                    cur_bytes = store.read(NOTES_FILE) if has_notes_file else b""
                    if payload != cur_bytes:
                        store.write(NOTES_FILE, payload, durable=True)
                        touched = True
                        if failed_mtimes:
                            earliest = min(failed_mtimes) - 1_000_000
                            with suppress(Error, OSError):
                                store.utime(NOTES_FILE, earliest)
                elif has_notes_file:
                    store.delete(NOTES_FILE, durable=True)
                    touched = True
                if touched and store.is_dir("memories"):
                    store.sync_dirs(["memories"])

            # Best-effort disposable cache sync while holding `corpus.lock` (discovers `mem_sources` under lock).
            index_warning = ""
            try:
                mem_sources = records.valid_memory_sources(store, reserved=cfg.reserved_sources)
                if touched and existing_clean:
                    changed_notes = [
                        (row_offsets[ref][0], row_offsets[ref][1], comp)
                        for ref, comp in changed_by_ref.items()
                        if ref in row_offsets
                    ]
                    index.sync_source_delta(
                        store,
                        cfg.name,
                        "_notes",
                        NOTES_FILE,
                        pre_stat=pre_notes_stat,
                        changed_rows=changed_notes,
                        row_offsets=row_offsets,
                        removed_refs=removed_refs,
                        total_count=len(desired),
                        reserved=cfg.reserved_sources,
                        locked=True,
                    )
                meta_upd = {"build_sig": cur_sig} if not problems else None
                if (
                    (touched and not existing_clean)
                    or index.is_stale(store, mem_sources)
                    or (meta_upd is not None and index.get_meta(store, "build_sig") != cur_sig)
                ):
                    index.sync_all(
                        store,
                        cfg.name,
                        mem_sources,
                        reserved=cfg.reserved_sources,
                        meta_updates=meta_upd,
                        locked=True,
                    )
            except (Error, OSError) as err:
                index_warning = models.redact_secrets(models.printable(models.failure(err)))
            if written or removed:
                built_event_data = {"notes": note_count, "written": written, "removed": removed}

    if built_event_data is not None:
        state.append_event(store, "notes.built", "bf", built_event_data, now=now)

    out_doc: dict[str, Any] = {
        "ok": not problems,
        "brain": cfg.name,
        "notes": note_count,
        "written": written,
        "reused": reused,
        "removed": removed,
        "output": NOTES_FILE,
        "dry_run": dry_run,
        "problems": problems,
    }
    if not dry_run and index_warning:
        out_doc["index_warning"] = index_warning
    return out_doc


def _load_source_refs(store: Store, source: str) -> set[str]:
    ref_set: set[str] = set()
    with (
        suppress(FileNotFoundError, Error, OSError),
        closing(records.iter_jsonl(store, records.memory_file(source), check_duplicates=False)) as rows,
    ):
        for _, obj in rows:
            if isinstance(r := obj.get("ref"), str) and r:
                ref_set.add(r)
    return ref_set


def validate(
    store: Store, *, limit: int = 50, all_problems: bool = False, strict: bool = False, notes_only: bool = False
) -> dict[str, Any]:
    """Strictly read-only config, schemas, JSONL records, notes, loops, and link checks."""
    if not all_problems and (limit < 1 or limit > 5000):
        raise InputError("limit must be between 1 and 5000")
    errors: list[str] = []
    warnings: list[str] = []
    by_kind: dict[str, int] = {}
    seen_msgs: set[str] = set()

    def add(bucket: list[str], kind: str, *msgs: str) -> None:
        for msg in msgs:
            if msg not in seen_msgs:
                seen_msgs.add(msg)
                bucket.append(msg)
                by_kind[kind] = by_kind.get(kind, 0) + 1

    try:
        cfg = config.load(store)
    except Error as err:
        return {
            "ok": False,
            "brain": store.root.name,
            "notes_only": notes_only,
            "counts": {"notes": 0, "memories": None if notes_only else 0},
            "summary": {"errors": 1, "warnings": 0, "by_kind": {"config": 1}},
            "truncated": False,
            "problems": [str(err)],
        }
    try:
        config.load_dotenv(store)
    except Error as err:
        add(errors, "config", str(err))

    validators: dict[str, Any] = {}
    schema_skips: list[str] = []
    schema_dirs, schema_files = store.subdirs("schemas", skipped=schema_skips)
    for sub in schema_dirs:
        add(errors, "schema", f"schemas/{sub}: schemas/ holds only flat <source>.json files")
    for sp in schema_files:
        if not sp.endswith(".json"):
            add(errors, "schema", f"{sp}: schemas/ holds only flat <source>.json files")
        else:
            stem = sp[8:-5]
            if not models.NAME_RE.fullmatch(stem) or stem in models.RESERVED_SOURCES:
                add(errors, "schema", f"{sp}: invalid or reserved schema name {stem!r}")
                continue
            try:
                validators[stem] = schemas.load_validator(store, sp)
            except Error as err:
                validators[stem] = None
                add(errors, "schema", str(err))
    add(errors, "schema", *schema_skips)

    source_schema_map: dict[str, str] = {}
    for lp_name, loop in cfg.loops.items():
        if loop.ingest:
            eff_schema = loop.schema_name or loop.ingest
            source_schema_map[loop.ingest] = eff_schema
            if loop.schema_name and loop.schema_name not in validators:
                add(errors, "schema", f"loops.{lp_name}: explicit schema schemas/{loop.schema_name}.json not found")
        if not loop.enabled:
            continue
        try:
            argv, _, _, _, _ = config.resolve_loop(store, loop, cfg.reserved_sources)
            config.validate_loop_scripts(store, argv, lp_name)
        except Error as err:
            add(errors, "loop", str(err))

    note_skips: list[str] = []
    authored_list = authored_notes(store, cfg.notes, skipped=note_skips)
    add(errors, "note", *note_skips)
    authored_set = set(authored_list)

    reserved = cfg.reserved_sources
    forgotten_map: dict[str, set[str]] = {}
    built_notes: dict[str, dict[str, Any]] = {}
    source_refs: dict[str, set[str]] = {}
    rec_count = total_scanned = 0

    b_skips: list[str] = []
    b_dirs, b_files = store.subdirs("memories", skipped=b_skips)
    for sub in b_dirs:
        add(errors, "memory", f"memories/{sub}: memories/ holds flat <source>.jsonl files")
    for rec_path in b_files:
        leaf = rec_path.removeprefix("memories/")
        if leaf.endswith(".forgotten"):
            src = leaf.removesuffix(".forgotten")
            if not models.NAME_RE.fullmatch(src) or src in reserved:
                add(errors, "memory", f"{rec_path}: invalid or reserved source {src!r}")
                continue
            try:
                forgotten_map.setdefault(src, set()).update(f"{src}:{fid}" for fid in records.forgotten_ids(store, src))
            except Error as err:
                add(errors, "memory", str(err))
            continue
        if not leaf.endswith(".jsonl"):
            add(errors, "memory", f"{rec_path}: memories/ holds flat <source>.jsonl files")
            continue
        if rec_path == NOTES_FILE:
            try:
                with closing(records.iter_jsonl(store, rec_path, check_duplicates=True)) as note_rows:
                    for _, obj in note_rows:
                        ref_s = str(obj.get("ref") or "")
                        if not ref_s or ref_s not in authored_set:
                            add(errors, "note", f"{rec_path}: orphaned compiled note {ref_s!r}; run bf build")
                        elif ref_s in built_notes:
                            add(errors, "note", f"{rec_path}: duplicate compiled note {ref_s!r}; run bf build")
                        else:
                            built_notes[ref_s] = obj
            except Error as err:
                add(errors, "note", str(err))
            continue
        src = leaf.removesuffix(".jsonl")
        if not models.NAME_RE.fullmatch(src) or src in reserved:
            add(errors, "memory", f"{rec_path}: invalid or reserved source {src!r}")
            continue
        if notes_only:
            continue

        schema_stem = source_schema_map.get(src, src)
        user_val = validators.get(schema_stem)
        seen_ids: set[str] = set()
        refs_for_src = source_refs.setdefault(src, set())
        src_rows = 0
        try:
            with closing(records.iter_jsonl(store, rec_path, check_duplicates=True)) as mem_rows:
                for idx, (_, rec_obj) in enumerate(mem_rows, 1):
                    if (total_scanned := total_scanned + 1) > MAX_TOTAL_FILES:
                        raise Error(f"memories exceeds the {MAX_TOTAL_FILES:,}-record total scan limit")
                    src_rows += 1
                    rec_count += 1
                    rec_id_str = rid if isinstance(rid := rec_obj.get("id"), str) else ""
                    if rec_id_str:
                        refs_for_src.add(f"{src}:{rec_id_str}")
                        if rec_id_str in seen_ids:
                            add(errors, "memory", f"{rec_path}:{idx}: duplicate record id {rec_id_str!r} in {src}")
                        seen_ids.add(rec_id_str)
                    label = f"{rec_path}:{idx}"
                    rec_errs = schemas.validate_core_record(rec_obj, label)
                    if user_val is not None:
                        rec_errs.extend(schemas.validate_instance(user_val, rec_obj, label))
                    checks: tuple[tuple[str, str], ...] = (("source", src), ("kind", "memory"))
                    if rec_id_str:
                        checks = (*checks, ("ref", f"{src}:{rec_id_str}"))
                    for key, exp in checks:
                        if rec_obj.get(key) != exp:
                            rec_errs.append(f"{label}: {key} must equal {exp!r}")
                    if isinstance(lks := rec_obj.get("links"), list):
                        rec_errs.extend(
                            f"{label}: {prob}"
                            for lnk in lks
                            if isinstance(lnk, dict)
                            and (prob := refs.record_link_problem(lnk, note_dirs=cfg.notes, reserved=reserved))
                        )
                    if rec_errs:
                        add(errors, "memory", *rec_errs)
            size_b = store.stat(rec_path).st_size
            if src_rows > models.SCALE_WARN_RECORDS or size_b > models.SCALE_WARN_BYTES:
                mb = size_b / (1 << 20)
                add(
                    warnings,
                    "scale",
                    f"{rec_path}: warning: {src_rows:,} records ({mb:.1f} MiB) exceeds recommended single-file sweet spot; consider partitioning",
                )
        except Error as err:
            if "total scan limit" in str(err):
                raise
            add(errors, "memory", str(err))
    add(errors, "memory", *b_skips)

    anchors: dict[tuple[str, str], set[str]] = {}
    note_links: dict[str, list[dict[str, Any]]] = {}
    for ref in authored_list:
        src = ref.split("/")[0]
        try:
            item = markdown.compile_note(
                ref, store.read(ref, models.MAX_NOTE), brain_name=cfg.name, note_dirs=cfg.notes
            )
        except (Error, OSError) as err:
            add(errors, "note", str(err) if isinstance(err, Error) else f"{ref}: unreadable file")
            continue
        anchors[("", ref)] = markdown.compiled_anchors(item)
        note_links[ref] = list(item.get("links") or ())
        note_errs = schemas.validate_core_record(item, ref)
        if (dir_val := validators.get(src)) is not None:
            note_errs.extend(schemas.validate_instance(dir_val, item, ref))
        built_obj = built_notes.get(ref)
        if built_obj is None:
            note_errs.append(f"{NOTES_FILE}: missing compiled note for {ref}; run bf build")
        elif encode(built_obj) != encode(item):
            note_errs.append(f"{NOTES_FILE}: out of sync with {ref}; run bf build")
        if note_errs:
            add(errors, "note", *note_errs)

    exists_cache: dict[str, bool] = dict.fromkeys(authored_list, True)
    siblings: dict[str, tuple[Store, config.Config] | None] = {}
    sib_errs: dict[str, str] = {}
    sib_refs: dict[tuple[str, str], set[str]] = {}
    snap_srcs = {lp.ingest for lp in cfg.loops.values() if lp.enabled and lp.ingest and lp.mode == "snapshot"}
    no_frag = "record links do not support #anchor fragments"

    def resolve_sib(alias: str, origin: str, *, warn_only: bool = False) -> tuple[Store, config.Config] | None:
        if alias not in siblings:
            siblings[alias] = None
            try:
                sib_store = config.resolve_brain(store, cfg, alias)
                siblings[alias] = (sib_store, config.load(sib_store))
            except Error as err:
                sib_errs[alias] = str(err)
        if siblings[alias] is None and (err_msg := sib_errs.get(alias)):
            if warn_only:
                add(warnings, "brain", f"{origin}: warning: {err_msg}")
            else:
                add(errors, "link", f"{origin}: {err_msg}")
        return siblings[alias]

    def check_anchor(target: Store, brain_key: str, rel: str, anc: str, origin: str) -> None:
        if rel.endswith(".md"):
            key = (brain_key, rel)
            if key not in anchors:
                try:
                    anchors[key] = markdown.anchor_ids(rel, target.read(rel, models.MAX_NOTE))
                except (Error, OSError) as err:
                    msg = str(err) if isinstance(err, Error) else f"{rel}: unreadable file"
                    add(errors, "link", f"{origin}: {msg}")
                    return
            if anc not in anchors[key]:
                tgt = f"cross-brain anchor bf://{brain_key}/{rel}#{anc}" if brain_key else f"anchor #{anc} in {rel}"
                add(errors, "link", f"{origin}: missing {tgt}")

    referenced_sibs: set[str] = set()
    for ref, links in sorted(note_links.items()):
        for link in (lk for lk in links if lk["kind"] != "url"):
            kind, to, anc = link["kind"], str(link["to"]), str(link.get("anchor") or "")
            origin = f"{ref} (#{sec})" if (sec := str(link.get("section") or "")) else ref
            if kind == "brain":
                sib_name, _, rest = to.removeprefix("bf://").partition("/")
                referenced_sibs.add(sib_name)
                if (sib_pair := resolve_sib(sib_name, origin, warn_only=False)) is not None:
                    sib, sib_cfg = sib_pair
                    if (parsed_r := refs.parse_record_ref(rest, sib_cfg.reserved_sources)) is not None:
                        if anc:
                            add(errors, "link", f"{origin}: {no_frag}")
                        else:
                            s_src = parsed_r[0]
                            s_key = (sib_name, s_src)
                            if s_key not in sib_refs:
                                sib_refs[s_key] = _load_source_refs(sib, s_src)
                            if rest not in sib_refs[s_key]:
                                add(errors, "link", f"{origin}: broken cross-brain link bf://{sib_name}/{rest}")
                    elif not refs.is_allowed_note_path(rest, sib_cfg.notes) or not sib.exists(rest):
                        add(errors, "link", f"{origin}: broken cross-brain link bf://{sib_name}/{rest}")
                    elif anc:
                        check_anchor(sib, sib_name, rest, anc, origin)
            elif kind == "record" and anc:
                add(errors, "link", f"{origin}: {no_frag}")
            elif kind == "record":
                parsed_r = refs.parse_record_ref(to, reserved)
                if parsed_r is None:
                    add(errors, "link", f"{origin}: invalid record link {to}")
                else:
                    src = parsed_r[0]
                    if src not in source_refs:
                        source_refs[src] = _load_source_refs(store, src)
                    if to not in source_refs[src]:
                        if src in snap_srcs or to in forgotten_map.get(src, ()):
                            add(
                                warnings,
                                _WARN_FORGOTTEN,
                                f"{origin}: warning: missing record target {to} (snapshot or forgotten)",
                            )
                        else:
                            add(errors, "link", f"{origin}: broken link to {to}")
            elif not refs.is_allowed_link_path(to, cfg.notes):
                add(errors, "link", f"{origin}: link target {to} must be a non-private brain file without dotfiles")
            elif not exists_cache.setdefault(to, to in exists_cache or store.exists(to)):
                add(errors, "link", f"{origin}: broken link to {to}")
            elif anc:
                check_anchor(store, "", to, anc, origin)

    for alias in cfg.brains:
        if alias not in referenced_sibs:
            resolve_sib(alias, f"bf.yaml brains.{alias}", warn_only=True)

    msgs, err_n, warn_n = [*errors, *warnings], len(errors), len(warnings)
    trunc = (not all_problems) and len(msgs) > limit
    ok_all = err_n == 0 and (not strict or warn_n == 0)
    shown = msgs if all_problems else msgs[:limit]
    return {
        "ok": ok_all,
        "brain": cfg.name,
        "notes_only": notes_only,
        "counts": {"notes": len(authored_list), "memories": None if notes_only else rec_count},
        "summary": {"errors": err_n, "warnings": warn_n, "by_kind": by_kind},
        "truncated": trunc,
        "problems": shown,
    }


def review(store: Store, *, now: datetime | None = None, limit: int = 20, all_items: bool = False) -> dict[str, Any]:
    """Read-only staleness (`newer_evidence`, `due`, `future_updated`) and open task summary (indexed with JSONL fallback)."""
    if not all_items and (limit < 1 or limit > 5000):
        raise InputError("limit must be between 1 and 5000")
    cfg = config.load(store)
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    today, now_iso = instant.date(), now_utc(instant)
    problems: list[str] = []

    prefixes = (f"bf://{cfg.name}/", "bf:///")

    def norm(ref_s: str) -> str:
        bare = ref_s.partition("#")[0]
        return next((bare.removeprefix(p) for p in prefixes if bare.startswith(p)), bare)

    note_entries: list[dict[str, Any]] = []
    note_time: dict[str, str] = {}
    incoming: dict[str, set[str]] = {}
    rec_time: dict[str, str] = {}

    if store.exists(NOTES_FILE):
        try:
            with closing(records.iter_jsonl(store, NOTES_FILE, check_duplicates=False)) as note_rows:
                for _, obj in note_rows:
                    if obj.get("kind") != "note" or not isinstance(obj.get("ref"), str):
                        continue
                    ref_val = str(obj["ref"])
                    note_entries.append(obj)
                    if upd := str(obj.get("updated") or obj.get("created") or ""):
                        note_time[ref_val] = models.ts_key(upd)
                    for lk in obj.get("links") or ():
                        if isinstance(lk, dict) and isinstance(lk.get("to"), str):
                            tgt = norm(str(lk["to"]))
                            if tgt and tgt != ref_val:
                                incoming.setdefault(tgt, set()).add(ref_val)
        except Error as err:
            problems.append(str(err))

    wanted_refs: set[str] = set()
    wanted_incoming: set[str] = set()
    for entry in note_entries:
        if str(entry.get("status") or "stable") in _TERMINAL_STATUSES:
            continue
        n_ref = str(entry["ref"])
        if n_ref not in note_time:
            continue
        wanted_incoming.add(n_ref)
        for lk in entry.get("links") or ():
            if isinstance(lk, dict) and lk.get("kind") == "record" and isinstance(lk.get("to"), str):
                wanted_refs.add(norm(str(lk["to"])))
        for raw_id in (entry.get("entity"), *(entry["aliases"] if isinstance(entry.get("aliases"), list) else ())):
            if isinstance(raw_id, str) and raw_id:
                nr = norm(raw_id)
                bare = nr.removeprefix("urn:")
                wanted_incoming.update((nr, bare, f"urn:{bare}"))

    mem_sources = records.valid_memory_sources(store, reserved=cfg.reserved_sources, skipped=problems)
    stale_idx = index.is_stale(store, mem_sources, include_notes=False, exact_sources=True)
    used_index = False
    if (wanted_refs or wanted_incoming) and not stale_idx:
        con = index.open_readonly(store)
        if con is not None:
            idx_rec_time: dict[str, str] = {}
            idx_incoming: dict[str, set[str]] = {}
            try:
                if wanted_refs:
                    ref_list = sorted(wanted_refs)
                    for i in range(0, len(ref_list), 500):
                        chunk = ref_list[i : i + 500]
                        ph = ", ".join("?" for _ in chunk)
                        sql_ref = f"SELECT ref, sort_ts FROM records WHERE kind = 'memory' AND ref IN ({ph})"  # noqa: S608
                        for r_ref, r_ts in con.execute(sql_ref, chunk):
                            if ts := str(r_ts or ""):
                                idx_rec_time[str(r_ref)] = max(idx_rec_time.get(str(r_ref), ""), ts)
                if wanted_incoming:
                    inc_list = sorted(wanted_incoming)
                    for i in range(0, len(inc_list), 500):
                        chunk = inc_list[i : i + 500]
                        ph = ", ".join("?" for _ in chunk)
                        sql_inc = (
                            "SELECT l.src_ref, l.to_ref, r.sort_ts "  # noqa: S608
                            "FROM links l JOIN records r ON r.ref = l.src_ref "
                            f"WHERE r.kind = 'memory' AND l.to_ref IN ({ph})"
                        )
                        for src_ref, to_ref, r_ts in con.execute(sql_inc, chunk):
                            idx_incoming.setdefault(str(to_ref), set()).add(str(src_ref))
                            if ts := str(r_ts or ""):
                                idx_rec_time[str(src_ref)] = max(idx_rec_time.get(str(src_ref), ""), ts)
                for rk, rt in idx_rec_time.items():
                    rec_time[rk] = max(rec_time.get(rk, ""), rt)
                for ik, ivs in idx_incoming.items():
                    incoming.setdefault(ik, set()).update(ivs)
                used_index = True
            except sqlite3.Error:
                used_index = False
                stale_idx = True
            finally:
                con.close()
        else:
            stale_idx = bool(mem_sources)

    if not used_index and (wanted_refs or wanted_incoming):
        scanned_records = 0
        for src in mem_sources:
            mem_path = records.memory_file(src)
            try:
                with closing(store.iter_lines(mem_path, models.MAX_FILE)) as lines:
                    for idx_ln, raw_ln in enumerate(lines, 1):
                        if not (stripped := raw_ln.strip()):
                            continue
                        scanned_records += 1
                        if scanned_records > MAX_TOTAL_FILES:
                            raise Error(f"memories exceeds the {MAX_TOTAL_FILES:,}-record total scan limit")
                        try:
                            obj = models.decode(stripped, f"{mem_path}:{idx_ln}", check_duplicates=False)
                        except Error as err:
                            problems.append(str(err))
                            continue
                        if not isinstance(obj, dict):
                            continue
                        rec_id = str(obj.get("id") or "")
                        ref_val = str(obj.get("ref") or (f"{src}:{rec_id}" if rec_id else ""))
                        if not ref_val:
                            continue
                        matched_incoming = False
                        if isinstance(lks := obj.get("links"), list):
                            for lk in lks:
                                if (
                                    isinstance(lk, dict)
                                    and isinstance(lk.get("to"), str)
                                    and (tgt := norm(str(lk["to"]))) in wanted_incoming
                                ):
                                    incoming.setdefault(tgt, set()).add(ref_val)
                                    matched_incoming = True
                        if (matched_incoming or ref_val in wanted_refs) and (
                            ts := str(obj.get("updated") or obj.get("created") or "")
                        ):
                            rec_time[ref_val] = max(rec_time.get(ref_val, ""), models.ts_key(ts))
            except (Error, OSError) as err:
                if isinstance(err, Error) and "total scan limit" in str(err):
                    raise
                problems.append(str(err) if isinstance(err, Error) else f"{mem_path}: unreadable file")

    stale: list[dict[str, Any]] = []
    tasks_open = 0
    today_iso = today.isoformat()
    for entry in sorted(note_entries, key=lambda x: str(x["ref"])):
        ref, status = str(entry["ref"]), str(entry.get("status") or "stable")
        if status in _TERMINAL_STATUSES:
            continue
        tasks_open += sum(1 for t in (entry.get("tasks") or ()) if isinstance(t, dict) and not t.get("done"))
        reasons: list[str] = []
        newer: list[str] = []
        due = False
        upd = str(entry.get("updated") or entry.get("created") or "")
        if upd and (
            (len(upd) == 10 and upd > today_iso) or (len(upd) > 10 and models.ts_key(upd) > models.ts_key(now_iso))
        ):
            reasons.append("future_updated")
        sa = entry.get("stale_after")
        if isinstance(sa, str) and sa:
            due = True
            is_dur = bool(models.DURATION_RE.fullmatch(sa))
            if ref_date := (upd if is_dur else sa):
                try:
                    dt = datetime.fromisoformat(
                        f"{ref_date}T00:00:00Z" if len(ref_date) == 10 else ref_date
                    ).astimezone(UTC)
                    due = instant >= (dt + timedelta(seconds=models.parse_duration(sa)) if is_dur else dt)
                except (Error, OverflowError, ValueError):
                    due = now_iso >= sa
        if due:
            reasons.append("due")
        if ref in note_time:
            outs = {
                norm(str(lk["to"]))
                for lk in (entry.get("links") or ())
                if isinstance(lk, dict) and lk.get("kind") in {"note", "record"} and lk.get("to") != ref
            }
            linked = outs | incoming.get(ref, set())
            for raw_id in (entry.get("entity"), *(entry["aliases"] if isinstance(entry.get("aliases"), list) else ())):
                if isinstance(raw_id, str) and raw_id:
                    nr = norm(raw_id)
                    bare = nr.removeprefix("urn:")
                    linked.update(*(incoming.get(k, ()) for k in (nr, bare, f"urn:{bare}")))
            linked.discard(ref)
            if newer := [t for t in sorted(linked) if (rec_time.get(t) or note_time.get(t) or "") > note_time[ref]]:
                reasons.append("newer_evidence")
        if reasons:
            newer_d: dict[str, Any] = (
                {"newer": newer[:5], **({"newer_count": len(newer), "newer_truncated": True} if len(newer) > 5 else {})}
                if newer
                else {}
            )
            stale.append({"ref": ref, "title": str(entry.get("title") or ""), "reasons": reasons, **newer_d})

    stale.sort(key=lambda x: (-len(x["reasons"]), -int(x.get("newer_count") or len(x.get("newer", ()))), x["ref"]))
    stale_out, truncated = (stale if all_items else stale[:limit]), (not all_items) and len(stale) > limit
    unbuilt = unbuilt_note_dirs(store, cfg.notes, fast=True)
    return {
        "ok": not problems,
        "brain": cfg.name,
        "total_stale": len(stale),
        "total_tasks_open": tasks_open,
        "stale": stale_out,
        "truncated": truncated,
        **({"unbuilt_notes": unbuilt} if unbuilt else {}),
        **({"index_stale": True} if stale_idx else {}),
        "problems": list(dict.fromkeys(problems)),
    }
