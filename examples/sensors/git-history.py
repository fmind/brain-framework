#!/usr/bin/env python3
"""Collect bounded local Git history as Brain Framework records; no provider authentication.

Usage: git-history.py ROOT START END [--skip RELATIVE_REPO]...
Requires Git 2.37 or later on PATH.
Scans repositories one or two levels below ROOT. Automated history stays out: hidden repositories (such as
~/.codex/memories), repositories named with --skip (such as an autonomous agent loop), commits by bots or
reserved test domains, and internal refs such as stashes and notes.
"""

import json
import os
import re
import selectors
import subprocess
import sys
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# `git log --since-as-filter` first shipped in Git 2.37.
MIN_GIT = (2, 37)
MAX_REPOSITORIES = 200
MAX_COMMITS = 10000
MAX_BYTES = 16 << 20
# BF record bounds: titles and links are bounded single lines.
MAX_TITLE, MAX_REF = 4096, 8192
# C0 and C1 control characters, which BF rejects in ids, titles, URLs and identities.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class InvalidError(ValueError):
    """A content-free diagnostic safe to show on stderr."""


def run(argv: list[str], limit: int, timeout: int) -> bytes:
    """Bound provider output while it runs; inherit Brain Framework's cancellable process group."""
    # Only literal provider commands reach this helper; no shell interprets argv.
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        if child.stdout is None:
            raise ValueError("provider pipe is missing")
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("provider exceeded its timeout")
                if not selector.get_map():
                    with suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=0.05)
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    output.extend(chunk)
                    if len(output) > limit:
                        raise InvalidError("Git output exceeds its byte limit; narrow the window")
        if code := child.wait():
            raise subprocess.CalledProcessError(code, argv)
        return bytes(output)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


def github(repository: Path) -> str:
    """Project only a recognized GitHub identity, never raw remote credentials."""
    try:
        raw = run(["git", "-C", str(repository), "remote", "get-url", "origin"], 8192, 10)
    except (subprocess.CalledProcessError, ValueError):
        return ""
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?",
        raw.decode(errors="replace").strip(),
    )
    return "repo:github.com/" + match[1].lower() if match else ""


def nameable(relative: str) -> bool:
    """Whether a folder name can appear in record ids and titles: UTF-8, without control characters."""
    try:
        relative.encode()
    except UnicodeEncodeError:
        return False
    return not CONTROL.search(relative)


def line(value: str, fallback: str) -> str:
    """One bounded title line: control characters become spaces and whitespace collapses."""
    return " ".join(CONTROL.sub(" ", value).split())[:MAX_TITLE].rstrip() or fallback


AUTOMATED = re.compile(r"(\[bot\]@users\.noreply\.github\.com|@([a-z0-9-]+\.)*(invalid|test|example|localhost))$")


def linked(marker: Path) -> bool:
    """Whether a `.git` file names a linked worktree, whose history its main checkout already holds.

    `git worktree add` writes `gitdir: COMMON/worktrees/NAME`, a folder holding a `commondir` file; a submodule's
    `.git` file names its own repository, without one.
    """
    if not marker.is_file():
        return False
    try:
        with marker.open("rb") as stream:
            line = stream.read(4096).decode(errors="replace").strip()
    except OSError:
        return False
    if not line.startswith("gitdir: "):
        return False
    folder = Path(line.removeprefix("gitdir: "))
    return (folder if folder.is_absolute() else marker.parent / folder).joinpath("commondir").is_file()


def repositories(root: Path, skip: frozenset[str]) -> tuple[list[Path], int]:
    """Repositories one or two levels below root, and the count of folders that could not be used.

    An unreadable folder cannot hold a readable repository, and window mode never removes saved records, so
    skipping one (for example a protected ~/Documents under launchd) cannot lose evidence already collected.
    """
    skipped: set[Path] = set()

    def directories(parent: Path) -> list[Path]:
        return [p for p in parent.iterdir() if not p.name.startswith(".") and not p.is_symlink() and p.is_dir()]

    parents = directories(root)
    candidates = list(parents)
    for parent in parents:
        try:
            candidates.extend(directories(parent))
        except PermissionError:
            skipped.add(parent)
    found = []
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        try:
            marker = path / ".git"
            # A linked worktree would collect its repository's whole history again under another name.
            if not marker.exists() or marker.is_symlink() or relative in skip or linked(marker):
                continue
        except PermissionError:
            skipped.add(path)
            continue
        if not nameable(relative):
            skipped.add(path)
            continue
        found.append(path)
    if len(found) > MAX_REPOSITORIES:
        raise InvalidError(f"more than {MAX_REPOSITORIES} repositories; select a narrower ROOT or add --skip")
    return sorted(found), len(skipped)


def supported() -> None:
    """Check Git once: an older Git rejects `--since-as-filter`, which would fail every repository by name."""
    try:
        reply = run(["git", "version"], 1024, 10).decode(errors="replace")
    except (OSError, ValueError, TimeoutError, subprocess.CalledProcessError):
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
        MAX_BYTES,
        30,
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
    if not root.is_dir():
        raise InvalidError("ROOT is not a directory")
    selected, skipped = repositories(root, skip)
    if selected:
        supported()
    records: list[dict[str, object]] = []
    for repository in selected:
        relative = repository.relative_to(root).as_posix()
        try:
            remote = github(repository)
            fields = history(repository, start, end)
        except InvalidError as error:
            raise InvalidError(f"{relative}: {error}") from None
        except (subprocess.CalledProcessError, TimeoutError, ValueError):
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
            links = [repository_ref, *([remote] if remote else [])]
            person = "person:email/" + quote(author, safe="/:@+").replace("~", "%7E")
            if "@" in author and len(person) <= MAX_REF:
                # An over-long author email is dropped: one invalid link would fail the whole collection.
                links.append(person)
            message = message.strip()
            subject = message.partition("\n")[0]
            key = relative + "@" + commit
            records.append(
                {
                    "id": key,
                    "title": line(f"{relative}: {subject}", relative),
                    "time": when,
                    "text": f"Repository: {relative}\nCommit: {commit}\nAuthor: {author}\nCommitted: {when}\n\n{message}",
                    "links": sorted(links),
                    "aliases": ["commit:local/" + quote(key, safe="/@")],
                    "attributes": {
                        "repository": relative,
                        "commit": commit,
                        "author_email": author,
                        "author_refs": [link for link in links if link.startswith("person:")],
                        "repository_refs": [link for link in links if link.startswith("repo:")],
                    },
                }
            )
        if len(records) > MAX_COMMITS:
            raise InvalidError(f"the window has more than {MAX_COMMITS} commits; narrow it")
    return records, skipped


if __name__ == "__main__":
    try:
        options = sys.argv[4:]
        if len(sys.argv) < 4 or len(options) % 2 or any(flag != "--skip" for flag in options[::2]):
            raise InvalidError("expected ROOT START END [--skip RELATIVE_REPO]...")
        result, skipped = collect(Path(sys.argv[1]), sys.argv[2], sys.argv[3], frozenset(options[1::2]))
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > MAX_BYTES:
            raise InvalidError("normalized history exceeds 16 MiB; narrow the window")
        if skipped:
            print(f"Git history skipped {skipped} unreadable, non-UTF-8 or control-character folders.", file=sys.stderr)
        print(payload)
    except InvalidError as error:
        print(f"Git history collection failed: {error}.", file=sys.stderr)
        sys.exit(1)
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        print("Git history collection failed; check repository access and the requested window.", file=sys.stderr)
        sys.exit(1)
