"""Private runtime state (`.bf/state/<loop>.json`, `events.jsonl`, `timers.jsonl`), stderr PEM logging, and orphan discovery."""

from __future__ import annotations

import os
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import nullcontext, suppress
from datetime import UTC, datetime
from typing import Any

from bf import models
from bf.models import PEM_BEG_RE, PEM_END_RE, Error, InputError, clean, decode, encode, now_utc
from bf.storage import Store, append_regular, private_dir, tail_lines, writer

MAX_EVENTS = 10_000
COMPACT_BYTES = 16 << 20
MAX_EVENTS_FILE = 32 << 20
MAX_EVENT_LINE = 1 << 20
MAX_EMIT_BATCH = 4 << 20
MAX_EMIT_EVENTS = 1_000
EVENTS_FILE = "events.jsonl"
TIMERS_FILE = "timers.jsonl"
MAX_TIMERS = 1000
MAX_TIMERS_FILE = 4 << 20
STATE_PARTS = (".bf", "state")
_PEM_MAX_BYTES = 16384
_MAX_DT = datetime.max.replace(tzinfo=UTC)


def _state_dir(store: Store, *, create: bool = True) -> Store:
    return private_dir(store, *STATE_PARTS, create=create)


def int_field(doc: Mapping[str, object], key: str) -> int:
    """Return integer field `key` from a state mapping, defaulting safely to `0` on missing/wrong JSON types."""
    val = doc.get(key)
    return val if type(val) is int else 0


def _parse_iso(raw: str) -> datetime | None:
    with suppress(ValueError, OverflowError):
        if raw and (dt := datetime.fromisoformat(raw)).tzinfo is not None:
            return dt.astimezone(UTC)
    return None


def _timer_sort_key(item: Mapping[str, Any]) -> tuple[datetime, str]:
    return (_parse_iso(str(item.get("deliver_at") or "")) or _MAX_DT, str(item.get("id") or ""))


def default_unit_state(name: str, *, event_seq: int = 0) -> dict[str, Any]:
    zeros = dict.fromkeys(
        (
            "runs",
            "failures",
            "consecutive_failures",
            "schedule_failures",
            "incomplete_runs",
            "event_failures",
            "dead_lettered",
            "last_dead_letter_seq",
            "pending_events_missed",
            "duration_ms",
        ),
        0,
    )
    empty = dict.fromkeys(
        (
            "last_start",
            "last_end",
            "last_schedule_end",
            "last_event_end",
            "last_success",
            "last_scheduled",
            "error",
            "paused_until",
        ),
        "",
    )
    base = {
        "name": name,
        "status": "idle",
        "updated": now_utc(),
        "event_seq": event_seq,
        "paused": False,
        "schedule_last": {},
        "last_run": {},
    }
    return {**base, **empty, **zeros}


def is_paused(doc: Mapping[str, object], now: datetime) -> bool:
    """Return True if loop state `doc` is actively paused at `now` (indefinitely or before `paused_until`)."""
    if not doc.get("paused"):
        return False
    until_raw = str(doc.get("paused_until") or "")
    if not until_raw:
        return True
    until_dt = _parse_iso(until_raw)
    return until_dt is not None and now.astimezone(UTC) < until_dt


def pause_remaining(doc: Mapping[str, object], now: datetime) -> float | None:
    """Return seconds until temporary `paused_until` expires, or `None` when unpaused or indefinitely paused."""
    if not doc.get("paused"):
        return None
    until_dt = _parse_iso(str(doc.get("paused_until") or ""))
    if until_dt is None:
        return None
    rem = (until_dt - now.astimezone(UTC)).total_seconds()
    return rem if rem > 0 else None


def has_unit_state(store: Store, name: str) -> bool:
    """Return True if `.bf/state/<name>.json` exists on disk as a regular file."""
    with suppress(Error, OSError, FileNotFoundError):
        return _state_dir(store, create=False).exists(f"{name}.json")
    return False


def load_unit_state(store: Store, name: str, *, default_event_seq: int | None = None) -> dict[str, Any]:
    """Load `.bf/state/<name>.json`, returning an initialized `idle` state anchored at `default_event_seq` when absent."""
    with suppress(FileNotFoundError):
        st_dir = _state_dir(store, create=False)
        if isinstance(parsed := decode(st_dir.read(f"{name}.json", 1 << 20), f".bf/state/{name}.json"), dict):
            return dict(parsed)
    seq = latest_event_seq(store) if default_event_seq is None else default_event_seq
    return default_unit_state(name, event_seq=seq)


def save_unit_state(store: Store, doc: Mapping[str, Any], *, locked: bool = False, durable: bool = True) -> None:
    """Write `.bf/state/<name>.json` atomically with `0600` permissions."""
    with nullcontext() if locked else writer(store):
        _state_dir(store).write(f"{doc['name']}.json", encode(dict(doc)), mode=0o600, durable=durable)


def mark_unit_state(store: Store, name: str, **fields: Any) -> None:
    """Atomically read-modify-write fields on `.bf/state/<name>.json` under `writer(store)`, recovering corrupt JSON."""
    with writer(store):
        _state_dir(store)
        try:
            cur = load_unit_state(store, name)
        except Error:
            cur = default_unit_state(name, event_seq=latest_event_seq(store))
        save_unit_state(store, {**cur, "name": name, **fields}, locked=True)


def find_orphans(store: Store, known_loops: Collection[str]) -> dict[str, list[str]]:
    """Return orphaned `.bf/state/<loop>.json` states, `.bf/state/<loop>/` dirs, `.bf/locks/loop-*.lock`, and `logs/<loop>.jsonl`."""
    states: list[str] = []
    dirs: list[str] = []
    locks: list[str] = []
    logs: list[str] = []
    with suppress(FileNotFoundError):
        st_dir = _state_dir(store, create=False)
        sub_dirs, top_files = st_dir.subdirs("")
        dirs.extend(d for d in sub_dirs if d not in known_loops)
        states.extend(p[:-5] for p in top_files if p.endswith(".json") and p[:-5] not in known_loops)
    with suppress(FileNotFoundError):
        locks_dir = private_dir(store, ".bf", "locks", create=False)
        _, lock_files = locks_dir.subdirs("")
        for p in lock_files:
            if p.startswith("loop-") and p.endswith(".lock"):
                lp = p[len("loop-") : -len(".lock")]
                if lp not in known_loops:
                    locks.append(p)
    with suppress(FileNotFoundError):
        logs_dir = private_dir(store, "logs", create=False)
        _, log_files = logs_dir.subdirs("")
        logs.extend(p[:-6] for p in log_files if p.endswith(".jsonl") and p[:-6] not in known_loops)
    return {"states": sorted(states), "dirs": sorted(dirs), "locks": sorted(locks), "logs": sorted(logs)}


def make_stderr_logger(
    store: Store, name: str, run_id: str, *, secrets: Collection[str] = ()
) -> Callable[[bytes], str]:
    """Return a stateful line callback redacting exact secrets & multi-line PEM blocks before persisting to `logs/`."""
    secret_tuple = tuple(sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True))
    in_pem, pem_bytes = False, 0

    def _on_line(raw_line: bytes) -> str:
        nonlocal in_pem, pem_bytes
        text = raw_line.decode("utf-8", "replace")
        begs = [m.start() for m in PEM_BEG_RE.finditer(text)]
        ends = [m.start() for m in PEM_END_RE.finditer(text)]
        last_beg, last_end = (begs[-1] if begs else -1), (ends[-1] if ends else -1)
        if in_pem:
            pem_bytes += len(raw_line)
            if last_end > last_beg:
                in_pem, pem_bytes = False, 0
                return ""
            if last_beg > last_end:
                pem_bytes = 0
                return ""
            if pem_bytes <= _PEM_MAX_BYTES:
                return ""
            in_pem, pem_bytes = False, 0
        if begs:
            if last_beg > last_end:
                in_pem, pem_bytes = True, 0
            append_loop_stderr(store, name, run_id, b"[REDACTED]", secrets=secret_tuple)
            return "[REDACTED]"
        return append_loop_stderr(store, name, run_id, raw_line, secrets=secret_tuple)

    return _on_line


def append_loop_stderr(store: Store, name: str, run_id: str, raw_line: bytes, *, secrets: Collection[str] = ()) -> str:
    """Parse one loop stderr line, redact secrets, append to `logs/<name>.jsonl`, and return sanitized summary text."""
    text = raw_line.decode("utf-8", "replace").strip()
    if not text:
        return ""
    if text.startswith("{"):
        with suppress(Error):
            parsed = decode(text, f"logs/{name}.jsonl", check_duplicates=False)
            if isinstance(parsed, dict) and isinstance(safe := models.redact_value(parsed, secrets=secrets), dict):
                t1, t2, t3 = safe.pop("time", ""), safe.pop("timestamp", ""), safe.pop("time_iso", "")
                s1, s2 = safe.pop("severity", ""), safe.pop("level", "")
                m1, m2 = safe.pop("message", ""), safe.pop("event", "")
                ts = str(t1 or t2 or t3 or "")
                sev = str(s1 or s2 or "INFO")
                msg = str(m1 or m2 or "log")[:4096]
                safe["loop"], safe["run"] = name, run_id
                with suppress(Error, OSError):
                    store.append_log(name, models.log_line(msg, severity=sev, time_iso=ts, **safe))
                return msg
    clean_msg = models.redact_secrets(text, secrets=secrets)[:4096]
    with suppress(Error, OSError):
        store.append_log(name, models.log_line(clean_msg, severity="WARN", loop=name, run=run_id))
    return clean_msg


def _parse_event(raw: bytes) -> dict[str, Any] | None:
    with suppress(Error):
        stripped = raw.strip()
        if stripped and isinstance(
            parsed := decode(stripped, f".bf/state/{EVENTS_FILE}", check_duplicates=False), dict
        ):
            seq = parsed.get("seq")
            if type(seq) is int and seq > 0:
                return dict(parsed)
    return None


def _pread_all(fd: int, length: int, offset: int) -> bytes:
    chunks: list[bytes] = []
    rem, pos = length, offset
    while rem > 0:
        piece = os.pread(fd, min(rem, 1 << 20), pos)
        if not piece:
            break
        chunks.append(piece)
        pos += len(piece)
        rem -= len(piece)
    return b"".join(chunks)


def _tail_from_fd(fd: int, size: int, span: int) -> tuple[bytes, int]:
    off = max(0, size - span)
    raw = _pread_all(fd, size - off, off)
    return (raw.split(b"\n", 1)[1] if b"\n" in raw else b"") if off else raw, off


def _latest_seq_fd(fd: int, size: int) -> tuple[int, bool]:
    dirty = False
    for span in (65536, MAX_EVENTS_FILE):
        tail, off = _tail_from_fd(fd, size, span)
        dirty = bool((tail and not tail.endswith(b"\n")) or (not tail and off > 0))
        for ln in reversed(tail.splitlines()):
            if ln.strip():
                if parsed := _parse_event(ln):
                    return int(parsed["seq"]), dirty
                dirty = True
        if off == 0:
            break
    else:
        dirty = True
    return 0, dirty


def _first_seq_fd(fd: int, size: int) -> int:
    min_off = max(0, size - MAX_EVENTS_FILE)
    head = _pread_all(fd, min(size - min_off, 65536), min_off)
    if min_off > 0:
        head = head.split(b"\n", 1)[1] if b"\n" in head else b""
    for ln in head.splitlines():
        if ln.strip() and (parsed := _parse_event(ln)):
            return int(parsed["seq"])
    if size - min_off > 65536:
        tail, _ = _tail_from_fd(fd, size, MAX_EVENTS_FILE)
        for ln in tail.splitlines():
            if ln.strip() and (parsed := _parse_event(ln)):
                return int(parsed["seq"])
    return 0


def _event_tail(st_dir: Store) -> tuple[int, bool]:
    with suppress(FileNotFoundError), st_dir.open_for_reads(EVENTS_FILE) as (fd, info):
        if info.st_size <= 0:
            return 0, False
        return _latest_seq_fd(fd, info.st_size)
    return 0, False


def event_bounds(store: Store) -> tuple[int, int]:
    """Strictly read-only single-fd `(first_seq, latest_seq)` bounds of surviving events in `.bf/state/events.jsonl`."""
    with suppress(FileNotFoundError):
        st_dir = _state_dir(store, create=False)
        with st_dir.open_for_reads(EVENTS_FILE) as (fd, info):
            if info.st_size <= 0:
                return 0, 0
            latest, _ = _latest_seq_fd(fd, info.st_size)
            if latest <= 0:
                return 0, 0
            first = _first_seq_fd(fd, info.st_size)
            return (first or latest), latest
    return 0, 0


def latest_event_seq(store: Store) -> int:
    """Return the highest valid `seq` recorded in `.bf/state/events.jsonl` (`0` when absent or empty)."""
    with suppress(FileNotFoundError):
        return _event_tail(_state_dir(store, create=False))[0]
    return 0


def read_events_meta(
    store: Store, *, after_seq: int = 0, heal_cursor: bool = True
) -> tuple[list[dict[str, Any]], int, int]:
    """Read valid events from `.bf/state/events.jsonl` on a single fd, returning `(events, latest_seq, first_seq)`."""
    with suppress(FileNotFoundError):
        st_dir = _state_dir(store, create=False)
        with st_dir.open_for_reads(EVENTS_FILE) as (fd, info):
            size = info.st_size
            if size <= 0:
                return [], 0, 0
            latest, _ = _latest_seq_fd(fd, size)
            if latest <= 0:
                return [], 0, 0
            first = _first_seq_fd(fd, size) or latest
            floor = 0 if (heal_cursor and after_seq > latest) else after_seq
            if floor >= latest:
                return [], latest, first
            if floor > 0:
                # Backward chunked tail scan stops as soon as `seq <= floor` (O(delta) in steady state).
                min_off = max(0, size - MAX_EVENTS_FILE)
                pos = size
                carry = b""
                rev_matched: list[dict[str, Any]] = []
                done = False
                while pos > min_off and not done:
                    step = min(65536, pos - min_off)
                    pos -= step
                    buf = _pread_all(fd, step, pos) + carry
                    lines = buf.splitlines()
                    if pos > min_off:
                        carry = lines[0] if lines else buf
                        lines = lines[1:]
                    else:
                        carry = b""
                        if min_off > 0 and lines:
                            lines = lines[1:]
                    for ln in reversed(lines):
                        if parsed := _parse_event(ln):
                            seq = int(parsed["seq"])
                            if seq <= floor:
                                done = True
                                break
                            rev_matched.append(parsed)
                rev_matched.reverse()
                return rev_matched, latest, first
            tail, _ = _tail_from_fd(fd, size, MAX_EVENTS_FILE)
            all_events = [parsed for ln in tail.splitlines() if (parsed := _parse_event(ln))]
            return all_events, latest, first
    return [], 0, 0


def read_events(store: Store, *, after_seq: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read valid events from `.bf/state/events.jsonl` with crash-tail tolerance and cursor self-healing."""
    evs, latest, _ = read_events_meta(store, after_seq=after_seq, heal_cursor=True)
    return evs, latest


def _valid_events_tail(st_dir: Store, span: int = MAX_EVENTS_FILE) -> list[bytes]:
    tail, _ = st_dir.read_tail(EVENTS_FILE, span, label=f".bf/state/{EVENTS_FILE}")
    return [ln if ln.endswith(b"\n") else ln + b"\n" for ln in tail.splitlines(keepends=True) if _parse_event(ln)]


def _maybe_compact_events(st_dir: Store, size_after: int, prev_seq: int, seq: int) -> None:
    stride = min(1000, max(200, MAX_EVENTS // 10))
    crossed = seq > MAX_EVENTS and (prev_seq // stride) < (seq // stride)
    if size_after > COMPACT_BYTES or crossed:
        all_lines = _valid_events_tail(st_dir, COMPACT_BYTES)
        if len(all_lines) > MAX_EVENTS or size_after > COMPACT_BYTES:
            kept = tail_lines(all_lines, MAX_EVENTS, COMPACT_BYTES >> 1)
            st_dir.write(EVENTS_FILE, b"".join(kept), mode=0o600, durable=True)


def _find_dedupe_hits(
    st_dir: Store, wanted_keys: set[tuple[str, str, str]]
) -> dict[tuple[str, str, str], dict[str, Any]]:
    existing_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not wanted_keys:
        return existing_by_key
    sources = {src for src, _, _ in wanted_keys}
    ids = {mid for _, _, mid in wanted_keys}
    src_needles = [encode(s).rstrip(b"\n") for s in sources]
    id_needles = [encode(wid).rstrip(b"\n") for wid in ids] if len(ids) <= 16 else None
    with suppress(FileNotFoundError):
        tail, _ = st_dir.read_tail(EVENTS_FILE, MAX_EVENTS_FILE, label=f".bf/state/{EVENTS_FILE}")
        for ln in reversed(tail.splitlines()):
            if id_needles is not None:
                if not any(n in ln for n in id_needles):
                    continue
            elif not any(s in ln for s in src_needles):
                continue
            if (
                (parsed := _parse_event(ln))
                and isinstance(pd := parsed.get("data"), dict)
                and isinstance(mid := pd.get("id"), str)
                and mid in ids
            ):
                key = (str(parsed.get("source") or ""), str(parsed.get("kind") or ""), mid)
                if key in wanted_keys:
                    existing_by_key.setdefault(key, parsed)
                    if len(existing_by_key) == len(wanted_keys):
                        break
    return existing_by_key


def _append_mixed_events_locked(
    st_dir: Store,
    prepared: Sequence[tuple[str, str, Any, bool]],
    *,
    ts: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Append mixed `(source, kind, safe_data, dedupe)` items in one combined dedupe scan + single durable write."""
    wanted_keys: set[tuple[str, str, str]] = {
        (src, ev_k, sd["id"])
        for src, ev_k, sd, ded in prepared
        if ded and isinstance(sd, dict) and isinstance(sd.get("id"), str) and sd["id"]
    }
    prev_seq, dirty = _event_tail(st_dir)
    if dirty:
        st_dir.write(EVENTS_FILE, b"".join(_valid_events_tail(st_dir)), mode=0o600, durable=True)
        prev_seq = _event_tail(st_dir)[0]

    existing_by_key = _find_dedupe_hits(st_dir, wanted_keys) if wanted_keys else {}

    seq = prev_seq
    batch_bytes: list[bytes] = []
    written_events: list[dict[str, Any]] = []
    deduped_any = False
    for src, ev_k, safe_data, dedupe in prepared:
        if dedupe and isinstance(safe_data, dict) and isinstance(msg_id := safe_data.get("id"), str) and msg_id:
            dk = (src, ev_k, msg_id)
            if dk in existing_by_key:
                deduped_any = True
                continue
            existing_by_key[dk] = {}
        seq += 1
        eid = f"evt-{seq}-{os.urandom(4).hex()}"
        event = {"seq": seq, "id": eid, "time": ts, "kind": ev_k, "source": src, "data": safe_data}
        if len(raw_line := encode(event)) > MAX_EVENT_LINE:
            raise Error(f".bf/state/{EVENTS_FILE}: event exceeds {MAX_EVENT_LINE} bytes")
        batch_bytes.append(raw_line)
        written_events.append(event)
        if dedupe and isinstance(safe_data, dict) and isinstance(msg_id := safe_data.get("id"), str) and msg_id:
            existing_by_key[(src, ev_k, msg_id)] = event

    if batch_bytes:
        size_after = append_regular(
            st_dir, EVENTS_FILE, b"".join(batch_bytes), label=f".bf/state/{EVENTS_FILE}", durable=True
        )
        _maybe_compact_events(st_dir, size_after, prev_seq, seq)
    elif len(prepared) == 1 and deduped_any:
        src, ev_k, safe_data, _ = prepared[0]
        existing_hit = existing_by_key.get((src, ev_k, str(safe_data.get("id"))))
        if existing_hit:
            return [existing_hit], True
    return written_events, deduped_any


def _append_events_batch(
    store: Store,
    source: str,
    entries: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    dedupe: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
    secrets: Collection[str] = (),
) -> tuple[list[dict[str, Any]], bool]:
    """Atomically append one or more validated events to `.bf/state/events.jsonl` under `write.lock`."""
    if not entries:
        return [], False
    clean(source, "event source")
    if not models.NAME_RE.fullmatch(source):
        raise Error("event source must start with a lowercase letter and use lowercase letters, digits, or '-'")
    prepared: list[tuple[str, str, Any, bool]] = []
    for ev_k, payload in entries:
        clean(ev_k, "event kind")
        if not models.EVENT_KIND_RE.fullmatch(ev_k):
            raise Error(
                "event kind must start with a lowercase letter and use lowercase letters, digits, '.', '_', or '-'"
            )
        prepared.append((source, ev_k, models.redact_value(dict(payload), secrets=secrets), dedupe))

    wanted_keys: set[tuple[str, str, str]] = {
        (src, ev_k, sd["id"])
        for src, ev_k, sd, ded in prepared
        if ded and isinstance(sd, dict) and isinstance(sd.get("id"), str) and sd["id"]
    }
    ts = now_utc(now)

    if dry_run:
        prev_seq = 0
        existing_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        with suppress(FileNotFoundError):
            st_dir = _state_dir(store, create=False)
            prev_seq = _event_tail(st_dir)[0]
            if wanted_keys:
                existing_by_key = _find_dedupe_hits(st_dir, wanted_keys)
        seq = prev_seq
        preview: list[dict[str, Any]] = []
        deduped_any = False
        for src, ev_k, safe_data, ded in prepared:
            if ded and isinstance(safe_data, dict) and isinstance(msg_id := safe_data.get("id"), str) and msg_id:
                dk = (src, ev_k, msg_id)
                if dk in existing_by_key:
                    deduped_any = True
                    if len(prepared) == 1 and existing_by_key[dk]:
                        return [existing_by_key[dk]], True
                    continue
                existing_by_key[dk] = {}
            seq += 1
            event = {
                "seq": seq,
                "id": f"evt-{seq}-{os.urandom(4).hex()}",
                "time": ts,
                "kind": ev_k,
                "source": src,
                "data": safe_data,
            }
            if len(encode(event)) > MAX_EVENT_LINE:
                raise Error(f".bf/state/{EVENTS_FILE}: event exceeds {MAX_EVENT_LINE} bytes")
            preview.append(event)
        return preview, deduped_any

    with writer(store):
        st_dir = _state_dir(store)
        return _append_mixed_events_locked(st_dir, prepared, ts=ts)


def append_event(
    store: Store,
    kind: str,
    source: str,
    data: Mapping[str, Any] | None = None,
    *,
    dedupe: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
    secrets: Collection[str] = (),
) -> tuple[dict[str, Any], bool]:
    """Append (or dry-run validate) one durable event in `.bf/state/events.jsonl`; return `(event_dict, was_deduped)`."""
    events, deduped = _append_events_batch(
        store, source, [(kind, dict(data or {}))], dedupe=dedupe, dry_run=dry_run, now=now, secrets=secrets
    )
    return events[0], deduped


def parse_emitted_event(raw_line: bytes, default_kind: str, label: str) -> tuple[str, dict[str, Any]]:
    """Parse one emitted JSONL line into `(event_kind, payload_dict)` without duplicating `kind` inside `data`."""
    parsed = decode(raw_line, label)
    if not isinstance(parsed, dict):
        raise Error(f"{label}: emitted event must be a JSON object")
    payload = dict(parsed)
    raw_k = payload.pop("kind", None)
    if raw_k is not None and not isinstance(raw_k, str):
        raise Error(f"{label}: event kind must be a string")
    ev_kind = models.clean(raw_k or default_kind, f"{label} kind")
    if not models.EVENT_KIND_RE.fullmatch(ev_kind):
        raise Error(f"{label}: invalid event kind")
    return ev_kind, payload


def emit_output_events(
    store: Store,
    name: str,
    default_kind: str,
    out_bytes: bytes,
    *,
    now: datetime | None = None,
    secrets: Collection[str] = (),
) -> int:
    """Validate all JSONL stdout events first (bounded by bytes and count), batch-append deduped events."""
    if len(out_bytes) > MAX_EMIT_BATCH:
        raise Error(f"loops.{name}: emitted output exceeds {MAX_EMIT_BATCH} bytes")
    label = f"loops.{name}"
    parsed_evs: list[tuple[str, dict[str, Any]]] = []
    for idx, raw_ln in enumerate(out_bytes.splitlines(), 1):
        if not (stripped := raw_ln.strip()):
            continue
        if len(parsed_evs) >= MAX_EMIT_EVENTS:
            raise Error(f"loops.{name}: emitted output exceeds {MAX_EMIT_EVENTS} events")
        parsed_evs.append(parse_emitted_event(stripped, default_kind, f"{label} line {idx}"))
    if not parsed_evs:
        return 0
    written, _ = _append_events_batch(store, name, parsed_evs, dedupe=True, now=now, secrets=secrets)
    return len(written)


def _parse_timer(raw: bytes) -> dict[str, Any] | None:
    with suppress(Error):
        stripped = raw.strip()
        if stripped and isinstance(
            parsed := decode(stripped, f".bf/state/{TIMERS_FILE}", check_duplicates=False), dict
        ):
            tid, deliver, kind, src, data = (
                parsed.get("id"),
                parsed.get("deliver_at"),
                parsed.get("kind"),
                parsed.get("source"),
                parsed.get("data"),
            )
            if (
                isinstance(tid, str)
                and tid
                and isinstance(deliver, str)
                and _parse_iso(deliver) is not None
                and isinstance(kind, str)
                and models.EVENT_KIND_RE.fullmatch(kind)
                and isinstance(src, str)
                and models.NAME_RE.fullmatch(src)
                and isinstance(data, dict)
            ):
                return dict(parsed)
    return None


def _read_timers_dir(st_dir: Store) -> list[dict[str, Any]]:
    timers: list[dict[str, Any]] = []
    with suppress(FileNotFoundError):
        raw = st_dir.read(TIMERS_FILE, MAX_TIMERS_FILE)
        timers.extend(parsed for ln in raw.splitlines() if (parsed := _parse_timer(ln)))
    timers.sort(key=_timer_sort_key)
    return timers


def read_timers(store: Store) -> list[dict[str, Any]]:
    """Strictly read-only list of valid pending delayed events in `.bf/state/timers.jsonl`, sorted by UTC `deliver_at`."""
    with suppress(FileNotFoundError):
        return _read_timers_dir(_state_dir(store, create=False))
    return []


def next_timer_delay(store: Store, now: datetime) -> float | None:
    """Strictly read-only seconds until the earliest pending delayed event is due, or `None` when none exist."""
    timers = read_timers(store)
    if not timers:
        return None
    dt = _parse_iso(str(timers[0]["deliver_at"]))
    return max(0.0, (dt - now.astimezone(UTC)).total_seconds()) if dt is not None else None


def schedule_event(
    store: Store,
    kind: str,
    source: str,
    data: Mapping[str, Any] | None = None,
    *,
    deliver_at: datetime,
    dedupe: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
    secrets: Collection[str] = (),
) -> tuple[dict[str, Any], bool]:
    """Schedule a durable delayed event in `.bf/state/timers.jsonl` for delivery at `deliver_at`."""
    clean(source, "event source")
    if not models.NAME_RE.fullmatch(source):
        raise Error("event source must start with a lowercase letter and use lowercase letters, digits, or '-'")
    clean(kind, "event kind")
    if not models.EVENT_KIND_RE.fullmatch(kind):
        raise Error("event kind must start with a lowercase letter and use lowercase letters, digits, '.', '_', or '-'")
    safe_data = models.redact_value(dict(data or {}), secrets=secrets)
    if not isinstance(safe_data, dict):
        safe_data = {}
    ts, deliver_iso = now_utc(now), now_utc(deliver_at)
    dedupe_id = safe_data.get("id") if (dedupe and isinstance(safe_data.get("id"), str) and safe_data.get("id")) else ""
    timer = {
        "id": f"tmr-{os.urandom(6).hex()}",
        "created": ts,
        "deliver_at": deliver_iso,
        "kind": kind,
        "source": source,
        "data": safe_data,
        **({"dedupe": True} if dedupe else {}),
    }
    raw_line = encode(timer)
    if len(raw_line) > MAX_EVENT_LINE:
        raise Error(f".bf/state/{TIMERS_FILE}: timer event exceeds {MAX_EVENT_LINE} bytes")

    def _find_dup(existing: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
        if not dedupe_id:
            return None
        for item in existing:
            if (
                item.get("kind") == kind
                and item.get("source") == source
                and isinstance(item_d := item.get("data"), dict)
                and item_d.get("id") == dedupe_id
            ):
                return item
        return None

    if dry_run:
        existing = read_timers(store)
        if hit := _find_dup(existing):
            return hit, True
        if len(existing) >= MAX_TIMERS:
            raise Error(f".bf/state/{TIMERS_FILE}: exceeds {MAX_TIMERS} pending timers")
        return timer, False

    with writer(store):
        st_dir = _state_dir(store)
        existing = _read_timers_dir(st_dir)
        if hit := _find_dup(existing):
            return hit, True
        if len(existing) >= MAX_TIMERS:
            raise Error(f".bf/state/{TIMERS_FILE}: exceeds {MAX_TIMERS} pending timers")
        updated = sorted([*existing, timer], key=_timer_sort_key)
        blob = b"".join(encode(item) for item in updated)
        if len(blob) > MAX_TIMERS_FILE:
            raise Error(f".bf/state/{TIMERS_FILE}: exceeds {MAX_TIMERS_FILE} bytes")
        st_dir.write(TIMERS_FILE, blob, mode=0o600, durable=True)
    return timer, False


def promote_due_timers(store: Store, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Promote all due delayed events (`deliver_at <= now`) in one atomic locked batch into `events.jsonl`."""
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    try:
        ro_dir = _state_dir(store, create=False)
        preview = _read_timers_dir(ro_dir)
    except FileNotFoundError:
        return []
    if not any((dt := _parse_iso(str(t["deliver_at"]))) is not None and dt <= instant for t in preview):
        return []

    ts = now_utc(instant)
    with writer(store):
        st_dir = _state_dir(store)
        all_timers = _read_timers_dir(st_dir)
        due: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for item in all_timers:
            dt = _parse_iso(str(item["deliver_at"]))
            if dt is not None and dt <= instant:
                due.append(item)
            else:
                pending.append(item)
        if not due:
            return []
        prepared = [
            (
                str(item["source"]),
                str(item["kind"]),
                models.redact_value(dict(item["data"])),
                bool(item.get("dedupe")),
            )
            for item in due
        ]
        promoted, _ = _append_mixed_events_locked(st_dir, prepared, ts=ts)
        if pending:
            st_dir.write(TIMERS_FILE, b"".join(encode(item) for item in pending), mode=0o600, durable=True)
        else:
            with suppress(FileNotFoundError):
                st_dir.delete(TIMERS_FILE, durable=True)
    return promoted


def validate_list_events_args(
    *,
    since: int | None = None,
    kind: str = "",
    source: str = "",
    limit: int = 20,
    all_items: bool = False,
) -> None:
    """Validate brain-independent `bf events` arguments, raising `InputError` on bad selectors/limits."""
    if since is not None and since < 0:
        raise InputError("--since must be a non-negative integer")
    if not all_items and not (1 <= limit <= 5000):
        raise InputError("--limit must be between 1 and 5000")
    if kind and not models.EVENT_KIND_RE.fullmatch(kind):
        raise InputError("--kind must match ^[a-z][a-z0-9._-]{0,127}$")
    if source and not models.NAME_RE.fullmatch(source):
        raise InputError("--source must use lowercase letters, digits, and hyphens")


def list_events(
    store: Store,
    brain_name: str,
    *,
    since: int | None = None,
    kind: str = "",
    source: str = "",
    timers_only: bool = False,
    limit: int = 20,
    all_items: bool = False,
) -> dict[str, Any]:
    """Strictly read-only inspection of `.bf/state/events.jsonl` or pending `.bf/state/timers.jsonl`."""
    validate_list_events_args(since=since, kind=kind, source=source, limit=limit, all_items=all_items)

    timers = read_timers(store)
    if timers_only:
        latest_seq = latest_event_seq(store)
        filtered = [
            t for t in timers if (not kind or t.get("kind") == kind) and (not source or t.get("source") == source)
        ]
        total = len(filtered)
        truncated = not all_items and total > limit
        items = filtered[:limit] if truncated else filtered
        return {
            "ok": True,
            "brain": brain_name,
            "mode": "timers",
            "events_seq": latest_seq,
            "pending_timers": len(timers),
            "count": len(items),
            "total": total,
            "truncated": truncated,
            "items": items,
        }

    evs, latest_seq, first_seq = read_events_meta(store, after_seq=0 if since is None else since, heal_cursor=False)
    filtered = [e for e in evs if (not kind or e.get("kind") == kind) and (not source or e.get("source") == source)]
    total = len(filtered)
    truncated = not all_items and total > limit
    items = (filtered[:limit] if since is not None else filtered[-limit:]) if truncated else filtered
    return {
        "ok": True,
        "brain": brain_name,
        "mode": "events",
        "events_seq": latest_seq,
        "first_seq": first_seq,
        "pending_timers": len(timers),
        "count": len(items),
        "total": total,
        "truncated": truncated,
        "items": items,
    }
