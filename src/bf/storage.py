"""Confined, bounded filesystem access and one physical-brain writer lock."""

from __future__ import annotations

import fcntl
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath

from bf.models import MAX_FILE, MAX_FILES, Error, digest


class BusyError(Error):
    """Another process holds the brain writer lock."""


_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


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
        except OSError as error:
            raise Error("brain directory does not exist; pass an existing --brain or BF_BRAIN") from error
        if not self.root.is_dir():
            raise Error("brain must be a directory")

    @contextmanager
    def parent(self, name: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        parts = relative(name)
        descriptor = os.open(self.root, _DIR)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    else:
                        # Persist the new directory entry before a transaction can depend on its files.
                        os.fsync(descriptor)
                child = os.open(part, _DIR, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1]
        finally:
            os.close(descriptor)

    def read(self, name: str, limit: int = MAX_FILE) -> bytes:
        with self.parent(name) as (parent, leaf):
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise Error(f"{name}: expected a regular file")
                data = stream.read(limit + 1)
        if len(data) > limit:
            raise Error(f"{name}: file exceeds {limit} bytes")
        return data

    def write(self, name: str, data: bytes) -> None:
        """Replace a regular file atomically; readers see the old or the new bytes, never a mix."""
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
                os.fsync(parent)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent)

    def delete(self, name: str) -> None:
        with self.parent(name) as (parent, leaf):
            if not stat.S_ISREG(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
                raise Error(f"{name}: refusing to delete a non-regular file")
            os.unlink(leaf, dir_fd=parent)
            os.fsync(parent)

    def rmdir(self, name: str) -> None:
        """Remove an empty directory without following links."""
        with self.parent(name) as (parent, leaf):
            if not stat.S_ISDIR(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
                raise Error(f"{name}: expected a directory")
            os.rmdir(leaf, dir_fd=parent)
            os.fsync(parent)

    def files(self, directory: str) -> list[str]:
        """Bound the entire traversal, including directories and ignored extensions."""
        relative(directory)
        result: list[str] = []
        visited = 0

        def visit(fd: int, prefix: str) -> None:
            nonlocal visited
            if prefix.count("/") > 64:
                raise Error("directory exceeds the 64-level depth limit")
            with os.scandir(fd) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_FILES:
                        raise Error(f"{directory} exceeds the {MAX_FILES:,}-entry scan limit")
                    path = f"{prefix}/{entry.name}"
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, _DIR, dir_fd=fd)
                        try:
                            visit(child, path)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        result.append(path)
                    else:
                        raise Error(f"{path}: symlinks and special files are forbidden")

        try:
            with self.parent(directory) as (parent, leaf):
                fd = os.open(leaf, _DIR, dir_fd=parent)
        except FileNotFoundError:
            return []
        try:
            visit(fd, directory)
        finally:
            os.close(fd)
        return sorted(result)

    def fingerprint(self, name: str) -> tuple[int, int, int, int]:
        """Size, mtime, ctime and inode: ctime catches an edit whose mtime was preserved."""
        with self.parent(name) as (parent, leaf):
            info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise Error(f"{name}: expected a regular file")
            return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino


def state_store(root: Path) -> Store:
    """Private runtime state stays outside the evidence brain."""
    home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))).expanduser()
    if not home.is_absolute() or home.resolve().is_relative_to(root):
        raise Error("state directory must be absolute and outside the brain")
    directory = home / "bf" / digest(os.fsencode(root))
    # Reject redirected state roots before creating any descendant.
    for path in [directory, *directory.parents]:
        if path.is_symlink():
            raise Error("state directory may not contain symlinks")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise Error("state directory must be owned by you with mode 700")
    return Store(directory)


@contextmanager
def _lock(store: Store, name: str, wait: float, *, shared: bool = False) -> Iterator[None]:
    state = state_store(store.root)
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
                    raise BusyError("another writer is active for this brain; retry after it finishes") from error
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
    """Keep record partitions stable during one exact read; readers can run concurrently."""
    with _lock(store, "write.lock", wait, shared=True):
        yield


@contextmanager
def collecting(store: Store, sensor: str, wait: float = 0) -> Iterator[None]:
    """Order runs of one sensor without blocking offline reads while its provider is running."""
    relative(sensor)
    with _lock(store, f"collect-{sensor}.lock", wait):
        yield
