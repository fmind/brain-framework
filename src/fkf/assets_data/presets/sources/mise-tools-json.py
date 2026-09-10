#!/usr/bin/env python3
"""Project the active mise tool inventory."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

MAX_PROVIDER_BYTES = 64 << 20
MAX_OUTPUT_BYTES = 64 << 20


def invoke(arguments: list[str]) -> Any:
    if arguments[:1] != ["mise"]:
        raise ValueError
    # Keep provider diagnostics private and emit nothing until every stage succeeds.
    with subprocess.Popen(["mise", *arguments[1:]], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
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


def project(_arguments: list[str]) -> list[dict[str, Any]]:
    values = invoke(["mise", "ls", "--current", "--json"])
    if not isinstance(values, dict):
        raise TypeError
    records = []
    for name, versions in values.items():
        if not isinstance(versions, list) or any(not isinstance(item, dict) for item in versions):
            raise TypeError
        first = versions[0] if versions else {}
        source = first.get("source") or {}
        version = first.get("version")
        records.append({
            "id": name, "version": version, "requested": first.get("requested_version"), "source": source.get("path"),
            "installed": any(item.get("installed") for item in versions),
            "active": any(item.get("active") for item in versions),
            "title": name + " " + (version if version is not None else "?"),
        })
    return records


def main(arguments: list[str]) -> int:
    if arguments[:1] in (["--version"], ["-v"]):
        sys.stdout.write("mise-tools-json.py (fkf preset helper)\n")
        return 0
    if arguments:
        sys.stderr.write("usage: mise-tools-json.py\n")
        return 2
    try:
        value = project(arguments)
        output = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        encoded = output.encode("utf-8")
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError, RecursionError):
        sys.stderr.write("mise-tools-json.py: provider or response validation failed; no output emitted\n")
        return 1
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
