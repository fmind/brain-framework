"""Records live in independent JSON files keyed by the SHA-256 of their source item id."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from bf.models import MAX_FILES, MAX_RECORD, NAME, Error, Model, Record, decode, digest, encode, explain
from bf.storage import Store, reader

_PENDING = "memories/.pending"
_MANIFEST = _PENDING + "/manifest.json"
_MANIFEST_LIMIT = 16 << 20


class _Change(Model):
    name: Annotated[str, Field(pattern=r"^[0-9a-f]{64}\.json$")]
    existed: bool


class _Journal(Model):
    version: Literal[1] = 1
    source: Annotated[str, Field(pattern=NAME)]
    complete: bool = False
    changes: Annotated[list[_Change], Field(max_length=MAX_FILES)]


def _pending(store: Store) -> bool:
    try:
        # An excluded memories root cannot contain an accessible transaction. Retrieval reports
        # the root through its scan; a linked .pending inside a real root still fails closed.
        with store.parent("memories") as (parent, leaf):
            if not stat.S_ISDIR(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
                return False
        with store.parent(_MANIFEST):
            return True
    except FileNotFoundError:
        return False


def _clear(store: Store) -> None:
    names = store.files(_PENDING)
    if any(
        not re.fullmatch(r"memories/\.pending/(?:manifest\.json|[0-9]+\.before|\.write-[0-9a-f]{32})", n) for n in names
    ):
        raise Error("unexpected file in memories/.pending; preserve it and repair the pending transaction")
    # The completion marker remains until every staged file is gone.
    for name in sorted(names, key=lambda name: name == _MANIFEST):
        store.delete(name)
    with suppress(FileNotFoundError):
        store.rmdir(_PENDING)


def recover(store: Store) -> None:
    """Roll back an interrupted commit, under the caller's exclusive brain writer lock.

    Originals and the manifest are durable evidence under memories/, never disposable state.
    A completion marker means all records were committed (or restored) and only cleanup remains.
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
        raise Error("invalid memories/.pending manifest; preserve it and repair the pending transaction") from error
    if len({change.name for change in journal.changes}) != len(journal.changes):
        raise Error("duplicate paths in memories/.pending manifest")
    if not journal.complete:
        # Check all backups before restoring any source file.
        for number, change in enumerate(journal.changes):
            if change.existed:
                store.read(f"{_PENDING}/{number}.before", MAX_RECORD)
        for number, change in enumerate(journal.changes):
            target = f"memories/{journal.source}/{change.name}"
            if change.existed:
                store.write(target, store.read(f"{_PENDING}/{number}.before", MAX_RECORD))
            else:
                with suppress(FileNotFoundError):
                    store.delete(target)
        journal.complete = True
        store.write(_MANIFEST, encode(journal.model_dump()))
    _clear(store)


def require_ready(store: Store) -> None:
    """Keep read operations from applying an untrusted on-disk recovery journal."""
    if _pending(store):
        raise Error("records contain an interrupted transaction; run bf build to recover it before reading")


@contextmanager
def reading(store: Store) -> Iterator[None]:
    """Read a consistent set of records without changing durable evidence."""
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
        changes.append(_Change(name=name.removeprefix(f"memories/{source}/"), existed=existed))
    journal = _Journal(source=source, changes=changes)
    manifest = encode(journal.model_dump())
    if len(manifest) > _MANIFEST_LIMIT:
        raise Error("record transaction exceeds its manifest limit")
    try:
        for number, (name, change) in enumerate(zip(replacements, changes, strict=True)):
            if change.existed:
                store.write(f"{_PENDING}/{number}.before", store.read(name, MAX_RECORD))
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
            raise Error("record transaction needs recovery; preserve memories/.pending and retry") from recovery_error
        raise error
    # A cleanup failure cannot turn a durable successful commit into a failed collection.
    with suppress(OSError):
        _clear(store)


def path(source: str, record_id: str) -> str:
    """Stable identity, independent of event time, provider ordering and collection mode."""
    return f"memories/{source}/{digest(record_id.encode())}.json"


def line(record: Record) -> bytes:
    return encode(record.model_dump(exclude_defaults=True))


def files(store: Store, source: str = "", *, skipped: dict[str, tuple[int, int, int, int]] | None = None) -> list[str]:
    """Include obsolete JSONL files so an unconverted brain fails visibly instead of losing evidence."""
    directory = f"memories/{source}" if source else "memories"
    return [
        name
        for name in store.files(directory, skipped=skipped)
        if name.endswith((".json", ".jsonl")) and not name.startswith(_PENDING + "/")
    ]


def load(store: Store, name: str) -> list[Record]:
    source = source_of(name)
    try:
        record = Record.model_validate(decode(store.read(name, MAX_RECORD)))
    except ValidationError as error:
        raise Error(f"{name}: invalid record: {explain(error, {*Record.model_fields, '[key]'})}") from error
    except Error as error:
        raise Error(f"{name}: {error}") from error
    if path(source, record.id) != name:
        raise Error(f"{name}: record id does not match its SHA-256 filename; run bf validate and reconcile it")
    return [record]


def source_of(name: str) -> str:
    parts = name.split("/")
    if name.endswith(".jsonl"):
        raise Error(f"{name}: obsolete JSON Lines storage; follow the manual upgrade before collecting")
    if (
        len(parts) != 3
        or parts[0] != "memories"
        or not re.fullmatch(NAME, parts[1])
        or not re.fullmatch(r"[0-9a-f]{64}\.json", parts[2])
    ):
        raise Error(f"{name}: expected memories/<source>/<sha256-id>.json")
    return parts[1]


def upsert(store: Store, source: str, incoming: list[Record], *, snapshot: bool) -> dict[str, int]:
    """Update independent record files; snapshot removal and all replacements remain transactional."""
    recover(store)
    if not re.fullmatch(NAME, source):
        raise Error("invalid record source")
    wanted = {record.id for record in incoming}
    if len(wanted) != len(incoming):
        raise Error("duplicate incoming record ids; reconcile them before collecting")
    existing: dict[str, Record] = {}
    for name in files(store, source):
        record = load(store, name)[0]
        existing[record.id] = record
    counts = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    replacements: dict[str, bytes | None] = {}
    if snapshot:
        for record_id in existing.keys() - wanted:
            replacements[path(source, record_id)] = None
            counts["removed"] += 1
    for record in incoming:
        previous = existing.get(record.id)
        reliable = previous and not (previous.observed and previous.updated > previous.observed)
        if reliable and previous.updated and record.updated and previous.updated > record.updated:
            record = previous
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
        if record != previous:
            data = line(record)
            if len(data) > MAX_RECORD:
                raise Error(f"{path(source, record.id)}: record exceeds its {MAX_RECORD}-byte limit")
            replacements[path(source, record.id)] = data
    _commit(store, source, replacements)
    return counts


def find(store: Store, source: str, record_id: str) -> tuple[str, Record] | None:
    """Exact identities resolve directly, without depending on cache hints or scanning other records."""
    if not re.fullmatch(NAME, source):
        return None
    try:
        with store.parent(f"memories/{source}") as (parent, leaf):
            info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise Error(f"source {source} is unreadable; run bf validate")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise Error(f"source {source} is unreadable; run bf validate") from error
    with reading(store):
        name = path(source, record_id)
        try:
            return name, load(store, name)[0]
        except FileNotFoundError:
            # Do not turn malformed or unconverted evidence into proof of absence.
            skipped: dict[str, tuple[int, int, int, int]] = {}
            for other in files(store, source, skipped=skipped):
                load(store, other)
            if skipped:
                raise Error(
                    f"source {source} has unreadable records; run bf validate before concluding a record is absent"
                ) from None
            return None
