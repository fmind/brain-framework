"""Confined O_NOFOLLOW filesystem access, private `.bf/` state, and brain writer locks."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

from bf.models import MAX_FILE, MAX_FILES, BusyError, Error

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_TEMPORARY = re.compile(r"\.write-[0-9a-f]{32}")
UNNAMED = "file name is not valid UTF-8 or contains a backslash; rename it"
LINKED = "symlinks are not followed; replace the link with the file or folder itself"
_WRITE_BUSY = "another writer is active for this brain; retry after it finishes"


def _shown(name: str) -> str | None:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return os.fsencode(name).decode("utf-8", "backslashreplace")
    return name if "\\" in name else None


def relative(value: str) -> tuple[str, ...]:
    """Validate and split a normalized brain-relative path."""
    parts = value.split("/")
    if any(p in {"", ".", ".."} for p in parts) or "\x00" in value or "\\" in value:
        raise Error("expected a normalized brain-relative path")
    return tuple(parts)


def _atomic_replace(parent: int, temporary: str, leaf: str, name: str, *, durable: bool = True) -> None:
    with suppress(FileNotFoundError):
        if not stat.S_ISREG(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
            raise Error(f"{name}: refusing to replace a non-regular file")
    os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
    if durable:
        os.fsync(parent)


class Store:
    """Confined directory store opening every descendant through `O_NOFOLLOW` descriptors."""

    def __init__(self, root: Path) -> None:
        try:
            self.root = root.resolve(strict=True)
            info = self.root.stat()
        except OSError as error:
            raise Error("brain directory does not exist; pass an existing --brain or BF_BRAIN") from error
        if not stat.S_ISDIR(info.st_mode):
            raise Error("brain must be a directory")
        self.identity = f"{info.st_dev}-{info.st_ino}"

    @contextmanager
    def parent(self, name: str, *, create: bool = False) -> Generator[tuple[int, str]]:
        parts = relative(name)
        fd = os.open(self.root, _DIR)
        try:
            for number, part in enumerate(parts[:-1], 1):
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o755, dir_fd=fd)
                        os.fsync(fd)
                try:
                    child = os.open(part, _DIR, dir_fd=fd)
                except OSError as error:
                    if error.errno not in {errno.ELOOP, errno.ENOTDIR}:
                        raise
                    loc = "/".join(parts[:number])
                    if stat.S_ISREG(os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode):
                        raise Error(f"{loc}: expected a directory, found a file") from error
                    raise Error(f"{loc}: expected a directory; symlinks and special files are not followed") from error
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def read(self, name: str, limit: int = MAX_FILE) -> bytes:
        """Read up to `limit` bytes from a regular file without following symlinks."""
        with self.parent(name) as (parent, leaf):
            try:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise Error(f"{name}: {LINKED}") from error
                if error.errno in {errno.ENXIO, errno.EOPNOTSUPP} and not stat.S_ISREG(
                    os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode
                ):
                    raise Error(f"{name}: expected a regular file") from error
                raise
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise Error(f"{name}: expected a regular file")
                size = min(info.st_size, limit) + 1
                data = stream.read(size)
                if len(data) == size and len(data) <= limit:
                    data += stream.read(limit + 1 - len(data))
        if len(data) > limit:
            raise Error(f"{name}: file exceeds {limit} bytes")
        return data

    def write(self, name: str, data: bytes, *, mode: int = 0o644, durable: bool = True) -> None:
        """Write `data` to a temporary sibling and replace `name` atomically."""
        with self.parent(name, create=True) as (parent, leaf):
            tmp = ".write-" + os.urandom(16).hex()
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                _atomic_replace(parent, tmp, leaf, name, durable=durable)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(tmp, dir_fd=parent)

    def delete(self, name: str, *, durable: bool = True) -> None:
        """Delete a regular file without following symlinks."""
        with self.parent(name) as (parent, leaf):
            if not stat.S_ISREG(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
                raise Error(f"{name}: refusing to delete a non-regular file")
            os.unlink(leaf, dir_fd=parent)
            if durable:
                os.fsync(parent)

    def exists(self, name: str) -> bool:
        try:
            return stat.S_ISREG(self.mode(name))
        except Error, OSError:
            return False

    def is_dir(self, name: str) -> bool:
        try:
            return stat.S_ISDIR(self.mode(name))
        except Error, OSError:
            return False

    def mode(self, name: str) -> int:
        with self.parent(name) as (parent, leaf):
            return os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode

    def files(self, directory: str, *, skipped: list[str] | None = None) -> list[str]:
        """List regular files recursively below `directory`, recording bad entries in `skipped`."""
        relative(directory)
        found: list[str] = []
        visited = 0

        def record_skip(path: str, message: str, err: Exception | None = None) -> None:
            if skipped is None:
                raise Error(message) from err
            skipped.append(f"{path}: {message}")

        def visit(fd: int, prefix: str) -> None:
            nonlocal visited
            if prefix.count("/") > 64:
                raise Error(f"{prefix}: directory exceeds the 64-level depth limit")
            with os.scandir(fd) as entries:
                for entry in entries:
                    if _TEMPORARY.fullmatch(entry.name):
                        continue
                    visited += 1
                    if visited > MAX_FILES:
                        raise Error(f"{directory} exceeds the {MAX_FILES:,}-entry scan limit")
                    shown = _shown(entry.name)
                    path = f"{prefix}/{shown or entry.name}"
                    try:
                        info = entry.stat(follow_symlinks=False)
                        opened = (
                            os.open(entry.name, _DIR, dir_fd=fd)
                            if stat.S_ISDIR(info.st_mode) and shown is None
                            else None
                        )
                    except FileNotFoundError:
                        continue
                    except PermissionError as error:
                        record_skip(path, "permission denied", error)
                        continue
                    except OSError as error:
                        if error.errno not in {errno.ELOOP, errno.ENOTDIR}:
                            raise
                        record_skip(path, LINKED, error)
                        continue
                    if shown is not None:
                        record_skip(path, UNNAMED)
                    elif opened is not None:
                        try:
                            visit(opened, path)
                        finally:
                            os.close(opened)
                    elif stat.S_ISREG(info.st_mode):
                        found.append(path)
                    elif stat.S_ISLNK(info.st_mode):
                        record_skip(path, LINKED)
                    else:
                        record_skip(path, "special files are not allowed")

        fd = os.open(self.root, _DIR)
        try:
            prefix: list[str] = []
            for part in relative(directory):
                prefix.append(part)
                key = "/".join(prefix)
                try:
                    info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                    child = os.open(part, _DIR, dir_fd=fd) if stat.S_ISDIR(info.st_mode) else None
                except FileNotFoundError:
                    return []
                except PermissionError as error:
                    record_skip(key, "permission denied", error)
                    return []
                if child is None:
                    why = LINKED if stat.S_ISLNK(info.st_mode) else "expected a directory"
                    record_skip(key, why)
                    return []
                os.close(fd)
                fd = child
            visit(fd, directory)
        finally:
            os.close(fd)
        return sorted(found)


def expand(path: Path) -> Path:
    try:
        return path.expanduser()
    except RuntimeError as error:
        hint = "set HOME" if path.parts[:1] == ("~",) else "check the user name"
        raise Error(f"cannot resolve home directory in path; {hint}") from error


def xdg(variable: str, fallback: str) -> Path:
    if (raw := os.environ.get(variable, "").strip()) and (candidate := Path(raw)).is_absolute():
        return candidate
    try:
        return Path.home() / fallback
    except RuntimeError as error:
        raise Error("cannot find the home directory; set HOME") from error


def private_dir(store: Store, *parts: str) -> Store:
    """Ensure `<brain>/.bf/...` exists with mode `0700` owned by the current user without following symlinks."""
    target = parts or (".bf",)
    if any(not p or p in {".", ".."} or "/" in p or "\\" in p or "\x00" in p for p in target):
        raise Error("invalid private state path")
    fd = os.open(store.root, _DIR)
    try:
        for part in target:
            try:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                child = os.open(part, _DIR, dir_fd=fd)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise Error("state directory must be a real directory without symlinks") from error
                extra = f" ({error.strerror})" if error.strerror else ""
                raise Error(f"state directory {part} is inaccessible{extra}") from error
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Error("state directory .bf must be owned by you with mode 700")
    finally:
        os.close(fd)
    return Store(store.root.joinpath(*target))


@contextmanager
def writer(store: Store, wait: float = 10.0, *, busy: str = _WRITE_BUSY) -> Generator[None]:
    """Hold an exclusive advisory lock on `.bf/locks/write.lock`."""
    locks = private_dir(store, ".bf", "locks")
    with locks.parent("write.lock") as (parent, leaf):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(leaf, flags | os.O_CREAT, 0o600, dir_fd=parent)
        except FileNotFoundError:
            fd = os.open(leaf, flags, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Error("invalid writer lock")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise BusyError(busy) from error
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)
