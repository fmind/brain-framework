"""Records live in monthly JSON Lines partitions: one line per source item, upserted by id."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import date
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from fkf.models import MAX_PARTITION, NAME, Error, Model, Record, decode, encode, explain
from fkf.storage import Store, reader

SNAPSHOT = "snapshot"
UNDATED = "undated"
_PENDING = "records/.pending"
_MANIFEST = _PENDING + "/manifest.json"
_MANIFEST_LIMIT = 1 << 20


class _Change(Model):
    name: Annotated[str, Field(pattern=r"^(?:[0-9]{4}-[0-9]{2}|snapshot|undated)\.jsonl$")]
    existed: bool


class _Journal(Model):
    version: Literal[1] = 1
    source: Annotated[str, Field(pattern=NAME)]
    complete: bool = False
    changes: Annotated[list[_Change], Field(max_length=10_000)]


def _pending(store: Store) -> bool:
    try:
        with store.parent(_MANIFEST):
            return True
    except FileNotFoundError:
        return False


def _clear(store: Store) -> None:
    names = store.files(_PENDING)
    if any(
        not re.fullmatch(r"records/\.pending/(?:manifest\.json|[0-9]+\.before|\.write-[0-9a-f]{32})", n) for n in names
    ):
        raise Error("unexpected file in records/.pending; preserve it and repair the pending transaction")
    # The completion marker remains until every staged file is gone.
    for name in sorted(names, key=lambda name: name == _MANIFEST):
        store.delete(name)
    with suppress(FileNotFoundError):
        store.rmdir(_PENDING)


def recover(store: Store) -> None:
    """Roll back an interrupted commit, under the caller's exclusive base writer lock.

    Originals and the manifest are durable evidence under records/, never disposable state.
    A completion marker means all partitions were committed (or restored) and only cleanup remains.
    """
    if not _pending(store):
        return
    try:
        raw = store.read(_MANIFEST, _MANIFEST_LIMIT)
    except FileNotFoundError:
        # Preparation never changes a source before its manifest is durable.
        _clear(store)
        return
    try:
        journal = _Journal.model_validate(decode(raw))
    except ValidationError as error:
        raise Error("invalid records/.pending manifest; preserve it and repair the pending transaction") from error
    if len({change.name for change in journal.changes}) != len(journal.changes):
        raise Error("duplicate paths in records/.pending manifest")
    if not journal.complete:
        # Check all backups before restoring any source file.
        for number, change in enumerate(journal.changes):
            if change.existed:
                store.read(f"{_PENDING}/{number}.before", MAX_PARTITION)
        for number, change in enumerate(journal.changes):
            target = f"records/{journal.source}/{change.name}"
            if change.existed:
                store.write(target, store.read(f"{_PENDING}/{number}.before", MAX_PARTITION))
            else:
                with suppress(FileNotFoundError):
                    store.delete(target)
        journal.complete = True
        store.write(_MANIFEST, encode(journal.model_dump()))
    _clear(store)


def require_ready(store: Store) -> None:
    """Keep read operations from applying an untrusted on-disk recovery journal."""
    if _pending(store):
        raise Error("records contain an interrupted transaction; run fkf build to recover it before reading")


@contextmanager
def reading(store: Store) -> Iterator[None]:
    """Read a consistent set of partitions without changing durable evidence."""
    with reader(store):
        require_ready(store)
        yield


def _commit(store: Store, source: str, replacements: dict[str, bytes | None]) -> None:
    if not replacements:
        return
    changes = []
    for name in replacements:
        try:
            store.fingerprint(name)
            existed = True
        except FileNotFoundError:
            existed = False
        changes.append(_Change(name=name.removeprefix(f"records/{source}/"), existed=existed))
    journal = _Journal(source=source, changes=changes)
    manifest = encode(journal.model_dump())
    if len(manifest) > _MANIFEST_LIMIT:
        raise Error("record transaction exceeds its manifest limit")
    try:
        for number, (name, change) in enumerate(zip(replacements, changes, strict=True)):
            if change.existed:
                store.write(f"{_PENDING}/{number}.before", store.read(name, MAX_PARTITION))
        store.write(_MANIFEST, manifest)
        for name, data in replacements.items():
            if data is None:
                store.delete(name)
            else:
                store.write(name, data)
        journal.complete = True
        store.write(_MANIFEST, encode(journal.model_dump()))
    except BaseException as error:
        try:
            recover(store)
        except (Error, OSError) as recovery_error:
            raise Error("record transaction needs recovery; preserve records/.pending and retry") from recovery_error
        raise error
    # A cleanup failure cannot turn a durable successful commit into a failed collection.
    with suppress(OSError):
        _clear(store)


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
    source_of(name)
    return list(parse(name, store.read(name, MAX_PARTITION)))


def source_of(name: str) -> str:
    parts = name.split("/")
    if len(parts) != 3 or parts[0] != "records" or not re.fullmatch(NAME, parts[1]):
        raise Error(f"{name}: expected records/<source>/<YYYY-MM|undated|snapshot>.jsonl")
    filename = parts[2]
    if filename not in {"undated.jsonl", "snapshot.jsonl"}:
        try:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}\.jsonl", filename):
                raise ValueError
            date.fromisoformat(filename.removesuffix(".jsonl") + "-01")
        except ValueError:
            raise Error(f"{name}: expected a YYYY-MM, undated or snapshot JSON Lines partition") from None
    return parts[1]


def upsert(store: Store, source: str, incoming: list[Record], *, snapshot: bool) -> dict[str, int]:
    """Window sources add or replace items by id; snapshot sources replace their complete catalog.

    Each id appears once per source, in the partition of its event month, so a rescheduled
    event moves between partitions instead of leaving a stale copy behind.
    """
    recover(store)
    if not re.fullmatch(NAME, source):
        raise Error("invalid record source")
    if not incoming and not snapshot:
        return {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    existing: dict[str, dict[str, Record]] = {}
    located: dict[str, str] = {}
    for name in partitions(store, source):
        key = name.rsplit("/", 1)[1].removesuffix(".jsonl")
        partition_records: dict[str, Record] = {}
        for record in load(store, name):
            if record.id in located:
                raise Error(f"source {source} contains duplicate record ids; run fkf validate and reconcile them")
            located[record.id] = key
            partition_records[record.id] = record
        existing[key] = partition_records
    before = {key: dict(records) for key, records in existing.items()}
    counts = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    if snapshot:
        wanted = {r.id for r in incoming}
        counts["removed"] = len(located.keys() - wanted)
        existing = {SNAPSHOT: {}}
    for record in incoming:
        key = SNAPSHOT if snapshot else partition(record)
        previous = before.get(located.get(record.id, ""), {}).get(record.id)
        if previous and previous.updated and record.updated and previous.updated > record.updated:
            # Historical backfills must not replace a known newer upstream revision.
            record = previous
            key = SNAPSHOT if snapshot else partition(record)
        if previous is None:
            counts["added"] += 1
        elif previous.model_dump(exclude={"attributes": {"observed"}}) == record.model_dump(
            exclude={"attributes": {"observed"}}
        ):
            counts["unchanged"] += 1
            if previous.observed:
                record = previous
        else:
            counts["updated"] += 1
        if not snapshot and located.get(record.id, key) != key:
            existing[located[record.id]].pop(record.id)
        existing.setdefault(key, {})[record.id] = record
    replacements: dict[str, bytes | None] = {}
    for key in sorted(before.keys() | existing.keys()):
        records = existing.get(key, {})
        if records == before.get(key, {}):
            continue
        if records:
            ordered = sorted(records.values(), key=lambda r: (r.time, r.id))
            data = b"".join(line(r) for r in ordered)
            if len(data) > MAX_PARTITION:
                raise Error(f"{path(source, key)}: record partition exceeds its {MAX_PARTITION}-byte limit")
            replacements[path(source, key)] = data
        else:
            replacements[path(source, key)] = None
    _commit(store, source, replacements)
    return counts


def find(store: Store, source: str, record_id: str) -> tuple[str, Record] | None:
    """Scan one source directly: exact record reads never depend on the derived index."""
    if not re.fullmatch(NAME, source):
        return None
    unreadable = False
    with reading(store):
        for name in sorted(partitions(store, source), reverse=True):
            try:
                items = load(store, name)
            except Error, OSError, UnicodeError:
                # Match indexing's file boundary: one malformed partition must not hide other evidence.
                unreadable = True
                continue
            for record in items:
                if record.id == record_id:
                    return name, record
    if unreadable:
        raise Error(f"source {source} has unreadable partitions; run fkf validate before concluding a record is absent")
    return None
