"""Local usage counts: when search and read ran, the kind of scope or page, how many results and unmatched terms.

`usage.jsonl` holds two kinds of line. An event, one per search or read, is the time, the operation, its kind and
result count, plus a zero-result search's unmatched tokens. A day, written by rotation, sums one past UTC day's
events. Neither holds query text, refs, paths or identities: a kind is one of the fixed words below.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, NotRequired, TypedDict

from bf import links
from bf.markdown import authored, split_ref
from bf.models import Error, decode, encode, timestamp
from bf.storage import Store, state_store

if TYPE_CHECKING:
    from bf.models import Query

USAGE = "usage.jsonl"
LIMIT = 1 << 20
# Rotation keeps today's events while the file stays within this, so the next one waits for a quarter of the limit.
KEEP = LIMIT * 3 // 4
WINDOWS = {"7d": 7, "30d": 30}
MAX_UNMATCHED = 8
MAX_TOKEN = 64
TOP_TERMS = 10
# A day line keeps its most frequent unmatched tokens: 30 days of them stay far below KEEP.
DAY_TERMS = 32
OTHER = "other"
# The shape of a search's scope or of a read's page, never its text. An unknown kind is counted as `other`.
KINDS: dict[str, frozenset[str]] = {
    "search": frozenset(
        {"none", "period", "folder", "note", "memories", "source", "source-period", "record", "identity", "tag"}
        | {"combined", OTHER}
    ),
    "read": frozenset(
        {"home", "period", "tasks", "tags", "tag", "memories", "source", "source-period", "record", "folder"}
        | {"note", "section", "action", "identity", "relation", OTHER}
    ),
}


class Counts(TypedDict):
    """One window's searches, searches that found nothing and reads.

    `kinds` counts each operation's scope or page kinds and `empty_kinds` the scope kinds of zero-result searches;
    events written before kinds existed count only in the totals. `since`, when an older release's rotation dropped
    events inside the window, is the oldest one it counts.
    """

    search: int
    empty: int
    read: int
    kinds: NotRequired[dict[str, dict[str, int]]]
    empty_kinds: NotRequired[dict[str, int]]
    empty_terms: NotRequired[list[dict[str, object]]]
    since: NotRequired[str]


def scope(query: Query) -> str:
    """A search's scope kind; a period with any other scope is `combined`, except a source's own period pages."""
    timed = bool(query.since or query.until or query.undated)
    parts = query.prefix.split("/") if query.prefix else []
    if query.target and parts:
        return "combined"
    if query.target:
        try:
            shape = "tag" if links.tag(query.target) else "identity"
        except Error:
            shape = "identity"
    elif not parts:
        shape = ""
    elif parts[0] == "memories":
        shape = ("memories", "source")[len(parts) - 1] if len(parts) <= 2 else "record"
    else:
        shape = "note" if authored(query.prefix) else "folder"
    if not timed:
        return shape or "none"
    if not shape:
        return "period"
    return "source-period" if shape == "source" else "combined"


def exact(value: Mapping[str, object]) -> str:
    """An exact read's kind: a record, a note's section, an action's ACTION.md or another note."""
    if "record" in value:
        return "record"
    path, fragment = split_ref(str(value.get("ref", "")))
    if fragment:
        return "section"
    return "action" if re.fullmatch(r"actions/[^/]+/ACTION\.md", path) else "note"


def _instant(line: bytes, key: str) -> datetime | None:
    """The timezone-aware instant a line holds under `key`, in UTC; None for any other line."""
    try:
        value = decode(line)
        instant = datetime.fromisoformat(str(value[key])) if isinstance(value, dict) and key in value else None
        return instant.astimezone(UTC) if instant and instant.tzinfo else None
    except Error, ValueError, OverflowError:
        return None


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _counter(value: object, allowed: frozenset[str] | None = None) -> Counter[str] | None:
    """A mapping of kinds, or of tokens without `allowed`, to counts; None when malformed."""
    if not isinstance(value, dict):
        return None
    counter: Counter[str] = Counter()
    for key, count in value.items():
        if _count(count) is None:
            return None
        # A kind this release does not know, or an unbounded token, is dropped rather than kept.
        if (key in allowed) if allowed is not None else (isinstance(key, str) and 0 < len(key) <= MAX_TOKEN):
            counter[key] += count
    return counter


@dataclass
class _Tally:
    """One UTC day's counts, from its events and day lines."""

    search: int = 0
    empty: int = 0
    read: int = 0
    kinds: dict[str, Counter[str]] = field(default_factory=lambda: {op: Counter() for op in KINDS})
    empty_kinds: Counter[str] = field(default_factory=Counter)
    unmatched: Counter[str] = field(default_factory=Counter)

    def add(self, other: _Tally) -> None:
        self.search += other.search
        self.empty += other.empty
        self.read += other.read
        for operation, counter in other.kinds.items():
            self.kinds[operation].update(counter)
        self.empty_kinds.update(other.empty_kinds)
        self.unmatched.update(other.unmatched)

    def line(self, day: date) -> bytes:
        entry: dict[str, object] = {"day": day.isoformat(), "search": self.search, "empty": self.empty}
        entry["read"] = self.read
        if kinds := {operation: dict(counter) for operation, counter in self.kinds.items() if counter}:
            entry["kinds"] = kinds
        if self.empty_kinds:
            entry["empty_kinds"] = dict(self.empty_kinds)
        if self.unmatched:
            entry["unmatched"] = dict(_top(self.unmatched, DAY_TERMS))
        return encode(entry)


def _top(counter: Counter[str], count: int) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:count]


def _event(value: dict[str, object]) -> tuple[date, datetime, _Tally] | None:
    """A search or read event: its UTC day, instant and counts."""
    try:
        instant = datetime.fromisoformat(str(value["at"]))
        moment = instant.astimezone(UTC) if instant.tzinfo else None
    except KeyError, ValueError, OverflowError:
        return None
    operation, results = value.get("op"), _count(value.get("results"))
    if moment is None or operation not in KINDS or results is None:
        return None
    tally = _Tally()
    kind = value.get("kind")
    if operation == "read":
        tally.read = 1
    else:
        tally.search, tally.empty = 1, int(not results)
    known = kind if isinstance(kind, str) and kind in KINDS[str(operation)] else ""
    if known:
        tally.kinds[str(operation)][known] = 1
    if operation == "search" and not results:
        if known:
            tally.empty_kinds[known] = 1
        raw = value.get("unmatched")
        terms = (
            [term for term in raw if isinstance(term, str) and 0 < len(term) <= MAX_TOKEN]
            if isinstance(raw, list)
            else []
        )
        tally.unmatched.update(terms[:MAX_UNMATCHED])
    return moment.date(), moment, tally


def _day(value: dict[str, object]) -> tuple[date, _Tally] | None:
    """A day line's UTC day and counts."""
    try:
        day = date.fromisoformat(str(value["day"]))
    except KeyError, ValueError:
        return None
    totals = [_count(value.get(key)) for key in ("search", "empty", "read")]
    kinds = value.get("kinds", {})
    if not isinstance(kinds, dict) or set(kinds) - set(KINDS):
        return None
    counters = {operation: _counter(kinds.get(operation, {}), allowed) for operation, allowed in KINDS.items()}
    empty_kinds, unmatched = (
        _counter(value.get("empty_kinds", {}), KINDS["search"]),
        _counter(value.get("unmatched", {})),
    )
    if None in totals or empty_kinds is None or unmatched is None or any(c is None for c in counters.values()):
        return None
    search, empty, read = (int(total or 0) for total in totals)
    tally = _Tally(search, min(empty, search), read, empty_kinds=empty_kinds, unmatched=unmatched)
    tally.kinds = {operation: counter or Counter() for operation, counter in counters.items()}
    return day, tally


def _parse(line: bytes) -> tuple[date, datetime | None, _Tally] | None:
    """An event's or a day line's UTC day, the event's instant and its counts; None for any other line."""
    try:
        value = decode(line)
    except Error:
        return None
    if not isinstance(value, dict):
        return None
    if "at" in value:
        return _event(value)
    if "day" in value and (found := _day(value)):
        return found[0], None, found[1]
    return None


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, UTC)


def _first(today: date, days: int) -> date:
    """A window of `days` counts today and the UTC days before it, whole."""
    return today - timedelta(days=days - 1)


def _rotate(lines: list[bytes], now: datetime) -> bytes:
    """Past days' events as one day line each, today's events kept as they are, nothing outside the largest window.

    Today's events are summed too when keeping them would exceed KEEP: the file stays within the limit.
    """
    today = now.date()
    first = _first(today, max(WINDOWS.values()))
    days: dict[date, _Tally] = {}
    current = _Tally()
    events: list[bytes] = []
    for line in lines:
        if (parsed := _parse(line)) is None or not first <= parsed[0] <= today:
            continue
        day, instant, tally = parsed
        if day == today and instant is not None:
            events.append(line if line.endswith(b"\n") else line + b"\n")
            current.add(tally)
        else:
            days.setdefault(day, _Tally()).add(tally)
    # An older release's mark, while the events it dropped still fall inside the largest window.
    since = _instant(lines[0], "since") if lines else None
    mark = encode({"since": timestamp(since.isoformat())}) if since and since > _midnight(first) else b""
    kept = mark + b"".join(days[day].line(day) for day in sorted(days))
    if len(kept) + sum(map(len, events)) <= KEEP:
        return kept + b"".join(events)
    days.setdefault(today, _Tally()).add(current)
    return mark + b"".join(days[day].line(day) for day in sorted(days))


def note(store: Store, operation: str, results: int, *, kind: str, unmatched: Sequence[str] = ()) -> None:
    """Append one event to private state outside the brain; counting never breaks retrieval.

    Concurrent processes append with O_APPEND, so each line stays whole. Rotation reads the file through the
    descriptor that appended and replaces it atomically: an event another process appends meanwhile may be lost,
    never mixed into a partial or oversized file.
    """
    try:
        state = state_store(store.root)
        path = state.root / USAGE
        now = datetime.now(UTC)
        tokens = (
            [str(term).strip().lower()[:MAX_TOKEN] for term in unmatched if str(term).strip()][:MAX_UNMATCHED]
            if operation == "search" and results == 0 and unmatched
            else []
        )
        known = kind if kind in KINDS.get(operation, ()) else OTHER
        entry: dict[str, object] = {"at": timestamp(now.isoformat()), "op": operation, "kind": known}
        entry["results"] = results
        if tokens:
            entry["unmatched"] = tokens
        line = encode(entry)
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "a+b") as stream:
            stream.write(line)
            size = stream.tell()
            if size <= LIMIT:
                return
            # Concurrent appends may push the file past the limit: read at most twice it, from a whole line.
            start = max(0, size - 2 * LIMIT)
            stream.seek(start)
            lines = stream.read(size - start).splitlines(keepends=True)[1 if start else 0 :]
        state.write(USAGE, _rotate(lines, now))
    except OSError, Error:
        pass


def summary(store: Store, now: datetime | None = None) -> dict[str, Counts]:
    """Searches, searches that found nothing, and reads over today and the 6 or 29 UTC days before it."""
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    counts = {window: Counts(search=0, empty=0, read=0) for window in WINDOWS}
    tallies = {window: _Tally() for window in WINDOWS}
    try:
        lines = state_store(store.root).read(USAGE, LIMIT * 2).splitlines()
    except OSError, Error:
        return counts
    for line in lines:
        # Private counts are advisory: a malformed line never hides the rest.
        if (parsed := _parse(line)) is None:
            continue
        day, instant, tally = parsed
        if day > today or (instant is not None and instant > now):
            continue
        for window, days in WINDOWS.items():
            if day >= _first(today, days):
                tallies[window].add(tally)
    for window, tally in tallies.items():
        counts[window].update(search=tally.search, empty=tally.empty, read=tally.read)
        if any(tally.kinds.values()):
            counts[window]["kinds"] = {op: dict(sorted(counter.items())) for op, counter in tally.kinds.items()}
        if tally.empty_kinds:
            counts[window]["empty_kinds"] = dict(sorted(tally.empty_kinds.items()))
        if tally.unmatched:
            counts[window]["empty_terms"] = [
                {"term": term, "count": count} for term, count in _top(tally.unmatched, TOP_TERMS)
            ]
    # An older release's rotation mark, its first line: a window that began before the oldest event it kept is
    # incomplete.
    if lines and (since := _instant(lines[0], "since")):
        for window, days in WINDOWS.items():
            if since > _midnight(_first(today, days)):
                counts[window]["since"] = timestamp(since.isoformat())
    return counts
