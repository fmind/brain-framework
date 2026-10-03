"""Local usage counts: when search and read ran and how many results they returned, never what was asked."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import NotRequired, TypedDict

from bf.models import Error, decode, encode, timestamp
from bf.storage import Store, state_store

USAGE = "usage.jsonl"
LIMIT = 1 << 20
# Rotation keeps at most this much, so the next one waits for at least a quarter of the limit in new lines.
KEEP = LIMIT * 3 // 4
WINDOWS = {"7d": 7, "30d": 30}


class Counts(TypedDict):
    """One window's searches, searches that found nothing and reads.

    `since`, when rotation dropped events inside the window to stay within the limit, is the oldest one it counts.
    """

    search: int
    empty: int
    read: int
    since: NotRequired[str]


def _instant(line: bytes, key: str) -> datetime | None:
    """The timezone-aware instant a line holds under `key`, in UTC; None for any other line."""
    try:
        value = decode(line)
        instant = datetime.fromisoformat(str(value[key])) if isinstance(value, dict) and key in value else None
        return instant.astimezone(UTC) if instant and instant.tzinfo else None
    except Error, ValueError, OverflowError:
        return None


def _rotate(tail: bytes, now: datetime) -> bytes:
    """The whole lines the largest window counts; beyond KEEP, the newest ones after a mark naming the oldest kept."""
    start = now - timedelta(days=max(WINDOWS.values()))
    # The tail may start inside its first line.
    lines = tail.splitlines(keepends=True)[1:]
    recent = b"".join(line for line in lines if (instant := _instant(line, "at")) and instant >= start)
    if len(recent) <= KEEP:
        return recent
    # Whole lines only: drop the one the cut may split.
    kept = recent[len(recent) - KEEP :].partition(b"\n")[2]
    since = _instant(kept.split(b"\n", 1)[0], "at")
    return (encode({"since": timestamp(since.isoformat())}) if since else b"") + kept


def note(store: Store, operation: str, results: int) -> None:
    """Append one line to private state outside the brain; counting never breaks retrieval."""
    try:
        state = state_store(store.root)
        path = state.root / USAGE
        now = datetime.now(UTC)
        line = encode({"at": timestamp(now.isoformat()), "op": operation, "results": results})
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "a+b") as stream:
            stream.write(line)
            size = stream.tell()
            if size > LIMIT:
                stream.seek(size - LIMIT)
                tail = stream.read()
        if size > LIMIT:
            state.write(USAGE, _rotate(tail, now))
    except OSError, Error:
        pass


def summary(store: Store, now: datetime | None = None) -> dict[str, Counts]:
    """Searches, searches that found nothing, and reads over the last 7 and 30 days."""
    now = now or datetime.now(UTC)
    counts = {window: Counts(search=0, empty=0, read=0) for window in WINDOWS}
    try:
        lines = state_store(store.root).read(USAGE, LIMIT * 2).splitlines()
    except OSError, Error:
        return counts
    for raw in lines:
        try:
            event = decode(raw)
            if not isinstance(event, dict):
                continue
            age = now - datetime.fromisoformat(str(event["at"]))
            operation, results = str(event["op"]), event["results"]
            if type(results) is not int or results < 0:
                continue
        except Error, KeyError, TypeError, ValueError:
            # Private counts are advisory: a malformed line never hides the rest.
            continue
        for window, days in WINDOWS.items():
            if timedelta(0) <= age <= timedelta(days=days) and operation in ("search", "read"):
                counts[window][operation] += 1
                counts[window]["empty"] += operation == "search" and not results
    # Rotation's mark, its first line: a window that began before the oldest event it kept is incomplete.
    if lines and (since := _instant(lines[0], "since")):
        for window, days in WINDOWS.items():
            if now - since < timedelta(days=days):
                counts[window]["since"] = timestamp(since.isoformat())
    return counts
