"""Unified JSONL record storage (`memories/` and `usages/`), ingestion, structured loop logs, and `logs/events.jsonl`."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Generator, Iterable
from contextlib import nullcontext, suppress
from datetime import datetime
from typing import Any, Literal

from bf import models
from bf.models import DEFAULT_NOTES, RESERVED_SOURCES, Error, InputError, clean, decode, encode, now_utc
from bf.storage import Store, open_regular, private_dir, tail_lines, writer

Target = Literal["memories", "usages"]
Mode = Literal["upsert", "snapshot"]
MAX_EVENTS, COMPACT_BYTES = 1000, 8 << 20
MAX_EVENT_LINE = 1 << 20
_PEM_BEG_RE = re.compile(r"-----BEGIN [^-]{0,40}PRIVATE KEY-----")
_PEM_END_RE = re.compile(r"-----END [^-]{0,40}PRIVATE KEY-----")
_PEM_MAX_BYTES = 16384


def record_file(source: str, target: Target = "memories") -> str:
    """Return the JSONL path `<target>/<source>.jsonl` (`memories/` or `usages/`)."""
    return f"{target}/{source}.jsonl"


def memory_file(source: str) -> str:
    """Return the JSONL path `memories/<source>.jsonl` for a note or sensor source."""
    return record_file(source, "memories")


def valid_record_sources(
    store: Store,
    target: Target = "memories",
    note_dirs: Iterable[str] = DEFAULT_NOTES,
    *,
    skipped: list[str] | None = None,
) -> list[str]:
    """Return sorted source names for valid `<target>/<source>.jsonl` files."""
    notes_set = frozenset(note_dirs) if target == "memories" else frozenset()
    _, top_files = store.subdirs(target, skipped=skipped)
    prefix = f"{target}/"
    out: list[str] = []
    for rel in top_files:
        if not rel.endswith(".jsonl"):
            continue
        src = rel.removeprefix(prefix).removesuffix(".jsonl")
        if src in notes_set or (models.NAME_RE.fullmatch(src) and src not in RESERVED_SOURCES and src not in note_dirs):
            out.append(src)
    return sorted(out)


def iter_jsonl(store: Store, path: str, *, check_duplicates: bool = False) -> Generator[tuple[bytes, dict[str, Any]]]:
    """Stream `(raw_line_bytes, parsed_dict)` for each non-empty line in JSONL file `path`."""
    for idx, raw_line in enumerate(store.iter_lines(path, models.MAX_FILE), 1):
        if not (stripped := raw_line.strip()):
            continue
        parsed = decode(stripped, f"{path}:{idx}", check_duplicates=check_duplicates)
        if not isinstance(parsed, dict):
            raise Error(f"{path}:{idx}: expected a JSON object")
        yield stripped, parsed


def _forgotten_ids(store: Store, source: str, target: Target = "memories") -> set[str]:
    try:
        raw = store.read(f"{target}/{source}.forgotten", models.MAX_NOTE)
    except FileNotFoundError:
        return set()
    return {ln.strip() for ln in raw.decode("utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")}


def load_unit_state(store: Store, name: str) -> dict[str, Any]:
    """Load `.bf/<name>.json`, returning an initialized `idle` state when absent."""
    with suppress(FileNotFoundError):
        state = private_dir(store, create=False)
        if isinstance(parsed := decode(state.read(f"{name}.json", 1 << 20), f".bf/{name}.json"), dict):
            return dict(parsed)
    zeros = dict.fromkeys(("event_seq", "runs", "failures", "consecutive_failures", "duration_ms"), 0)
    empty = dict.fromkeys(("last_start", "last_end", "last_success", "last_scheduled", "error"), "")
    base = {"kind": "loop", "name": name, "status": "idle", "updated": now_utc(), "schedule_last": {}, "last_run": {}}
    return {**base, **empty, **zeros}


def save_unit_state(store: Store, doc: dict[str, Any], *, locked: bool = False) -> None:
    """Write `.bf/<name>.json` atomically with `0600` permissions."""
    state, path = private_dir(store), f"{doc['name']}.json"
    if locked:
        state.write(path, encode(doc), mode=0o600)
    else:
        with writer(store):
            state.write(path, encode(doc), mode=0o600)


def mark_unit_state(store: Store, name: str, **fields: Any) -> None:
    """Atomically read-modify-write fields on `.bf/<name>.json` under `writer(store)`."""
    with writer(store):
        cur = load_unit_state(store, name)
        save_unit_state(store, {**cur, "kind": "loop", "name": name, **fields}, locked=True)


def find_orphans(store: Store, known_units: set[str]) -> list[str]:
    """Return sorted orphaned `.bf/<loop>.json` stems not declared in `bf.yaml`."""
    with suppress(FileNotFoundError):
        private_dir(store, create=False)
        _, top_files = store.subdirs(".bf")
        return sorted(p[4:-5] for p in top_files if p.endswith(".json") and p[4:-5] not in known_units)
    return []


def make_stderr_logger(store: Store, name: str, run_id: str) -> Any:
    """Return a stateful line callback that redacts multi-line PEM blocks across streamed stderr lines."""
    in_pem, pem_bytes = False, 0

    def _on_line(raw_line: bytes) -> None:
        nonlocal in_pem, pem_bytes
        text = raw_line.decode("utf-8", "replace")
        begs = [m.start() for m in _PEM_BEG_RE.finditer(text)]
        ends = [m.end() for m in _PEM_END_RE.finditer(text)]
        last_beg, last_end = (begs[-1] if begs else -1), (ends[-1] if ends else -1)
        if in_pem:
            pem_bytes += len(raw_line)
            if last_end > last_beg:
                in_pem, pem_bytes = False, 0
                return
            if last_beg > last_end:
                pem_bytes = 0
                return
            if pem_bytes <= _PEM_MAX_BYTES:
                return
            in_pem, pem_bytes = False, 0
        if begs:
            if last_beg > last_end:
                in_pem, pem_bytes = True, 0
            append_loop_stderr(store, name, run_id, b"[REDACTED]")
            return
        append_loop_stderr(store, name, run_id, raw_line)

    return _on_line


def append_loop_stderr(store: Store, name: str, run_id: str, raw_line: bytes) -> None:
    """Parse one loop stderr line (structured JSONL/structlog or plain text) and append to `logs/<name>.jsonl`."""
    text = raw_line.decode("utf-8", "replace").strip()
    if not text:
        return
    if text.startswith("{"):
        with suppress(Error):
            parsed = decode(text, f"logs/{name}.jsonl", check_duplicates=False)
            if isinstance(parsed, dict):
                safe = models.redact_value(parsed)
                if isinstance(safe, dict):
                    ts = str(safe.pop("time", "") or safe.pop("timestamp", "") or safe.pop("time_iso", "") or "")
                    sev = str(safe.pop("severity", "") or safe.pop("level", "") or "INFO")
                    msg = str(safe.pop("message", "") or safe.pop("event", "") or "log")
                    safe["loop"], safe["run"] = name, run_id
                    line = models.log_line(msg[:4096], severity=sev, time_iso=ts, **safe)
                    with suppress(Error, OSError):
                        store.append_log(name, line)
                    return
    clean_msg = models.redact_secrets(text)[:4096]
    with suppress(Error, OSError):
        store.append_log(name, models.log_line(clean_msg, severity="WARN", loop=name, run=run_id))


def latest_event_seq(store: Store) -> int:
    """Return the highest `seq` recorded in `logs/events.jsonl` (`0` when absent or empty)."""
    with suppress(FileNotFoundError):
        logs_dir = private_dir(store, "logs", create=False)
        for span in (65536, 16 << 20):
            tail, off = logs_dir.read_tail("events.jsonl", span, label="logs/events.jsonl")
            for ln in reversed(tail.splitlines()):
                if stripped := ln.strip():
                    parsed = decode(stripped, "logs/events.jsonl", check_duplicates=False)
                    return seq if isinstance(parsed, dict) and type(seq := parsed.get("seq")) is int else 0
            if off == 0:
                break
    return 0


def read_events(store: Store, *, after_seq: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read `logs/events.jsonl` and return `(events_after_seq, latest_seq)` with cursor reset self-healing."""
    all_events: list[dict[str, Any]] = []
    latest = 0
    with suppress(FileNotFoundError):
        raw = private_dir(store, "logs", create=False).read("events.jsonl", 16 << 20)
        for idx, ln in enumerate(raw.splitlines(), 1):
            if stripped := ln.strip():
                parsed = decode(stripped, f"logs/events.jsonl:{idx}", check_duplicates=False)
                if isinstance(parsed, dict) and type(seq := parsed.get("seq")) is int:
                    all_events.append(dict(parsed))
                    latest = max(latest, seq)
    floor = 0 if (latest and after_seq > latest) else after_seq
    return [ev for ev in all_events if int(ev["seq"]) > floor], latest


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
    """Append one event line atomically to `logs/events.jsonl` (`0600` private, bounded compaction)."""
    clean(kind, "event kind")
    clean(source, "event source")
    if not models.EVENT_KIND_RE.fullmatch(kind):
        raise Error("event kind must start with a lowercase letter and use lowercase letters, digits, '.', '_', or '-'")
    if not models.NAME_RE.fullmatch(source):
        raise Error("event source must start with a lowercase letter and use lowercase letters, digits, or '-'")
    safe_data = models.redact_value(dict(data or {}))
    with nullcontext() if locked else writer(store):
        logs_dir = private_dir(store, "logs")
        msg_id = safe_data.get("id") if isinstance(safe_data, dict) else None
        prev_seq = latest_event_seq(store)
        if dedupe and isinstance(msg_id, str) and msg_id:
            with suppress(FileNotFoundError):
                needle = encode(msg_id).rstrip(b"\n")
                for raw_ln in reversed(logs_dir.read("events.jsonl", 16 << 20).splitlines()):
                    if (
                        needle in raw_ln
                        and isinstance(parsed := decode(raw_ln, "logs/events.jsonl", check_duplicates=False), dict)
                        and parsed.get("kind") == kind
                        and parsed.get("source") == source
                        and isinstance(pdata := parsed.get("data"), dict)
                        and pdata.get("id") == msg_id
                    ):
                        return dict(parsed)
        seq, ts = prev_seq + 1, now_utc(now)
        event = {
            "seq": seq,
            "id": f"evt-{seq}-{os.urandom(4).hex()}",
            "time": ts,
            "kind": kind,
            "source": source,
            "data": safe_data,
        }
        raw_line = encode(event)
        if len(raw_line) > MAX_EVENT_LINE:
            raise Error(f"logs/events.jsonl: event exceeds {MAX_EVENT_LINE} bytes")
        with logs_dir.parent("events.jsonl", create=True) as (parent, leaf):
            fd = open_regular(parent, leaf, "logs/events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "wb", buffering=0) as stream:
                if not stat.S_ISREG((info := os.fstat(stream.fileno())).st_mode):
                    raise Error("logs/events.jsonl: expected a regular file")
                os.fchmod(stream.fileno(), 0o600)
                view = memoryview(raw_line)
                try:
                    while view:
                        if not (written := stream.write(view)):
                            raise Error("logs/events.jsonl: short write")
                        view = view[written:]
                except BaseException:
                    with suppress(OSError):
                        os.ftruncate(stream.fileno(), info.st_size)
                    raise
                size_after = info.st_size + len(raw_line)
        if size_after > COMPACT_BYTES or (seq > MAX_EVENTS and seq % 200 == 0):
            all_lines = logs_dir.read("events.jsonl", 32 << 20).splitlines(keepends=True)
            if len(all_lines) > MAX_EVENTS or size_after > COMPACT_BYTES:
                kept = tail_lines(all_lines, MAX_EVENTS, COMPACT_BYTES >> 1)
                logs_dir.write("events.jsonl", b"".join(kept), mode=0o600, durable=False)
        return event


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


def _dedupe_typed_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    typed = {(it["to"], it["kind"], it.get("anchor", ""), it.get("section", "")) for it in links if it.get("meta")}
    return [
        it
        for it in links
        if it.get("meta") or (it["to"], it["kind"], it.get("anchor", ""), it.get("section", "")) not in typed
    ]


def _normalize_record_links(
    raw_links: object, label: str, brain_name: str, note_dirs: Iterable[str] = DEFAULT_NOTES
) -> list[dict[str, Any]]:
    from bf.markdown import add_link, parse_link, record_link_problem

    if raw_links is None:
        return []
    if not isinstance(raw_links, list):
        raise Error(f"{label}.links must be a list")
    dirs = tuple(note_dirs)
    roots = tuple(f"{folder}/" for folder in dirs)
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def add_one(tgt: str, sec: str = "", meta: dict[str, str] | None = None, req: bool = True, kind: str = "") -> None:
        parsed = parse_link(
            label, tgt, brain_name, note_dirs=dirs, section=sec, attr_meta=meta, relative_base=False, require_uri=req
        )
        if kind and parsed["kind"] != kind:
            raise Error(f"{label}.links: link kind {kind!r} does not match target")
        if prob := record_link_problem(parsed, note_dirs=dirs):
            raise Error(f"{label}: {prob}")
        add_link(seen, out, parsed)

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
    return _dedupe_typed_links(out)


def _prepare_record(
    raw: dict[str, Any],
    source: str,
    idx: int,
    brain_name: str,
    note_dirs: Iterable[str],
    *,
    target: Target = "memories",
) -> tuple[dict[str, Any], bool]:
    from bf.markdown import normalize_tags

    label, rec = f"{source}[{idx}]", dict(raw)
    if not isinstance(raw_id := rec.get("id"), str):
        raise Error(f"{label}: missing required string 'id'")
    if not isinstance(raw_title := rec.get("title"), str):
        raise Error(f"{label}: missing required string 'title'")
    rec_id = clean(raw_id, f"{label}.id")
    if any(ch.isspace() for ch in rec_id) or "#" in rec_id or models.invisible(rec_id):
        raise Error(f"{label}.id must contain no whitespace, '#', or invisible format characters")
    exp_kind = "usage" if target == "usages" else "memory"
    rec["id"], rec["title"], exp_ref = rec_id, clean(raw_title, f"{label}.title"), f"{source}:{rec_id}"
    for key, exp in (("source", source), ("ref", exp_ref), ("kind", exp_kind)):
        if key in rec and rec[key] != exp:
            raise Error(f"{label}.{key} must {'equal' if key == 'kind' else 'match'} {exp!r}")
    rec["source"], rec["ref"], rec["kind"] = source, exp_ref, exp_kind
    if (txt := rec.get("text")) is None:
        rec["text"] = ""
    elif not isinstance(txt, str) or len(txt) > 4 << 20:
        raise Error(f"{label}.text {'exceeds 4 MiB' if isinstance(txt, str) else 'must be a string'}")
    rec["tags"] = normalize_tags(rec["tags"], label) if rec.get("tags") is not None else []
    rec["links"] = _normalize_record_links(rec.get("links"), label, brain_name, note_dirs)
    if "url" in rec:
        if not isinstance(rec["url"], str):
            raise Error(f"{label}.url must be a string")
        clean(rec["url"], f"{label}.url") if rec["url"] else rec.pop("url")
    for field_name in ("time", "updated"):
        if field_name in rec:
            if not isinstance(val := rec[field_name], str):
                raise Error(f"{label}.{field_name} must be an ISO 8601 string")
            if not val:
                del rec[field_name]
            else:
                norm = models.normalize_date if field_name == "updated" and len(val) == 10 else models.normalize_time
                rec[field_name] = norm(val, f"{label}.{field_name}")
    if isinstance(sa := rec.get("stale_after"), str) and models.DURATION_RE.fullmatch(sa):
        models.parse_duration_seconds(sa, f"{label}.stale_after")
    encode(rec)
    return rec, "updated" in rec


def parse_emitted_event(raw_line: bytes, default_kind: str, label: str) -> tuple[str, dict[str, Any]]:
    """Parse one emitted JSONL line into `(event_kind, payload_dict)` for both daemon and cycle loops."""
    parsed = decode(raw_line, label)
    if not isinstance(parsed, dict):
        raise Error(f"{label}: emitted event must be a JSON object")
    raw_k = parsed.get("kind")
    if raw_k is not None and not isinstance(raw_k, str):
        raise Error(f"{label}: event kind must be a string")
    ev_kind = models.clean(raw_k or default_kind, f"{label} kind")
    if not models.EVENT_KIND_RE.fullmatch(ev_kind):
        raise Error(f"{label}: invalid event kind")
    return ev_kind, dict(parsed)


def emit_output_events(
    store: Store, name: str, default_kind: str, out_bytes: bytes, *, now: datetime | None = None
) -> int:
    """Validate all JSONL stdout events first, then atomically append deduplicated events."""
    if len(out_bytes) > models.MAX_BATCH:
        raise Error(f"loops.{name}: emitted output exceeds {models.MAX_BATCH} bytes")
    label = f"loops.{name}"
    parsed_evs = [
        parse_emitted_event(stripped, default_kind, f"{label} line {idx}")
        for idx, raw_ln in enumerate(out_bytes.splitlines(), 1)
        if (stripped := raw_ln.strip())
    ]
    if parsed_evs:
        with writer(store):
            for ev_k, payload in parsed_evs:
                append_event(store, ev_k, name, payload, dedupe=True, locked=True, now=now)
    return len(parsed_evs)


def require_ingest_validators(store: Store, source: str, target: Target = "memories") -> list[Any]:
    """Return all validators enforced for `<target>/<source>.jsonl` (core `record` + additive/domain schemas)."""
    from bf.schemas import base_record_validator, load_validator, require_validator

    vals: list[Any] = [base_record_validator()]
    if (local_rec := load_validator(store, "schemas/record.json")) is not None:
        vals.append(local_rec)
    if target == "usages":
        vals.append(require_validator(store, "usages"))
        if (src_val := load_validator(store, f"schemas/{source}.json")) is not None:
            vals.append(src_val)
    else:
        vals.append(require_validator(store, source))
    return vals


def ingest(
    store: Store,
    source: str,
    data: bytes,
    *,
    target: Target = "memories",
    mode: Mode = "upsert",
    allow_removal: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate against bundled `record` + required domain schema and write `<target>/<source>.jsonl`."""
    from bf import config
    from bf.schemas import validate_instance

    if not models.NAME_RE.fullmatch(source):
        raise InputError("source must start with a lowercase letter and use only lowercase letters, digits, or '-'")
    if target not in {"memories", "usages"}:
        raise InputError("target must be memories or usages")
    cfg = config.load(store)
    if source in cfg.reserved_sources:
        raise InputError(f"source {source!r} is reserved; choose a sensor name")
    if mode not in {"upsert", "snapshot"}:
        raise InputError("mode must be upsert or snapshot")
    validators = require_ingest_validators(store, source, target)

    raw_items, incomplete = parse_batch_envelope(data)
    prepared: list[tuple[dict[str, Any], bool]] = []
    seen_ids: set[str] = set()
    stamp = now_utc(now)
    for idx, item in enumerate(raw_items):
        rec, explicit_updated = _prepare_record(item, source, idx, cfg.name, cfg.notes, target=target)
        if (rec_id := str(rec["id"])) in seen_ids:
            raise Error(f"{source}: duplicate record id {rec_id!r} in input batch")
        seen_ids.add(rec_id)
        candidate = rec if explicit_updated else {**rec, "updated": stamp}
        for validator in validators:
            if errs := validate_instance(validator, candidate, f"{source}[{idx}]"):
                raise Error("; ".join(errs))
        prepared.append((rec, explicit_updated))

    target_file = record_file(source, target)
    with nullcontext() if dry_run else writer(store):
        forgotten = _forgotten_ids(store, source, target)
        existing: dict[str, tuple[bytes, dict[str, Any]]] = {}
        with suppress(FileNotFoundError):
            for raw_ln, parsed in iter_jsonl(store, target_file):
                if isinstance(eid := parsed.get("id"), str) and eid:
                    existing[eid] = (raw_ln, parsed)

        next_lines: dict[str, bytes] = {}
        added = updated_count = unchanged = skipped_forgotten = 0
        for rec, explicit_updated in prepared:
            rec_id = str(rec["id"])
            if rec_id in forgotten:
                skipped_forgotten += 1
                continue
            stamped = rec if explicit_updated else {**rec, "updated": stamp}
            if rec_id not in existing:
                next_lines[rec_id] = encode(stamped).rstrip(b"\n")
                added += 1
                continue
            old_raw, old_rec = existing[rec_id]
            cmp_rec = old_rec if explicit_updated else {k: v for k, v in old_rec.items() if k != "updated"}
            if cmp_rec == rec:
                next_lines[rec_id] = old_raw
                unchanged += 1
            else:
                next_lines[rec_id] = encode(stamped).rstrip(b"\n")
                updated_count += 1

        snap_complete = mode == "snapshot" and not incomplete
        if snap_complete:
            removed_ids = [eid for eid in existing if eid not in next_lines]
        else:
            removed_ids = []
            for eid, (old_raw, _) in existing.items():
                if eid in forgotten:
                    removed_ids.append(eid)
                elif eid not in next_lines:
                    next_lines[eid] = old_raw

        removed, existing_n, surviving = len(removed_ids), len(existing), len(next_lines)
        too_many = surviving == 0 or (existing_n >= 10 and removed * 2 > existing_n)
        if snap_complete and existing and not allow_removal and too_many:
            raise Error(f"{source}: snapshot would remove {removed} of {existing_n} records; pass --allow-removal")

        if not dry_run and (added or updated_count or removed):
            if surviving > 0:
                payload = b"\n".join(next_lines[k] for k in sorted(next_lines)) + b"\n"
                store.write(target_file, payload)
            else:
                with suppress(FileNotFoundError):
                    store.delete(target_file)
            ev_data = {
                "target": target,
                "records": surviving,
                "added": added,
                "updated": updated_count,
                "removed": removed,
            }
            append_event(store, "records.ingested", source, ev_data, locked=True, now=now)
        return {
            "ok": True,
            "brain": cfg.name,
            "source": source,
            "target": target,
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
