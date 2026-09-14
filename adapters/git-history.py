#!/usr/bin/env python3
"""Collect bounded local Git history as FKF records; no provider authentication."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def collect(root: Path, start: str, end: str) -> list[dict[str, object]]:
    if not root.is_dir():
        raise ValueError("root is not a directory")
    repositories = sorted(p.parent for p in root.glob("*/*/.git") if not p.parent.is_symlink())
    repositories.extend(p.parent for p in root.glob("*/.git") if not p.parent.is_symlink())
    repositories = sorted(set(repositories))
    if len(repositories) > 200:
        raise ValueError("repository count exceeds 200")
    records: list[dict[str, object]] = []
    for repository in repositories:
        relative = repository.relative_to(root).as_posix()
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
                    "--format=%H%x00%cI%x00%B",
                ],
                check=True,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            output.seek(0)
            payload = output.read((16 << 20) + 1)
        fields = payload.decode().split("\0")
        if fields[-1] != "" or (len(fields) - 1) % 3:
            raise ValueError("invalid Git history framing")
        if (len(fields) - 1) // 3 > 10000 or len(payload) > 16 << 20:
            raise ValueError("Git history exceeds its completeness ceiling")
        for offset in range(0, len(fields) - 1, 3):
            commit, when, message = fields[offset : offset + 3]
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
                    "text": f"Repository: {relative}\nCommit: {commit}\nCommitted: {when}\n\n{message}",
                    "links": ["repo:local/" + relative],
                    "aliases": ["commit:local/" + key],
                    "attributes": {"repository": relative, "commit": commit},
                }
            )
        if len(records) > 10000:
            raise ValueError("total history exceeds 10000 records")
    return records


if __name__ == "__main__":
    try:
        result = collect(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > 16 << 20:
            raise ValueError("normalized history exceeds 16 MiB")
        print(payload)
    except OSError, UnicodeError, ValueError, subprocess.SubprocessError, IndexError:
        print("Git history collection failed; check repository access and the requested window.", file=sys.stderr)
        sys.exit(1)
