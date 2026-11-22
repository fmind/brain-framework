"""Confined O_NOFOLLOW filesystem access, private `.bf/` and `logs/` directories, and brain writer locks."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import threading
import time
from collections.abc import Generator, Iterable
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from bf.models import MAX_FILE, MAX_FILES, BusyError, Error

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_TEMPORARY = re.compile(r"\.write-[0-9a-f]{32}")
UNNAMED = "file name is not valid UTF-8 or contains a backslash; rename it"
LINKED = "symlinks are not followed; replace the link with the file or folder itself"
_WRITE_BUSY = "another writer is active for this brain; retry after it finishes"
_LOG_LOCK = threading.Lock()
_LOG_APPENDS: dict[tuple[Path, str], int] = {}


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


def open_regular(dir_fd: int, leaf: str, name: str, flags: int = os.O_RDONLY, mode: int = 0o600) -> int:
    """Open `leaf` under `dir_fd` with `O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`, failing closed on symlinks/specials."""
    try:
        return os.open(leaf, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, mode, dir_fd=dir_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise Error(f"{name}: {LINKED}") from error
        if error.errno in {errno.ENXIO, errno.EOPNOTSUPP}:
            raise Error(f"{name}: expected a regular file") from error
        raise


def append_regular(store: Store, name: str, data: bytes, *, mode: int = 0o600, label: str = "") -> int:
    """Atomically append `data` to a regular file via `O_APPEND | O_NOFOLLOW`, rolling back on write errors."""
    tag = label or name
    with store.parent(name, create=True) as (parent, leaf):
        fd = open_regular(parent, leaf, tag, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
        with os.fdopen(fd, "wb", buffering=0) as stream:
            if not stat.S_ISREG((info := os.fstat(stream.fileno())).st_mode):
                raise Error(f"{tag}: expected a regular file")
            os.fchmod(stream.fileno(), mode)
            view = memoryview(data)
            try:
                while view:
                    if not (written := stream.write(view)):
                        raise Error(f"{tag}: short write")
                    view = view[written:]
            except BaseException:
                with suppress(OSError):
                    os.ftruncate(stream.fileno(), info.st_size)
                raise
            return info.st_size + len(data)


def tail_lines(lines: list[bytes], max_lines: int, budget: int) -> list[bytes]:
    """Return the newest suffix of `lines` fitting within `max_lines` and `budget` bytes."""
    kept: list[bytes] = []
    for item in reversed(lines[-max_lines:] if max_lines > 0 and budget > 0 else ()):
        if len(item) > budget:
            break
        kept.append(item)
        budget -= len(item)
    kept.reverse()
    return kept


def _read_fd(fd: int, name: str, limit: int, *, private: bool = False) -> bytes:
    try:
        if not stat.S_ISREG((info := os.fstat(fd)).st_mode):
            raise Error(f"{name}: expected a regular file")
        if private and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise Error(f"{name} must be owned by you with mode 600")
        chunks, total = [], 0
        while total <= limit and (chunk := os.read(fd, min(limit + 1 - total, max(info.st_size - total + 1, 4096)))):
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    if total > limit:
        raise Error(f"{name}: file exceeds {limit} bytes")
    return chunks[0] if len(chunks) == 1 else b"".join(chunks)


def _atomic_replace(parent: int, temporary: str, leaf: str, name: str, *, durable: bool = True) -> None:
    with suppress(FileNotFoundError):
        if not stat.S_ISREG(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
            raise Error(f"{name}: refusing to replace a non-regular file")
    os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
    if durable:
        os.fsync(parent)


class Store:
    """Confined directory store opening every descendant through `O_NOFOLLOW` descriptors."""

    def __init__(self, root: Path, *, _base: Path | None = None, _private: tuple[str, ...] = ()) -> None:
        if _base is not None:
            self._base, self._private, self.root = _base, _private, _base.joinpath(*_private)
            return
        try:
            self.root = root.resolve(strict=True)
            info = self.root.stat()
        except OSError as error:
            raise Error("brain directory does not exist; pass an existing --brain or BRAIN_LOCATION") from error
        if not stat.S_ISDIR(info.st_mode):
            raise Error("brain must be a directory")
        self._base, self._private = self.root, ()

    def _open_dir(self, parts: tuple[str, ...], *, create: bool = False) -> int:
        fd = os.open(self._base, _DIR)
        all_parts = (*self._private, *parts)
        priv_n = len(self._private)
        try:
            for number, part in enumerate(all_parts, 1):
                if create and number > priv_n:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o755, dir_fd=fd)
                        os.fsync(fd)
                try:
                    child = os.open(part, _DIR, dir_fd=fd)
                except OSError as error:
                    if error.errno not in {errno.ELOOP, errno.ENOTDIR}:
                        raise
                    if number <= priv_n:
                        raise Error("state directory must be a real directory without symlinks") from error
                    loc = "/".join(parts[: number - priv_n])
                    if stat.S_ISREG(os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode):
                        raise Error(f"{loc}: expected a directory, found a file") from error
                    raise Error(f"{loc}: expected a directory; symlinks and special files are not followed") from error
                os.close(fd)
                fd = child
                if number <= priv_n and ((pinfo := os.fstat(fd)).st_uid != os.geteuid() or pinfo.st_mode & 0o077):
                    raise Error(f"state directory {'/'.join(self._private)} must be owned by you with mode 700")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def parent(self, name: str, *, create: bool = False) -> Generator[tuple[int, str]]:
        parts = relative(name)
        fd = self._open_dir(parts[:-1], create=create)
        try:
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def sync_dirs(self, dirs: Iterable[str]) -> None:
        """Fsync each unique brain-relative directory without following symlinks."""
        for directory in sorted(set(dirs)):
            fd = self._open_dir(() if directory == "" else relative(directory))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def read(self, name: str, limit: int = MAX_FILE, *, private: bool = False) -> bytes:
        """Read up to `limit` bytes from a regular file without following symlinks."""
        with self.parent(name) as (parent, leaf):
            fd = open_regular(parent, leaf, name)
        return _read_fd(fd, name, limit, private=private)

    def iter_lines(self, name: str, limit: int = MAX_FILE) -> Generator[bytes]:
        """Stream lines from a confined regular file enforcing a `limit`-byte total cap."""
        with self.parent(name) as (parent, leaf):
            fd = open_regular(parent, leaf, name)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise Error(f"{name}: expected a regular file")
            total = 0
            while line := stream.readline(min(limit + 1 - total, 16 << 20)):
                total += len(line)
                if total > limit:
                    raise Error(f"{name}: file exceeds {limit} bytes")
                yield line

    def write(self, name: str, data: bytes, *, mode: int = 0o644, durable: bool = True) -> None:
        """Write `data` to a temporary sibling and replace `name` atomically."""
        with self.parent(name, create=True) as (parent, leaf):
            tmp = ".write-" + os.urandom(16).hex()
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    if durable:
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

    def _is(self, name: str, pred: Any) -> bool:
        with suppress(Error, OSError):
            return bool(pred(self.mode(name)))
        return False

    def exists(self, name: str) -> bool:
        return self._is(name, stat.S_ISREG)

    def is_dir(self, name: str) -> bool:
        return self._is(name, stat.S_ISDIR)

    def stat(self, name: str) -> os.stat_result:
        with self.parent(name) as (parent, leaf):
            return os.stat(leaf, dir_fd=parent, follow_symlinks=False)

    def mode(self, name: str) -> int:
        return self.stat(name).st_mode

    def _record_skip(self, skipped: list[str] | None, path: str, message: str, err: Exception | None = None) -> None:
        if skipped is None:
            raise Error(message) from err
        skipped.append(f"{path}: {message}")

    def _open_scan_dir(self, directory: str, skipped: list[str] | None) -> int | None:
        try:
            return self._open_dir(relative(directory))
        except FileNotFoundError:
            return None
        except PermissionError as error:
            self._record_skip(skipped, directory, "permission denied", error)
            return None
        except Error as error:
            if skipped is None:
                raise
            skipped.append(str(error))
            return None

    def subdirs(self, directory: str, *, skipped: list[str] | None = None) -> tuple[list[str], list[str]]:
        """Return `(sorted_subdir_names, sorted_top_file_paths)` directly under `directory` without following links."""
        dirs: list[str] = []
        top_files: list[str] = []
        relative(directory)
        if (fd := self._open_scan_dir(directory, skipped)) is None:
            return dirs, top_files
        try:
            visited = 0
            raw: list[os.DirEntry[str]] = []
            with os.scandir(fd) as raw_entries:
                for entry in raw_entries:
                    if entry.name.startswith(".write-") and _TEMPORARY.fullmatch(entry.name):
                        continue
                    if (visited := visited + 1) > MAX_FILES:
                        raise Error(f"{directory} exceeds the {MAX_FILES:,}-entry scan limit")
                    raw.append(entry)
            for entry in sorted(raw, key=lambda item: item.name):
                if (shown := _shown(entry.name)) is not None:
                    self._record_skip(skipped, f"{directory}/{shown}", UNNAMED)
                    continue
                path = f"{directory}/{entry.name}"
                try:
                    if entry.is_symlink():
                        self._record_skip(skipped, path, LINKED)
                    elif entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        top_files.append(path)
                    else:
                        self._record_skip(skipped, path, "special files are not allowed")
                except FileNotFoundError:
                    continue
                except PermissionError as error:
                    self._record_skip(skipped, path, "permission denied", error)
        finally:
            os.close(fd)
        return dirs, top_files

    def files(self, directory: str, *, skipped: list[str] | None = None) -> list[str]:
        """List regular files recursively below `directory`, recording bad entries in `skipped`."""
        relative(directory)
        out: list[str] = []
        visited = 0

        def visit(fd: int, prefix: str) -> None:
            nonlocal visited
            if prefix.count("/") > 64:
                raise Error(f"{prefix}: directory exceeds the 64-level depth limit")
            with os.scandir(fd) as raw_entries:
                entries = list(raw_entries)
            for entry in entries:
                if entry.name.startswith(".write-") and _TEMPORARY.fullmatch(entry.name):
                    continue
                if (visited := visited + 1) > MAX_FILES:
                    raise Error(f"{directory} exceeds the {MAX_FILES:,}-entry scan limit")
                if (shown := _shown(entry.name)) is not None:
                    self._record_skip(skipped, f"{prefix}/{shown}", UNNAMED)
                    continue
                path, opened = f"{prefix}/{entry.name}", None
                try:
                    if entry.is_symlink():
                        self._record_skip(skipped, path, LINKED)
                    elif entry.is_dir(follow_symlinks=False):
                        opened = os.open(entry.name, _DIR, dir_fd=fd)
                    elif entry.is_file(follow_symlinks=False):
                        out.append(path)
                    else:
                        self._record_skip(skipped, path, "special files are not allowed")
                except FileNotFoundError:
                    continue
                except PermissionError as error:
                    self._record_skip(skipped, path, "permission denied", error)
                except OSError as error:
                    if error.errno not in {errno.ELOOP, errno.ENOTDIR}:
                        raise
                    self._record_skip(skipped, path, LINKED, error)
                if opened is not None:
                    try:
                        visit(opened, path)
                    finally:
                        os.close(opened)

        if (fd := self._open_scan_dir(directory, skipped)) is None:
            return out
        try:
            visit(fd, directory)
        finally:
            os.close(fd)
        return sorted(out)

    def read_tail(self, name: str, span: int, *, label: str = "") -> tuple[bytes, int]:
        """Read up to the last `span` bytes of a regular file, dropping any split leading line."""
        tag = label or name
        with self.parent(name) as (parent, leaf), os.fdopen(open_regular(parent, leaf, tag), "rb") as stream:
            if not stat.S_ISREG((info := os.fstat(stream.fileno())).st_mode):
                raise Error(f"{tag}: expected a regular file")
            stream.seek(off := max(0, info.st_size - span))
            tail = stream.read(span)
        return (tail.split(b"\n", 1)[1] if b"\n" in tail else b"") if off else tail, off

    def append_log(self, name: str, line: bytes, *, max_lines: int = 500, max_bytes: int = 2 << 20) -> None:
        """Append one private (`0600`) JSONL line to `logs/<name>.jsonl` via `O_APPEND` with amortized compaction."""
        leaf_name, path = f"{name}.jsonl", f"logs/{name}.jsonl"
        entry = line if line.endswith(b"\n") else line + b"\n"
        if len(entry) > max_bytes:
            raise Error(f"{path}: log entry exceeds {max_bytes} bytes")
        key = (self.root, leaf_name)
        with _LOG_LOCK, writer(self, name="logs.lock"):
            logs_store = private_dir(self, "logs")
            size_after = append_regular(logs_store, leaf_name, entry, mode=0o600, label=path)
            if key not in _LOG_APPENDS and len(_LOG_APPENDS) >= 256:
                _LOG_APPENDS.pop(next(iter(_LOG_APPENDS)))
            count = _LOG_APPENDS.get(key, 0) + 1
            _LOG_APPENDS[key] = count
            stride = max(1, max_lines // 4)
            limit = max_lines if max_lines <= 16 else max_lines + stride
            check_lines = max_lines <= 16 or count == 1 or count % stride == 0
            if size_after > max_bytes or check_lines:
                tail, off = logs_store.read_tail(leaf_name, max_bytes + 1, label=path)
                raw_lines = tail.splitlines(keepends=True)
                if off > 0 or size_after > max_bytes or len(raw_lines) > limit:
                    kept = tail_lines(raw_lines, max_lines, max_bytes) or ([entry] if max_lines > 0 else [])
                    logs_store.write(leaf_name, b"".join(kept), mode=0o600, durable=False)


def expand(path: Path) -> Path:
    try:
        return path.expanduser()
    except RuntimeError as error:
        hint = "set HOME" if path.parts[:1] == ("~",) else "check the user name"
        raise Error(f"cannot resolve home directory in path; {hint}") from error


def private_dir(store: Store, *parts: str, create: bool = True) -> Store:
    """Ensure `<brain>/.bf/...` or `logs/` exists with mode `0700` owned by the current user without symlinks."""
    target = parts or (".bf",)
    if any(not p or p in {".", ".."} or "/" in p or "\\" in p or "\x00" in p for p in target):
        raise Error("invalid private state path")
    fd = os.open(store.root, _DIR)
    try:
        for part in target:
            try:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=fd)
                        os.fsync(fd)
                child = os.open(part, _DIR, dir_fd=fd)
            except OSError as error:
                if error.errno == errno.ENOENT and not create:
                    raise
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise Error("state directory must be a real directory without symlinks") from error
                raise Error(
                    f"state directory {part} is inaccessible{f' ({error.strerror})' if error.strerror else ''}"
                ) from error
            os.close(fd)
            fd = child
            if (info := os.fstat(fd)).st_uid != os.geteuid() or info.st_mode & 0o077:
                raise Error(f"state directory {'/'.join(target)} must be owned by you with mode 700")
    finally:
        os.close(fd)
    return Store(store.root, _base=store.root, _private=tuple(target))


@contextmanager
def writer(store: Store, wait: float = 10.0, *, busy: str = _WRITE_BUSY, name: str = "write.lock") -> Generator[None]:
    """Hold an exclusive advisory lock on `.bf/write.lock` (or `.bf/<name>`)."""
    with private_dir(store).parent(name) as (parent, leaf):
        fd = os.open(leaf, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
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
