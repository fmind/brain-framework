#!/usr/bin/env python3
"""Minimal local JSONL inbox/outbox channel (`listen [--once]` and `send`) for `channels:`."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path


def _check_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise SystemExit(f"refusing symlink path: {path.name}")


def _drain(inbox: Path, offset_file: Path) -> None:
    _check_not_symlink(inbox)
    _check_not_symlink(offset_file)
    try:
        seen = max(0, int(offset_file.read_text(encoding="utf-8").strip())) if offset_file.is_file() else 0
    except ValueError:
        seen = 0
    if not inbox.is_file():
        return
    lines = inbox.read_text(encoding="utf-8").splitlines()
    if len(lines) < seen:
        seen = 0
    for line in lines[seen:]:
        if stripped := line.strip():
            try:
                parsed = json.loads(stripped)
            except ValueError:
                sys.stderr.write("local.py: skipped malformed JSON line in inbox.jsonl\n")
                continue
            if isinstance(parsed, dict):
                sys.stdout.write(json.dumps(parsed) + "\n")
                sys.stdout.flush()
    offset_file.write_text(f"{len(lines)}\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    state = Path(os.environ.get("BF_STATE_DIR", ".bf/local"))
    state.mkdir(parents=True, exist_ok=True)

    if mode == "send":
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict) or not str(payload.get("text", "")).strip():
            raise SystemExit("expected JSON message object with nonempty text")
        outbox = state / "outbox.jsonl"
        _check_not_symlink(outbox)
        existing = outbox.read_text(encoding="utf-8") if outbox.is_file() else ""
        outbox.write_text(existing + json.dumps(payload) + "\n", encoding="utf-8")
        reply = {"id": str(payload.get("id", "local-1")), "conversation": str(payload.get("conversation", "local"))}
        sys.stdout.write(json.dumps(reply) + "\n")
        return 0

    if mode == "listen":
        once = "--once" in argv[2:]
        stopping = False

        def handle_stop(_sig: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, handle_stop)
        signal.signal(signal.SIGINT, handle_stop)
        inbox, offset_file = state / "inbox.jsonl", state / "inbox.offset"
        while not stopping:
            _drain(inbox, offset_file)
            if once:
                break
            time.sleep(0.2)
        return 0

    raise SystemExit("usage: local.py <listen [--once]|send>")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
