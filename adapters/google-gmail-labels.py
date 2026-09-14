#!/usr/bin/env python3
"""Capture the current Gmail label catalog through gws; date windows do not filter a catalog."""

import json
import subprocess
import sys
import tempfile
from urllib.parse import quote

MAX_BYTES = 16 << 20


def collect() -> list[dict[str, object]]:
    # https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.labels/list
    with tempfile.TemporaryFile() as output:
        subprocess.run(
            ["gws", "gmail", "users", "labels", "list", "--params", '{"userId":"me"}'],
            check=True,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        output.seek(0)
        data = output.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("label catalog exceeds limit")
    page = json.loads(data)
    if not isinstance(page, dict) or not isinstance(page.get("labels"), list):
        raise ValueError("invalid label catalog")
    records = []
    seen: set[str] = set()
    for label in page["labels"]:
        if not isinstance(label, dict):
            raise ValueError("invalid label")
        identifier, name = label.get("id"), label.get("name")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in seen
            or not isinstance(name, str)
            or not name.strip()
        ):
            raise ValueError("invalid label identity")
        seen.add(identifier)
        if len(seen) > 10000:
            raise ValueError("label count exceeds limit")
        records.append(
            {
                "id": identifier,
                "kind": "container",
                "title": " ".join(name.split()),
                "text": "Gmail label: " + name,
                "aliases": ["gmail-label:" + quote(identifier, safe="")],
                "attributes": label,
            }
        )
    return records


if __name__ == "__main__":
    try:
        payload = json.dumps(collect(), ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > MAX_BYTES:
            raise ValueError("normalized catalog exceeds limit")
        print(payload)
    except OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError:
        print("Gmail label collection failed; check gws access to the selected account.", file=sys.stderr)
        sys.exit(1)
