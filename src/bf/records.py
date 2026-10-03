"""Records live in independent JSON files keyed by the SHA-256 of their source item id."""

from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from bf.models import MAX_FILES, MAX_RECORD, NAME, RECORD_KEYS, Error, Model, Record, decode, digest, encode, explain
from bf.storage import BusyError, Store, reader

_PENDING = "memories/.pending"
_MANIFEST = _PENDING + "/manifest.json"
_MANIFEST_LIMIT = 16 << 20
# Link failures of filesystems or mounts without hard links: backups and restores copy the bytes instead.
_UNLINKABLE = frozenset({errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})
# Seconds an exact read waits for a writer before reading one whole file without the brain lock.
WAIT = 3


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
        # An excluded or unreadable memories root cannot contain an accessible transaction. Retrieval reports the
        # root through its scan; a linked or unreadable .pending inside a readable root still fails closed.
        if not stat.S_ISDIR(store.mode("memories")):
            return False
        try:
            store.mode(_PENDING)
        except PermissionError:
            return False
        with store.parent(_MANIFEST):
            return True
    except FileNotFoundError:
        return False


def interrupted(store: Store) -> bool:
    """Whether a pending journal may describe half-changed records.

    A completed manifest proves every record was committed or restored: only cleanup remains, which the next writer
    finishes, so reads need not wait for it. A missing or invalid manifest proves nothing.
    """
    if not _pending(store):
        return False
    try:
        return not _Journal.model_validate(decode(store.read(_MANIFEST, _MANIFEST_LIMIT))).complete
    except FileNotFoundError, Error, ValidationError:
        return True


def _completed(store: Store) -> bool:
    """Whether the pending manifest on disk marks its commit complete."""
    try:
        return _Journal.model_validate(decode(store.read(_MANIFEST, _MANIFEST_LIMIT))).complete
    except FileNotFoundError, Error, ValidationError:
        return False


def _clear(store: Store) -> None:
    names = store.files(_PENDING)
    if any(
        not re.fullmatch(r"memories/\.pending/(?:manifest\.json|[0-9]+\.before|\.write-[0-9a-f]{32})", n) for n in names
    ):
        raise Error("unexpected file in memories/.pending; preserve it and repair the pending transaction")
    # The completion marker remains until every staged file is gone. Removing the directory syncs its
    # parent once: a crash before then leaves staged files that the next recovery clears again.
    for name in sorted(names, key=lambda name: name == _MANIFEST):
        store.delete(name, durable=False)
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
    directory = f"memories/{journal.source}"
    if not journal.complete:
        # Check all backups before restoring any source file.
        for number, change in enumerate(journal.changes):
            if change.existed and store.lstat(f"{_PENDING}/{number}.before").st_size > MAX_RECORD:
                raise Error(f"{_PENDING}/{number}.before: file exceeds {MAX_RECORD} bytes")
        # Only files the commit reached change back, as their backups were made; one directory sync then makes
        # every restored entry durable before the completion marker.
        for number, change in enumerate(journal.changes):
            target = f"{directory}/{change.name}"
            if not change.existed:
                with suppress(FileNotFoundError):
                    store.delete(target, durable=False)
            elif not _kept(store, target, f"{_PENDING}/{number}.before"):
                _copy(store, f"{_PENDING}/{number}.before", target)
        with suppress(FileNotFoundError):
            # A commit interrupted before creating its source directory changed nothing there.
            store.sync(directory)
        journal.complete = True
        store.write(_MANIFEST, encode(journal.model_dump()))
    store.sweep(directory)
    _clear(store)


def _kept(store: Store, target: str, backup: str) -> bool:
    """Whether a commit left a file as its backup holds it: the same inode, or the same bytes for a copied backup."""
    try:
        current = store.lstat(target)
    except FileNotFoundError:
        return False
    kept = store.lstat(backup)
    if os.path.samestat(current, kept):
        return True
    return current.st_size == kept.st_size and store.read(target, MAX_RECORD) == store.read(backup, MAX_RECORD)


def _copy(store: Store, name: str, target: str) -> None:
    """Give `target` the bytes of `name`: a hard link where the filesystem allows one, otherwise a synced copy."""
    try:
        store.link(name, target)
    except OSError as error:
        if error.errno not in _UNLINKABLE:
            raise
        store.write(target, store.read(name, MAX_RECORD), durable=False)


def require_ready(store: Store) -> None:
    """Keep read operations from applying an untrusted on-disk recovery journal."""
    if interrupted(store):
        raise Error(
            "records contain an interrupted transaction; run bf build or bf update to recover it before reading"
        )


@contextmanager
def reading(store: Store) -> Iterator[None]:
    """Read a consistent set of records without changing durable evidence."""
    with reader(store):
        require_ready(store)
        yield


def _commit(store: Store, source: str, replacements: dict[str, bytes | None]) -> None:
    if not replacements:
        return
    # A commit's backups and its manifest share memories/.pending, which the scan limit bounds like any tree.
    if len(replacements) >= MAX_FILES:
        # A failed operation, like any other collection failure: history records it and evidence stays as it was.
        raise Error(
            f"one collection may change at most {MAX_FILES - 1:,} records; narrow the window or split the source"
        )
    changes = []
    for name in replacements:
        try:
            store.fingerprint(name)
            existed = True
        except FileNotFoundError:
            existed = False
        changes.append(_Change(name=name.removeprefix(f"memories/{source}/"), existed=existed))
    # A source past the scan limit would fail every search and read of the brain, so collection stops below it.
    tree, entries = f"memories/{source}", {}
    store.scan(tree, skipped={}, counts=entries, fingerprints=False)
    grown = sum(
        (data is not None and not change.existed) - (data is None and change.existed)
        for change, data in zip(changes, replacements.values(), strict=True)
    )
    if entries.get(tree, 0) + grown > MAX_FILES:
        raise Error(
            f"{tree} would exceed the {MAX_FILES:,}-entry scan limit; split its sensor into several sources or "
            "archive older records outside the brain"
        )
    journal = _Journal(source=source, changes=changes)
    manifest = encode(journal.model_dump())
    if len(manifest) > _MANIFEST_LIMIT:
        raise Error("record transaction exceeds its manifest limit")
    try:
        # Each file's bytes are synced as it is written; one sync per directory then makes its entries
        # durable. Backups are durable before the manifest names them, and records before completion. A backup
        # links the replaced file's inode, whose bytes a commit never changes, instead of copying and syncing them.
        backups = False
        for number, (name, change) in enumerate(zip(replacements, changes, strict=True)):
            if change.existed:
                _copy(store, name, f"{_PENDING}/{number}.before")
                backups = True
        if backups:
            store.sync(_PENDING)
        store.write(_MANIFEST, manifest)
        for name, data in replacements.items():
            if data is None:
                store.delete(name, durable=False)
            else:
                store.write(name, data, durable=False)
        store.sync(f"memories/{source}")
        journal.complete = True
        store.write(_MANIFEST, encode(journal.model_dump()))
    except BaseException as error:
        # The completion marker may land before its directory sync fails: the commit then succeeded.
        landed = journal.complete and _completed(store)
        try:
            recover(store)
        except (Error, OSError) as recovery_error:
            raise Error("record transaction needs recovery; preserve memories/.pending and retry") from recovery_error
        if not (landed and isinstance(error, Error | OSError)):
            raise
        return
    # A cleanup failure cannot turn a durable successful commit into a failed collection; recovery clears it later.
    with suppress(Error, OSError):
        _clear(store)


def path(source: str, record_id: str) -> str:
    """Stable identity, independent of event time, provider ordering and collection mode."""
    return f"memories/{source}/{digest(record_id.encode())}.json"


def serialize(record: Record) -> bytes:
    return encode(record.model_dump(exclude_defaults=True))


def stored(name: str) -> bool:
    """Whether collection, search and `bf validate` read a memories/ file as a record, even a hidden or misnamed one."""
    return name.endswith(".json") and not name.startswith(_PENDING + "/")


def files(store: Store, source: str = "", *, skipped: dict[str, tuple[int, int, int, int]] | None = None) -> list[str]:
    """Record files below memories/, each source bounded on its own; `bf validate` also reports other visible files.

    Callers hold a brain lock, so no writer changes records meanwhile: the listing types them without a stat each.
    """
    directory = f"memories/{source}" if source else "memories"
    listed = store.scan(directory, skipped=skipped, split=not source, fingerprints=False)
    return sorted(name for name in listed if stored(name))


def load(store: Store, name: str) -> Record:
    """The one record a file holds; its SHA-256 filename must match its id."""
    return parse(name, store.read(name, MAX_RECORD))


def parse(name: str, data: bytes) -> Record:
    """The record in one file's bytes, as `load` reads it."""
    source = source_of(name)
    try:
        record = Record.model_validate(decode(data))
    except ValidationError as error:
        raise Error(f"{name}: invalid record: {explain(error, RECORD_KEYS)}") from error
    except Error as error:
        raise Error(f"{name}: {error}") from error
    if path(source, record.id) != name:
        raise Error(f"{name}: record id does not match its SHA-256 filename; run bf validate and reconcile it")
    return record


def _stored(store: Store, name: str) -> Record | None:
    """The record a change replaces or removes; None when the file holds exactly this name's id but breaks the
    current record rules, such as a display-name alias or an over-long title.

    Its SHA-256 name proves the file belongs to this record, so collection may replace or remove it; misnamed,
    unparsable or unreadable evidence still fails the change. A missing file raises FileNotFoundError.
    """
    try:
        return load(store, name)
    except FileNotFoundError:
        raise
    except Error:
        try:
            value = decode(store.read(name, MAX_RECORD))
        except Error:
            value = None
        identifier = value.get("id") if isinstance(value, dict) else None
        if isinstance(identifier, str) and path(source_of(name), identifier) == name:
            return None
        raise


def source_of(name: str) -> str:
    parts = name.split("/")
    if (
        len(parts) != 3
        or parts[0] != "memories"
        or not re.fullmatch(NAME, parts[1])
        or not re.fullmatch(r"[0-9a-f]{64}\.json", parts[2])
    ):
        raise Error(f"{name}: expected memories/<source>/<sha256-id>.json")
    return parts[1]


def upsert(store: Store, source: str, incoming: list[Record], *, snapshot: bool) -> dict[str, int]:
    """Update independent record files; snapshot removal and all replacements remain transactional.

    Only files a change replaces or removes are parsed, and each must hold its own id: collection never
    overwrites or deletes misnamed evidence, while a file with its own id that breaks the current record
    rules is replaced like any update. `bf validate` and the search cache check every other file.
    """
    recover(store)
    if not re.fullmatch(NAME, source):
        raise Error("invalid record source")
    # Only writers holding the brain lock write records: a temporary file there belongs to a killed write.
    store.sweep(f"memories/{source}")
    wanted = {path(source, record.id): record for record in incoming}
    if len(wanted) != len(incoming):
        raise Error("duplicate incoming record ids; reconcile them before collecting")
    counts = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    replacements: dict[str, bytes | None] = {}
    # A window leaves other files alone; a snapshot lists its catalog without parsing what it keeps.
    for name in files(store, source) if snapshot else ():
        if name not in wanted:
            _stored(store, name)
            replacements[name] = None
            counts["removed"] += 1
    for name, record in wanted.items():
        existed = True
        try:
            previous = _stored(store, name)
        except FileNotFoundError:
            previous, existed = None, False
        reliable = previous and not (previous.observed and previous.updated > previous.observed)
        if reliable and previous.updated and record.updated and previous.updated > record.updated:
            record = previous
        if previous is None:
            counts["updated" if existed else "added"] += 1
        elif previous.model_dump(exclude={"attributes": {"observed"}}) == record.model_dump(
            exclude={"attributes": {"observed"}}
        ):
            counts["unchanged"] += 1
            if previous.observed:
                record = previous
        else:
            counts["updated"] += 1
        if record != previous:
            data = serialize(record)
            if len(data) > MAX_RECORD:
                raise Error(f"{name}: record exceeds its {MAX_RECORD}-byte limit")
            replacements[name] = data
    _commit(store, source, replacements)
    return counts


def find(
    store: Store, source: str, record_id: str, *, complete: bool = False
) -> tuple[str, Record, bytes, int, bool] | None:
    """Exact identities resolve directly by their SHA-256 filename, with the file's bytes and modification time.

    Malformed or misnamed evidence never proves absence: a missing file is confirmed by parsing the source,
    unless `complete` states that a ready search cache has already parsed all of it without a problem.
    The last value is true when a writer kept the brain lock past WAIT seconds: the file was then read whole
    without it, since writes rename complete files into place, but it may be changing, and absence fails instead.
    """
    if not re.fullmatch(NAME, source):
        return None
    try:
        if not stat.S_ISDIR(store.mode(f"memories/{source}")):
            raise Error(f"source {source} is unreadable; run bf validate")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise Error(f"source {source} is unreadable; run bf validate") from error
    name = path(source, record_id)
    with ExitStack() as locked:
        try:
            locked.enter_context(reader(store, wait=WAIT))
        except BusyError:
            try:
                data, modified = store.stamped(name, MAX_RECORD)
            except FileNotFoundError:
                raise BusyError("another writer is changing this brain's records; retry the read shortly") from None
            return name, parse(name, data), data, modified, True
        require_ready(store)
        try:
            data, modified = store.stamped(name, MAX_RECORD)
            return name, parse(name, data), data, modified, False
        except FileNotFoundError:
            if complete:
                return None
            skipped: dict[str, tuple[int, int, int, int]] = {}
            for other in files(store, source, skipped=skipped):
                load(store, other)
            if skipped:
                raise Error(
                    f"source {source} has unreadable records; run bf validate before concluding a record is absent"
                ) from None
            return None
