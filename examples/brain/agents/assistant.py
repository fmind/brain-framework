#!/usr/bin/env python3
"""Offline example agent loop demonstrating the `agents/` convention (`timeout: 15m` in `bf.yaml`).

Reads `prompts/digest.md` and any matched JSONL events from `stdin`, logs a structured JSONL event
to `stderr` (`logs/assistant.jsonl`), and sends a plain-text reply through `routines/local.py send`.
In a live brain, replace the local reply step with your coding harness (`agy`, `copilot`, etc.).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _confined_file(root: Path, *parts: str) -> Path | None:
    cur = root
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            return None
    if not cur.is_file() or not cur.resolve().is_relative_to(root):
        return None
    return cur


def main() -> int:
    root = Path(os.environ.get("BF_BRAIN", ".")).resolve()
    prompt_path = _confined_file(root, "prompts", "digest.md")
    if prompt_path is None:
        sys.stderr.write(json.dumps({"severity": "ERROR", "message": "missing or invalid prompts/digest.md"}) + "\n")
        return 1
    local_script = _confined_file(root, "routines", "local.py")
    if local_script is None:
        sys.stderr.write(json.dumps({"severity": "ERROR", "message": "missing or invalid routines/local.py"}) + "\n")
        return 1

    events = [json.loads(ln) for ln in sys.stdin.read().splitlines() if ln.strip()]
    event_count = len(events)
    trigger = os.environ.get("BF_TRIGGER") or "manual"

    sys.stderr.write(
        json.dumps(
            {
                "severity": "INFO",
                "message": "assistant.digest",
                "prompt": prompt_path.name,
                "trigger": trigger,
                "events": event_count,
            }
        )
        + "\n"
    )
    reply = {"text": f"Digest from {prompt_path.name} ({trigger}, {event_count} event(s))"}
    minimal_env = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
    subprocess.run(  # noqa: S603 - fixed local script argv without a shell
        [sys.executable, str(local_script), "send"],
        cwd=root,
        env=minimal_env,
        input=json.dumps(reply, ensure_ascii=False).encode("utf-8"),
        check=True,
        timeout=30,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
