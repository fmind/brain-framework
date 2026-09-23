#!/usr/bin/env python3
"""Collect bounded local Git history as FKF records; no provider authentication.

Usage: git-history.py ROOT START END [--skip RELATIVE_REPO]...
Automated history stays out: hidden repositories (such as ~/.codex/memories), repositories named with
--skip (such as an autonomous agent loop), and commits by bots or reserved test domains.
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote


def github(repository: Path) -> str:
    """Project only a recognized GitHub identity, never raw remote credentials."""
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            ["git", "-C", str(repository), "remote", "get-url", "origin"],
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        output.seek(0)
        raw = output.read(8193)
    if result.returncode or len(raw) > 8192:
        return ""
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?",
        raw.decode().strip(),
    )
    return "repo:github.com/" + match[1].lower() if match else ""


AUTOMATED = re.compile(r"(\[bot\]@users\.noreply\.github\.com|@([a-z0-9-]+\.)*(invalid|test|example|localhost))$")


def collect(root: Path, start: str, end: str, skip: frozenset[str] = frozenset()) -> list[dict[str, object]]:
    if not root.is_dir():
        raise ValueError("root is not a directory")
    candidates = [p.parent for pattern in ("*/*/.git", "*/.git") for p in root.glob(pattern)]
    repositories = sorted(
        {
            p
            for p in candidates
            if not p.is_symlink()
            and not any(part.startswith(".") for part in p.relative_to(root).parts)
            and p.relative_to(root).as_posix() not in skip
        }
    )
    if len(repositories) > 200:
        raise ValueError("repository count exceeds 200")
    records: list[dict[str, object]] = []
    for repository in repositories:
        relative = repository.relative_to(root).as_posix()
        remote = github(repository)
        with tempfile.TemporaryFile() as output:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "log",
                    "--all",
                    "--max-count=10001",
                    f"--since={start}",
                    f"--until={end}",
                    "--no-show-signature",
                    "--no-notes",
                    "--encoding=UTF-8",
                    "-z",
                    "--format=%H%x00%cI%x00%ae%x00%B",
                ],
                check=True,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            output.seek(0)
            payload = output.read((16 << 20) + 1)
        fields = payload.decode().split("\0")
        if fields[-1] != "" or (len(fields) - 1) % 4:
            raise ValueError("invalid Git history framing")
        if (len(fields) - 1) // 4 > 10000 or len(payload) > 16 << 20:
            raise ValueError("Git history exceeds its completeness ceiling")
        for offset in range(0, len(fields) - 1, 4):
            commit, when, author, message = fields[offset : offset + 4]
            author = author.strip().lower()
            if AUTOMATED.search(author):
                continue
            links = ["repo:local/" + relative, *([remote] if remote else [])]
            if "@" in author:
                links.append("person:email/" + quote(author, safe="/:@+").replace("~", "%7E"))
            message = message.strip()
            subject = message.partition("\n")[0]
            if not subject:
                raise ValueError("commit message has no subject")
            key = relative + "@" + commit
            records.append(
                {
                    "id": key,
                    "title": f"{relative}: {subject}",
                    "time": when,
                    "text": f"Repository: {relative}\nCommit: {commit}\nAuthor: {author}\nCommitted: {when}\n\n{message}",
                    "links": sorted(links),
                    "aliases": ["commit:local/" + key],
                    "attributes": {"repository": relative, "commit": commit, "author_email": author},
                }
            )
        if len(records) > 10000:
            raise ValueError("total history exceeds 10000 records")
    return records


if __name__ == "__main__":
    try:
        options = sys.argv[4:]
        if len(options) % 2 or any(flag != "--skip" for flag in options[::2]):
            raise ValueError("expected --skip RELATIVE_REPO pairs")
        result = collect(Path(sys.argv[1]), sys.argv[2], sys.argv[3], frozenset(options[1::2]))
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > 16 << 20:
            raise ValueError("normalized history exceeds 16 MiB")
        print(payload)
    except OSError, UnicodeError, ValueError, subprocess.SubprocessError, IndexError:
        print("Git history collection failed; check repository access and the requested window.", file=sys.stderr)
        sys.exit(1)
