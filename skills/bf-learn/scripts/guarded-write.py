#!/usr/bin/env python3
"""Replace a file with stdin only if it still has the SHA-256 digest you read; never overwrite another edit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

# Skills use the host's python3; this helper needs only the Python 3.11 standard library.
MAX_BYTES = 16 << 20  # bf reads authored files up to 16 MiB.


class ChangedError(Exception):
    """The file no longer has the expected digest."""


def current(path: Path) -> tuple[str, int]:
    """The digest and permission bits of a regular file, read without following a symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise OSError("file too large")
    return hashlib.sha256(data).hexdigest(), stat.S_IMODE(info.st_mode)


def replace(path: Path, expected: str, data: bytes) -> str:
    """Write beside the file and check the digest again just before the atomic rename."""
    digest, mode = current(path)
    if digest != expected:
        raise ChangedError(digest)
    parent = path.parent
    temporary = parent / f".guarded-write-{os.urandom(8).hex()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        # An editor may have saved while this helper wrote: the second check narrows that window to the rename.
        digest, _ = current(path)
        if digest != expected:
            raise ChangedError(digest)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return hashlib.sha256(data).hexdigest()


def sync(directory: Path) -> None:
    """Persist the rename itself."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="existing file to replace, such as projects/new-website.md")
    parser.add_argument("--expect-sha256", required=True, help="the sha256 that bf read returned for the file")
    args = parser.parse_args()
    expected = args.expect_sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        parser.error("--expect-sha256 takes the 64 hexadecimal characters of a SHA-256 digest")
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        parser.exit(1, f"Refusing to write: stdin must hold the new content, 1 byte to {MAX_BYTES} bytes.\n")
    try:
        written = replace(args.path, expected, data)
    except ChangedError as change:
        parser.exit(
            1,
            f"Not written: the file changed since it was read (sha256 {change}, expected {expected}). "
            "Read it again, reapply your edit to the new text, and retry with the new sha256.\n",
        )
    except OSError:
        parser.exit(1, "Not written: expected an existing regular file that you can replace; nothing changed.\n")
    try:
        sync(args.path.parent)
    except OSError:
        parser.exit(1, "Written, but its directory could not be synced; check the file system before relying on it.\n")
    sys.stdout.write(json.dumps({"written": str(args.path), "sha256": written}, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
