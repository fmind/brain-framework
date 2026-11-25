#!/usr/bin/env python3
"""Minimal local JSONL inbox/outbox daemon (`listen [--once]` and `send`) for `loops:`."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import stat
import sys
import time
from pathlib import Path

MAX_BYTES = 16 << 20
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _state_dir() -> Path:
    svc_state = os.environ.get("BF_STATE") if os.environ.get("BF_TRIGGER") in {None, "daemon"} else None
    raw = Path(svc_state or ".bf/state/local")
    if ".." in raw.parts:
        raise SystemExit("refusing traversal in BF_STATE")
    owner = os.geteuid()
    cur = Path(raw.anchor) if raw.is_absolute() else Path()
    parts = raw.parts[1:] if raw.is_absolute() else raw.parts
    for idx, part in enumerate(parts):
        cur = cur / part
        if cur.is_symlink():
            raise SystemExit(f"refusing symlink {cur}")
        if not cur.exists():
            cur.mkdir(mode=0o700)
            cur.chmod(0o700)
        info = cur.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise SystemExit(f"refusing non-directory {cur}")
        if idx == len(parts) - 1 and (info.st_uid != owner or (info.st_mode & 0o077) != 0):
            raise SystemExit(f"refusing {cur}: expected user-owned mode 700")
    return raw


def _open_private_reg(path: Path, flags: int, *, create: bool = False) -> int:
    owner = os.geteuid()
    open_flags = flags | _NOFOLLOW | _NONBLOCK | _CLOEXEC | (os.O_CREAT if create else 0)
    try:
        fd = os.open(path, open_flags, 0o600)
    except OSError as err:
        raise SystemExit(f"refusing {path}: {err}") from err
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
            raise SystemExit(f"refusing non-regular or foreign file {path}")
        if create:
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_bounded(path: Path, limit: int = MAX_BYTES) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as err:
        raise SystemExit(f"refusing {path}: {err}") from err
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise SystemExit(f"refusing non-regular or foreign file {path}")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(limit + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > limit:
        raise SystemExit(f"{path.name} exceeds {limit} bytes")
    return data


def _write_private_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".write-{secrets.token_hex(16)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _append_private(path: Path, line: str) -> None:
    fd = _open_private_reg(path, os.O_WRONLY | os.O_APPEND, create=True)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line)


def _drain(state: Path) -> None:
    offset_file, inbox_file = state / "inbox.offset", state / "inbox.jsonl"
    raw = _read_bounded(inbox_file, MAX_BYTES)
    if raw is None:
        return
    raw_off = _read_bounded(offset_file, 64)
    try:
        seen = max(0, int(raw_off.decode("utf-8").strip())) if raw_off is not None else 0
    except (ValueError, UnicodeDecodeError):
        seen = 0
    lines = raw.decode("utf-8").splitlines()
    if len(lines) == seen and raw_off is not None:
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
    _write_private_atomic(offset_file, f"{len(lines)}\n".encode())


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
