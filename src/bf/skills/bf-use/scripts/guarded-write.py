#!/usr/bin/env python3
"""Change a file only while it still has the SHA-256 digest you read; never overwrite another edit.

Replace the whole file with stdin, or pass --old and --new (or --old-file and --new-file) to replace exactly
one occurrence of a passage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable
from pathlib import Path

# Skills use the host's python3; this helper needs only the Python 3.11 standard library.
MAX_BYTES = 16 << 20  # bf reads authored files up to 16 MiB.


class ChangedError(Exception):
    """The file no longer has the expected digest."""


class EditError(Exception):
    """The requested edit does not apply to the file as read."""


def snapshot(path: Path) -> tuple[bytes, int]:
    """The content and permission bits of a regular file, read without following a symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise OSError("file too large")
    return data, stat.S_IMODE(info.st_mode)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def whole(data: bytes) -> Callable[[bytes], bytes]:
    """An edit replacing the whole file, whatever it held when read."""
    return lambda _current: data


def substitute(old: bytes, new: bytes) -> Callable[[bytes], bytes]:
    """An edit replacing the one occurrence of `old`; none or several leave the file unchanged."""

    def edit(data: bytes) -> bytes:
        count = data.count(old)
        if count != 1:
            raise EditError(
                "the old text does not occur in the file; read it again and copy the passage exactly"
                if not count
                else f"the old text occurs {count} times; include surrounding text so it occurs once"
            )
        return data.replace(old, new, 1)

    return edit


def replace(path: Path, expected: str, edit: Callable[[bytes], bytes]) -> str:
    """Apply `edit` to the file as read, write beside it and check the digest again just before the atomic rename."""
    data, mode = snapshot(path)
    if digest(data) != expected:
        raise ChangedError(digest(data))
    result = edit(data)
    if not result or len(result) > MAX_BYTES:
        raise EditError(f"the new content must hold 1 byte to {MAX_BYTES} bytes")
    temporary = path.parent / f".guarded-write-{os.urandom(8).hex()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(result)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        # An editor may have saved while this helper wrote: the second check narrows that window to the rename.
        latest = digest(snapshot(path)[0])
        if latest != expected:
            raise ChangedError(latest)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return digest(result)


def sync(directory: Path) -> None:
    """Persist the rename itself."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def text(parser: argparse.ArgumentParser, value: str | None, file: Path | None, name: str) -> bytes | None:
    """An --old/--new passage from its argument or file, as UTF-8 bytes."""
    if file is None:
        return None if value is None else value.encode()
    try:
        with file.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
    except OSError:
        parser.exit(1, f"Not written: --{name}-file is not a readable file; nothing changed.\n")
    if len(data) > MAX_BYTES:
        parser.exit(1, f"Not written: --{name}-file exceeds {MAX_BYTES} bytes; nothing changed.\n")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="existing file to change, such as projects/new-website.md")
    parser.add_argument("--expect-sha256", required=True, help="the sha256 that bf read returned for the file")
    old = parser.add_mutually_exclusive_group()
    old.add_argument("--old", help="the exact passage to replace; it must occur once")
    old.add_argument("--old-file", type=Path, help="read the passage to replace from this file")
    new = parser.add_mutually_exclusive_group()
    new.add_argument("--new", help="its replacement; may be empty to delete the passage")
    new.add_argument("--new-file", type=Path, help="read the replacement from this file")
    args = parser.parse_args()
    expected = args.expect_sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        parser.error("--expect-sha256 takes the 64 hexadecimal characters of a SHA-256 digest")
    before = text(parser, args.old, args.old_file, "old")
    after = text(parser, args.new, args.new_file, "new")
    if (before is None) != (after is None):
        parser.error("pass both the old passage and its replacement, or neither to replace the file with stdin")
    if before is not None and after is not None:
        if not before:
            parser.error("the old passage must not be empty")
        edit = substitute(before, after)
    else:
        data = sys.stdin.buffer.read(MAX_BYTES + 1)
        if not data or len(data) > MAX_BYTES:
            parser.exit(1, f"Refusing to write: stdin must hold the new content, 1 byte to {MAX_BYTES} bytes.\n")
        edit = whole(data)
    try:
        written = replace(args.path, expected, edit)
    except ChangedError as change:
        parser.exit(
            1,
            f"Not written: the file changed since it was read (sha256 {change}, expected {expected}). "
            "Read it again, reapply your edit to the new text, and retry with the new sha256.\n",
        )
    except EditError as error:
        parser.exit(1, f"Not written: {error}; nothing changed.\n")
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
