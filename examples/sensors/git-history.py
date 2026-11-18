#!/usr/bin/env python3
"""Collect bounded local Git history as Brain Framework records; no provider authentication.

Usage: git-history.py ROOT START END [--skip RELATIVE_REPO]...
Requires Git 2.37 or later on PATH.
Scans checkouts one or two levels below ROOT and collects each repository once, however many of its worktrees
are there. Automated history stays out: hidden repositories (such as ~/.codex/memories), repositories named
with --skip (such as an autonomous agent loop), commits by bots or reserved test domains, and internal refs
such as stashes and notes.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

# `git log --since-as-filter` first shipped in Git 2.37.
MIN_GIT = (2, 37)
MAX_REPOSITORIES = 200
MAX_COMMITS = 10000
MAX_BYTES = 16 << 20
AUTOMATED = re.compile(r"(\[bot\]@users\.noreply\.github\.com|@([a-z0-9-]+\.)*(invalid|test|example|localhost))$")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_GIT_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
)


class InvalidError(ValueError):
    """Input or repository state violated the expected contract."""


class ProviderError(Exception):
    """Git subprocess failed, timed out, or could not start."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def run(argv: Sequence[str], *, timeout: float, max_bytes: int) -> bytes:
    """Execute Git from / without inherited hook GIT_* overrides, with bounded output and process-group reaping."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV}
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd="/",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise ProviderError(f"cannot start {argv[0]}") from error
    stdout, stderr = proc.stdout, proc.stderr
    if stdout is None or stderr is None:
        if stdout is not None:
            stdout.close()
        if stderr is not None:
            stderr.close()
        raise RuntimeError("subprocess pipes missing")
    out_fd, err_fd = stdout.fileno(), stderr.fileno()
    chunks: list[bytes] = []
    overflow = False

    def drain_out() -> None:
        nonlocal overflow
        total = 0
        while data := os.read(out_fd, 65536):
            total += len(data)
            if total > max_bytes:
                overflow = True
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                return
            chunks.append(data)

    def drain_err() -> None:
        while os.read(err_fd, 65536):
            pass

    t_out = threading.Thread(target=drain_out, daemon=True)
    t_err = threading.Thread(target=drain_err, daemon=True)
    t_out.start()
    t_err.start()
    deadline = time.monotonic() + timeout
    try:
        while t_out.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise ProviderError(f"{argv[0]} exceeded its {int(timeout)}s timeout")
            t_out.join(timeout=min(remaining, 0.05))
        try:
            remaining = max(0.0, deadline - time.monotonic())
            code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise ProviderError(f"{argv[0]} exceeded its {int(timeout)}s timeout") from None
    finally:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        stdout.close()
        stderr.close()
    if overflow:
        raise InvalidError(f"{argv[0]} output exceeds {max_bytes} bytes")
    if code != 0:
        raise ProviderError(f"{argv[0]} exited with status {code}", status=code)
    return b"".join(chunks)


def line(value: str, fallback: str = "") -> str:
    cleaned = " ".join(CONTROL.sub(" ", value).split())
    return (cleaned[:4096] or fallback)[:4096] if (cleaned or fallback) else ""


def iso_utc(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise InvalidError("timestamps require timezones")
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def emit(records: list[dict[str, object]], *, max_bytes: int = MAX_BYTES) -> None:
    payload = (json.dumps(records, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > max_bytes:
        raise InvalidError(f"output exceeds {max_bytes} bytes")
    sys.stdout.buffer.write(payload)


def cli(action: Callable[[], None], summary: str, hint: str) -> None:
    try:
        action()
    except (InvalidError, ProviderError) as error:
        sys.stderr.write(f"{summary}: {error}.\n")
        sys.exit(1)
    except (OSError, UnicodeError, ValueError, LookupError, ArithmeticError, RecursionError):
        sys.stderr.write(f"{summary}; {hint}.\n")
        sys.exit(1)


def make_record(
    identifier: str,
    title: str,
    *,
    item_type: str,
    time_value: str,
    text: str,
    attributes: dict[str, object],
) -> dict[str, object]:
    out: dict[str, object] = {
        "id": identifier,
        "title": line(title, identifier),
        "type": item_type.lower(),
        "text": text,
        "time": iso_utc(time_value),
    }
    if attributes:
        out["attributes"] = attributes
    return out


def nameable(relative: str) -> bool:
    """Whether a folder name can appear in record ids and titles: UTF-8, without control or format characters."""
    try:
        relative.encode()
    except UnicodeEncodeError:
        return False
    return not CONTROL.search(relative) and not any(unicodedata.category(ch) == "Cf" for ch in relative)


def _read_regular(path: Path, label: str) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise InvalidError(f"{label} symlink is refused") from None
        raise
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise InvalidError(f"{label} must be a regular file")
        return os.fsdecode(stream.read(4096)).strip()


def _audit_git_dir(git_dir: Path, real_root: Path) -> None:
    """Reject symlinked administrative entries, grafts, or external alternates inside `git_dir`."""

    def scan_dir(path: Path, *, recurse: bool = False, depth: int = 0) -> None:
        if depth > 16:
            raise InvalidError("git refs exceed depth limit")
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        raise InvalidError(f"symlinked git entry {entry.name!r} is refused")
                    if recurse and entry.is_dir(follow_symlinks=False):
                        scan_dir(path / entry.name, recurse=True, depth=depth + 1)
        finally:
            os.close(fd)

    scan_dir(git_dir)
    for sub, recurse in (
        (git_dir / "info", False),
        (git_dir / "refs", True),
        (git_dir / "objects", False),
        (git_dir / "objects" / "info", False),
    ):
        with contextlib.suppress(FileNotFoundError):
            scan_dir(sub, recurse=recurse)
    if (git_dir / "objects" / "info" / "http-alternates").exists() or (git_dir / "info" / "grafts").exists():
        raise InvalidError("git grafts and http-alternates are refused")
    with contextlib.suppress(FileNotFoundError):
        raw_alt = _read_regular(git_dir / "objects" / "info" / "alternates", "alternates")
        for raw_ln in raw_alt.splitlines():
            if (ln := raw_ln.strip()) and not ln.startswith("#"):
                alt = Path(os.path.realpath((git_dir / "objects") / ln))
                if not alt.is_relative_to(real_root) or not alt.is_dir():
                    raise InvalidError("git alternate object store escapes ROOT")


def shared(marker: Path, real_root: Path) -> tuple[str, bool] | None:
    """The Git directory a checkout shares with its repository's other worktrees, and whether it is its own.

    A `.git` folder without `commondir` is its own repository. `git worktree add` writes a `.git` file naming
    `COMMON/worktrees/NAME`, whose `commondir` file leads back to the repository inside `ROOT`. Any `.git` entry,
    `commondir`, symlinked ref/object entry, or alternate pointing outside `ROOT` raises `InvalidError`.
    """
    try:
        info = marker.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(info.st_mode):
        return None
    if stat.S_ISDIR(info.st_mode):
        folder = Path(os.path.realpath(marker))
        if not folder.is_relative_to(real_root):
            raise InvalidError(".git directory resolves outside ROOT")
    elif stat.S_ISREG(info.st_mode):
        header = _read_regular(marker, ".git")
        if not header.startswith("gitdir: "):
            return os.path.realpath(marker), True
        folder = Path(os.path.realpath(marker.parent / header.removeprefix("gitdir: ")))
        if not folder.is_relative_to(real_root):
            raise InvalidError("gitdir target escapes ROOT")
    else:
        raise InvalidError("special .git entry is refused")
    _audit_git_dir(folder, real_root)
    try:
        common = _read_regular(folder / "commondir", "commondir")
    except (FileNotFoundError, NotADirectoryError):
        return str(folder), True
    resolved_common = Path(os.path.realpath(folder / common))
    if not resolved_common.is_relative_to(real_root):
        raise InvalidError("commondir target escapes ROOT")
    _audit_git_dir(resolved_common, real_root)
    return str(resolved_common), False


def repositories(root: Path, skip: frozenset[str]) -> tuple[list[tuple[str, Path]], int]:
    """Repositories checked out one or two levels below root, and the count of folders that could not be used.

    Each repository comes once, as its name and the checkout Git reads. An unreadable folder cannot hold a
    readable repository, and window mode never removes saved records, so skipping one (for example a protected
    ~/Documents under launchd) cannot lose evidence already collected.
    """
    skipped: set[Path] = set()

    # os.scandir and lstat raise on an unreadable folder; from Python 3.14, pathlib's checks return False instead.
    def directories(parent: Path) -> list[Path]:
        with os.scandir(root / parent) as entries:
            return [
                parent / entry.name
                for entry in entries
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False)
            ]

    try:
        parents = directories(Path())
    except (FileNotFoundError, NotADirectoryError):
        raise InvalidError("ROOT is not a directory") from None
    real = Path(os.path.realpath(root))
    candidates = list(parents)
    for parent in parents:
        try:
            candidates.extend(directories(parent))
        except OSError:
            skipped.add(parent)
    # Worktrees share their repository's history: group checkouts by the Git directory they share.
    groups: dict[str, list[tuple[str, bool]]] = {}
    for path in candidates:
        rel_posix = path.as_posix()
        if rel_posix in skip or path.parts[0] in skip:
            continue
        try:
            found = shared(root / path / ".git", real)
        except InvalidError as error:
            raise InvalidError(f"{rel_posix}: {error}") from None
        except OSError:
            skipped.add(path)
            continue
        if found is None:
            continue
        if not nameable(rel_posix):
            skipped.add(path)
            continue
        groups.setdefault(found[0], []).append((rel_posix, found[1]))
    selected = []
    for common, members in groups.items():
        owners = [relative for relative, own in members if own]
        name = checkout = min(owners or [relative for relative, _ in members])
        if not owners:
            # Only worktrees are scanned. Name the repository after its own folder below ROOT, such as a bare
            # `project.git`, so adding or removing a worktree keeps its record ids; else after its first worktree.
            home = Path(common).parent if Path(common).name == ".git" else Path(common)
            inside = home.relative_to(real).as_posix() if home.is_relative_to(real) else "."
            name = inside if inside != "." and nameable(inside) else checkout
        if (
            name not in skip
            and Path(name).parts[0] not in skip
            and not any(relative in skip for relative, _ in members)
        ):
            selected.append((name, root / checkout))
    if len(selected) > MAX_REPOSITORIES:
        raise InvalidError(f"more than {MAX_REPOSITORIES} repositories; select a narrower ROOT or add --skip")
    return sorted(selected), len(skipped)


def supported() -> None:
    """Check Git once: an older Git rejects `--since-as-filter`, which would fail every repository by name."""
    try:
        reply = run(["git", "version"], timeout=10, max_bytes=1024).decode(errors="replace")
    except ProviderError:
        reply = ""
    found = re.match(r"git version (\d+)\.(\d+)", reply)
    if not found or (int(found[1]), int(found[2])) < MIN_GIT:
        raise InvalidError("Git 2.37 or later is required; install it on PATH")


def history(repository: Path, start: str, end: str) -> list[str]:
    """One repository's commits in the window, as hash, committer time, author email and message fields."""
    payload = run(
        [
            "git",
            "-C",
            str(repository),
            "log",
            f"--max-count={MAX_COMMITS + 1}",
            # Visit older commits too: committer dates need not follow ancestry order.
            f"--since-as-filter={start}",
            f"--until={end}",
            "--no-show-signature",
            "--no-notes",
            "--encoding=UTF-8",
            "-z",
            "--format=%H%x00%cI%x00%ae%x00%B",
            # Branches, tags, remote branches and a detached HEAD; never stashes, notes or other internal
            # refs. An unborn HEAD in a new repository is not an error.
            "--ignore-missing",
            "--branches",
            "--tags",
            "--remotes",
            "HEAD",
            "--",
        ],
        timeout=30,
        max_bytes=MAX_BYTES,
    )
    # A legacy commit without an encoding header keeps its message, with replacement characters.
    fields = payload.decode(errors="replace").split("\0")
    if fields[-1] != "" or (len(fields) - 1) % 4:
        raise InvalidError("Git returned unexpected log framing")
    if (len(fields) - 1) // 4 > MAX_COMMITS:
        raise InvalidError(f"more than {MAX_COMMITS} commits in the window; narrow it")
    return fields[:-1]


def collect(
    root: Path, start: str, end: str, skip: frozenset[str] = frozenset()
) -> tuple[list[dict[str, object]], int]:
    try:
        begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        raise InvalidError("START and END must be ISO 8601 timestamps") from None
    if begin.tzinfo is None or finish.tzinfo is None or begin >= finish:
        raise InvalidError("START and END need timezones, with START before END")
    selected, skipped = repositories(root, skip)
    if selected:
        supported()
    records: list[dict[str, object]] = []
    for relative, repository in selected:
        try:
            fields = history(repository, start, end)
        except InvalidError as error:
            raise InvalidError(f"{relative}: {error}") from None
        except (ProviderError, ValueError):
            # A stale worktree, damaged objects, another owner's repository or a stalled mount: name it so it can
            # be repaired or skipped. Its window is retried, never passed over.
            raise InvalidError(
                f"{relative}: Git failed or timed out; repair the repository or add --skip {relative}"
            ) from None
        encoded_repo = quote(relative, safe="/")
        for offset in range(0, len(fields), 4):
            commit, when, author, message = fields[offset : offset + 4]
            # Git's date bounds are inclusive; BF windows are [start, end).
            instant = datetime.fromisoformat(when)
            if instant.tzinfo is None:
                raise InvalidError("Git returned a commit time without a timezone")
            if not begin <= instant < finish:
                continue
            author = author.strip().lower()
            if AUTOMATED.search(author):
                continue
            message = message.strip()
            subject = message.partition("\n")[0]
            key = f"{encoded_repo}@{commit}"
            records.append(
                make_record(
                    key,
                    line(f"{relative}: {subject}", relative),
                    item_type="Commit",
                    time_value=when,
                    text=f"Repository: {relative}\nCommit: {commit}\nAuthor: {author}\nCommitted: {when}\n\n{message}",
                    attributes={
                        "repository": relative,
                        "commit": commit,
                        "author_email": author,
                    },
                )
            )
        if len(records) > MAX_COMMITS:
            raise InvalidError(f"the window has more than {MAX_COMMITS} commits; narrow it")
    return records, skipped


def main() -> None:
    options = sys.argv[4:]
    if len(sys.argv) < 4 or len(options) % 2 or any(flag != "--skip" for flag in options[::2]):
        raise InvalidError("expected ROOT START END [--skip RELATIVE_REPO]...")
    # Git starts from /: a relative ROOT names a folder below the brain, where BF runs this sensor.
    result, skipped = collect(Path(sys.argv[1]).absolute(), sys.argv[2], sys.argv[3], frozenset(options[1::2]))
    emit(result, max_bytes=MAX_BYTES)
    if skipped:
        print(f"Git history skipped {skipped} unreadable, non-UTF-8 or control-character folders.", file=sys.stderr)


if __name__ == "__main__":
    cli(main, "Git history collection failed", "check repository access and the requested window")
