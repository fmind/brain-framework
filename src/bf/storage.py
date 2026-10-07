"""Confined, bounded filesystem access and one physical-brain writer lock."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path, PurePosixPath

from bf.models import MAX_FILE, MAX_FILES, Error, digest


class BusyError(Error):
    """Another process holds the brain writer lock or runs the same program."""


class FileAncestorError(Error):
    """A regular file stands where a path needs a directory, so nothing below it can exist."""


_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# The temporary name `write` gives new bytes before renaming them over their file.
_TEMPORARY = re.compile(r"\.write-[0-9a-f]{32}")
# A scanned name BF cannot address: its bytes are not UTF-8, or it holds a backslash, which relative() rejects.
UNNAMED = "file name is not valid UTF-8 or contains a backslash; rename it"


def temporary(path: str) -> bool:
    """A file an interrupted write left behind: BF names its temporaries `.write-HEX`, never a note or an input."""
    return _TEMPORARY.fullmatch(path.rsplit("/", 1)[-1]) is not None


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
        return self.stamped(name, limit)[0]

    def stamped(self, name: str, limit: int = MAX_FILE) -> tuple[bytes, int]:
        """A regular file's bytes and the modification time, in nanoseconds, of the descriptor that read them."""
        with self.parent(name) as (parent, leaf):
            try:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise Error(f"{name}: symlinks are not followed; replace it with a regular file") from error
                # A Unix socket fails to open before the type check below: ENXIO on Linux, EOPNOTSUPP on macOS.
                if error.errno in {errno.ENXIO, errno.EOPNOTSUPP} and not stat.S_ISREG(
                    os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode
                ):
                    raise Error(f"{name}: expected a regular file") from error
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
        return data, info.st_mtime_ns

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

    def link(self, name: str, target: str) -> None:
        """Replace `target` atomically with a second name for a regular file's bytes, without copying or syncing them.

        Safe as a snapshot only because `write` renames new files into place: no BF write changes an existing inode.
        A later `sync()` of the target's directory persists the entry. Raises OSError where the filesystem cannot link.
        """
        with self.parent(name) as (source, leaf), self.parent(target, create=True) as (parent, new):
            temporary = ".write-" + os.urandom(16).hex()
            # A symlink is linked as itself, never followed; the check below then refuses it.
            os.link(leaf, temporary, src_dir_fd=source, dst_dir_fd=parent, follow_symlinks=False)
            try:
                if not stat.S_ISREG(os.stat(temporary, dir_fd=parent, follow_symlinks=False).st_mode):
                    raise Error(f"{name}: expected a regular file")
                try:
                    info = os.stat(new, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(info.st_mode):
                        raise Error(f"{target}: refusing to replace a non-regular file")
                os.replace(temporary, new, src_dir_fd=parent, dst_dir_fd=parent)
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

    @contextmanager
    def _directory(self, name: str) -> Iterator[int]:
        """A descriptor of one directory, reached and opened without following links."""
        with self.parent(name) as (parent, leaf):
            descriptor = os.open(leaf, _DIR, dir_fd=parent)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def sync(self, directory: str) -> None:
        """Persist the entries that writes and deletes with `durable=False` changed in one directory."""
        with self._directory(directory) as descriptor:
            os.fsync(descriptor)

    def sweep(self, directory: str) -> None:
        """Remove the temporary files that killed `write` calls left directly in a directory.

        Call it under the brain writer lock for a directory only lock holders write, where no such file can belong to
        a live write. Removal need not be durable: a file a crash brings back is removed again.
        """
        try:
            with self._directory(directory) as descriptor, os.scandir(descriptor) as entries:
                for entry in entries:
                    if _TEMPORARY.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                        with suppress(FileNotFoundError):
                            os.unlink(entry.name, dir_fd=descriptor)
        except OSError as error:
            # Nothing to sweep; the caller's own access names a linked or special directory.
            if error.errno not in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                raise

    def rmdir(self, name: str) -> None:
        """Remove an empty directory without following links."""
        with self.parent(name) as (parent, leaf):
            if not stat.S_ISDIR(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
                raise Error(f"{name}: expected a directory")
            os.rmdir(leaf, dir_fd=parent)
            os.fsync(parent)

    def files(
        self, directory: str, *, skipped: dict[str, tuple[int, int, int, int]] | None = None, split: bool = False
    ) -> list[str]:
        return sorted(self.scan(directory, skipped=skipped, split=split))

    def scan(
        self,
        directory: str,
        *,
        skipped: dict[str, tuple[int, int, int, int]] | None = None,
        split: bool = False,
        counts: dict[str, int] | None = None,
        fingerprints: bool = True,
    ) -> dict[str, tuple[int, int, int, int]]:
        """Regular files below a directory, fingerprinted by the traversal's own no-follow stats.

        Symlinks, special files and names BF cannot address are never followed. They fail the scan, or, when
        the caller passes `skipped`, land there with their own fingerprint so the caller can report them and
        carry on; an unaddressable name lands under its escaped form, for which `unnamed` holds. So does a folder
        BF cannot read, the scanned one included.
        Each traversed tree holds at most MAX_FILES entries, counting directories and ignored extensions: the whole
        directory, or with `split`, its own listing and each subdirectory's tree, such as one source of memories/.
        `counts` receives the entries of each tree. Without `fingerprints`, the directory listing alone types regular
        files and folders, without a stat each, and their fingerprints are zero.
        """
        relative(directory)
        result: dict[str, tuple[int, int, int, int]] = {}
        visited = {} if counts is None else counts

        def visit(fd: int, prefix: str, tree: str) -> None:
            if prefix.count("/") > 64:
                raise Error(f"{prefix}: directory exceeds the 64-level depth limit")
            with os.scandir(fd) as entries:
                for entry in entries:
                    visited[tree] = visited.get(tree, 0) + 1
                    if visited[tree] > MAX_FILES:
                        raise Error(f"{tree} exceeds the {MAX_FILES:,}-entry scan limit; {_crowded(tree)}")
                    path = f"{prefix}/{entry.name}"
                    shown = _shown(entry.name)
                    stamp = _UNSTATED
                    try:
                        # The listing types regular files and folders; fingerprints, links and special files need
                        # the entry's own no-follow stat.
                        mode = 0 if fingerprints else _listed(entry)
                        if not mode:
                            info = entry.stat(follow_symlinks=False)
                            mode, stamp = info.st_mode, _fingerprint(info)
                        # An unaddressable folder is reported whole, like a linked one: its entries have no ref.
                        opened = stat.S_ISDIR(mode) and shown is None
                        child = os.open(entry.name, _DIR, dir_fd=fd) if opened else None
                    except FileNotFoundError:
                        # Removed after listing, as by an editor's atomic save; later refreshes compare again.
                        continue
                    except PermissionError:
                        # An unreadable folder is reported whole too, while the rest of the brain still answers.
                        if skipped is None:
                            raise
                        skipped[f"{prefix}/{shown}" if shown is not None else path] = stamp
                        continue
                    if shown is not None:
                        if skipped is None:
                            raise Error(f"{prefix}/{shown}: {UNNAMED}")
                        skipped[f"{prefix}/{shown}"] = stamp
                        continue
                    if child is not None:
                        try:
                            visit(child, path, path if split and prefix == directory else tree)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(mode):
                        result[path] = stamp
                    elif skipped is not None:
                        skipped[path] = stamp
                    else:
                        raise Error(f"{path}: symlinks and special files are forbidden")

        fd = os.open(self.root, _DIR)
        try:
            prefix = []
            for part in relative(directory):
                prefix.append(part)
                stamp = _UNSTATED
                try:
                    info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                    stamp = _fingerprint(info)
                    child = os.open(part, _DIR, dir_fd=fd) if stat.S_ISDIR(info.st_mode) else None
                except FileNotFoundError:
                    return {}
                except PermissionError:
                    # Like a nested folder: an unreadable projects/ or memories/ leaves the rest of the brain answering.
                    if skipped is None:
                        raise
                    skipped["/".join(prefix)] = stamp
                    return {}
                if child is None:
                    path = "/".join(prefix)
                    if skipped is None:
                        raise Error(f"{path}: expected a directory; symlinks and special files are forbidden")
                    skipped[path] = stamp
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
        """An entry's own type and permission bits, never a link target's; FileNotFoundError when it is absent."""
        return self._status(name).st_mode

    def lstat(self, name: str) -> os.stat_result:
        """A regular file's own status, never a link target's."""
        if not stat.S_ISREG((info := self._status(name)).st_mode):
            raise Error(f"{name}: expected a regular file")
        return info

    def fingerprint(self, name: str) -> tuple[int, int, int, int]:
        """Size, mtime, ctime and inode: ctime catches an edit whose mtime was preserved."""
        return _fingerprint(self.lstat(name))


def _listed(entry: os.DirEntry[str]) -> int:
    """The type of a regular file or folder as its directory entry states it, usually without a stat; else 0."""
    if entry.is_file(follow_symlinks=False):
        return stat.S_IFREG
    return stat.S_IFDIR if entry.is_dir(follow_symlinks=False) else 0


# The fingerprint of an entry the scan did not stat: a name-only listing, or a folder it could not reach.
_UNSTATED = (0, 0, 0, 0)


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int]:
    # Hashed inode numbers, as mergerfs reports them, can exceed SQLite's signed 64-bit integers: wrap them.
    inode = info.st_ino - (1 << 64) if info.st_ino >= 1 << 63 else info.st_ino
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, inode


def _crowded(tree: str) -> str:
    """How to bring a tree back under the scan limit."""
    if tree.split("/")[0] == "memories":
        return "split its sensor into several sources or archive older records outside the brain"
    return "keep bulky files in the brain's root inputs/ or originals/, which are not scanned"


def expand(path: Path) -> Path:
    """Expand a leading ~ or ~user; a user without a home directory is an input error, not a crash."""
    try:
        return path.expanduser()
    except RuntimeError as error:
        raise Error("a path's ~ or ~user home directory cannot be resolved; check the user name") from error


def xdg_setting(name: str) -> Path | None:
    """An explicit XDG base directory; like the specification, ignore empty and relative values."""
    try:
        path = Path(os.environ.get(name, "")).expanduser()
    except RuntimeError:
        return None
    return path if path.is_absolute() else None


def xdg(name: str, default: str) -> Path:
    """An XDG base directory, or `default` below home."""
    if found := xdg_setting(name):
        return found
    try:
        return Path.home() / default
    except RuntimeError as error:
        raise Error(f"cannot find the home directory; set HOME or {name}") from error


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
    try:
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
    except OSError as error:
        # Locks, run history and usage live there: every command needs it, whichever brain it reads.
        cause = f" ({error.strerror})" if error.strerror else ""
        raise Error(
            f"state directory {top} is inaccessible{cause}; make it writable or set XDG_STATE_HOME to a writable directory"
        ) from error
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
    # The writer lock also holds the brain directory; other locks order work that only this state directory sees.
    whole = name == "write.lock"
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
        _flock(fd, shared=shared, deadline=deadline, busy=busy)
        if whole:
            with _brain_lock(store, shared=shared, deadline=deadline, busy=busy):
                yield
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


@contextmanager
def _brain_lock(store: Store, *, shared: bool, deadline: float, busy: str) -> Iterator[None]:
    """Also lock the brain directory itself: processes with different state directories, or accounts sharing the
    brain, keep separate lock files, which alone would let two writers commit at once.

    Some network filesystems cannot lock a directory, such as NFS for an exclusive lock: the state lock then stands
    alone, as it did before.
    """
    fd = os.open(store.root, _DIR)
    try:
        try:
            _flock(fd, shared=shared, deadline=deadline, busy=busy)
        except OSError as error:
            if error.errno not in {errno.EBADF, errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL}:
                raise
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
    """Keep record files stable during one consistent read; readers can run concurrently."""
    with _lock(store, "write.lock", wait, shared=True):
        yield


@contextmanager
def building(store: Store, wait: float = 0) -> Iterator[None]:
    """Serialize full search-cache builds, which read the brain without holding its writer lock."""
    with _lock(store, "build.lock", wait, busy="another search cache build is active; retry after it finishes"):
        yield


@contextmanager
def generation(store: Store, *, shared: bool, wait: float) -> Iterator[None]:
    """Search-cache readers hold this shared while connected; replacing the cache file holds it exclusively.

    SQLite finds a database's -wal file by name, so no connection to a replaced cache may outlive its rename.
    Readers pass a gate before connecting, which a replacement closes before it waits: overlapping readers cannot
    keep it waiting forever, since only connections opened before it arrived delay it.
    """
    if shared:
        busy = "the search cache is being replaced; retry shortly"
        with ExitStack() as connected:
            with _lock(store, "cache-gate.lock", wait, shared=True, busy=busy):
                connected.enter_context(_lock(store, "cache.lock", wait, shared=True, busy=busy))
            yield
    else:
        busy = "readers kept the search cache open; retry bf build"
        with (
            _lock(store, "cache-gate.lock", wait, busy=busy),
            _lock(store, "cache.lock", wait, busy=busy),
        ):
            yield


@contextmanager
def collecting(store: Store, sensor: str, wait: float = 0) -> Iterator[None]:
    """Order runs of one sensor without blocking offline reads while its provider is running."""
    relative(sensor)
    with _lock(store, f"collect-{sensor}.lock", wait, busy=f"{sensor} is already running for this brain; retry later"):
        yield
