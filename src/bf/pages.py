"""Computed pages: bounded, linkable views over the cache; every entry carries a readable ref."""

from __future__ import annotations

import heapq
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import islice
from typing import TypedDict, cast

from bf import graph, index, links, usage
from bf.config import brain_name, load
from bf.health import attention, source_health
from bf.markdown import action_note, authored, editor_lock, entry_note, note, reference, split_ref
from bf.models import AUTHORED, MAX_REPLY, NAME, Error, NotFoundError, addressable, encode, tag_name, timestamp
from bf.storage import UNNAMED, Store, relative, unnamed

# The automatic reminder interval for project files; modification time is not evidence of verification.
REVIEW_DAYS = 14
SECTION = 20
PAGE = 50
LISTING = 200
# A page's items fill at most half a reply: a page of large items ends early and continues at next_offset.
BUDGET = MAX_REPLY // 2
BROWSE = ["projects", "concepts", "actions", "tasks", "memories", "tags", "today", "7d"]
CACHE = "the search cache is unavailable; run bf build"
_SCOPE = (
    "scope accepts a folder or file (projects, memories/gmail), a period (today, 7d, 2026-09, 2026-09-25), "
    "a source period or its undated records (memories/gmail/7d, memories/gmail/undated), an identity or a tag "
    "(bf://NAME/tags/LABEL)"
)
_MISSING = "page not found; read projects, concepts, actions or memories to browse the brain"
_BELOW = "(i.path=:prefix OR substr(i.path,1,length(:prefix)+1)=:prefix||'/')"


Build = Callable[[Store, str, sqlite3.Connection], dict[str, object] | None]
Parts = list[tuple[str, dict[str, object]]]


class Scope(TypedDict, total=False):
    """Search bounds: at most one folder or file, time window or undated items, and identity."""

    since: str
    until: str
    undated: bool
    prefix: str
    target: str


@dataclass(frozen=True)
class Period:
    since: str
    until: str
    previous: str = ""
    next: str = ""


def _midnight(day: date) -> str:
    # A naive datetime resolves the local offset at that date, including DST transitions.
    return timestamp(datetime.combine(day, time.min).astimezone().isoformat())


def period(value: str, now: datetime | None = None) -> Period | None:
    """A local day or month, or a trailing window ending now; None when the value names no period.

    links.PERIOD is the one period syntax, the page namespace links.reserved() keeps from entities.
    """
    if links.PERIOD.fullmatch(value) is None:
        return None
    now = (now or datetime.now(UTC)).astimezone()
    try:
        if value in {"today", "yesterday"}:
            day = now.date() - timedelta(days=value == "yesterday")
        elif value[-1] in "hdw":
            start = now - timedelta(hours=int(value[:-1]) * {"h": 1, "d": 24, "w": 168}[value[-1]])
            return Period(timestamp(start.isoformat()), timestamp(now.isoformat()))
        elif len(value) == len("YYYY-MM"):
            first = date.fromisoformat(value + "-01")
            following = (first + timedelta(days=32)).replace(day=1)
            previous = (first - timedelta(days=1)).replace(day=1)
            return Period(_midnight(first), _midnight(following), previous.isoformat()[:7], following.isoformat()[:7])
        else:
            day = date.fromisoformat(value)
        following = day + timedelta(days=1)
        return Period(
            _midnight(day), _midnight(following), (day - timedelta(days=1)).isoformat(), following.isoformat()
        )
    except (ValueError, OverflowError) as error:
        raise Error(f"invalid period: {value}") from error


def scope(value: str, now: datetime | None = None) -> Scope:
    """Search bounds for one scope: a period, an explicit identity, or a brain folder or file."""
    value = value.strip()
    if not value:
        return {}
    if found := period(value, now):
        return {"since": found.since, "until": found.until}
    if index.identity(value) or value.lower().startswith("bf:"):
        links.tag(value)
        return {"target": links.identity(value)}
    path = value.rstrip("/")
    try:
        parts = relative(path)
    except Error:
        raise Error(_SCOPE) from None
    if parts[0] not in (*AUTHORED, "memories"):
        raise Error(_SCOPE)
    if parts[0] == "memories" and len(parts) >= 3:
        # The source pages below memories/SOURCE, and nothing else: another segment would scope nothing.
        if len(parts) == 3 and not parts[2].endswith(".json") and (found := period(parts[2], now)):
            return {"prefix": "/".join(parts[:2]), "since": found.since, "until": found.until}
        if len(parts) == 3 and parts[2] == "undated":
            return {"prefix": "/".join(parts[:2]), "undated": True}
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{64}\.json", parts[2]):
            raise Error(_SCOPE)
    return {"prefix": path}


def label(brain: str, item: dict[str, object]) -> dict[str, object]:
    """Name the brain and add the portable address of one cached item."""
    ref = str(item["ref"])
    uri = links.address(brain, ref) if item.get("kind") == "record" else links.address(brain, *split_ref(ref))
    return {"brain": brain, **item, "uri": uri}


def brains(
    stores: list[Store], build: Build, *, strict: bool = True, counted: bool = True, stack: ExitStack | None = None
) -> tuple[Parts, dict[str, object]]:
    """Build one part per brain; a failing brain is reported unless it is the only one requested.

    A build returns None when its brain has nothing to add, such as an unknown source. With `stack`, each
    connection stays open for the part's streams; otherwise it closes once the part is built.
    """
    parts: Parts = []
    problems: list[dict[str, object]] = []
    stale: list[str] = []
    answered = False
    for store in stores:
        name = ""
        try:
            name = load(store).name
            with ExitStack() as local:
                connection, state = (stack or local).enter_context(index.database(store))
                part = build(store, name, connection)
                skipped = index.problems(connection)
        except (Error, OSError, UnicodeError, sqlite3.DatabaseError) as error:
            if strict and len(stores) == 1:
                if isinstance(error, sqlite3.DatabaseError):
                    raise Error(CACHE) from error
                raise
            message = str(error) if isinstance(error, Error) else "inaccessible brain or cache; run bf status"
            problems.append({"brain": name or brain_name(store), "error": message.replace(str(store.root), "<brain>")})
            continue
        answered = True
        problems.extend({"brain": name, **problem} for problem in skipped)
        if state != "ready":
            stale.append(name)
        if part is None:
            continue
        problems.extend(cast("list[dict[str, object]]", part.pop("problems", [])))
        if counted:
            usage.note(store, "read", 1)
        parts.append((name, part))
    if strict and stores and not answered:
        raise Error("no selected brain could be read; run bf status with --brain for each brain")
    return parts, {**({"stale": stale} if stale else {}), **({"problems": problems} if problems else {})}


def absent(extra: Mapping[str, object], message: str = _MISSING) -> Error:
    """A missing page or source is NotFoundError only when every selected brain answered from a complete cache."""
    if extra.get("problems") or extra.get("stale"):
        return Error("not found in the readable evidence; run bf status before concluding it is absent")
    return NotFoundError(message)


def unique(problems: list[dict[str, object]]) -> list[dict[str, object]]:
    """Each problem once: identity expansion and the page itself can meet the same unavailable brain."""
    return list({encode(problem): problem for problem in problems}.values())


def fitting(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """The leading items within the reply budget; at least one, so a continuation always advances."""
    size = 0
    for count, item in enumerate(items):
        size += len(encode(item))
        if count and size > BUDGET:
            return items[:count]
    return items


def _gather(parts: Parts, key: str) -> list[dict[str, object]]:
    return [
        label(name, item) if "ref" in item else {"brain": name, **item}
        for name, part in parts
        for item in cast("list[dict[str, object]]", part.get(key, []))
    ]


def _newest(
    items: list[dict[str, object]], limit: int, *, oldest: bool = False, modified: bool = False
) -> list[dict[str, object]]:
    """Newest event first, or newest modification first; a note's date is both. SQL previews use the same key."""

    def key(item: dict[str, object]) -> tuple[str, str]:
        return str((modified and item.get("updated")) or item.get("time", "")), str(item["ref"])

    return sorted(items, key=key, reverse=not oldest)[:limit]


def _rows(
    connection: sqlite3.Connection, where: str, params: Mapping[str, object], order: str, limit: int
) -> list[dict[str, object]]:
    return list(index.listing(connection, where, params, order, limit)[0])


def _paged(
    stores: list[Store],
    build: Build,
    key: Callable[[dict[str, object]], tuple[object, ...]],
    limit: int,
    offset: int,
    *,
    counted: bool,
    reverse: bool = False,
    review: datetime | None = None,
) -> tuple[dict[str, object], Parts]:
    """Merge each part's ordered `rows` into one page of the global order, retaining only that page in memory.

    Each part gives its brain's `total`; its other fields, such as a summary, stay in the returned parts.
    """
    connections: dict[str, sqlite3.Connection] = {}

    def opened(store: Store, name: str, connection: sqlite3.Connection) -> dict[str, object] | None:
        connections[name] = connection
        return build(store, name, connection)

    with ExitStack() as stack:
        parts, extra = brains(stores, opened, counted=counted, stack=stack)
        streams = [cast("Iterator[dict[str, object]]", part.pop("rows")) for _, part in parts]
        try:
            merged = heapq.merge(*streams, key=key, reverse=reverse)
            items = list(islice(islice(merged, offset, None), limit))
            if review:
                for name, _ in parts:
                    if selected := [item for item in items if item["brain"] == name]:
                        _review(connections[name], selected, review)
        except sqlite3.DatabaseError as error:
            raise Error(CACHE) from error
    items = fitting(items)
    total = sum(cast("int", part.pop("total")) for _, part in parts)
    reply: dict[str, object] = {
        "items": items,
        "total": total,
        **extra,
        **({"next_offset": offset + len(items)} if offset + len(items) < total else {}),
    }
    return reply, parts


def _listing(
    stores: list[Store],
    where: str,
    params: Mapping[str, object],
    order: str,
    key: Callable[[dict[str, object]], tuple[str, ...]],
    limit: int,
    offset: int,
    *,
    counted: bool,
    reverse: bool = True,
    review: datetime | None = None,
    summary: Build | None = None,
) -> tuple[dict[str, object], Parts]:
    """Items matching fixed SQL in every brain; `summary` adds a brain's page context, or skips it with None."""

    def build(store: Store, name: str, connection: sqlite3.Connection) -> dict[str, object] | None:
        extra = summary(store, name, connection) if summary else {}
        if extra is None:
            return None
        rows, total = index.listing(connection, where, params, order)
        return {**extra, "rows": (label(name, row) for row in rows), "total": total}

    return _paged(stores, build, key, limit, offset, counted=counted, reverse=reverse, review=review)


def _time_key(item: dict[str, object]) -> tuple[str, str]:
    return str(item.get("time", "")), str(item["ref"])


def _review(connection: sqlite3.Connection, items: list[dict[str, object]], now: datetime) -> None:
    """Derive reminders from local edits and explicit deadlines, without asserting a semantic review."""
    rows = {
        row["ref"]: row
        for row in connection.execute(
            "SELECT i.ref,i.review_after,i.review_due,f.mtime FROM items i JOIN files f ON f.path=i.path "
            "WHERE i.ref IN (SELECT value FROM json_each(?))",
            (json.dumps([item["ref"] for item in items]),),
        )
    }
    stamp = timestamp(now.isoformat())
    for item in items:
        row = rows.get(item["ref"])
        if (
            row is None
            or item.get("status") == "deprecated"
            or not entry_note(str(item["ref"]))
            or not (item.get("type") == "project" or row["review_after"] or row["review_due"])
        ):
            continue
        reasons = []
        modified = ""
        due = ""
        try:
            instant = datetime.fromtimestamp(row["mtime"] / 1_000_000_000, UTC)
            modified = timestamp(instant.isoformat())
            item["modified"] = modified
            if modified > stamp:
                # A future clock cannot establish an age. Surface it instead of postponing forever.
                reasons.append("future_modified")
            if not row["review_due"]:
                due = timestamp((instant + timedelta(days=row["review_after"] or REVIEW_DAYS)).isoformat())
        except ValueError, OverflowError, OSError:
            reasons.append("unknown_modified")
        if row["review_due"]:
            due = _midnight(date.fromisoformat(row["review_due"]))
        item["review_source"] = "review_due" if row["review_due"] else "modified"
        if due:
            item["review_due"] = due
            if due <= stamp:
                reasons.append("due")
        # Incoming evidence keeps its event-date semantics; copying another note is not new evidence.
        newer = index.newer_links(connection, str(item["ref"]), modified) if modified else 0
        if newer:
            item["new_links"] = newer
            reasons.append("newer_evidence")
        if reasons:
            item["review"] = True
            item["review_reasons"] = reasons


def home(stores: list[Store], now: datetime | None = None, *, counted: bool = True) -> dict[str, object]:
    """What needs attention now: current project notes, recent actions and notes, activity and the coming week."""
    now = now or datetime.now(UTC)
    stamp = timestamp(now.isoformat())
    day, week = (timestamp((now - timedelta(days=days)).isoformat()) for days in (1, 7))
    ahead = timestamp((now + timedelta(days=7)).isoformat())

    def build(store: Store, _name: str, connection: sqlite3.Connection) -> dict[str, object]:
        # Each preview's SQL order matches the Python merge below, so ties at a cap select the same items.
        projects = _rows(
            connection,
            f"{index.ENTRY} AND i.type='project' AND i.status!='deprecated'",
            {},
            "time DESC,i.ref DESC",
            LISTING,
        )
        _review(connection, projects, now)
        actions = _rows(connection, index.ACTION, {}, "i.path DESC", 10)
        changed = _rows(
            connection,
            f"i.kind='note' AND i.time!='' AND ({index.TIME})>=:since AND ({index.TIME})<:until",
            {"since": week, "until": stamp},
            "time DESC,i.ref DESC",
            SECTION,
        )
        upcoming = _rows(connection, index.WITHIN, {"since": stamp, "until": ahead}, "time,i.ref", SECTION)
        activity = [
            {"source": source, "records": count, "page": f"memories/{source}/24h"}
            for source, count in index.activity(connection, day, stamp)
        ]
        issues: list[dict[str, object]] = []
        try:
            alerts = attention(store, now)
        except Error, OSError, UnicodeError:
            alerts = []
            issues.append({"brain": _name, "error": "operational status is unavailable; run bf status"})
        return {
            "problems": issues,
            "projects": projects,
            "actions": actions,
            "changed": changed,
            "activity": activity,
            "upcoming": upcoming,
            "attention": alerts,
        }

    parts, extra = brains(stores, build, counted=counted)
    projects = sorted(
        _newest(_gather(parts, "projects"), LISTING * len(parts)), key=lambda item: not item.get("review")
    )
    return {
        "page": "",
        "projects": projects,
        "actions": sorted(_gather(parts, "actions"), key=lambda i: str(i["ref"]), reverse=True)[:10],
        "changed": _newest(_gather(parts, "changed"), SECTION),
        "activity": _gather(parts, "activity"),
        "upcoming": _newest(_gather(parts, "upcoming"), SECTION, oldest=True),
        "attention": _gather(parts, "attention"),
        "pages": BROWSE,
        **extra,
    }


def timeline(
    stores: list[Store], name: str, found: Period, *, offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """Items dated within a period, items modified within it, and each source's share of the period."""
    params = {"since": found.since, "until": found.until}

    def summary(_store: Store, _brain: str, connection: sqlite3.Connection) -> dict[str, object]:
        changed = _rows(
            connection,
            f"{index.MODIFIED} AND NOT ({index.WITHIN})",
            params,
            f"({index.UPDATED}) DESC,i.ref DESC",
            SECTION,
        )
        sources = [
            {"source": source, "records": count, "page": f"memories/{source}/{name}"}
            for source, count in index.activity(connection, found.since, found.until)
        ]
        return {"changed": changed, "sources": sources}

    reply, parts = _listing(
        stores, index.WITHIN, params, "time DESC,i.ref DESC", _time_key, PAGE, offset, counted=counted, summary=summary
    )
    return {
        "page": name,
        "since": found.since,
        "until": found.until,
        **reply,
        "changed": _newest(_gather(parts, "changed"), SECTION, modified=True),
        "sources": _gather(parts, "sources"),
        **({"previous": found.previous, "next": found.next} if found.previous else {}),
    }


def _directory(store: Store, path: str) -> bool:
    try:
        relative(path)
        with store.parent(path) as (parent, leaf):
            return stat.S_ISDIR(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode)
    except Error, OSError:
        return False


def folder(
    stores: list[Store], path: str, now: datetime | None = None, *, offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """Notes below an authored folder, with a continuation through the complete global order."""
    now = now or datetime.now(UTC)
    top = path.split("/")[0]
    where = index.ACTION if path == "actions" else f"i.kind='note' AND {_BELOW}"
    order = {
        "projects": "i.status='deprecated',time DESC,i.ref DESC",
        "concepts": "i.title,i.ref",
    }.get(top, "i.path DESC")

    def key(item: dict[str, object]) -> tuple[str, ...]:
        if top == "projects":
            return (str(item.get("status") != "deprecated"), *_time_key(item))
        if top == "concepts":
            return str(item.get("title", "")), str(item["ref"])
        return (str(item["ref"]),)

    reply, _ = _listing(
        stores,
        where,
        {"prefix": path},
        order,
        key,
        LISTING,
        offset,
        counted=counted,
        reverse=top != "concepts",
        review=now,
    )
    return {"page": path, **reply}


def memories(
    stores: list[Store], parts: tuple[str, ...], now: datetime | None = None, *, offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """Sources with their coverage, one source with its records, or one source's records in a period."""
    now = now or datetime.now(UTC)
    if len(parts) == 1:
        if offset:
            raise Error("offset applies to a source's records; read memories/SOURCE")

        def overview(store: Store, _name: str, connection: sqlite3.Connection) -> dict[str, object]:
            counts = index.sources(connection)
            health = source_health(store, counts, now=now)
            return {
                "sources": [
                    {"source": name, "page": f"memories/{name}", **counts.get(name, {"records": 0}), **value}
                    for name, value in health.items()
                ]
            }

        found, extra = brains(stores, overview, counted=counted)
        return {"page": "memories", "sources": _gather(found, "sources"), **extra}
    source = parts[1]
    if not re.fullmatch(NAME, source) or len(parts) > 3:
        raise NotFoundError(_MISSING)

    def known(store: Store, connection: sqlite3.Connection) -> dict[str, object] | None:
        """The source's record count in a brain that indexes or configures it."""
        counts = index.sources(connection)
        if source not in counts and source not in load(store).sensors:
            return None
        return counts.get(source, {"records": 0})

    if len(parts) == 2:

        def coverage(store: Store, _name: str, connection: sqlite3.Connection) -> dict[str, object] | None:
            if (count := known(store, connection)) is None:
                return None
            return {"sources": [{"source": source, **count, **source_health(store, [source], now=now)[source]}]}

        reply, found = _listing(
            stores,
            "i.kind='record' AND i.source=:source",
            {"source": source},
            "time DESC,i.ref DESC",
            _time_key,
            SECTION,
            offset,
            counted=counted,
            summary=coverage,
        )
        if not found:
            # An unknown source is a missing page, never an empty answer.
            raise absent(reply)
        return {"page": "/".join(parts), "sources": _gather(found, "sources"), **reply}
    name = parts[2]
    window = None if name.endswith(".json") else period(name, now)
    if window:
        where = "i.kind='record' AND i.source=:source AND i.time!='' AND i.time>=:since AND i.time<:until"
        params = {"source": source, "since": window.since, "until": window.until}
    elif name == "undated":
        where, params = "i.kind='record' AND i.source=:source AND i.time=''", {"source": source}
    elif re.fullmatch(r"[0-9a-f]{64}\.json", name):
        where, params = "i.kind='record' AND i.path=:path", {"path": f"memories/{source}/{name}"}
    else:
        raise NotFoundError(_MISSING)

    def exists(store: Store, _name: str, connection: sqlite3.Connection) -> dict[str, object] | None:
        return None if known(store, connection) is None else {}

    reply, found = _listing(
        stores, where, params, "time DESC,i.ref DESC", _time_key, PAGE, offset, counted=counted, summary=exists
    )
    if not found:
        raise absent(reply)
    return {
        "page": "/".join(parts),
        **reply,
        **(
            {"previous": f"memories/{source}/{window.previous}", "next": f"memories/{source}/{window.next}"}
            if window and window.previous
            else {}
        ),
    }


def tasks(stores: list[Store], *, offset: int = 0, counted: bool = True) -> dict[str, object]:
    """Open checkboxes in current entry notes, with source lines and complete aggregate counts."""
    # Working attachments can quote source checkboxes: only authored entry notes join the task queue.
    eligible = f"{index.ENTRY} AND i.status!='deprecated'"

    def build(_store: Store, name: str, connection: sqlite3.Connection) -> dict[str, object]:
        summary = connection.execute(
            "SELECT coalesce(sum(t.done=0),0),coalesce(sum(t.done=1),0),count(DISTINCT t.item) "  # noqa: S608 - fixed SQL
            f"FROM tasks t JOIN items i ON i.id=t.item WHERE {eligible}"
        ).fetchone()
        rows = connection.execute(
            "SELECT i.ref,i.title,t.line,t.fragment,t.text FROM tasks t JOIN items i ON i.id=t.item "  # noqa: S608 - fixed SQL
            f"WHERE t.done=0 AND {eligible} ORDER BY i.ref,t.line"
        )

        def items() -> Iterator[dict[str, object]]:
            for ref, title, line, fragment, text in rows:
                yield label(
                    name,
                    {
                        "ref": ref + ("#" + fragment if fragment else ""),
                        "note": ref,
                        "title": title,
                        "line": line,
                        "text": text,
                    },
                )

        counts = dict(zip(("open", "done", "notes"), summary, strict=True))
        return {"rows": items(), "total": counts["open"], "summary": counts}

    reply, parts = _paged(
        stores,
        build,
        lambda item: (str(item["note"]), int(cast("int", item["line"])), str(item["brain"])),
        PAGE,
        offset,
        counted=counted,
    )
    summary = {
        key: sum(cast("dict[str, int]", part["summary"])[key] for _, part in parts) for key in ("open", "done", "notes")
    }
    return {"page": "tasks", **reply, "summary": summary}


def tags(stores: list[Store], *, offset: int = 0, counted: bool = True) -> dict[str, object]:
    """Enumerate local tag identities, with exact note counts and bounded cross-brain pagination."""

    def build(_store: Store, name: str, connection: sqlite3.Connection) -> dict[str, object]:
        rows = connection.execute("SELECT target,count(*) AS total FROM tags GROUP BY target ORDER BY target")

        def items() -> Iterator[dict[str, object]]:
            for target, total in rows:
                yield {"brain": name, "tag": links.tag(target), "ref": target, "uri": target, "total": total}

        total = connection.execute("SELECT count(DISTINCT target) FROM tags").fetchone()[0]
        return {"rows": items(), "total": total}

    reply, _ = _paged(stores, build, lambda item: (str(item["uri"]),), LISTING, offset, counted=counted)
    return {"page": "tags", **reply}


def _label(name: str) -> None:
    try:
        tag_name(name)
    except ValueError as error:
        raise Error("invalid tag label; use a ref returned by bf read tags") from error


def tagged(stores: list[Store], name: str, *, offset: int = 0, counted: bool = True) -> dict[str, object]:
    """List only explicit frontmatter membership, not prose, aliases or ordinary links to a tag."""
    _label(name)
    targets = []
    for store in stores:
        with suppress(Error, OSError, UnicodeError):
            targets.append(links.address(load(store).name, f"tags/{name}"))
    reply, _ = _listing(
        stores,
        "i.id IN (SELECT item FROM tags WHERE target IN (SELECT value FROM json_each(:targets)))",
        {"targets": json.dumps(targets)},
        "time DESC,i.ref DESC",
        _time_key,
        LISTING,
        offset,
        counted=counted,
    )
    return {"page": f"tags/{name}", **reply}


def page(
    stores: list[Store], path: str, now: datetime | None = None, *, offset: int = 0, counted: bool = True
) -> dict[str, object] | None:
    """The page a ref names, or None when it names a note, a record or an identity."""
    if not path:
        if offset:
            raise Error("offset applies to listing pages; follow a home page's projects, actions or period link")
        return home(stores, now, counted=counted)
    if found := period(path, now):
        return timeline(stores, path, found, offset=offset, counted=counted)
    parts = tuple(path.rstrip("/").split("/"))
    if parts == ("tasks",):
        return tasks(stores, offset=offset, counted=counted)
    if parts[0] == "tags":
        if len(parts) == 1:
            return tags(stores, offset=offset, counted=counted)
        return tagged(stores, path.removeprefix("tags/"), offset=offset, counted=counted)
    if parts[0] == "memories":
        relative("/".join(parts))
        return memories(stores, parts, now, offset=offset, counted=counted)
    # Notes and their sections (`path#heading`) are items, not folders.
    if authored(split_ref(path)[0]):
        return None
    if parts[0] in AUTHORED and not (parts[0] == "actions" and len(parts) == 2):
        name = "/".join(parts)
        relative(name)
        # Entity names such as projects/archive are logical: only an existing folder is a page.
        if name in AUTHORED or any(_directory(store, name) for store in stores):
            return folder(stores, name, now, offset=offset, counted=counted)
    return None


def readable(ref: str) -> str:
    """Check a read ref's syntax without a brain, as `page` and `retrieve.read` apply it.

    A malformed ref fails here; a well-formed one can still name nothing in the selected brains.
    """
    if len(ref) > 8192:
        raise Error("expected a reference of at most 8192 characters")
    value = ref.strip()
    if parsed := links.parse(value):
        links.tag(parsed.identity)
        if parsed.fragment:
            return ref
    path = parsed.path if parsed else value
    parts = path.rstrip("/").split("/")
    period(path)
    if parts[0] == "tags" and len(parts) > 1:
        _label(path.removeprefix("tags/"))
    elif authored(note := split_ref(path)[0]):
        # A note's section fragment is free text; only its path must be normalized.
        relative(note)
    elif parts[0] in (*AUTHORED, "memories"):
        # Pages and folders check the whole path, including any `#`.
        relative("/".join(parts))
    return ref


def _backlinks(
    connection: sqlite3.Connection, brain: str, targets: set[str], exclude: str = ""
) -> list[dict[str, object]]:
    groups = index.incoming(connection, targets, exclude=exclude)
    for group in groups:
        for item in cast("list[dict[str, object]]", group["items"]):
            claims, truncated = graph.explanations(connection, str(item["ref"]), targets)
            if claims:
                item["relations"] = claims
            if truncated:
                item["relations_truncated"] = True
        group["items"] = [label(brain, item) for item in cast("list[dict[str, object]]", group["items"])]
    return groups


def _merge(parts: Parts) -> list[dict[str, object]]:
    """Combine each brain's relationship groups; items stay newest first within their relationship."""
    merged: dict[str, dict[str, object]] = {}
    for _, part in parts:
        for group in cast("list[dict[str, object]]", part["backlinks"]):
            relation = str(group.get("relation", ""))
            into = merged.setdefault(relation, {"relation": relation, "total": 0, "items": []})
            into["total"] = cast("int", into["total"]) + cast("int", group["total"])
            into["items"] = [*cast("list", into["items"]), *cast("list", group["items"])]
    result = []
    for relation in sorted(merged, key=lambda value: (value == "", value)):
        group = merged[relation]
        group["items"] = _newest(cast("list[dict[str, object]]", group["items"]), SECTION)
        result.append(group if relation else {key: value for key, value in group.items() if key != "relation"})
    return result


def _graph(targets: set[str], owner: Store | None = None, ref: str = "") -> Build:
    """Each brain's backlinks and typed claims about the expanded targets; the owning item is not its own backlink."""

    def build(store: Store, name: str, connection: sqlite3.Connection) -> dict[str, object]:
        local = graph.local_refs(connection, targets)
        claims, truncated = graph.outgoing(connection, local)
        exclude = ref if owner is not None and store.root == owner.root else ""
        return {
            "backlinks": _backlinks(connection, name, local, exclude),
            "claims": [{"brain": name, **claim} for claim in claims],
            "claims_truncated": truncated,
        }

    return build


def _related(parts: Parts) -> dict[str, object]:
    claims = [claim for _, part in parts for claim in cast("list[dict[str, object]]", part["claims"])]
    return {
        "backlinks": _merge(parts),
        **({"claims": claims} if claims else {}),
        **({"claims_truncated": True} if any(part["claims_truncated"] for _, part in parts) else {}),
    }


def context(stores: list[Store], owner: Store, brain: str, reply: dict[str, object]) -> dict[str, object]:
    """Backlinks of a whole note or record and typed claims about it across the selected brains.

    An action's ACTION.md also lists the action's files and the projects it links to.
    """
    ref = str(reply["ref"])
    record = "record" in reply
    path, fragment = ("", "") if record else split_ref(ref)
    if fragment:
        return {}
    if not record and not addressable(path):
        # Record ids are bounded to fit a BF address; a note path is not, but the note itself still reads.
        return {"problems": [{"brain": brain, "error": "backlinks are unavailable for a path this long; shorten it"}]}
    try:
        targets, problems = graph.expand(stores, links.address(brain, ref if record else path))
    except Error:
        # Only an invalid address raises, such as a note path with a control character; the note itself reads.
        return {
            "problems": [{"brain": brain, "file": path, "error": "backlinks are unavailable for this path; rename it"}]
        }
    parts, extra = brains(stores, _graph(targets, owner, ref), strict=False, counted=False)
    result = _related(parts)
    issues: list[dict[str, object]] = [*problems, *cast("list[dict[str, object]]", extra.get("problems", []))]
    if not record and action_note(path):
        skipped: dict[str, tuple[int, int, int, int]] = {}
        try:
            result.update(_action(owner, brain, path, str(reply["text"]), skipped))
        except Error, OSError, sqlite3.DatabaseError:
            issues.append({"brain": brain, "error": "the action's files or projects are unavailable; run bf status"})
        # Never followed, so never offered as the action's files; the owner can replace or rename them.
        issues.extend(
            {
                "brain": brain,
                "file": name,
                "error": UNNAMED if unnamed(name) else "symlinks and special files are not listed",
            }
            for name in sorted(name for name in skipped if not editor_lock(name))[:LISTING]
        )
    return {
        **result,
        **({"stale": extra["stale"]} if "stale" in extra else {}),
        **({"problems": unique(issues)} if issues else {}),
    }


def _action(
    store: Store, brain: str, path: str, text: str, skipped: dict[str, tuple[int, int, int, int]]
) -> dict[str, object]:
    """The files kept with an action, and the projects its ACTION.md links to."""
    folder = path.rsplit("/", 1)[0]
    files = [name for name in store.files(folder, skipped=skipped) if name != path and not editor_lock(name)][:LISTING]
    projects = set()
    for target in note(path, text.encode()).targets:
        resolved = reference(path, target)
        parsed = links.parse(resolved) if resolved.lower().startswith("bf:") else None
        if parsed and parsed.brain != brain:
            continue
        name = split_ref(parsed.path if parsed else resolved)[0]
        if name.startswith("projects/") and authored(name):
            projects.add(name)
    with index.database(store) as (connection, _state):
        linked = _rows(
            connection,
            "i.ref IN (SELECT value FROM json_each(:refs))",
            {"refs": json.dumps(sorted(projects))},
            "i.ref",
            SECTION,
        )
    return {"files": files, "projects": [label(brain, item) for item in linked]}


def identity(stores: list[Store], value: str, *, counted: bool = True) -> dict[str, object] | None:
    """Everything that links to an identity without an owning note, and the claims made about it."""
    if not (index.identity(value) or value.lower().startswith("bf:")):
        return None
    value = links.identity(value)
    targets, problems = graph.expand(stores, value)
    parts, extra = brains(stores, _graph(targets), strict=False, counted=counted)
    result = _related(parts)
    issues: list[dict[str, object]] = [*problems, *cast("list[dict[str, object]]", extra.get("problems", []))]
    if not result["backlinks"] and "claims" not in result:
        if issues or extra.get("stale"):
            raise Error("identity read is incomplete; run bf status before concluding it is absent")
        return None
    return {
        "page": value,
        **result,
        **({"stale": extra["stale"]} if "stale" in extra else {}),
        **({"problems": unique(issues)} if issues else {}),
    }
