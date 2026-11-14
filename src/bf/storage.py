"""Confined, bounded filesystem access, brain-local private state and the locks that follow each physical brain."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import time
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager, suppress
from pathlib import Path

from bf.models import MAX_FILE, MAX_FILES, Error


class BusyError(Error):
    """Another process holds a lock this operation needs, such as the brain writer lock or a program's lock."""


class FileAncestorError(Error):
    """A regular file stands where a path needs a directory, so nothing below it can exist."""


_Stamp = tuple[int, int, int, int]
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_TEMPORARY = re.compile(r"\.write-[0-9a-f]{32}")
UNNAMED = "file name is not valid UTF-8 or contains a backslash; rename it"
LINKED = "symlinks are not followed; replace the link with the file or folder itself"
QUEUE, _UNSTATED = ".bf/runtime.sqlite3", (0, 0, 0, 0)
_LOCK_BUSY = "another writer is active for this brain; retry after it finishes"
_WRITE_BUSY = "another process is reading or changing this brain's records; retry after it finishes"


def temporary(path: str) -> bool:
    return _TEMPORARY.fullmatch(path.rsplit("/", 1)[-1]) is not None


def unnamed(path: str) -> bool:
    return "\\" in path


def _shown(name: str) -> str | None:
    try:
        name.encode()
    except UnicodeEncodeError:
        return os.fsencode(name).decode("utf-8", "backslashreplace")
    return name if "\\" in name else None


def relative(value: str) -> tuple[str, ...]:
    """Validate and split a normalized brain-relative path."""
    parts = value.split("/")
    if any(p in {"", ".", ".."} for p in parts) or "\x00" in value or "\\" in value:
        raise Error("expected a normalized brain-relative path")
    return tuple(parts)


def _atomic_replace(parent: int, temporary: str, leaf: str, name: str, *, durable: bool = False) -> None:
    with suppress(FileNotFoundError):
        if not stat.S_ISREG(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
            raise Error(f"{name}: refusing to replace a non-regular file")
    os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
    if durable:
        os.fsync(parent)


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
        # Locks and brain selection compare physical directory identity (handles bind mounts/case variants).
        self.identity = f"{info.st_dev}-{info.st_ino}"

    @contextmanager
    def parent(self, name: str, *, create: bool = False) -> Generator[tuple[int, str]]:
        parts = relative(name)
        fd = os.open(self.root, _DIR)
        try:
            for number, part in enumerate(parts[:-1], 1):
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=fd)
                        os.fsync(fd)
                try:
                    child = os.open(part, _DIR, dir_fd=fd)
                except OSError as error:
                    if error.errno not in {errno.ELOOP, errno.ENOTDIR}:
                        raise
                    loc = "/".join(parts[:number])
                    if stat.S_ISREG(os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode):
                        raise FileAncestorError(f"{loc}: expected a directory, found a file") from error
                    raise Error(f"{loc}: expected a directory; symlinks and special files are not followed") from error
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def read(self, name: str, limit: int = MAX_FILE) -> bytes:
        return self.stamped(name, limit)[0]

    def stamped(self, name: str, limit: int = MAX_FILE) -> tuple[bytes, int]:
        """A regular file's bytes and descriptor mtime_ns."""
        with self.parent(name) as (parent, leaf):
            try:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise Error(f"{name}: {LINKED}") from error
                # Unix sockets fail before fstat: ENXIO on Linux, EOPNOTSUPP on macOS.
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
        return data, info.st_mtime_ns

    def write(self, name: str, data: bytes, *, durable: bool = True) -> None:
        """Replace a regular file atomically (`durable=False` defers directory fsync to `sync()`)."""
        with self.parent(name, create=True) as (parent, leaf):
            tmp = ".write-" + os.urandom(16).hex()
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                _atomic_replace(parent, tmp, leaf, name, durable=durable)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(tmp, dir_fd=parent)

    def link(self, name: str, target: str) -> None:
        """Atomically hard-link regular file `name` to `target`."""
        with self.parent(name) as (source, leaf), self.parent(target, create=True) as (parent, new):
            tmp = ".write-" + os.urandom(16).hex()
            os.link(leaf, tmp, src_dir_fd=source, dst_dir_fd=parent, follow_symlinks=False)
            try:
                if not stat.S_ISREG(os.stat(tmp, dir_fd=parent, follow_symlinks=False).st_mode):
                    raise Error(f"{name}: expected a regular file")
                _atomic_replace(parent, tmp, new, target)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(tmp, dir_fd=parent)

    def delete(self, name: str, *, durable: bool = True) -> None:
        with self.parent(name) as (parent, leaf):
            if not stat.S_ISREG(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
                raise Error(f"{name}: refusing to delete a non-regular file")
            os.unlink(leaf, dir_fd=parent)
            if durable:
                os.fsync(parent)

    @contextmanager
    def _directory(self, name: str) -> Generator[int]:
        with self.parent(name) as (parent, leaf):
            descriptor = os.open(leaf, _DIR, dir_fd=parent)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def sync(self, directory: str) -> None:
        with self._directory(directory) as descriptor:
            os.fsync(descriptor)

    def sweep(self, directory: str) -> None:
        """Remove `.write-*` temporaries left directly inside `directory`."""
        try:
            with self._directory(directory) as descriptor, os.scandir(descriptor) as entries:
                for entry in entries:
                    if _TEMPORARY.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                        with suppress(FileNotFoundError):
                            os.unlink(entry.name, dir_fd=descriptor)
        except OSError as error:
            if error.errno not in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                raise

    def entries(self, name: str) -> list[str]:
        with self._directory(name) as descriptor, os.scandir(descriptor) as found:
            return sorted(entry.name for entry in found)

    def rmdir(self, name: str) -> None:
        with self.parent(name) as (parent, leaf):
            if not stat.S_ISDIR(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
                raise Error(f"{name}: expected a directory")
            os.rmdir(leaf, dir_fd=parent)
            os.fsync(parent)

    def files(self, directory: str, *, skipped: dict[str, _Stamp] | None = None, split: bool = False) -> list[str]:
        return sorted(self.scan(directory, skipped=skipped, split=split))

    def scan(
        self,
        directory: str,
        *,
        skipped: dict[str, _Stamp] | None = None,
        split: bool = False,
        counts: dict[str, int] | None = None,
        fingerprints: bool = True,
        reuse: Callable[[str, _Stamp], bool] | None = None,
    ) -> dict[str, _Stamp]:
        """Walk regular files below `directory` via no-follow stats, recording unreadable/linked entries in `skipped`."""
        relative(directory)
        result, visited = {}, ({} if counts is None else counts)

        def fail_or_skip(key: str, stamp: _Stamp, msg: str, err: Exception | None = None) -> None:
            if skipped is None:
                raise Error(msg) from err
            skipped[key] = stamp

        def visit(fd: int, prefix: str, tree: str) -> None:
            if prefix.count("/") > 64:
                raise Error(f"{prefix}: directory exceeds the 64-level depth limit")
            with os.scandir(fd) as entries:
                for entry in entries:
                    visited[tree] = visited.get(tree, 0) + 1
                    if visited[tree] > MAX_FILES:
                        hint = (
                            "split its sensor into several sources or archive older records outside the brain"
                            if tree.split("/")[0] == "memories"
                            else "keep bulky files in the brain's root inputs/ or originals/, which are not scanned"
                        )
                        raise Error(f"{tree} exceeds the {MAX_FILES:,}-entry scan limit; {hint}")
                    path, shown, stamp = f"{prefix}/{entry.name}", _shown(entry.name), _UNSTATED
                    try:
                        if fingerprints or not (
                            mode := stat.S_IFREG
                            if entry.is_file(follow_symlinks=False)
                            else (stat.S_IFDIR if entry.is_dir(follow_symlinks=False) else 0)
                        ):
                            info = entry.stat(follow_symlinks=False)
                            mode, stamp = info.st_mode, _fingerprint(info)
                        opened = stat.S_ISDIR(mode) and shown is None
                        if opened and split and prefix == directory and reuse is not None and reuse(path, stamp):
                            continue
                        child = os.open(entry.name, _DIR, dir_fd=fd) if opened else None
                    except FileNotFoundError:
                        continue
                    except PermissionError:
                        fail_or_skip(f"{prefix}/{shown}" if shown is not None else path, stamp, "")
                        if skipped is None:
                            raise
                        continue
                    except OSError as error:
                        if error.errno not in {errno.ELOOP, errno.ENOTDIR}:
                            raise
                        fail_or_skip(path, stamp, f"{path}: symlinks and special files are forbidden", error)
                        continue
                    if shown is not None:
                        fail_or_skip(f"{prefix}/{shown}", stamp, f"{prefix}/{shown}: {UNNAMED}")
                    elif child is not None:
                        try:
                            visit(child, path, path if split and prefix == directory else tree)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(mode):
                        result[path] = stamp
                    else:
                        fail_or_skip(path, stamp, f"{path}: symlinks and special files are forbidden")

        fd = os.open(self.root, _DIR)
        try:
            prefix = []
            for part in relative(directory):
                prefix.append(part)
                stamp, key = _UNSTATED, "/".join(prefix)
                try:
                    info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                    stamp = _fingerprint(info)
                    child = os.open(part, _DIR, dir_fd=fd) if stat.S_ISDIR(info.st_mode) else None
                except FileNotFoundError:
                    return {}
                except PermissionError:
                    if skipped is None:
                        raise
                    skipped[key] = stamp
                    return {}
                if child is None:
                    fail_or_skip(key, stamp, f"{key}: expected a directory; symlinks and special files are forbidden")
                    return {}
                os.close(fd)
                fd = child
            visit(fd, directory, directory)
        finally:
            os.close(fd)
        return result

    def _status(self, name: str) -> os.stat_result:
        with self.parent(name) as (parent, leaf):
            return os.stat(leaf, dir_fd=parent, follow_symlinks=False)

    def mode(self, name: str) -> int:
        return self._status(name).st_mode

    def lstat(self, name: str) -> os.stat_result:
        if not stat.S_ISREG((info := self._status(name)).st_mode):
            raise Error(f"{name}: expected a regular file")
        return info

    def fingerprint(self, name: str, *, regular: bool = True) -> _Stamp:
        return _fingerprint(self.lstat(name) if regular else self._status(name))


def _fingerprint(info: os.stat_result) -> _Stamp:
    # Wrap unsigned 64-bit inodes (e.g. mergerfs) into signed SQLite INTEGER.
    inode = info.st_ino - (1 << 64) if info.st_ino >= 1 << 63 else info.st_ino
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, inode


def unread(store: Store, name: str) -> str:
    if unnamed(name):
        return UNNAMED
    try:
        mode = (store.root / name).lstat().st_mode
    except PermissionError:
        mode = stat.S_IFDIR
    except OSError:
        mode = 0
    if stat.S_ISDIR(mode):
        return "unreadable folder; grant read and search permission or move it out of the brain"
    if stat.S_ISLNK(mode):
        return LINKED
    if stat.S_ISREG(mode) or "/" not in name:
        return "expected a folder; replace the file with a folder"
    return "special files are not read; replace it with a regular file"


def expand(path: Path) -> Path:
    try:
        return path.expanduser()
    except RuntimeError as error:
        hint = "set HOME" if path.parts[:1] == ("~",) else "check the user name"
        raise Error(f"a path's ~ or ~user home directory cannot be resolved; {hint}") from error


def xdg(name: str, default: str) -> Path:
    """Resolve an XDG base directory, ignoring empty, invalid, or relative values."""
    with suppress(RuntimeError):
        if (path := Path(os.environ.get(name, "")).expanduser()).is_absolute():
            return path
    try:
        return Path.home() / default
    except RuntimeError as error:
        raise Error(f"cannot find the home directory; set HOME or {name}") from error


def private_dir(store: Store, *parts: str) -> Store:
    """Ensure `<brain>/.bf/...` exists with mode 700, owned by you, without following symlinks."""
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
                    raise Error("state directory must be a directory and may not contain symlinks") from error
                extra = f" ({error.strerror})" if error.strerror else ""
                raise Error(f"state directory {part} is inaccessible{extra}") from error
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Error("state directory must be owned by you with mode 700")
    finally:
        os.close(fd)
    return Store(store.root.joinpath(*target))


def program_state(store: Store, name: str) -> Store:
    """Ensure `<brain>/.bf/state/<name>/` exists for one program's private cursors and tokens."""
    if not name or name.startswith(".") or "/" in name or "\\" in name or "\x00" in name:
        raise Error("invalid program name")
    return private_dir(store, ".bf", "state", name)


def ping_wakeup(store: Store) -> None:
    """Write a monotonic timestamp atomically to `.bf/wakeup`, ignoring access failures."""
    with suppress(Error, OSError):
        private_dir(store, ".bf").write("wakeup", f"{time.monotonic_ns()}\n".encode(), durable=False)


def wakeup_mtime(store: Store) -> int:
    """Return `.bf/wakeup` modification time in nanoseconds, or `0` when absent."""
    try:
        return store.lstat(".bf/wakeup").st_mtime_ns
    except Error, OSError:
        return 0


def lock_file(store: Store, name: str) -> tuple[Store, str]:
    """Return `(.bf/locks Store, lock_leaf)`."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise Error("invalid lock name")
    return private_dir(store, ".bf", "locks"), name


@contextmanager
def _lock(store: Store, name: str, wait: float, *, shared: bool = False, busy: str = _LOCK_BUSY) -> Generator[None]:
    state, leaf = lock_file(store, name)
    with state.parent(leaf) as (parent, entry):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(entry, flags | os.O_CREAT, 0o600, dir_fd=parent)
        except FileNotFoundError:
            # Concurrent first creation can report ENOENT on macOS; reopen without creating.
            fd = os.open(entry, flags, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Error("invalid writer lock")
        deadline = time.monotonic() + wait
        _flock(fd, shared=shared, deadline=deadline, busy=busy)
        if name == "write.lock":
            root_fd = os.open(store.root, _DIR)
            try:
                try:
                    _flock(root_fd, shared=shared, deadline=deadline, busy=busy)
                except OSError as error:
                    if error.errno not in {errno.EBADF, errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL}:
                        raise
                yield
            finally:
                os.close(root_fd)
        else:
            yield
    finally:
        os.close(fd)


def _flock(fd: int, *, shared: bool, deadline: float, busy: str) -> None:
    while True:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            if time.monotonic() >= deadline:
                raise BusyError(busy) from error
            time.sleep(0.05)


def writer(store: Store, wait: float = 0, *, busy: str = _WRITE_BUSY) -> AbstractContextManager[None]:
    """Serialize writers of one physical brain."""
    return _lock(store, "write.lock", wait, busy=busy)


def reader(store: Store, wait: float = 120) -> AbstractContextManager[None]:
    """Hold shared access across record reads so writers wait cleanly."""
    return _lock(store, "write.lock", wait, shared=True)


def building(store: Store, wait: float = 0) -> AbstractContextManager[None]:
    """Serialize full search-cache builds."""
    return _lock(store, "build.lock", wait, busy="another search cache build is active; retry after it finishes")


@contextmanager
def generation(store: Store, *, shared: bool, wait: float) -> Generator[None]:
    """Gate shared cache readers against exclusive cache replacement."""
    if shared:
        busy = "the search cache is being replaced; retry shortly"
        with ExitStack() as connected:
            with _lock(store, "cache-gate.lock", wait, shared=True, busy=busy):
                connected.enter_context(_lock(store, "cache.lock", wait, shared=True, busy=busy))
            yield
        return
    busy = "readers kept the search cache open; retry bf build"
    with _lock(store, "cache-gate.lock", wait, busy=busy), _lock(store, "cache.lock", wait, busy=busy):
        yield


def appending(store: Store, name: str, wait: float = 10) -> AbstractContextManager[None]:
    """Serialize writers of one program's log."""
    relative(name)
    return _lock(store, f"log-{name}.lock", wait, busy=f"another run of {name} is writing its log")


def collecting(store: Store, sensor: str, wait: float = 0) -> AbstractContextManager[None]:
    """Serialize runs of one job without blocking offline reads."""
    relative(sensor)
    return _lock(store, f"collect-{sensor}.lock", wait, busy=f"{sensor} is already running for this brain; retry later")
