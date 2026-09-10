#!/usr/bin/env python3
"""Validate and project the complete Kaggle datasets inventory."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

MAX_PROVIDER_BYTES = 64 << 20
MAX_OUTPUT_BYTES = 64 << 20


def invoke(arguments: list[str]) -> Any:
    if arguments[:1] != ["kaggle-json.py"]:
        raise ValueError
    # Keep provider diagnostics private and emit nothing until every stage succeeds.
    with subprocess.Popen(["kaggle-json.py", *arguments[1:]], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
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
    values = invoke(['kaggle-json.py', '--all-pages', 'datasets', 'list', '-m', '--format', 'json'])
    if not isinstance(values, list):
        raise TypeError
    records = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            raise TypeError
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref or ref in seen:
            raise ValueError
        seen.add(ref)
        record = {key: item.get(key) for key in ('ref', 'title', 'totalBytes', 'lastUpdated', 'downloadCount', 'voteCount', 'usabilityRating')}
        if record["title"] is None or record["title"] is False:
            record["title"] = ref
        records.append(record)
    return sorted(records, key=lambda record: record["ref"])


def main(arguments: list[str]) -> int:
    if arguments[:1] in (["--version"], ["-v"]):
        sys.stdout.write("kaggle-datasets-json.py (fkf preset helper)\n")
        return 0
    if arguments:
        sys.stderr.write("usage: kaggle-datasets-json.py\n")
        return 2
    try:
        value = project(arguments)
        output = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        encoded = output.encode("utf-8")
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError, RecursionError):
        sys.stderr.write("kaggle-datasets-json.py: provider or response validation failed; no output emitted\n")
        return 1
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
