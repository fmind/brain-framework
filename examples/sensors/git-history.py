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
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

# `git log --since-as-filter` first shipped in Git 2.37.
MIN_GIT = (2, 37)
MAX_REPOSITORIES = 200
MAX_COMMITS = 10000
MAX_BYTES = 16 << 20
MAX_REF = 8192
MAX_LINKS = 1000
AUTOMATED = re.compile(r"(\[bot\]@users\.noreply\.github\.com|@([a-z0-9-]+\.)*(invalid|test|example|localhost))$")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
IDENTITY = re.compile(r"[a-z][a-z0-9+.-]*:\S+")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
_SCP_REMOTE = re.compile(r"^git@([A-Za-z0-9.-]+):([^/\s]+)/([^/\s]+?)(?:\.git)?$")
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


def reference(value: str) -> str:
    if (
        not value
        or len(value) > MAX_REF
        or any(c.isspace() for c in value)
        or CONTROL.search(value)
        or not IDENTITY.fullmatch(value)
    ):
        return ""
    return value


def person(email: str) -> str:
    cleaned = email.strip().lower()
    return reference("person:email/" + quote(cleaned, safe="@.")) if "@" in cleaned else ""


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


def _valid_host(host: str) -> str:
    lowered = host.lower()
    labels = lowered.split(".")
    if (
        len(labels) < 2
        or any(not _DNS_LABEL.fullmatch(lbl) for lbl in labels)
        or not re.fullmatch(r"[a-z]{2,63}", labels[-1])
    ):
        return ""
    return lowered


def repository_identity(raw_url: str) -> str:
    """Canonical lowercase repo identity for credential-free HTTPS, ssh://git@, and git@HOST:OWNER/REPO remotes."""
    url = raw_url.strip()
    if not url or CONTROL.search(url):
        return ""
    if match := _SCP_REMOTE.fullmatch(url):
        host, owner, name = _valid_host(match[1]), match[2], match[3]
    else:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return ""
        if parsed.query or parsed.fragment or parsed.port is not None:
            return ""
        if parsed.scheme == "https":
            if parsed.username is not None or parsed.password is not None:
                return ""
        elif parsed.scheme == "ssh":
            if parsed.username != "git" or parsed.password is not None or parsed.netloc.partition("@")[0] != "git":
                return ""
        else:
            return ""
        host = _valid_host(parsed.hostname or "")
        path = parsed.path.lstrip("/")
        path = path.removesuffix(".git")
        parts = path.split("/")
        if len(parts) != 2:
            return ""
        owner, name = parts
    if (
        not host
        or not _SEGMENT.fullmatch(owner)
        or not _SEGMENT.fullmatch(name)
        or owner in {".", ".."}
        or name in {".", ".."}
    ):
        return ""
    return reference(f"repo:{host}/{quote(owner.lower(), safe='')}/{quote(name.lower(), safe='')}")


def make_record(
    identifier: str,
    title: str,
    *,
    item_type: str,
    time_value: str,
    text: str,
    aliases: Sequence[str],
    relations: dict[str, list[str]],
    attributes: dict[str, object],
) -> dict[str, object]:
    out: dict[str, object] = {
        "id": identifier,
        "title": line(title, identifier),
        "@type": item_type,
        "text": text,
        "time": iso_utc(time_value),
    }
    valid_aliases = sorted({a for a in aliases if reference(a)})
    if valid_aliases:
        out["aliases"] = valid_aliases
    cleaned = {k: list(dict.fromkeys(r for r in v if reference(r)))[:MAX_LINKS] for k, v in relations.items()}
    nonempty = {k: v for k, v in cleaned.items() if v}
    if nonempty:
        out["relations"] = nonempty
    if attributes:
        out["attributes"] = attributes
    return out


def remote(repository: Path) -> str:
    """The origin remote's repository identity, never its raw URL or credentials; "" when there is none."""
    try:
        raw = run(["git", "-C", str(repository), "remote", "get-url", "origin"], timeout=10, max_bytes=8192)
    except ProviderError as error:
        if error.status is None:
            raise
        return ""
    except InvalidError:
        return ""
    return repository_identity(raw.decode(errors="replace"))


def nameable(relative: str) -> bool:
    """Whether a folder name can appear in record ids and titles: UTF-8, without control characters."""
    try:
        relative.encode()
    except UnicodeEncodeError:
        return False
    return not CONTROL.search(relative)


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
            origin = remote(repository)
            fields = history(repository, start, end)
        except InvalidError as error:
            raise InvalidError(f"{relative}: {error}") from None
        except (ProviderError, ValueError):
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
            author_person = person(author)
            author_refs = [author_person] if author_person else []
            message = message.strip()
            subject = message.partition("\n")[0]
            key = relative + "@" + commit
            records.append(
                make_record(
                    key,
                    line(f"{relative}: {subject}", relative),
                    item_type="Commit",
                    time_value=when,
                    text=f"Repository: {relative}\nCommit: {commit}\nAuthor: {author}\nCommitted: {when}\n\n{message}",
                    aliases=["commit:local/" + quote(key, safe="/@")],
                    relations={
                        "author": author_refs,
                        "repository": repo_refs,
                    },
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
