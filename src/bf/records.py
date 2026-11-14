"""Records live in independent JSON files keyed by the SHA-256 of their source item id."""

from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from bf.models import (
    MAX_FILES,
    MAX_RECORD,
    NAME,
    RECORD_KEYS,
    RESERVED,
    Error,
    Model,
    Record,
    decode,
    digest,
    encode,
    explain,
)
from bf.storage import BusyError, Store, reader

_PENDING = "memories/.pending"
_MANIFEST = _PENDING + "/manifest.json"
_MANIFEST_LIMIT = 16 << 20
# Cross-device or unsupported hard links fall back to synced copies.
_UNLINKABLE = frozenset({errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})
WAIT = 3
FORGOTTEN = ".forgotten"
_FORGOTTEN_LIMIT = 8 << 20
_DIGEST = re.compile(r"[0-9a-f]{64}")


class _Change(Model):
    name: Annotated[str, Field(pattern=r"^[0-9a-f]{64}\.json$")]
    existed: bool


class _Journal(Model):
    version: Literal[1] = 1
    source: Annotated[str, Field(pattern=NAME)]
    complete: bool = False
    changes: Annotated[list[_Change], Field(max_length=MAX_FILES)]


def pending(store: Store) -> bool:
    try:
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
    """Whether an incomplete pending journal may leave half-changed records."""
    return pending(store) and not _completed(store)


def _completed(store: Store) -> bool:
    try:
        return _Journal.model_validate(decode(store.read(_MANIFEST, _MANIFEST_LIMIT))).complete
    except FileNotFoundError, Error, ValidationError:
        return False


def _clear(store: Store) -> None:
    store.sweep(_PENDING)
    names = store.files(_PENDING)
    pattern = re.compile(r"memories/\.pending/(?:manifest\.json|[0-9]+\.before|\.write-[0-9a-f]{32})")
    if unexpected := next((name for name in names if not pattern.fullmatch(name)), ""):
        raise Error(
            f"{unexpected}: unexpected file in memories/.pending; preserve it and repair the pending transaction"
        )
    # Remove manifest last so recovery re-runs cleanly after an interrupted cleanup.
    for name in sorted(names, key=lambda name: name == _MANIFEST):
        store.delete(name, durable=False)
    with suppress(FileNotFoundError):
        store.rmdir(_PENDING)


def recover(store: Store) -> None:
    """Roll back an interrupted commit under the exclusive brain writer lock."""
    if not pending(store):
        return
    try:
        raw = store.read(_MANIFEST, _MANIFEST_LIMIT)
    except FileNotFoundError:
        _clear(store)
        return
    try:
        journal = _Journal.model_validate(decode(raw))
    except (Error, ValidationError) as error:
        raise Error("invalid memories/.pending manifest; preserve it and repair the pending transaction") from error
    if len({change.name for change in journal.changes}) != len(journal.changes):
        raise Error("duplicate paths in memories/.pending manifest")
    directory = f"memories/{journal.source}"
    if not journal.complete:
        for number, change in enumerate(journal.changes):
            if not change.existed:
                continue
            backup = f"{_PENDING}/{number}.before"
            try:
                size = store.lstat(backup).st_size
            except FileNotFoundError:
                raise Error(
                    f"{backup} is missing; preserve memories/.pending and repair the pending transaction"
                ) from None
            if size > MAX_RECORD:
                raise Error(f"{backup}: file exceeds {MAX_RECORD} bytes")
        for number, change in enumerate(journal.changes):
            target = f"{directory}/{change.name}"
            if not change.existed:
                with suppress(FileNotFoundError):
                    store.delete(target, durable=False)
            elif not _kept(store, target, f"{_PENDING}/{number}.before"):
                _copy(store, f"{_PENDING}/{number}.before", target)
        with suppress(FileNotFoundError):
            store.sync(directory)
        journal.complete = True
        store.write(_MANIFEST, encode(journal.model_dump()))
    store.sweep(directory)
    _clear(store)


def _kept(store: Store, target: str, backup: str) -> bool:
    """Whether `target` matches `backup` by inode or bytes."""
    try:
        current = store.lstat(target)
    except FileNotFoundError:
        return False
    kept = store.lstat(backup)
    if os.path.samestat(current, kept):
        return True
    return current.st_size == kept.st_size and store.read(target, MAX_RECORD) == store.read(backup, MAX_RECORD)


def _copy(store: Store, name: str, target: str) -> None:
    """Give `target` the bytes of `name` via hard link or synced copy fallback."""
    try:
        store.link(name, target)
    except OSError as error:
        if error.errno not in _UNLINKABLE:
            raise
        store.write(target, store.read(name, MAX_RECORD), durable=False)


def require_ready(store: Store) -> None:
    """Keep reads from acting on an interrupted transaction before recovery."""
    if interrupted(store):
        raise Error(
            "records contain an interrupted transaction; run bf build or bf update to recover it before reading"
        )


@contextmanager
def reading(store: Store) -> Generator[None]:
    """Read a consistent set of records without changing durable evidence."""
    with reader(store):
        require_ready(store)
        yield


def _commit(store: Store, source: str, replacements: dict[str, bytes | None]) -> None:
    if not replacements:
        return
    if len(replacements) >= MAX_FILES:
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
    try:
        # Persist backups and manifest before mutating source files, then mark complete before cleanup.
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
        landed = journal.complete and _completed(store)
        try:
            recover(store)
        except (Error, OSError) as recovery_error:
            raise Error("record transaction needs recovery; preserve memories/.pending and retry") from recovery_error
        if not (landed and isinstance(error, Error | OSError)):
            raise
        return
    with suppress(Error, OSError):
        _clear(store)


def path(source: str, record_id: str) -> str:
    """Stable identity, independent of event time, provider ordering and collection mode."""
    return f"memories/{source}/{digest(record_id.encode())}.json"


def serialize(record: Record) -> bytes:
    return encode(record.model_dump(exclude_defaults=True))


def stored(name: str) -> bool:
    """Whether collection, search and `bf validate` read a memories/ file as a record."""
    return name.endswith(".json") and not name.startswith(_PENDING + "/")


def files(store: Store, source: str = "", *, skipped: dict[str, tuple[int, int, int, int]] | None = None) -> list[str]:
    """Record files below memories/, each source bounded on its own (caller holds brain lock)."""
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
    """Replaced/removed record; None if unparsable under current rules yet its SHA-256 filename matches `id`."""
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
    if parts[1] == RESERVED:
        raise Error(
            f"{name}: source {RESERVED} is reserved for BF addresses; move memories/{RESERVED}/ to the folder of its "
            "renamed sensor or assistant"
        )
    return parts[1]


def upsert(
    store: Store, source: str, incoming: list[Record], *, snapshot: bool, removals: list[str] | None = None
) -> dict[str, int]:
    """Transactionally apply replacements, snapshot removals, and `removals` under the writer lock."""
    recover(store)
    if not re.fullmatch(NAME, source):
        raise Error("invalid record source")
    store.sweep(f"memories/{source}")
    counts, replacements = plan(store, source, incoming, snapshot=snapshot)
    for name in removals or ():
        if name in replacements:
            counts["removed"] -= 1
        replacements[name] = None
    _commit(store, source, replacements)
    return counts


def prune(store: Store, source: str, keep: int, ids: re.Pattern[str], *, newest: str = "") -> int:
    """Remove a source's oldest matching records beyond the newest `keep`, under the writer lock."""
    recover(store)
    names = files(store, source)
    if len(names) <= keep:
        return 0
    loaded = []
    for name in names:
        try:
            record = load(store, name)
        except Error, OSError:
            continue
        if ids.fullmatch(record.id):
            loaded.append((record.id == newest, record.time or record.observed, record.id, name))
    doomed = [name for *_, name in sorted(loaded)[: max(0, len(loaded) - keep)]]
    if not doomed:
        return 0
    _commit(store, source, dict.fromkeys(doomed))
    return len(doomed)


def plan(
    store: Store, source: str, incoming: list[Record], *, snapshot: bool
) -> tuple[dict[str, int], dict[str, bytes | None]]:
    """Compute counts and file mutations (`bytes` or `None` to delete) for an upsert without writing."""
    if not re.fullmatch(NAME, source):
        raise Error("invalid record source")
    wanted = {path(source, record.id): record for record in incoming}
    if len(wanted) != len(incoming):
        raise Error("duplicate incoming record ids; reconcile them before collecting")
    counts = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    replacements: dict[str, bytes | None] = {}
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
    return counts, replacements


def find(
    store: Store, source: str, record_id: str, *, complete: bool = False, wait: float = WAIT
) -> tuple[str, Record, bytes, int, bool] | None:
    """Resolve `source:record_id` by filename, verifying absence unless `complete` or returning unlocked on busy wait."""
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
            locked.enter_context(reader(store, wait=wait))
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


def forgotten(store: Store, source: str) -> set[str]:
    """The SHA-256 digests of the record ids `bf forget` removed from a source."""
    name = f"memories/{source}/{FORGOTTEN}"
    try:
        data = store.read(name, _FORGOTTEN_LIMIT)
    except FileNotFoundError:
        return set()
    digests = set()
    for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if not _DIGEST.fullmatch(entry):
            raise Error(f"{name}:{number}: expected the SHA-256 of a forgotten record id")
        digests.add(entry)
    return digests


def _digest(name: str) -> str:
    return name.rsplit("/", 1)[1].removesuffix(".json")


@dataclass(frozen=True)
class Retention:
    """Printed records kept alongside stored removals and forgotten/expired counts."""

    kept: list[Record]
    removals: list[str]
    forgotten: int
    expired: int


def retention(
    store: Store, source: str, printed: list[Record], *, cutoff: str, expire_stored: bool = True
) -> Retention:
    """Partition printed records and stored files against `forgotten` and event-time `cutoff`."""
    hidden = forgotten(store, source)
    forgot: set[str] = set()
    expired: set[str] = set()
    kept = []
    for record in printed:
        entry = digest(record.id.encode())
        if entry in hidden:
            forgot.add(entry)
        elif cutoff and record.time and record.time < cutoff:
            expired.add(entry)
        else:
            kept.append(record)
    alive = {digest(record.id.encode()) for record in kept}
    removals = []
    for name in files(store, source):
        entry = _digest(name)
        if entry in hidden:
            forgot.add(entry)
            removals.append(name)
            continue
        if entry in alive or not expire_stored:
            continue
        if entry in expired:
            removals.append(name)
            continue
        if not cutoff:
            continue
        try:
            record = load(store, name)
        except Error, OSError:
            continue
        if (age := record.time or record.observed) and age < cutoff:
            expired.add(entry)
            removals.append(name)
    return Retention(kept=kept, removals=removals, forgotten=len(forgot), expired=len(expired))


def remove(store: Store, source: str, names: list[str]) -> None:
    """Remove record files of one source in one transaction; the caller holds the writer lock."""
    _commit(store, source, dict.fromkeys(names))


def forget(store: Store, source: str, targets: dict[str, str], *, dry_run: bool = False) -> list[dict[str, object]]:
    """Record forgotten digests and remove their stored files under the writer lock."""
    if not re.fullmatch(NAME, source):
        raise Error("invalid record source")
    recover(store)
    known = forgotten(store, source)
    hidden = set(known)
    result, present = [], []
    for ref, entry in targets.items():
        name = f"memories/{source}/{entry}.json"
        try:
            store.fingerprint(name)
            stored_now = True
        except FileNotFoundError:
            stored_now = False
        result.append({"ref": ref, "removed": stored_now, "listed": entry in known})
        if stored_now:
            present.append(name)
        hidden.add(entry)
    if dry_run:
        return result
    listing = f"memories/{source}/{FORGOTTEN}"
    try:
        listed = store.read(listing, _FORGOTTEN_LIMIT)
    except FileNotFoundError:
        listed = b""
    if listed and not listed.endswith(b"\n"):
        listed += b"\n"
    listed += "".join(f"{entry}\n" for entry in sorted(hidden - known)).encode()
    if len(listed) > _FORGOTTEN_LIMIT:
        raise Error(f"{listing} would exceed {_FORGOTTEN_LIMIT:,} bytes; split the source")
    # Write the forgotten list first so any crash before removal still blocks future collection.
    store.write(listing, listed)
    remove(store, source, present)
    return result


def target(ref: str) -> tuple[str, str]:
    """The source and record file digest a `SOURCE:ID` ref or a `memories/SOURCE/SHA256.json` path names."""
    if ref.startswith("memories/"):
        source_of(ref)
        return ref.split("/")[1], _digest(ref)
    source, separator, record_id = ref.partition(":")
    if not separator or not record_id or not re.fullmatch(NAME, source) or source == RESERVED:
        raise Error("expected a SOURCE:ID record ref or a memories/SOURCE/SHA256.json record path")
    return source, digest(record_id.encode())
