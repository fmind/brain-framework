"""Pure JSON record storage (`memories/<source>/<sha256(id)>.json`), ingestion, and event logging."""

from __future__ import annotations

import os
import stat
from contextlib import nullcontext, suppress
from datetime import datetime
from typing import Any, Literal

from bf import models
from bf.models import Error, InputError, clean, decode, digest, encode, now_utc
from bf.storage import Store, open_regular, private_dir, tail_lines, writer

Mode = Literal["upsert", "snapshot"]
MAX_EVENTS, COMPACT_BYTES = 1000, 8 << 20


def record_file(source: str, rec_id: str) -> str:
    """Return the deterministic per-record JSON path `memories/<source>/<sha256(id)>.json`."""
    return f"memories/{source}/{digest(rec_id.encode('utf-8'))}.json"


def _forgotten_ids(store: Store, source: str) -> set[str]:
    try:
        raw = store.read(f"memories/{source}/.forgotten", 4 << 20)
    except FileNotFoundError:
        return set()
    return {ln.strip() for ln in raw.decode("utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")}


def latest_event_seq(store: Store) -> int:
    """Return the highest `seq` recorded in `.bf/events.jsonl` (`0` when absent or empty)."""
    with suppress(FileNotFoundError):
        state = private_dir(store, create=False)
        for span in (65536, 16 << 20):
            tail, off = state.read_tail("events.jsonl", span, label=".bf/events.jsonl")
            for ln in reversed(tail.splitlines()):
                if stripped := ln.strip():
                    parsed = decode(stripped, ".bf/events.jsonl", check_duplicates=False)
                    return s if isinstance(parsed, dict) and type(s := parsed.get("seq")) is int else 0
            if off == 0:
                break
    return 0


def read_events(store: Store, *, after_seq: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read `.bf/events.jsonl` and return `(events_after_seq, latest_seq)` with cursor reset self-healing."""
    all_evs, latest = [], 0
    with suppress(FileNotFoundError):
        raw = private_dir(store, create=False).read("events.jsonl", 16 << 20)
        for idx, ln in enumerate(raw.splitlines(), 1):
            if stripped := ln.strip():
                parsed = decode(stripped, f".bf/events.jsonl:{idx}", check_duplicates=False)
                if isinstance(parsed, dict) and type(s := parsed.get("seq")) is int:
                    all_evs.append(dict(parsed))
                    latest = max(latest, s)
    floor = 0 if (latest and after_seq > latest) else after_seq
    return [ev for ev in all_evs if int(ev["seq"]) > floor], latest


def append_event(
    store: Store,
    kind: str,
    source: str,
    data: dict[str, Any] | None = None,
    *,
    dedupe: bool = False,
    locked: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one event line to `.bf/events.jsonl` and `logs/events.log` (JSON lines)."""
    clean(kind, "event kind")
    clean(source, "event source")
    if not models.EVENT_KIND_RE.fullmatch(kind):
        raise Error("event kind must start with a lowercase letter and use lowercase letters, digits, '.', '_', or '-'")
    if not models.NAME_RE.fullmatch(source):
        raise Error("event source must start with a lowercase letter and use lowercase letters, digits, or '-'")
    safe_data = models.redact_value(dict(data or {}))
    with nullcontext() if locked else writer(store):
        state, msg_id = private_dir(store), (safe_data.get("id") if isinstance(safe_data, dict) else None)
        prev_seq = latest_event_seq(store)
        if dedupe and isinstance(msg_id, str) and msg_id:
            with suppress(FileNotFoundError):
                needle = encode(msg_id).rstrip(b"\n")
                for raw_ln in reversed(state.read("events.jsonl", 16 << 20).splitlines()):
                    if (
                        needle in raw_ln
                        and isinstance(p := decode(raw_ln, ".bf/events.jsonl", check_duplicates=False), dict)
                        and p.get("kind") == kind
                        and p.get("source") == source
                        and isinstance(pd := p.get("data"), dict)
                        and pd.get("id") == msg_id
                    ):
                        return dict(p)
        seq, ts = prev_seq + 1, now_utc(now)
        ev = {
            "seq": seq,
            "id": f"evt-{seq}-{os.urandom(4).hex()}",
            "time": ts,
            "kind": kind,
            "source": source,
            "data": safe_data,
        }
        store.append_log(
            "events", models.log_line(kind, time_iso=ts, seq=seq, id=ev["id"], source=source, data=safe_data)
        )
        raw_line = encode(ev)
        with state.parent("events.jsonl", create=True) as (parent, leaf):
            fd = open_regular(parent, leaf, ".bf/events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "wb", buffering=0) as stream:
                if not stat.S_ISREG((info := os.fstat(stream.fileno())).st_mode):
                    raise Error(".bf/events.jsonl: expected a regular file")
                os.fchmod(stream.fileno(), 0o600)
                view = memoryview(raw_line)
                while view:
                    view = view[stream.write(view) :]
                size_after = info.st_size + len(raw_line)
        if size_after > COMPACT_BYTES or (seq > MAX_EVENTS and seq % 200 == 0):
            all_lines = state.read("events.jsonl", 32 << 20).splitlines(keepends=True)
            if len(all_lines) > MAX_EVENTS or size_after > COMPACT_BYTES:
                kept = tail_lines(all_lines, MAX_EVENTS, COMPACT_BYTES >> 1)
                state.write("events.jsonl", b"".join(kept), mode=0o600, durable=False)
        return ev


def parse_batch_envelope(data: bytes, label: str = "input") -> tuple[list[dict[str, Any]], bool]:
    """Parse a JSON array, `{"records": [...]}` envelope, single object, or JSON Lines into `(items, incomplete)`."""
    if len(data) > models.MAX_BATCH:
        raise Error(f"{label} exceeds {models.MAX_BATCH} bytes; split the batch")
    try:
        text = data.decode("utf-8").removeprefix("\ufeff").strip()
    except UnicodeDecodeError as error:
        raise Error(f"{label} is not valid UTF-8") from error
    if not text:
        return [], False
    if text.startswith("["):
        if not isinstance(p := decode(text, label), list) or not all(isinstance(x, dict) for x in p):
            raise Error(f"{label}: expected a JSON array of record objects")
        return [dict(x) for x in p], False
    if text.startswith("{"):
        try:
            whole = decode(text, label)
        except Error as err:
            if "duplicate key" in str(err) or "must be finite" in str(err):
                raise
            whole = None
        if isinstance(whole, dict):
            if "records" not in whole:
                return [dict(whole)], False
            if not isinstance(recs := whole["records"], list) or not all(isinstance(x, dict) for x in recs):
                raise Error(f"{label}.records: expected a JSON array of record objects")
            inc = whole.get("incomplete")
            if isinstance(inc, dict) and set(inc) == {"missed"} and type(m := inc["missed"]) is int and m >= 0:
                return [dict(x) for x in recs], m > 0
            if inc is None or isinstance(inc, bool):
                return [dict(x) for x in recs], bool(inc)
            raise Error(f"{label}.incomplete: expected a boolean or {{'missed': non-negative integer}}")
    items: list[dict[str, Any]] = []
    for num, ln in enumerate(text.splitlines(), 1):
        if stripped := ln.strip():
            if not isinstance(p := decode(stripped, f"{label} line {num}"), dict):
                raise Error(f"{label} line {num}: expected a JSON object")
            items.append(dict(p))
    return items, False


def _dedupe_typed_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    typed = {(it["to"], it["kind"], it.get("anchor", ""), it.get("section", "")) for it in links if it.get("meta")}
    return [
        it
        for it in links
        if it.get("meta") or (it["to"], it["kind"], it.get("anchor", ""), it.get("section", "")) not in typed
    ]


def _normalize_record_links(raw_links: object, label: str, brain_name: str) -> list[dict[str, Any]]:
    from bf.markdown import add_link, parse_link, record_link_problem

    if raw_links is None:
        return []
    if not isinstance(raw_links, list):
        raise Error(f"{label}.links must be a list")
    out, seen, roots = [], set(), ("projects/", "concepts/", "actions/", "memories/")

    def plink(tgt: str, sec: str = "", meta: dict[str, str] | None = None, req: bool = True, kind: str = "") -> None:
        parsed = parse_link(label, tgt, brain_name, section=sec, attr_meta=meta, relative_base=False, require_uri=req)
        if kind and parsed["kind"] != kind:
            raise Error(f"{label}.links: link kind {kind!r} does not match target")
        if prob := record_link_problem(parsed):
            raise Error(f"{label}: {prob}")
        add_link(seen, out, parsed)

    for item in raw_links:
        if isinstance(item, str):
            if not item.strip():
                raise Error(f"{label}.links entries must be nonempty link targets")
            plink(item, req=not item.startswith(roots))
        elif isinstance(item, dict) and isinstance(item.get("to"), str):
            if extra := sorted(set(item) - {"to", "kind", "anchor", "meta", "section"}):
                raise Error(f"{label}.links: unknown link keys {', '.join(extra)}; put metadata under 'meta'")
            for fk in ("kind", "anchor", "section"):
                if item.get(fk) is not None and not isinstance(item[fk], str):
                    raise Error(f"{label}.links: {fk} must be a string")
            meta = {} if item.get("meta") is None else item["meta"]
            if not isinstance(meta, dict) or not all(isinstance(x, str) for kv in meta.items() for x in kv):
                raise Error(f"{label}.links: link meta must map strings to strings")
            tgt, frag = str(item["to"]), str(item.get("anchor") or "")
            if frag and "#" in tgt and tgt.partition("#")[2] != frag:
                raise Error(f"{label}.links: conflicting #anchor on target")
            tgt = f"{tgt}#{frag}" if frag and "#" not in tgt else tgt
            plink(tgt, str(item.get("section") or ""), meta, not tgt.startswith(roots), str(item.get("kind") or ""))
        else:
            raise Error(f"{label}.links entries must be target strings or link objects")
    return _dedupe_typed_links(out)


def _prepare_record(raw: dict[str, Any], source: str, index: int, brain_name: str) -> tuple[dict[str, Any], bool]:
    from bf.markdown import normalize_tags

    label, rec = f"{source}[{index}]", dict(raw)
    if "relations" in rec:
        raise Error(f"{label}: 'relations' was removed; use 'links'")
    if not isinstance(raw_id := rec.get("id"), str):
        raise Error(f"{label}: missing required string 'id'")
    if not isinstance(raw_title := rec.get("title"), str):
        raise Error(f"{label}: missing required string 'title'")
    rec_id = clean(raw_id, f"{label}.id")
    if any(ch.isspace() for ch in rec_id) or models.invisible(rec_id):
        raise Error(f"{label}.id must contain no whitespace or invisible format characters")
    rec["id"], rec["title"], exp_ref = rec_id, clean(raw_title, f"{label}.title"), f"{source}:{rec_id}"
    for k, exp in (("source", source), ("ref", exp_ref), ("kind", "memory")):
        if k in rec and rec[k] != exp:
            raise Error(f"{label}.{k} must {'equal' if k == 'kind' else 'match'} {exp!r}")
    rec["source"], rec["ref"], rec["kind"] = source, exp_ref, "memory"
    if (txt := rec.get("text")) is None:
        rec["text"] = ""
    elif not isinstance(txt, str) or len(txt) > 4 << 20:
        raise Error(f"{label}.text {'exceeds 4 MiB' if isinstance(txt, str) else 'must be a string'}")
    rec["tags"] = normalize_tags(rec["tags"], label) if rec.get("tags") is not None else []
    rec["links"] = _normalize_record_links(rec.get("links"), label, brain_name)
    if "url" in rec:
        if not isinstance(rec["url"], str):
            raise Error(f"{label}.url must be a string")
        clean(rec["url"], f"{label}.url") if rec["url"] else rec.pop("url")
    for tf in ("time", "updated"):
        if tf in rec:
            if not isinstance(val := rec[tf], str):
                raise Error(f"{label}.{tf} must be an ISO 8601 string")
            if not val:
                del rec[tf]
            else:
                fn = models.normalize_date if tf == "updated" and len(val) == 10 else models.normalize_time
                rec[tf] = fn(val, f"{label}.{tf}")
    if isinstance(sa := rec.get("stale_after"), str) and models.DURATION_RE.fullmatch(sa):
        models.parse_duration_seconds(sa, f"{label}.stale_after")
    encode(rec)
    return rec, "updated" in rec


def ingest(
    store: Store,
    source: str,
    data: bytes,
    *,
    mode: Mode = "upsert",
    allow_removal: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and atomically save JSON records into `memories/<source>/<sha256(id)>.json`."""
    from bf import config
    from bf.schemas import RECORD_SCHEMA, load_validator, validate_instance

    if not models.NAME_RE.fullmatch(source):
        raise InputError("source must start with a lowercase letter and use only lowercase letters, digits, or '-'")
    if source in models.RESERVED_SOURCES:
        raise InputError(f"source {source!r} is reserved; choose a sensor name")
    if mode not in {"upsert", "snapshot"}:
        raise InputError("mode must be upsert or snapshot")
    cfg = config.load(store)
    if (base_val := load_validator(store, "schemas/record.json", RECORD_SCHEMA)) is None:
        raise Error("schemas/record.json is unavailable")
    vals = [base_val, *([sv] if (sv := load_validator(store, f"schemas/{source}.json")) is not None else [])]
    raw_items, incomplete = parse_batch_envelope(data)
    prepared, seen_ids, stamp = [], set(), now_utc(now)
    for idx, item in enumerate(raw_items):
        rec, explicit_updated = _prepare_record(item, source, idx, cfg.name)
        if (rec_id := str(rec["id"])) in seen_ids:
            raise Error(f"{source}: duplicate record id {rec_id!r} in input batch")
        seen_ids.add(rec_id)
        cand = rec if explicit_updated else {**rec, "updated": stamp}
        for val in vals:
            if errs := validate_instance(val, cand, f"{source}[{idx}]"):
                raise Error("; ".join(errs))
        prepared.append((rec, explicit_updated))

    with nullcontext() if dry_run else writer(store):
        forgotten = _forgotten_ids(store, source)
        existing_paths = {p for p in store.files(f"memories/{source}") if not p.endswith("/.forgotten")}
        to_write, kept_paths = {}, set()
        added = updated_count = unchanged = skipped_forgotten = 0
        for rec, explicit_updated in prepared:
            if (rec_id := str(rec["id"])) in forgotten:
                skipped_forgotten += 1
                continue
            target_path = record_file(source, rec_id)
            kept_paths.add(target_path)
            stamped = rec if explicit_updated else {**rec, "updated": stamp}
            if target_path not in existing_paths:
                to_write[target_path], added = stamped, added + 1
                continue
            old_raw = decode(store.read(target_path), target_path, check_duplicates=False)
            old_rec = dict(old_raw) if isinstance(old_raw, dict) else {}
            cmp_rec = old_rec if explicit_updated else {k: v for k, v in old_rec.items() if k != "updated"}
            if cmp_rec == rec:
                unchanged += 1
            else:
                to_write[target_path], updated_count = stamped, updated_count + 1
        snap_complete = mode == "snapshot" and not incomplete
        if snap_complete:
            to_delete = [p for p in existing_paths if p not in kept_paths]
        else:
            to_delete = [p for fid in forgotten if (p := record_file(source, fid)) in existing_paths]
        removed, existing_n = len(to_delete), len(existing_paths)
        surviving = len(kept_paths) if snap_complete else existing_n + added - removed
        too_many = surviving == 0 or (existing_n >= 10 and removed * 2 > existing_n)
        if snap_complete and existing_paths and not allow_removal and too_many:
            raise Error(f"{source}: snapshot would remove {removed} of {existing_n} records; pass --allow-removal")
        if not dry_run:
            for path, payload in sorted(to_write.items()):
                store.write(path, encode(payload), durable=False)
            for path in sorted(to_delete):
                with suppress(FileNotFoundError):
                    store.delete(path, durable=False)
            if to_write or to_delete:
                store.sync_dirs({p.rsplit("/", 1)[0] for p in (*to_write, *to_delete)})
            if added or updated_count or removed:
                ev_data = {"records": surviving, "added": added, "updated": updated_count, "removed": removed}
                append_event(store, "records.ingested", source, ev_data, locked=True, now=now)
        return {
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
