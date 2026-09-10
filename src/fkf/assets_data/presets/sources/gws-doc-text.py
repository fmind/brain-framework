#!/usr/bin/env python3
"""Extract text runs from one Google Doc in response traversal order."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

MAX_PROVIDER_BYTES = 64 << 20
MAX_OUTPUT_BYTES = 64 << 20


def invoke(arguments: list[str]) -> Any:
    if arguments[:1] != ["gws"]:
        raise ValueError
    # Keep provider diagnostics private and emit nothing until every stage succeeds.
    with subprocess.Popen(["gws", *arguments[1:]], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
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


def text_runs(value: Any) -> list[str]:
    parts: list[str] = []
    if isinstance(value, dict):
        run = value.get("textRun")
        if isinstance(run, dict) and run.get("content") is not None:
            if not isinstance(run["content"], str):
                raise TypeError
            parts.append(run["content"])
        for child in value.values():
            parts.extend(text_runs(child))
    elif isinstance(value, list):
        for child in value:
            parts.extend(text_runs(child))
    return parts


def project(arguments: list[str]) -> str:
    document = invoke(["gws", "docs", "documents", "get", "--params", json.dumps({"documentId": arguments[0]})])
    if not isinstance(document, dict):
        raise TypeError
    return "".join(text_runs(document))


def main(arguments: list[str]) -> int:
    if arguments[:1] in (["--version"], ["-v"]):
        sys.stdout.write("gws-doc-text.py (fkf preset helper)\n")
        return 0
    if len(arguments) != 1 or not arguments[0] or arguments[0].startswith("-"):
        sys.stderr.write("usage: gws-doc-text.py <document-id>\n")
        return 2
    try:
        value = project(arguments)
        output = value + "\n"
        encoded = output.encode("utf-8")
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError, RecursionError):
        sys.stderr.write("gws-doc-text.py: provider or response validation failed; no output emitted\n")
        return 1
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
