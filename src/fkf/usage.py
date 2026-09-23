"""Local usage counts: when search and read ran and how many results they returned, never what was asked."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from fkf.models import Error, decode, encode
from fkf.storage import Store, state_store

USAGE = "usage.jsonl"
LIMIT = 1 << 20
WINDOWS = {"7d": 7, "30d": 30}


def note(store: Store, operation: str, results: int, now: datetime | None = None) -> None:
    """Append one line to private state outside the base; counting never breaks retrieval."""
    try:
        state = state_store(store.root)
        path = state.root / USAGE
        line = encode({"at": (now or datetime.now(UTC)).isoformat(), "op": operation, "results": results})
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "a+b") as stream:
            stream.write(line)
            size = stream.tell()
            if size > LIMIT:
                # Keep the newest half, starting at a complete line.
                stream.seek(size - LIMIT // 2)
                recent = stream.read()
        if size > LIMIT:
            state.write(USAGE, recent[recent.find(b"\n") + 1 :])
    except OSError, Error:
        pass


def summary(store: Store, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Searches, searches that found nothing, and reads over the last 7 and 30 days."""
    now = now or datetime.now(UTC)
    counts = {window: {"search": 0, "empty": 0, "read": 0} for window in WINDOWS}
    try:
        data = state_store(store.root).read(USAGE, LIMIT * 2)
    except OSError, Error:
        return counts
    for raw in data.splitlines():
        try:
            event = decode(raw)
            assert isinstance(event, dict)  # noqa: S101 - malformed lines are skipped below
            age = now - datetime.fromisoformat(str(event["at"]))
            operation, results = str(event["op"]), int(event["results"])
        except Error, AssertionError, KeyError, TypeError, ValueError:
            continue
        for window, days in WINDOWS.items():
            if age <= timedelta(days=days) and operation in {"search", "read"}:
                counts[window][operation] += 1
                counts[window]["empty"] += operation == "search" and not results
    return counts
