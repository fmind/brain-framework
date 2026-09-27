#!/usr/bin/env python3
"""Snapshot selected highlights from one explicit portable JSON export; never fetch source URLs."""

import argparse
import json
import os
import re
import stat
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

FILE_BYTES = 8 << 20
OUTPUT_BYTES = 16 << 20
TEXT_BYTES = 64 << 10
MAX_HIGHLIGHTS = 1000


class InvalidError(ValueError):
    """A content-free diagnostic safe to show on stderr."""


def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise InvalidError("export contains duplicate JSON keys")
        result[key] = value
    return result


def constant(_value: str) -> object:
    raise InvalidError("export contains a non-JSON number")


def fields(value: object, required: set[str], optional: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise InvalidError("object has missing or unknown fields; check the export contract")
    return value


def text(value: object, field: str, limit: int = TEXT_BYTES, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise InvalidError(f"{field} requires a nonempty string")
    if len(value.encode("utf-8")) > limit or any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise InvalidError(f"{field} exceeds its byte limit or contains control characters")
    return value


def read_file(path: Path) -> bytes:
    """Open every ancestor without following links, then bound a regular file's bytes."""
    path = path.expanduser().absolute()
    if ".." in path.parts or len(path.parts) > 64:
        raise InvalidError("export path must be normalized and at most 64 components deep")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=descriptor)
        with os.fdopen(file, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > FILE_BYTES:
                raise InvalidError("export must be a regular file no larger than 8 MiB")
            data = stream.read(FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(data) > FILE_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise InvalidError("export exceeds 8 MiB or changed while being read")
        return data
    finally:
        os.close(descriptor)


def highlight(value: object, label: str) -> dict[str, object]:
    item = fields(
        value,
        {"id", "title", "selection", "source_url", "locator", "captured_at"},
        {"annotation", "source_date"},
    )
    identifier = text(item["id"], "id", 128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identifier):
        raise InvalidError("id must be a stable token using letters, digits, dots, underscores or hyphens")
    title = text(item["title"], "title", 4096)
    if any(char in title for char in "\t\r\n"):
        raise InvalidError("title must occupy one line")
    source = text(item["source_url"], "source_url", 8192)
    try:
        parsed = urlsplit(source)
        port = parsed.port
    except ValueError as error:
        raise InvalidError("source_url requires an HTTPS document URL without credentials") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in source)
        or port == 0
    ):
        raise InvalidError("source_url requires an HTTPS document URL without credentials")
    locator = fields(item["locator"], set(), {"page", "section"})
    if not locator:
        raise InvalidError("locator requires page, section or both")
    if "page" in locator and (type(locator["page"]) is not int or not 1 <= locator["page"] <= 1_000_000):
        raise InvalidError("locator.page requires a positive integer at most 1000000")
    if "section" in locator:
        text(locator["section"], "locator.section", 4096)
    captured = text(item["captured_at"], "captured_at", 64)
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", captured):
            raise ValueError
        if datetime.fromisoformat(captured).utcoffset() is None:
            raise ValueError
    except ValueError as error:
        raise InvalidError("captured_at requires an ISO timestamp with timezone and seconds") from error
    attributes: dict[str, object] = {"locator": locator}
    if "annotation" in item:
        attributes["annotation"] = text(item["annotation"], "annotation", empty=True)
    if "source_date" in item:
        source_date = text(item["source_date"], "source_date", 10)
        try:
            if date.fromisoformat(source_date).isoformat() != source_date:
                raise ValueError
        except ValueError as error:
            raise InvalidError("source_date requires YYYY-MM-DD") from error
        attributes["source_date"] = source_date
    return {
        "id": f"{label}/{identifier}",
        "title": title,
        "text": text(item["selection"], "selection"),
        "url": source,
        "time": captured,
        "attributes": attributes,
    }


def collect(label: str, path: Path) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", label):
        raise InvalidError("label requires a stable lowercase scope name")
    export = fields(
        json.loads(read_file(path).decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant),
        {"version", "highlights"},
        set(),
    )
    if type(export["version"]) is not int or export["version"] != 1:
        raise InvalidError("export version must be 1")
    selected = export["highlights"]
    if not isinstance(selected, list) or len(selected) > MAX_HIGHLIGHTS:
        raise InvalidError("highlights requires an array of at most 1000 items")
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for position, value in enumerate(selected, 1):
        try:
            record = highlight(value, label)
            identifier = str(record["id"])
            if identifier in seen:
                raise InvalidError("duplicate highlight id")
            seen.add(identifier)
            records.append(record)
        except InvalidError as error:
            raise InvalidError(f"highlight {position}: {error}") from error
    records.sort(key=lambda record: str(record["id"]))
    payload = json.dumps(records, ensure_ascii=False, allow_nan=False)
    if len(payload.encode("utf-8")) > OUTPUT_BYTES:
        raise InvalidError("normalized highlight snapshot exceeds 16 MiB")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="stable personal/team export scope")
    parser.add_argument("export", type=Path, help="selected complete JSON export; use snapshot mode")
    args = parser.parse_args()
    try:
        print(collect(args.label, args.export))
    except (OSError, ValueError, RecursionError) as error:
        detail = (
            str(error) if isinstance(error, InvalidError) else "check the regular UTF-8 JSON file, syntax and symlinks"
        )
        print(f"Highlight collection failed: {detail}.", file=sys.stderr)
        sys.exit(1)
