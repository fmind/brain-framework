"""Pure JSON record storage (`memories/<source>/<sha256(id)>.json`), ingestion, and event logging."""

from __future__ import annotations

import errno
import os
import stat
from datetime import datetime
from typing import Any, Literal

from bf import config, models
from bf.markdown import add_link, normalize_tags, parse_link, record_link_problem
from bf.models import Error, InputError, clean, decode, digest, encode, now_utc
from bf.schemas import RECORD_SCHEMA, load_validator, validate_instance
from bf.storage import Store, private_dir, writer

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
    state = private_dir(store)
    try:
        with state.parent("events.jsonl") as (parent, leaf):
            try:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise Error(".bf/events.jsonl: symlinks are not followed") from error
                raise
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG((info := os.fstat(stream.fileno())).st_mode):
                    raise Error(".bf/events.jsonl: expected a regular file")
                if info.st_size == 0:
                    return 0
                for span in (65536, 16 << 20):
                    off = max(0, info.st_size - span)
                    stream.seek(off)
                    tail = stream.read(span)
                    if off:
                        tail = tail.split(b"\n", 1)[1] if b"\n" in tail else b""
                    for ln in reversed(tail.splitlines()):
                        if stripped := ln.strip():
                            parsed = decode(stripped, ".bf/events.jsonl", check_duplicates=False)
                            return s if isinstance(parsed, dict) and type(s := parsed.get("seq")) is int else 0
                    if off == 0:
                        break
    except FileNotFoundError:
        return 0
    return 0


def read_events(store: Store, *, after_seq: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read `.bf/events.jsonl` and return `(events_after_seq, latest_seq)` with cursor reset self-healing."""
    try:
        raw = private_dir(store).read("events.jsonl", 16 << 20)
    except FileNotFoundError:
        return [], 0
    all_evs: list[dict[str, Any]] = []
    latest = 0
    for idx, ln in enumerate(raw.splitlines(), 1):
        if stripped := ln.strip():
            parsed = decode(stripped, f".bf/events.jsonl:{idx}", check_duplicates=False)
            if isinstance(parsed, dict) and type(s := parsed.get("seq")) is int:
                all_evs.append(dict(parsed))
                latest = max(latest, s)
    floor = 0 if (latest and after_seq > latest) else after_seq
    return [ev for ev in all_evs if int(ev["seq"]) > floor], latest


def append_event(
    store: Store, kind: str, source: str, data: dict[str, Any] | None = None, **opts: Any
) -> dict[str, Any]:
    """Append one event line to `.bf/events.jsonl` and `logs/events.log` (`structlog` JSON)."""
    dedupe, locked, now = bool(opts.get("dedupe")), bool(opts.get("locked")), opts.get("now")
    clean(kind, "event kind")
    clean(source, "event source")

    def write_event() -> dict[str, Any]:
        state = private_dir(store)
        msg_id = (data or {}).get("id")
        if dedupe and isinstance(msg_id, str) and msg_id:
            items, prev_seq = read_events(store)
            for prev in reversed(items):
                same_src = prev.get("kind") == kind and prev.get("source") == source
                if same_src and isinstance(pd := prev.get("data"), dict) and pd.get("id") == msg_id:
                    return prev
        else:
            prev_seq = latest_event_seq(store)
        seq = prev_seq + 1
        event = {"seq": seq, "id": f"evt-{seq}-{os.urandom(4).hex()}", "time": now_utc(now)}
        event.update({"kind": kind, "source": source, "data": dict(data or {})})
        line = models.log_line(
            kind, time_iso=str(event["time"]), seq=seq, id=event["id"], source=source, data=event["data"]
        )
        store.append_log("events", line)
        raw_line = encode(event)
        with state.parent("events.jsonl", create=True) as (parent, leaf):
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                fd = os.open(leaf, flags, 0o600, dir_fd=parent)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise Error(".bf/events.jsonl: symlinks are not followed") from error
                raise
            with os.fdopen(fd, "wb") as stream:
                if not stat.S_ISREG((info := os.fstat(stream.fileno())).st_mode):
                    raise Error(".bf/events.jsonl: expected a regular file")
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw_line)
                stream.flush()
                size_after = info.st_size + len(raw_line)
        if size_after > COMPACT_BYTES or (seq > MAX_EVENTS and seq % 200 == 0):
            all_lines = state.read("events.jsonl", 32 << 20).splitlines(keepends=True)
            if len(all_lines) > MAX_EVENTS or size_after > COMPACT_BYTES:
                kept: list[bytes] = []
                budget = COMPACT_BYTES >> 1
                for item in reversed(all_lines[-MAX_EVENTS:]):
                    if len(item) > budget:
                        break
                    kept.append(item)
                    budget -= len(item)
                kept.reverse()
                state.write("events.jsonl", b"".join(kept), mode=0o600, durable=False)
        return event

    if locked:
        return write_event()
    with writer(store):
        return write_event()


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
        parsed = decode(text, label)
        if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
            raise Error(f"{label}: expected a JSON array of record objects")
        return [dict(item) for item in parsed], False
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
            recs = whole["records"]
            if not isinstance(recs, list) or not all(isinstance(item, dict) for item in recs):
                raise Error(f"{label}.records: expected a JSON array of record objects")
            inc = whole.get("incomplete")
            if inc is None or inc is False:
                missed_flag = False
            elif inc is True:
                missed_flag = True
            elif isinstance(inc, dict) and type(missed := inc.get("missed", 0)) is int and missed >= 0:
                missed_flag = missed > 0
            else:
                raise Error(f"{label}.incomplete: expected a boolean or {{'missed': non-negative integer}}")
            return [dict(item) for item in recs], missed_flag
    items: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if stripped := line.strip():
            if not isinstance(parsed := decode(stripped, f"{label} line {number}"), dict):
                raise Error(f"{label} line {number}: expected a JSON object")
            items.append(dict(parsed))
    return items, False


def _normalize_record_links(raw_links: object, label: str, brain_name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    roots = ("projects/", "concepts/", "actions/", "memories/")

    def plink(tgt: str, *, section: str = "", attr_meta: dict[str, str] | None = None, req_uri: bool = True) -> None:
        kw = {"section": section, "attr_meta": attr_meta, "relative_base": False, "require_uri": req_uri}
        parsed = parse_link(label, tgt, brain_name, **kw)
        if prob := record_link_problem(parsed):
            raise Error(f"{label}: {prob}")
        add_link(seen, out, parsed)

    if raw_links is not None:
        if not isinstance(raw_links, list):
            raise Error(f"{label}.links must be a list")
        for item in raw_links:
            if isinstance(item, str):
                if not item.strip():
                    raise Error(f"{label}.links entries must be nonempty link targets")
                plink(item, req_uri=True)
            elif isinstance(item, dict) and isinstance(item.get("to"), str):
                raw_meta = {} if item.get("meta") is None else item["meta"]
                if not isinstance(raw_meta, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in raw_meta.items()
                ):
                    raise Error(f"{label}.links: link meta must map strings to strings")
                target, frag, sec = str(item["to"]), item.get("anchor"), item.get("section")
                if frag is not None and not isinstance(frag, str):
                    raise Error(f"{label}.links: anchor must be a string")
                if isinstance(frag, str) and frag:
                    if "#" in target and target.partition("#")[2] != frag:
                        raise Error(f"{label}.links: conflicting #anchor on target")
                    target = target if "#" in target else f"{target}#{frag}"
                if sec is not None and not isinstance(sec, str):
                    raise Error(f"{label}.links: section must be a string")
                plink(target, section=sec or "", attr_meta=raw_meta, req_uri=not target.startswith(roots))
            else:
                raise Error(f"{label}.links entries must be target strings or link objects")

    typed = {(it["to"], it["kind"], it["anchor"], it["section"]) for it in out if it["meta"]}
    return [it for it in out if it["meta"] or (it["to"], it["kind"], it["anchor"], it["section"]) not in typed]


def _prepare_record(raw: dict[str, Any], source: str, index: int, brain_name: str) -> tuple[dict[str, Any], bool]:
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
    rec["id"], rec["title"] = rec_id, clean(raw_title, f"{label}.title")
    expected_ref = f"{source}:{rec_id}"
    if "source" in rec and rec["source"] != source:
        raise Error(f"{label}.source must match {source!r}")
    if "ref" in rec and rec["ref"] != expected_ref:
        raise Error(f"{label}.ref must match {expected_ref!r}")
    if "kind" in rec and rec["kind"] != "memory":
        raise Error(f"{label}.kind must equal 'memory'")
    rec["source"], rec["ref"], rec["kind"] = source, expected_ref, "memory"

    if "text" in rec and rec["text"] is not None:
        if not isinstance(rec["text"], str):
            raise Error(f"{label}.text must be a string")
        if len(rec["text"]) > 4 << 20:
            raise Error(f"{label}.text exceeds 4 MiB")
    else:
        rec["text"] = ""

    rec["tags"] = normalize_tags(rec["tags"], label) if "tags" in rec and rec["tags"] is not None else []
    rec["links"] = _normalize_record_links(rec.get("links"), label, brain_name)

    if "url" in rec:
        if not isinstance(rec["url"], str):
            raise Error(f"{label}.url must be a string")
        if rec["url"]:
            clean(rec["url"], f"{label}.url")
        else:
            del rec["url"]
    for tfield in ("time", "updated"):
        if tfield in rec:
            if not isinstance(val := rec[tfield], str):
                raise Error(f"{label}.{tfield} must be an ISO 8601 string")
            if not val:
                del rec[tfield]
            elif tfield == "updated" and len(val) == 10:
                rec[tfield] = models.normalize_date(val, f"{label}.{tfield}")
            else:
                rec[tfield] = models.normalize_time(val, f"{label}.{tfield}")
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
    if not models.NAME_RE.fullmatch(source):
        raise InputError("source must start with a lowercase letter and use only lowercase letters, digits, or '-'")
    if source in models.RESERVED_SOURCES:
        raise InputError(f"source {source!r} is reserved; choose a sensor name")
    if mode not in {"upsert", "snapshot"}:
        raise InputError("mode must be upsert or snapshot")
    cfg = config.load(store)
    if (base_val := load_validator(store, "schemas/record.json", RECORD_SCHEMA)) is None:
        raise Error("schemas/record.json is unavailable")
    src_val = load_validator(store, f"schemas/{source}.json")

    raw_items, incomplete = parse_batch_envelope(data)
    prepared: list[tuple[dict[str, Any], bool]] = []
    seen_ids: set[str] = set()
    stamp = now_utc(now)
    for idx, item in enumerate(raw_items):
        rec, explicit_updated = _prepare_record(item, source, idx, cfg.name)
        if (rec_id := str(rec["id"])) in seen_ids:
            raise Error(f"{source}: duplicate record id {rec_id!r} in input batch")
        seen_ids.add(rec_id)
        cand = rec if explicit_updated else {**rec, "updated": stamp}
        if errs := validate_instance(base_val, cand, f"{source}[{idx}]"):
            raise Error("; ".join(errs))
        if src_val is not None and (errs := validate_instance(src_val, cand, f"{source}[{idx}]")):
            raise Error("; ".join(errs))
        prepared.append((rec, explicit_updated))

    def apply() -> dict[str, Any]:
        forgotten = _forgotten_ids(store, source)
        existing_paths = {p for p in store.files(f"memories/{source}") if not p.endswith("/.forgotten")}

        to_write: dict[str, dict[str, Any]] = {}
        kept_paths: set[str] = set()
        added = updated_count = unchanged = skipped_forgotten = 0
        for rec, explicit_updated in prepared:
            if (rec_id := str(rec["id"])) in forgotten:
                skipped_forgotten += 1
                continue
            target_path = record_file(source, rec_id)
            kept_paths.add(target_path)
            stamped = rec if explicit_updated else {**rec, "updated": stamp}
            if target_path not in existing_paths:
                to_write[target_path] = stamped
                added += 1
                continue
            old_raw = decode(store.read(target_path), target_path, check_duplicates=False)
            old_rec = dict(old_raw) if isinstance(old_raw, dict) else {}
            cmp_rec = old_rec if explicit_updated else {k: v for k, v in old_rec.items() if k != "updated"}
            if cmp_rec == rec:
                unchanged += 1
            else:
                to_write[target_path] = stamped
                updated_count += 1

        snap_complete = mode == "snapshot" and not incomplete
        if snap_complete:
            to_delete = [p for p in existing_paths if p not in kept_paths]
        else:
            to_delete = [p for fid in forgotten if (p := record_file(source, fid)) in existing_paths]
        removed = len(to_delete)
        existing_n = len(existing_paths)
        surviving = len(kept_paths) if snap_complete else existing_n + added - len(to_delete)
        too_many = surviving == 0 or (existing_n >= 10 and removed * 2 > existing_n)
        if snap_complete and existing_paths and not allow_removal and too_many:
            raise Error(f"{source}: snapshot would remove {removed} of {existing_n} records; pass --allow-removal")
        if not dry_run:
            for path, payload in sorted(to_write.items()):
                store.write(path, encode(payload))
            for path in sorted(to_delete):
                if store.exists(path):
                    store.delete(path)
            if added or updated_count or removed:
                ev_payload = {"records": surviving, "added": added, "updated": updated_count, "removed": removed}
                append_event(store, "records.ingested", source, ev_payload, locked=True, now=now)
        c1 = {"records": surviving, "added": added, "updated": updated_count}
        c2 = {"unchanged": unchanged, "removed": removed, "forgotten": skipped_forgotten}
        head = {"brain": cfg.name, "source": source, "mode": mode}
        return {**head, **c1, **c2, "incomplete": incomplete, "dry_run": dry_run}

    if dry_run:
        return apply()
    with writer(store):
        return apply()
