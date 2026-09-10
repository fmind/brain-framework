#!/usr/bin/env python3
"""Collect a complete bounded project metadata snapshot."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

MAX_PROVIDER_BYTES = 64 << 20
MAX_OUTPUT_BYTES = 64 << 20


def invoke(arguments: list[str]) -> Any:
    if arguments[:1] != ["gcloud"]:
        raise ValueError
    # Keep provider diagnostics private and emit nothing until every stage succeeds.
    with subprocess.Popen(["gcloud", *arguments[1:]], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
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
    values = invoke(["gcloud", "projects", "list", "--limit=10001", "--format=json"])
    if not isinstance(values, list) or len(values) > 10_000:
        raise ValueError
    records = []
    for item in values:
        if not isinstance(item, dict):
            raise TypeError
        record = {key: item[key] for key in ("projectId", "name", "projectNumber", "lifecycleState", "createTime") if item.get(key) is not None}
        parent = item.get("parent")
        if parent is not None:
            if not isinstance(parent, dict):
                raise TypeError
            record["parent"] = {key: parent[key] for key in ("type", "id") if parent.get(key) is not None}
        records.append(record)
    return records


def main(arguments: list[str]) -> int:
    if arguments[:1] in (["--version"], ["-v"]):
        sys.stdout.write("gcloud-projects-json.py (fkf preset helper)\n")
        return 0
    if arguments:
        sys.stderr.write("usage: gcloud-projects-json.py\n")
        return 2
    try:
        value = project(arguments)
        output = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        encoded = output.encode("utf-8")
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError, RecursionError):
        sys.stderr.write("gcloud-projects-json.py: provider or response validation failed; no output emitted\n")
        return 1
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
