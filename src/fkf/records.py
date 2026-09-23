"""Records live in monthly JSON Lines partitions: one line per source item, upserted by id."""

from __future__ import annotations

import re
from collections.abc import Iterator

from pydantic import ValidationError

from fkf.models import MAX_PARTITION, NAME, Error, Record, decode, encode, explain
from fkf.storage import Store

SNAPSHOT = "snapshot"
UNDATED = "undated"


def partition(record: Record) -> str:
    return record.time[:7] if record.time else UNDATED


def path(source: str, name: str) -> str:
    return f"records/{source}/{name}.jsonl"


def line(record: Record) -> bytes:
    return encode(record.model_dump(exclude_defaults=True))


def partitions(store: Store, source: str = "") -> list[str]:
    """Record partitions of one source, or of every source; other files under records/ are ignored."""
    return [name for name in store.files(f"records/{source}" if source else "records") if name.endswith(".jsonl")]


def parse(name: str, data: bytes) -> Iterator[Record]:
    for number, raw in enumerate(data.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            yield Record.model_validate(decode(raw))
        except ValidationError as error:
            raise Error(f"{name}:{number}: invalid record: {explain(error)}") from error
        except Error as error:
            raise Error(f"{name}:{number}: {error}") from error


def load(store: Store, name: str) -> list[Record]:
    return list(parse(name, store.read(name, MAX_PARTITION)))


def source_of(name: str) -> str:
    return name.split("/")[1]


def upsert(store: Store, source: str, incoming: list[Record], *, snapshot: bool) -> dict[str, int]:
    """Window sources add or replace items by id; snapshot sources replace their complete catalog.

    Each id appears once per source, in the partition of its event month, so a rescheduled
    event moves between partitions instead of leaving a stale copy behind.
    """
    if not incoming and not snapshot:
        return {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    existing: dict[str, dict[str, Record]] = {}
    for name in partitions(store, source):
        existing[name.rsplit("/", 1)[1].removesuffix(".jsonl")] = {r.id: r for r in load(store, name)}
    located = {record_id: key for key, records in existing.items() for record_id in records}
    before = {key: dict(records) for key, records in existing.items()}
    counts = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    if snapshot:
        wanted = {r.id for r in incoming}
        counts["removed"] = len(located.keys() - wanted)
        existing = {SNAPSHOT: {}}
    for record in incoming:
        key = SNAPSHOT if snapshot else partition(record)
        previous = before.get(located.get(record.id, ""), {}).get(record.id)
        if previous is None:
            counts["added"] += 1
        elif previous == record:
            counts["unchanged"] += 1
        else:
            counts["updated"] += 1
        if not snapshot and located.get(record.id, key) != key:
            existing[located[record.id]].pop(record.id)
        existing.setdefault(key, {})[record.id] = record
    for key in sorted(before.keys() | existing.keys()):
        records = existing.get(key, {})
        if records == before.get(key, {}):
            continue
        if records:
            ordered = sorted(records.values(), key=lambda r: (r.time, r.id))
            store.write(path(source, key), b"".join(line(r) for r in ordered))
        else:
            store.delete(path(source, key))
    return counts


def find(store: Store, source: str, record_id: str) -> tuple[str, Record] | None:
    """Scan one source directly: exact record reads never depend on the derived index."""
    if not re.fullmatch(NAME, source):
        return None
    for name in sorted(partitions(store, source), reverse=True):
        for record in load(store, name):
            if record.id == record_id:
                return name, record
    return None
