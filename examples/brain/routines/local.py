#!/usr/bin/env python3
"""Minimal local JSONL inbox/outbox daemon (`listen [--once]` and `send`) for `loops:`."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

MAX_BYTES = 16 << 20


def _state_dir() -> Path:
    svc_state = os.environ.get("BF_STATE") if os.environ.get("BF_TRIGGER") in {None, "daemon"} else None
    raw = Path(svc_state or ".bf/local")
    if ".." in raw.parts:
        raise SystemExit("refusing traversal in BF_STATE")
    cur = Path(raw.anchor) if raw.is_absolute() else Path()
    for part in raw.parts[1:] if raw.is_absolute() else raw.parts:
        cur = cur / part
        if cur.is_symlink():
            raise SystemExit(f"refusing symlink {cur}")
    raw.mkdir(parents=True, mode=0o700, exist_ok=True)
    if (raw.stat().st_mode & 0o077) != 0:
        raise SystemExit(f"refusing {raw}: expected mode 700")
    return raw


def _append_private(path: Path, line: str) -> None:
    if path.is_symlink():
        raise SystemExit(f"refusing symlink {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as err:
        raise SystemExit(f"refusing {path}: {err}") from err
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line)


def _drain(state: Path) -> None:
    offset_file, inbox_file = state / "inbox.offset", state / "inbox.jsonl"
    if not inbox_file.is_file() or inbox_file.is_symlink():
        return
    try:
        seen = max(0, int(offset_file.read_text(encoding="utf-8").strip())) if offset_file.is_file() else 0
    except ValueError:
        seen = 0
    raw = inbox_file.read_bytes()
    if len(raw) > MAX_BYTES:
        raise SystemExit(f"inbox.jsonl exceeds {MAX_BYTES} bytes")
    lines = raw.decode("utf-8").splitlines()
    if len(lines) == seen and offset_file.is_file():
        return
    if len(lines) < seen:
        seen = 0
    for line in lines[seen:]:
        if stripped := line.strip():
            try:
                parsed = json.loads(stripped)
            except ValueError:
                sys.stderr.write('{"severity":"WARN","message":"skipped malformed JSON line in inbox.jsonl"}\n')
                continue
            if isinstance(parsed, dict):
                sys.stdout.write(json.dumps(parsed) + "\n")
                sys.stdout.flush()
    offset_file.write_text(f"{len(lines)}\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    state = _state_dir()
    if mode == "send":
        raw_in = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw_in) > MAX_BYTES:
            raise SystemExit("stdin exceeds 16 MiB")
        payload = json.loads(raw_in.decode("utf-8"))
        if not isinstance(payload, dict) or not str(payload.get("text", "")).strip():
            raise SystemExit("expected JSON message object with nonempty text")
        _append_private(state / "outbox.jsonl", json.dumps(payload) + "\n")
        reply = {
            "id": str(payload.get("id", "local-1")),
            "conversation": str(payload.get("conversation", "local")),
        }
        sys.stdout.write(json.dumps(reply) + "\n")
        return 0
    if mode == "listen":
        once, stopping = "--once" in argv[2:], False

        def handle_stop(_sig: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, handle_stop)
        signal.signal(signal.SIGINT, handle_stop)
        while not stopping:
            _drain(state)
            if once:
                break
            time.sleep(0.2)
        return 0
    raise SystemExit("usage: local.py <listen [--once]|send>")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
