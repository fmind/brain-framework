"""Confined, bounded filesystem access and one physical-brain writer lock."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath

from bf.models import MAX_FILE, MAX_FILES, Error, digest


class BusyError(Error):
    """Another process holds the brain writer lock or runs the same program."""


class FileAncestorError(Error):
    """A regular file stands where a path needs a directory, so nothing below it can exist."""


_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# A scanned name BF cannot address: its bytes are not UTF-8, or it holds a backslash, which relative() rejects.
UNNAMED = "file name is not valid UTF-8 or contains a backslash; rename it"


def unnamed(path: str) -> bool:
    """Whether a scanned path names an entry that `scan` could only show escaped, never open or index."""
    return "\\" in path


def _shown(name: str) -> str | None:
    """A display-safe form of an unaddressable name, which always holds a backslash; None for other names.

    Undecodable bytes arrive as lone surrogates that SQLite and JSON cannot store: show them as `\\xNN` escapes.
    """
    try:
        name.encode()
    except UnicodeEncodeError:
        return os.fsencode(name).decode("utf-8", "backslashreplace")
    return name if "\\" in name else None


def relative(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or any(p in {"", ".", ".."} for p in parts) or "\x00" in value or "\\" in value:
        raise Error("expected a normalized brain-relative path")
    return tuple(parts)


class Store:
    """All descendants are opened through no-follow directory descriptors."""

    def __init__(self, root: Path) -> None:
        try:
            self.root = root.resolve(strict=True)
            info = self.root.stat()
        except OSError as error:
            raise Error("brain directory does not exist; pass an existing --brain or BF_BRAIN") from error
        if not stat.S_ISDIR(info.st_mode):
            raise Error("brain must be a directory")
        # Locks and brain selection compare the physical directory: bind mounts and case variants share it.
        self.identity = f"{info.st_dev}-{info.st_ino}"

    @contextmanager
    def parent(self, name: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        parts = relative(name)
        descriptor = os.open(self.root, _DIR)
        try:
            for number, part in enumerate(parts[:-1], 1):
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    else:
                        # Persist the new directory entry before a transaction can depend on its files.
                        os.fsync(descriptor)
                try:
                    child = os.open(part, _DIR, dir_fd=descriptor)
                except OSError as error:
                    if error.errno not in {errno.ELOOP, errno.ENOTDIR}:
                        raise
                    location = "/".join(parts[:number])
                    if stat.S_ISREG(os.stat(part, dir_fd=descriptor, follow_symlinks=False).st_mode):
                        raise FileAncestorError(f"{location}: expected a directory, found a file") from error
                    raise Error(
                        f"{location}: expected a directory; symlinks and special files are not followed"
                    ) from error
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1]
        finally:
            os.close(descriptor)

    def read(self, name: str, limit: int = MAX_FILE) -> bytes:
        with self.parent(name) as (parent, leaf):
            try:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise Error(f"{name}: symlinks are not followed; replace it with a regular file") from error
                raise
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise Error(f"{name}: expected a regular file")
                # Avoid allocating the full limit for every small file. The extra byte detects
                # in-place growth after fstat; finish that read within the original byte bound.
                size = min(info.st_size, limit) + 1
                data = stream.read(size)
                if len(data) == size and len(data) <= limit:
                    data += stream.read(limit + 1 - len(data))
        if len(data) > limit:
            raise Error(f"{name}: file exceeds {limit} bytes")
        return data

    def write(self, name: str, data: bytes, *, durable: bool = True) -> None:
        """Replace a regular file atomically; readers see the old or the new bytes, never a mix.

        The bytes are always synced; `durable=False` leaves syncing the directory entry to one later `sync()`.
        """
        with self.parent(name, create=True) as (parent, leaf):
            temporary = ".write-" + os.urandom(16).hex()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(info.st_mode):
                        raise Error(f"{name}: refusing to replace a non-regular file")
                os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                if durable:
                    os.fsync(parent)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent)

    def delete(self, name: str, *, durable: bool = True) -> None:
        with self.parent(name) as (parent, leaf):
            if not stat.S_ISREG(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
                raise Error(f"{name}: refusing to delete a non-regular file")
            os.unlink(leaf, dir_fd=parent)
            if durable:
                os.fsync(parent)

    def sync(self, directory: str) -> None:
        """Persist the entries that writes and deletes with `durable=False` changed in one directory."""
        with self.parent(directory) as (parent, leaf):
            descriptor = os.open(leaf, _DIR, dir_fd=parent)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def rmdir(self, name: str) -> None:
        """Remove an empty directory without following links."""
        with self.parent(name) as (parent, leaf):
            if not stat.S_ISDIR(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
                raise Error(f"{name}: expected a directory")
            os.rmdir(leaf, dir_fd=parent)
            os.fsync(parent)

    def files(self, directory: str, *, skipped: dict[str, tuple[int, int, int, int]] | None = None) -> list[str]:
        return sorted(self.scan(directory, skipped=skipped))

    def scan(
        self, directory: str, *, skipped: dict[str, tuple[int, int, int, int]] | None = None
    ) -> dict[str, tuple[int, int, int, int]]:
        """Regular files below a directory, fingerprinted by the traversal's own no-follow stats.

        Symlinks, special files and names BF cannot address are never followed. They fail the scan, or, when
        the caller passes `skipped`, land there with their own fingerprint so the caller can report them and
        carry on; an unaddressable name lands under its escaped form, for which `unnamed` holds.
        Bound the entire traversal, including directories and ignored extensions.
        """
        relative(directory)
        result: dict[str, tuple[int, int, int, int]] = {}
        visited = 0

        def visit(fd: int, prefix: str) -> None:
            nonlocal visited
            if prefix.count("/") > 64:
                raise Error(f"{prefix}: directory exceeds the 64-level depth limit")
            with os.scandir(fd) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_FILES:
                        raise Error(
                            f"{directory} exceeds the {MAX_FILES:,}-entry scan limit; keep bulky files "
                            "in the brain's root inputs/ or originals/, which are not scanned"
                        )
                    path = f"{prefix}/{entry.name}"
                    shown = _shown(entry.name)
                    try:
                        info = entry.stat(follow_symlinks=False)
                        # An unaddressable folder is reported whole, like a linked one: its entries have no ref.
                        opened = stat.S_ISDIR(info.st_mode) and shown is None
                        child = os.open(entry.name, _DIR, dir_fd=fd) if opened else None
                    except FileNotFoundError:
                        # Removed after listing, as by an editor's atomic save; later refreshes compare again.
                        continue
                    if shown is not None:
                        if skipped is None:
                            raise Error(f"{prefix}/{shown}: {UNNAMED}")
                        skipped[f"{prefix}/{shown}"] = _fingerprint(info)
                        continue
                    if child is not None:
                        try:
                            visit(child, path)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        result[path] = _fingerprint(info)
                    elif skipped is not None:
                        skipped[path] = _fingerprint(info)
                    else:
                        raise Error(f"{path}: symlinks and special files are forbidden")

        fd = os.open(self.root, _DIR)
        try:
            try:
                prefix = []
                for part in relative(directory):
                    prefix.append(part)
                    info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        path = "/".join(prefix)
                        if skipped is None:
                            raise Error(f"{path}: expected a directory; symlinks and special files are forbidden")
                        skipped[path] = _fingerprint(info)
                        return {}
                    child = os.open(part, _DIR, dir_fd=fd)
                    os.close(fd)
                    fd = child
            except FileNotFoundError:
                return {}
            visit(fd, directory)
        finally:
            os.close(fd)
        return result

    def fingerprint(self, name: str) -> tuple[int, int, int, int]:
        """Size, mtime, ctime and inode: ctime catches an edit whose mtime was preserved."""
        with self.parent(name) as (parent, leaf):
            info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise Error(f"{name}: expected a regular file")
            return _fingerprint(info)


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino


def xdg_setting(name: str) -> Path | None:
    """An explicit XDG base directory; like the specification, ignore empty and relative values."""
    try:
        path = Path(os.environ.get(name, "")).expanduser()
    except RuntimeError:
        return None
    return path if path.is_absolute() else None


def xdg(name: str, default: str) -> Path:
    """An XDG base directory, or `default` below home."""
    return xdg_setting(name) or Path.home() / default


def _private(*parts: str, root: Path) -> Store:
    """A private directory below XDG_STATE_HOME, or ~/.local/state, outside the evidence brain."""
    base = xdg("XDG_STATE_HOME", ".local/state")
    if base.resolve().is_relative_to(root):
        raise Error("state directory must be outside the brain")
    # Operating systems may link an ancestor, such as /home to /var/home; the state root itself may not be a link.
    home = base.parent.resolve() / base.name
    directory = home.joinpath("bf", *parts)
    # Reject redirected state roots before creating any descendant: check each level from home down.
    if any(path.is_symlink() for path in (directory, *directory.parents[: len(parts) + 1])):
        raise Error("state directory may not contain symlinks")
    # mkdir applies its mode to the last directory only: create the shared bf/ root explicitly too.
    top = home / "bf"
    top.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = top.stat()
    if info.st_uid != os.getuid():
        raise Error("state directory must be owned by you with mode 700")
    if info.st_mode & 0o077:
        # Other users could list the per-brain directories; each one below is verified on its own.
        top.chmod(0o700)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise Error("state directory must be owned by you with mode 700")
    return Store(directory)


def state_store(root: Path) -> Store:
    """Private run history and logs stay outside the evidence brain, keyed by its resolved path."""
    return _private(digest(os.fsencode(root)), root=root)


def lock_file(store: Store, name: str) -> tuple[Store, str]:
    """Where a lock lives: every lock follows the physical brain, whatever path or mount reached it."""
    leaf = f"{store.identity}.lock" if name == "write.lock" else f"{store.identity}-{name}"
    return _private("locks", root=store.root), leaf


@contextmanager
def _lock(
    store: Store,
    name: str,
    wait: float,
    *,
    shared: bool = False,
    busy: str = "another writer is active for this brain; retry after it finishes",
) -> Iterator[None]:
    state, name = lock_file(store, name)
    with state.parent(name) as (parent, leaf):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(leaf, flags | os.O_CREAT, 0o600, dir_fd=parent)
        except FileNotFoundError:
            # Concurrent first creation can report ENOENT on macOS. Reopen only an existing lock;
            # keep the pinned parent and safety flags, and never replace a missing lock on recovery.
            fd = os.open(leaf, flags, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Error("invalid writer lock")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise BusyError(busy) from error
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


@contextmanager
def writer(store: Store, wait: float = 0) -> Iterator[None]:
    """Serialize writers of one physical brain; wait up to `wait` seconds for another writer."""
    with _lock(store, "write.lock", wait):
        yield


@contextmanager
def reader(store: Store, wait: float = 120) -> Iterator[None]:
    """Keep record files stable during one exact read; readers can run concurrently."""
    with _lock(store, "write.lock", wait, shared=True):
        yield


@contextmanager
def collecting(store: Store, sensor: str, wait: float = 0) -> Iterator[None]:
    """Order runs of one sensor without blocking offline reads while its provider is running."""
    relative(sensor)
    with _lock(store, f"collect-{sensor}.lock", wait, busy=f"{sensor} is already running for this brain; retry later"):
        yield
