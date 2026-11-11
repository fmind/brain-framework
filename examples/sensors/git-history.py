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

import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import _sensor

# `git log --since-as-filter` first shipped in Git 2.37.
MIN_GIT = (2, 37)
MAX_REPOSITORIES = 200
MAX_COMMITS = 10000
MAX_BYTES = 16 << 20
AUTOMATED = re.compile(r"(\[bot\]@users\.noreply\.github\.com|@([a-z0-9-]+\.)*(invalid|test|example|localhost))$")
InvalidError = _sensor.InvalidError
repository_identity = _sensor.git_remote_identity


def remote(repository: Path) -> str:
    """The origin remote's repository identity, never its raw URL or credentials; "" when there is none."""
    try:
        raw = _sensor.run(["git", "-C", str(repository), "remote", "get-url", "origin"], timeout=10, max_bytes=8192)
    except _sensor.ProviderError as error:
        if error.status is None:
            raise
        return ""
    except _sensor.InvalidError:
        return ""
    return repository_identity(raw.decode(errors="replace"))


def nameable(relative: str) -> bool:
    """Whether a folder name can appear in record ids and titles: UTF-8, without control characters."""
    try:
        relative.encode()
    except UnicodeEncodeError:
        return False
    return not _sensor.CONTROL.search(relative)


def shared(marker: Path) -> tuple[str, bool] | None:
    """The Git directory a checkout shares with its repository's other worktrees, and whether it is its own.

    A `.git` folder is its own repository. `git worktree add` writes a `.git` file naming `COMMON/worktrees/NAME`,
    whose `commondir` file leads back to the repository, bare or not; a submodule or `--separate-git-dir` checkout
    names a repository of its own, without one. A malformed or pruned marker stands for itself, so Git fails there
    by name. None means no repository: no marker, or a symlinked one. Any other OSError is an unreadable folder.
    """
    try:
        info = marker.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(info.st_mode):
        return None
    if not stat.S_ISREG(info.st_mode):
        return os.path.realpath(marker), True
    with marker.open("rb") as stream:
        header = os.fsdecode(stream.read(4096)).strip()
    if not header.startswith("gitdir: "):
        return os.path.realpath(marker), True
    folder = marker.parent / header.removeprefix("gitdir: ")
    try:
        with (folder / "commondir").open("rb") as stream:
            common = os.fsdecode(stream.read(4096)).strip()
    except (FileNotFoundError, NotADirectoryError):
        return os.path.realpath(folder), True
    return os.path.realpath(folder / common), False


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
    candidates = list(parents)
    for parent in parents:
        try:
            candidates.extend(directories(parent))
        except OSError:
            skipped.add(parent)
    # Worktrees share their repository's history: group checkouts by the Git directory they share.
    groups: dict[str, list[tuple[str, bool]]] = {}
    for path in candidates:
        try:
            found = shared(root / path / ".git")
        except OSError:
            skipped.add(path)
            continue
        if found is None:
            continue
        if not nameable(path.as_posix()):
            skipped.add(path)
            continue
        groups.setdefault(found[0], []).append((path.as_posix(), found[1]))
    real = Path(os.path.realpath(root))
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
        if name not in skip and not any(relative in skip for relative, _ in members):
            selected.append((name, root / checkout))
    if len(selected) > MAX_REPOSITORIES:
        raise InvalidError(f"more than {MAX_REPOSITORIES} repositories; select a narrower ROOT or add --skip")
    return sorted(selected), len(skipped)


def supported() -> None:
    """Check Git once: an older Git rejects `--since-as-filter`, which would fail every repository by name."""
    try:
        reply = _sensor.run(["git", "version"], timeout=10, max_bytes=1024).decode(errors="replace")
    except _sensor.SensorError:
        reply = ""
    found = re.match(r"git version (\d+)\.(\d+)", reply)
    if not found or (int(found[1]), int(found[2])) < MIN_GIT:
        raise InvalidError("Git 2.37 or later is required; install it on PATH")


def history(repository: Path, start: str, end: str) -> list[str]:
    """One repository's commits in the window, as hash, committer time, author email and message fields."""
    payload = _sensor.run(
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
            origin = remote(repository)
            fields = history(repository, start, end)
        except InvalidError as error:
            raise InvalidError(f"{relative}: {error}") from None
        except (_sensor.ProviderError, ValueError):
            # A stale worktree, damaged objects, another owner's repository or a stalled mount: name it so it can
            # be repaired or skipped. Its window is retried, never passed over.
            raise InvalidError(
                f"{relative}: Git failed or timed out; repair the repository or add --skip {relative}"
            ) from None
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
            repository_ref = "repo:local/" + quote(relative, safe="/")
            repo_refs = [repository_ref, *([origin] if origin else [])]
            author_person = _sensor.person(author)
            author_refs = [author_person] if author_person else []
            message = message.strip()
            subject = message.partition("\n")[0]
            key = relative + "@" + commit
            records.append(
                _sensor.record(
                    key,
                    _sensor.line(f"{relative}: {subject}", relative),
                    time=when,
                    text=f"Repository: {relative}\nCommit: {commit}\nAuthor: {author}\nCommitted: {when}\n\n{message}",
                    links=[*repo_refs, *author_refs],
                    aliases=["commit:local/" + quote(key, safe="/@")],
                    attributes={
                        "repository": relative,
                        "commit": commit,
                        "author_email": author,
                        "author_refs": author_refs,
                        "repository_refs": repo_refs,
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
    _sensor.emit(result, max_bytes=MAX_BYTES)
    if skipped:
        print(f"Git history skipped {skipped} unreadable, non-UTF-8 or control-character folders.", file=sys.stderr)


if __name__ == "__main__":
    _sensor.cli(main, "Git history collection failed", "check repository access and the requested window")
