#!/usr/bin/env python3
"""Minimal local JSONL inbox/outbox service (`listen [--once]` and `send`) for `services:` and `jobs:`."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

MAX_BYTES = 16 << 20
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _open_state_dir(state: Path) -> int:
    if ".." in state.parts:
        raise SystemExit("invalid BF_STATE_DIR path")
    base: Path | None = None
    rel_parts: tuple[str, ...] = ()
    try:
        if not state.is_absolute():
            base, rel_parts = Path.cwd().resolve(strict=True), state.parts
        else:
            if base_str := os.environ.get("BF_BRAIN"):
                raw_brain = Path(base_str)
                real_brain = raw_brain.resolve(strict=True)
                for cand in (real_brain, raw_brain):
                    with contextlib.suppress(ValueError):
                        rel_parts = state.relative_to(cand).parts
                        base = real_brain
                        break
            if base is None:
                cur = state
                while cur != cur.parent and (cur == state or cur.name == ".bf" or cur.is_symlink() or not cur.exists()):
                    cur = cur.parent
                base, rel_parts = cur.resolve(strict=True), state.relative_to(cur).parts
    except (OSError, ValueError) as err:
        raise SystemExit(f"cannot resolve state directory parent: {err}") from err
    if not rel_parts or any(p in {"", ".", ".."} for p in rel_parts):
        raise SystemExit("invalid BF_STATE_DIR path")
    try:
        fd = os.open(base, _DIR_FLAGS)
    except OSError as err:
        raise SystemExit(f"cannot open state directory parent: {err.strerror}") from err
    try:
        for part in rel_parts:
            created = False
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as err:
                if err.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise SystemExit(f"refusing symlink or non-directory path: {part}") from err
                raise
            info = os.fstat(child)
            if info.st_uid != os.geteuid() or (not created and info.st_mode & 0o077):
                os.close(child)
                raise SystemExit(f"refusing state directory {part}: must be owned by you with mode 700")
            if created:
                os.fchmod(child, 0o700)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_file(dir_fd: int, name: str) -> str | None:
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as err:
        if err.errno == errno.ELOOP:
            raise SystemExit(f"refusing symlink path: {name}") from err
        raise
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SystemExit(f"refusing non-regular path: {name}")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise SystemExit(f"{name} exceeds {MAX_BYTES} bytes")
    return raw.decode("utf-8")


def _write_file(dir_fd: int, name: str, content: str) -> None:
    raw = content.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise SystemExit(f"{name} exceeds {MAX_BYTES} bytes")
    with contextlib.suppress(FileNotFoundError):
        if not stat.S_ISREG(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
            raise SystemExit(f"refusing non-regular path: {name}")
    tmp = f".tmp-{os.urandom(8).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    tfd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(tfd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def _append_file(dir_fd: int, name: str, line: str) -> None:
    raw = line.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise SystemExit(f"{name} exceeds {MAX_BYTES} bytes")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    except OSError as err:
        if err.errno in {errno.ELOOP, errno.ENXIO, errno.EOPNOTSUPP}:
            raise SystemExit(f"refusing symlink or non-regular path: {name}") from err
        raise
    with os.fdopen(fd, "wb", buffering=0) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(f"refusing non-regular path: {name}")
        if info.st_size + len(raw) > MAX_BYTES:
            raise SystemExit(f"{name} exceeds {MAX_BYTES} bytes")
        os.fchmod(stream.fileno(), 0o600)
        view = memoryview(raw)
        while view:
            view = view[stream.write(view) :]


def _drain(dir_fd: int) -> None:
    offset_raw = _read_file(dir_fd, "inbox.offset")
    try:
        seen = max(0, int(offset_raw.strip())) if offset_raw is not None else 0
    except ValueError:
        seen = 0
    inbox_raw = _read_file(dir_fd, "inbox.jsonl")
    if inbox_raw is None:
        return
    lines = inbox_raw.splitlines()
    if len(lines) == seen and offset_raw is not None:
        return
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
    _write_file(dir_fd, "inbox.offset", f"{len(lines)}\n")


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    svc_state = os.environ.get("BF_STATE_DIR") if os.environ.get("BF_TRIGGER") in {None, "service"} else None
    dir_fd = _open_state_dir(Path(svc_state or ".bf/local"))
    try:
        if mode == "send":
            raw_in = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(raw_in) > MAX_BYTES:
                raise SystemExit("stdin exceeds 16 MiB")
            payload = json.loads(raw_in.decode("utf-8"))
            if not isinstance(payload, dict) or not str(payload.get("text", "")).strip():
                raise SystemExit("expected JSON message object with nonempty text")
            _append_file(dir_fd, "outbox.jsonl", json.dumps(payload) + "\n")
            reply = {
                "id": str(payload.get("id", "local-1")),
                "conversation": str(payload.get("conversation", "local")),
            }
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
            while not stopping:
                _drain(dir_fd)
                if once:
                    break
                time.sleep(0.2)
            return 0

        raise SystemExit("usage: local.py <listen [--once]|send>")
    finally:
        os.close(dir_fd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
