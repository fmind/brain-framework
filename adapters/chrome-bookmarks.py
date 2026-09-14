#!/usr/bin/env python3
"""Capture one explicitly selected Chrome Bookmarks file, including its folder tree."""

import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

MAX_BYTES = 16 << 20


def collect(profile: str, filename: str) -> list[dict[str, object]]:
    # https://chromium.googlesource.com/chromium/src/+/main/components/bookmarks/browser/bookmark_codec.cc
    if not profile or len(profile) > 64 or not all(c.isalnum() or c in "-_" for c in profile):
        raise ValueError("profile must be an explicit short label")
    path = Path(filename).expanduser()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("bookmark file may not be linked")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("bookmark input must be a regular file")
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("bookmark input changed or exceeded its limit")
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("roots"), dict):
        raise ValueError("invalid bookmark document")
    pending = [(node, "", [], 0) for node in value["roots"].values()]
    records = []
    seen: set[str] = set()
    while pending:
        node, parent, folders, depth = pending.pop()
        if not isinstance(node, dict) or depth > 64 or len(records) >= 10000:
            raise ValueError("invalid or oversized bookmark tree")
        identifier, name, kind = node.get("id"), node.get("name"), node.get("type")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in seen
            or not isinstance(name, str)
            or kind not in {"folder", "url"}
        ):
            raise ValueError("invalid bookmark identity")
        seen.add(identifier)
        alias = f"bookmark:{quote(profile, safe='')}/{quote(identifier, safe='')}"
        url = node.get("url", "")
        if kind == "url" and (not isinstance(url, str) or not url):
            raise ValueError("bookmark URL is missing")
        when = ""
        if node.get("date_added"):
            when = (datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(node["date_added"]))).isoformat()
        records.append(
            {
                "id": profile + "/" + identifier,
                "kind": "container" if kind == "folder" else "record",
                "title": " ".join(name.split()) or ("Folder" if kind == "folder" else "Bookmark"),
                "text": "Folder: " + " / ".join(folders) + ("\n" + url if url else ""),
                "time": when,
                "url": url,
                "aliases": [alias],
                "parents": [parent] if parent else [],
                "attributes": {key: child for key, child in node.items() if key != "children"},
            }
        )
        if kind == "folder":
            children = node.get("children")
            if not isinstance(children, list):
                raise ValueError("folder children must be an array")
            pending.extend((child, alias, [*folders, name], depth + 1) for child in children)
    return records


if __name__ == "__main__":
    try:
        payload = json.dumps(collect(sys.argv[1], sys.argv[2]), ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > MAX_BYTES:
            raise ValueError("normalized bookmarks exceed limit")
        print(payload)
    except OSError, ValueError, TypeError, UnicodeError, IndexError, OverflowError, RecursionError:
        print(
            "Bookmark collection failed; select one readable profile file within the size and tree limits.",
            file=sys.stderr,
        )
        sys.exit(1)
