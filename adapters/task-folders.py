#!/usr/bin/env python3
"""Capture complete TASK.md bodies and the metadata of their input/output artifacts."""

import hashlib
import json
import os
import stat
import sys
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from urllib.parse import quote

MAX_BYTES = 16 << 20


def payload(path: Path) -> tuple[bytes, os.stat_result]:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("task evidence may not be linked")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("task evidence must be a regular file")
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("task evidence changed or exceeded its limit")
    return data, before


def collect(root: Path, start: str, end: str) -> list[dict[str, object]]:
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if begin.tzinfo is None or finish.tzinfo is None or begin >= finish:
        raise ValueError("invalid task window")
    if not root.is_dir() or root.is_symlink():
        raise ValueError("select a task directory")
    records: list[dict[str, object]] = []
    examined = 0
    total_bytes = 0
    folders = list(islice(root.iterdir(), 10001))
    if len(folders) > 10000:
        raise ValueError("task folder count exceeds limit")
    for folder in sorted(folders):
        if folder.is_symlink():
            raise ValueError("linked task directory")
        if not folder.is_dir():
            continue
        task = folder / "TASK.md"
        if not task.exists():
            raise ValueError("task folder lacks TASK.md")
        body, info = payload(task)
        original_info = info
        entries = []
        names = [task]
        for directory in ("inputs", "outputs"):
            selected = folder / directory
            if not selected.is_dir() or selected.is_symlink():
                raise ValueError("task folder lacks real inputs or outputs directory")
            for path in selected.rglob("*"):
                examined += 1
                if examined > 10000 or len(path.relative_to(root).parts) > 64:
                    raise ValueError("task traversal exceeds limit")
                if path.is_symlink():
                    raise ValueError("linked task artifact")
                if not path.is_dir():
                    names.append(path)
        states = {}
        for path in sorted(names):
            examined += 1
            if examined > 10000:
                raise ValueError("task file count exceeds limit")
            content, state = (body, original_info) if path == task else payload(path)
            states[path] = (state.st_size, state.st_mtime_ns, state.st_ino)
            total_bytes += len(content)
            if total_bytes > 128 << 20:
                raise ValueError("task bytes exceed limit")
            entries.append(
                {
                    "path": str(path.relative_to(root)),
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
            if state.st_mtime_ns > info.st_mtime_ns:
                info = state
        for path, expected in states.items():
            current = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or (current.st_size, current.st_mtime_ns, current.st_ino) != expected:
                raise ValueError("task evidence changed during capture")
        when = datetime.fromtimestamp(info.st_mtime, UTC)
        if begin <= when < finish:
            text = body.decode("utf-8")
            title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), folder.name)
            records.append(
                {
                    "id": folder.name,
                    "title": title,
                    "text": text,
                    "time": when.isoformat(),
                    "aliases": ["task:" + quote(folder.name, safe="")],
                    "links": ["tasks/" + folder.name + "/TASK.md"],
                    "attributes": {"files": entries},
                }
            )
    return records


if __name__ == "__main__":
    try:
        value = json.dumps(
            collect(Path(sys.argv[1]).expanduser(), sys.argv[2], sys.argv[3]), ensure_ascii=False, allow_nan=False
        )
        if len(value.encode()) > MAX_BYTES:
            raise ValueError("normalized tasks exceed limit")
        print(value)
    except OSError, ValueError, TypeError, UnicodeError, IndexError:
        print("Task collection failed; check the task folders, artifacts and explicit time window.", file=sys.stderr)
        sys.exit(1)
