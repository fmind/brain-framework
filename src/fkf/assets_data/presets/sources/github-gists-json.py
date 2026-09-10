#!/usr/bin/env python3
"""Project the complete authenticated gist inventory."""

from __future__ import annotations

import json
import subprocess
import sys
import unicodedata
from typing import Any

MAX_PROVIDER_BYTES = 64 << 20
MAX_OUTPUT_BYTES = 64 << 20


def invoke(arguments: list[str]) -> Any:
    if arguments[:1] != ["github-generic-list-json.py"]:
        raise ValueError
    # Keep provider diagnostics private and emit nothing until every stage succeeds.
    with subprocess.Popen(["github-generic-list-json.py", *arguments[1:]], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        if process.stdout is None:
            raise RuntimeError
        raw = process.stdout.read(MAX_PROVIDER_BYTES + 1)
        if len(raw) > MAX_PROVIDER_BYTES:
            process.kill()
            process.wait()
            raise ValueError
        if process.wait() != 0:
            raise RuntimeError

    def reject_constant(_value: str) -> None:
        raise ValueError

    return json.loads(raw, parse_constant=reject_constant)


def clean_title(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    visible = "".join(
        "" if unicodedata.category(char) == "Cf" else " " if unicodedata.category(char) == "Cc" else char
        for char in value
    )
    return " ".join(visible.split())


def project(_arguments: list[str]) -> list[dict[str, Any]]:
    values = invoke(["github-generic-list-json.py", "gists"])
    if not isinstance(values, list):
        raise TypeError
    records = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("files"), dict):
            raise TypeError
        record = {
            "id": item.get("id"),
            "description": clean_title(item.get("description")) or f"Gist {item.get('id')}",
            "url": item.get("html_url"),
            "updated_at": item.get("updated_at"),
            "public": item.get("public"),
            "files": sorted(item["files"]),
        }
        records.append({key: value for key, value in record.items() if value is not None})
    return records


def main(arguments: list[str]) -> int:
    if arguments[:1] in (["--version"], ["-v"]):
        sys.stdout.write("github-gists-json.py (fkf preset helper)\n")
        return 0
    if arguments:
        sys.stderr.write("usage: github-gists-json.py\n")
        return 2
    try:
        value = project(arguments)
        output = "".join(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n" for record in value)
        encoded = output.encode("utf-8")
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError, RecursionError):
        sys.stderr.write("github-gists-json.py: provider or response validation failed; no output emitted\n")
        return 1
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
