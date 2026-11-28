"""Unified JSONL record storage (`memories/<source>.jsonl`, `memories/_notes.jsonl`), validation, and ingestion."""

from __future__ import annotations

from collections.abc import Generator, Iterable
from contextlib import closing, nullcontext, suppress
from datetime import datetime
from typing import Any, Literal

from bf import index, models, refs, schemas, state
from bf.models import (
    DEFAULT_NOTES,
    Error,
    InputError,
    clean,
    decode,
    encode,
    failure,
    now_utc,
    printable,
    redact_secrets,
    sanitize_record_strings,
)
from bf.storage import Store, memory_file, valid_memory_sources, writer

__all__ = ["Mode", "forgotten_ids", "ingest", "iter_jsonl", "memory_file", "valid_memory_sources"]
Mode = Literal["upsert", "snapshot"]
MAX_EVENT_REFS = 64
MAX_EVENT_REFS_BYTES = 32_768


def _bound_refs(refs: list[str]) -> tuple[list[str], bool]:
    kept: list[str] = []
    used = 0
    for ref in refs:
        if len(kept) >= MAX_EVENT_REFS:
            return kept, True
        cost = len(encode(ref))
        if used + cost > MAX_EVENT_REFS_BYTES:
            return kept, True
        kept.append(ref)
        used += cost
    return kept, False


def iter_jsonl(store: Store, path: str, *, check_duplicates: bool = False) -> Generator[tuple[bytes, dict[str, Any]]]:
    """Stream `(raw_line_bytes, parsed_dict)` for each non-empty line in JSONL file `path`."""
    with closing(store.iter_lines(path, models.MAX_FILE)) as lines:
        for idx, raw_line in enumerate(lines, 1):
            if not (stripped := raw_line.strip()):
                continue
            parsed = decode(stripped, f"{path}:{idx}", check_duplicates=check_duplicates)
            if not isinstance(parsed, dict):
                raise Error(f"{path}:{idx}: expected a JSON object")
            yield stripped, parsed


def forgotten_ids(store: Store, source: str) -> set[str]:
    """Return the set of explicitly forgotten record IDs from `memories/<source>.forgotten`."""
    try:
        raw = store.read(f"memories/{source}.forgotten", models.MAX_NOTE)
    except FileNotFoundError:
        return set()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Error(f"memories/{source}.forgotten: file is not valid UTF-8") from None
    return {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")}


def parse_batch_envelope(data: bytes, label: str = "input") -> tuple[list[dict[str, Any]], bool]:
    """Parse a JSON array, `{"records": [...], "incomplete"?}` envelope, single object, or JSONL."""
    if len(data) > models.MAX_BATCH:
        raise Error(f"{label} exceeds {models.MAX_BATCH} bytes; split the batch")
    try:
        text = data.decode("utf-8").removeprefix("\ufeff").strip()
    except UnicodeDecodeError as error:
        raise Error(f"{label} is not valid UTF-8") from error
    if not text:
        return [], False
    if text.startswith("["):
        if not isinstance(parsed := decode(text, label), list) or not all(isinstance(x, dict) for x in parsed):
            raise Error(f"{label}: expected a JSON array of record objects")
        return [dict(x) for x in parsed], False
    if text.startswith("{"):
        try:
            whole = decode(text, label)
        except Error as err:
            if "duplicate key" in str(err) or "must be finite" in str(err):
                raise
            whole = None
        if isinstance(whole, dict):
            if "records" not in whole or "id" in whole:
                return [dict(whole)], False
            if extra := sorted(set(whole) - {"records", "incomplete"}):
                raise Error(f"{label}: unknown batch envelope keys {', '.join(extra)}")
            if not isinstance(recs := whole["records"], list) or not all(isinstance(x, dict) for x in recs):
                raise Error(f"{label}.records: expected a JSON array of record objects")
            inc = whole.get("incomplete")
            if isinstance(inc, dict) and set(inc) == {"missed"} and type(m := inc["missed"]) is int and m >= 0:
                return [dict(x) for x in recs], m > 0
            if inc is None or isinstance(inc, bool):
                return [dict(x) for x in recs], bool(inc)
            raise Error(f"{label}.incomplete: expected a boolean or {{'missed': non-negative integer}}")
    items: list[dict[str, Any]] = []
    for num, ln in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        if stripped := ln.strip():
            if not isinstance(parsed := decode(stripped, f"{label} line {num}"), dict):
                raise Error(f"{label} line {num}: expected a JSON object")
            items.append(dict(parsed))
    return items, False


def _normalize_record_links(
    raw_links: object, label: str, brain_name: str, note_dirs: Iterable[str] = DEFAULT_NOTES
) -> list[dict[str, Any]]:
    if raw_links is None:
        return []
    if not isinstance(raw_links, list):
        raise Error(f"{label}.links must be a list")
    dirs = tuple(note_dirs)
    roots = tuple(f"{folder}/" for folder in dirs)
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def add_one(tgt: str, sec: str = "", meta: dict[str, str] | None = None, req: bool = True, kind: str = "") -> None:
        parsed = refs.parse_link(
            label, tgt, brain_name, note_dirs=dirs, section=sec, attr_meta=meta, relative_base=False, require_uri=req
        )
        if kind and parsed["kind"] != kind:
            raise Error(f"{label}.links: link kind {kind!r} does not match target")
        if prob := refs.record_link_problem(parsed, note_dirs=dirs):
            raise Error(f"{label}: {prob}")
        refs.add_link(seen, out, parsed)

    for item in raw_links:
        if isinstance(item, str):
            if not item.strip():
                raise Error(f"{label}.links entries must be nonempty link targets")
            add_one(item, req=not item.startswith(roots))
        elif isinstance(item, dict) and isinstance(item.get("to"), str):
            if extra := sorted(set(item) - {"to", "kind", "anchor", "meta", "section"}):
                raise Error(f"{label}.links: unknown link keys {', '.join(extra)}; put metadata under 'meta'")
            for field_key in ("kind", "anchor", "section"):
                if item.get(field_key) is not None and not isinstance(item[field_key], str):
                    raise Error(f"{label}.links: {field_key} must be a string")
            meta = {} if item.get("meta") is None else item["meta"]
            if not isinstance(meta, dict) or not all(isinstance(x, str) for kv in meta.items() for x in kv):
                raise Error(f"{label}.links: link meta must map strings to strings")
            tgt, frag = str(item["to"]), str(item.get("anchor") or "")
            if frag and "#" in tgt and tgt.partition("#")[2] != frag:
                raise Error(f"{label}.links: conflicting #anchor on target")
            tgt = f"{tgt}#{frag}" if frag and "#" not in tgt else tgt
            add_one(tgt, str(item.get("section") or ""), meta, not tgt.startswith(roots), str(item.get("kind") or ""))
        else:
            raise Error(f"{label}.links entries must be target strings or link objects")
    return refs.dedupe_typed_links(out)


def _prepare_record(
    raw: dict[str, Any], source: str, idx: int, brain_name: str, note_dirs: Iterable[str]
) -> dict[str, Any]:
    label = f"{source}[{idx}]"
    raw_id_orig = raw.get("id")
    if not isinstance(raw_id_orig, str):
        raise Error(f"{label}: missing required string 'id'")
    if models.invisible(raw_id_orig):
        raise Error(f"{label}.id must contain no invisible format characters")
    rec = sanitize_record_strings(dict(raw))
    raw_id = str(rec["id"])
    if not isinstance(raw_title := rec.get("title"), str):
        raise Error(f"{label}: missing required string 'title'")
    rec_id = clean(raw_id, f"{label}.id")
    is_http_id = rec_id.lower().startswith(("https://", "http://"))
    if (
        any(ch.isspace() for ch in rec_id)
        or "#" in rec_id
        or rec_id.startswith("//")
        or ("?" in rec_id and not is_http_id)
    ):
        raise Error(f"{label}.id must contain no whitespace, '#', '//' prefix, or non-URL '?'")
    exp_ref = f"{source}:{rec_id}"
    rec["id"] = rec_id
    rec["title"] = clean(models.strip_unsafe_text(raw_title), f"{label}.title")
    for key, exp in (("source", source), ("ref", exp_ref), ("kind", "memory")):
        if key in rec and rec[key] != exp:
            raise Error(f"{label}.{key} must {'equal' if key == 'kind' else 'match'} {exp!r}")
    rec["source"], rec["ref"], rec["kind"] = source, exp_ref, "memory"
    if (txt := rec.get("text")) is None:
        rec["text"] = ""
    elif not isinstance(txt, str) or len(txt) > 4 << 20:
        raise Error(f"{label}.text {'exceeds 4 MiB' if isinstance(txt, str) else 'must be a string'}")
    rec["tags"] = refs.normalize_tags(rec["tags"], label) if rec.get("tags") is not None else []
    rec["links"] = _normalize_record_links(rec.get("links"), label, brain_name, note_dirs)
    if "url" in rec:
        if not isinstance(rec["url"], str):
            raise Error(f"{label}.url must be a string")
        if rec["url"]:
            rec["url"] = refs.validate_url(rec["url"], f"{label}.url")
        else:
            rec.pop("url")
    for field_name in ("created", "updated"):
        if field_name in rec:
            if not isinstance(val := rec[field_name], str):
                raise Error(f"{label}.{field_name} must be an ISO 8601 date or timestamp string")
            if not val:
                del rec[field_name]
            else:
                rec[field_name] = models.normalize_iso_date_or_time(val, f"{label}.{field_name}")
    if "stale_after" in rec and rec["stale_after"] is not None:
        rec["stale_after"] = models.validate_stale_after(rec["stale_after"], f"{label}.stale_after")
    rec.pop("ingested_at", None)
    encode(rec)
    return rec


def validate_ingest_args(
    source: str, *, mode: Mode = "upsert", schema_name: str = "", allow_removal: bool = False
) -> None:
    """Validate brain-independent `bf ingest` arguments before reading stdin/files or `bf.yaml`."""
    if not models.NAME_RE.fullmatch(source):
        raise InputError("source must start with a lowercase letter and use only lowercase letters, digits, or '-'")
    if source in models.RESERVED_SOURCES:
        raise InputError(f"source {source!r} is reserved; choose a sensor name")
    if mode not in {"upsert", "snapshot"}:
        raise InputError("mode must be upsert or snapshot")
    if allow_removal and mode != "snapshot":
        raise InputError("--allow-removal applies only to --mode snapshot")
    if schema_name:
        if not models.NAME_RE.fullmatch(schema_name):
            raise InputError("schema name must use lowercase letters, digits, or '-'")
        if schema_name in models.RESERVED_SOURCES:
            raise InputError(f"schema name {schema_name!r} is reserved")


def ingest(
    store: Store,
    source: str,
    data: bytes,
    *,
    mode: Mode = "upsert",
    schema_name: str = "",
    allow_removal: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate against native core schema + optional `schemas/<schema>.json`, write canonical JSONL, best-effort sync FTS5, and emit `records.ingested`."""
    from bf import config

    validate_ingest_args(source, mode=mode, schema_name=schema_name, allow_removal=allow_removal)
    cfg = config.load(store)
    if source in cfg.reserved_sources:
        raise InputError(f"source {source!r} is reserved; choose a sensor name")

    eff_schema = schema_name or source
    user_validator = schemas.load_validator(store, f"schemas/{eff_schema}.json")
    if schema_name and user_validator is None:
        raise Error(f"schemas/{schema_name}.json: explicitly requested schema not found")

    raw_items, incomplete = parse_batch_envelope(data)
    prepared: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    stamp = now_utc(now)
    for idx, item in enumerate(raw_items):
        rec = _prepare_record(item, source, idx, cfg.name, cfg.notes)
        if (rec_id := str(rec["id"])) in seen_ids:
            raise Error(f"{source}: duplicate record id {rec_id!r} in input batch")
        seen_ids.add(rec_id)
        candidate = {**rec, "ingested_at": stamp}
        if core_errs := schemas.validate_core_record(candidate, f"{source}[{idx}]"):
            raise Error("; ".join(core_errs))
        if user_validator is not None and (
            user_errs := schemas.validate_instance(user_validator, candidate, f"{source}[{idx}]")
        ):
            raise Error("; ".join(user_errs))
        prepared.append(rec)

    target_file = memory_file(source)
    emitted_event_data: dict[str, Any] | None = None
    with nullcontext() if dry_run else writer(store, name=index.CORPUS_LOCK):
        forgotten = forgotten_ids(store, source)
        pre_stat: tuple[int, int, int] | None = None
        existing: dict[str, tuple[bytes, dict[str, Any] | None]] = {}
        with suppress(FileNotFoundError):
            st_pre = store.stat(target_file)
            pre_stat = (st_pre.st_mtime_ns, st_pre.st_size, st_pre.st_ino)
            with closing(iter_jsonl(store, target_file, check_duplicates=False)) as existing_rows:
                for raw_ln, parsed in existing_rows:
                    if isinstance(eid := parsed.get("id"), str) and eid:
                        existing[eid] = (raw_ln, parsed if eid in seen_ids else None)

        next_lines: dict[str, bytes] = {}
        changed_by_ref: dict[str, dict[str, Any]] = {}
        added_refs: list[str] = []
        updated_refs: list[str] = []
        added = updated_count = unchanged = skipped_forgotten = 0
        for rec in prepared:
            rec_id = str(rec["id"])
            if rec_id in forgotten:
                skipped_forgotten += 1
                continue
            stamped = {**rec, "ingested_at": stamp}
            ref = f"{source}:{rec_id}"
            if rec_id not in existing:
                next_lines[rec_id] = encode(stamped).rstrip(b"\n")
                changed_by_ref[ref] = stamped
                added_refs.append(ref)
                added += 1
                continue
            old_raw, old_rec = existing[rec_id]
            cmp_old = {k: v for k, v in (old_rec or {}).items() if k != "ingested_at"}
            if old_rec is not None and encode(cmp_old) == encode(rec):
                next_lines[rec_id] = old_raw
                unchanged += 1
            else:
                next_lines[rec_id] = encode(stamped).rstrip(b"\n")
                changed_by_ref[ref] = stamped
                updated_refs.append(ref)
                updated_count += 1

        snap_complete = mode == "snapshot" and not incomplete
        if not snap_complete:
            for eid, (old_raw, _) in existing.items():
                if eid not in forgotten and eid not in next_lines:
                    next_lines[eid] = old_raw
        removed_ids = [eid for eid in existing if eid not in next_lines]
        removed = len(removed_ids)
        existing_n, surviving = len(existing), len(next_lines)
        too_many = surviving == 0 or (existing_n >= 10 and removed * 2 > existing_n)
        if snap_complete and existing and not allow_removal and too_many:
            raise Error(f"{source}: snapshot would remove {removed} of {existing_n} records; pass --allow-removal")

        index_warning = ""
        if not dry_run and (added or updated_count or removed):
            row_offsets: dict[str, tuple[int, int]] = {}
            if surviving > 0:
                chunks: list[bytes] = []
                pos = 0
                for key in sorted(next_lines):
                    line_b = next_lines[key]
                    line_len = len(line_b) + 1
                    row_offsets[f"{source}:{key}"] = (pos, line_len)
                    chunks.append(line_b)
                    chunks.append(b"\n")
                    pos += line_len
                if pos > models.MAX_FILE:
                    raise Error(f"{target_file}: file exceeds {models.MAX_FILE} bytes")
                store.write(target_file, b"".join(chunks))
            else:
                with suppress(FileNotFoundError):
                    store.delete(target_file)
            # Best-effort disposable cache sync: canonical JSONL write & event emission never fail on SQLite cache errors.
            changed_rows = [
                (row_offsets[ref][0], row_offsets[ref][1], rec_obj)
                for ref, rec_obj in changed_by_ref.items()
                if ref in row_offsets
            ]
            try:
                index.sync_source_delta(
                    store,
                    cfg.name,
                    source,
                    target_file,
                    pre_stat=pre_stat,
                    changed_rows=changed_rows,
                    row_offsets=row_offsets,
                    removed_refs=[f"{source}:{eid}" for eid in removed_ids],
                    total_count=surviving,
                    reserved=cfg.reserved_sources,
                    locked=True,
                )
            except (Error, OSError) as err:
                index_warning = redact_secrets(printable(failure(err)))
            added_out, added_trunc = _bound_refs(sorted(added_refs))
            updated_out, updated_trunc = _bound_refs(sorted(updated_refs))
            emitted_event_data = {
                "records": surviving,
                "added": added,
                "updated": updated_count,
                "removed": removed,
                "incomplete": incomplete,
                **({"added_refs": added_out} if added_out else {}),
                **({"updated_refs": updated_out} if updated_out else {}),
                **({"refs_truncated": True} if (added_trunc or updated_trunc) else {}),
            }

    if emitted_event_data is not None:
        state.append_event(store, "records.ingested", source, emitted_event_data, now=now)

    out_doc: dict[str, Any] = {
        "ok": True,
        "brain": cfg.name,
        "source": source,
        "mode": mode,
        "records": surviving,
        "added": added,
        "updated": updated_count,
        "unchanged": unchanged,
        "removed": removed,
        "forgotten": skipped_forgotten,
        "incomplete": incomplete,
        "dry_run": dry_run,
    }
    if not dry_run and index_warning:
        out_doc["index_warning"] = index_warning
    return out_doc
