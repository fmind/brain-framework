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
from bf.config import load
from bf.health import attention, source_health
from bf.markdown import authored, note, reference, split_ref
from bf.models import AUTHORED, NAME, Error, NotFoundError, tag_name, timestamp
from bf.storage import Store, relative

# Draft or stable projects whose note is older than this are marked for review; a reminder, never a failure.
REVIEW_DAYS = 14
SECTION = 20
PAGE = 50
LISTING = 200
BROWSE = ["projects", "concepts", "actions", "memories", "tags", "today", "7d"]
_SCOPE = "scope accepts a folder (projects, memories/gmail), a period (today, 7d, 2026-09, 2026-09-25) or an identity"
_MISSING = "page not found; read projects, concepts, actions or memories to browse the brain"
_BELOW = "(i.path=:prefix OR substr(i.path,1,length(:prefix)+1)=:prefix||'/')"
_CLOSED = {"done", "deprecated", "archived"}


Build = Callable[[Store, str, sqlite3.Connection], dict[str, object] | None]


class Scope(TypedDict, total=False):
    """Search bounds: at most one folder or file, time window and identity."""

    since: str
    until: str
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
    """A local day or month, or a trailing window ending now; None when the value names no period."""
    now = (now or datetime.now(UTC)).astimezone()
    try:
        if value in {"today", "yesterday"}:
            day = now.date() - timedelta(days=value == "yesterday")
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            day = date.fromisoformat(value)
        elif re.fullmatch(r"\d{4}-\d{2}", value):
            first = date.fromisoformat(value + "-01")
            following = (first + timedelta(days=32)).replace(day=1)
            previous = (first - timedelta(days=1)).replace(day=1)
            return Period(_midnight(first), _midnight(following), previous.isoformat()[:7], following.isoformat()[:7])
        elif match := re.fullmatch(r"(\d{1,5})([hdw])", value):
            start = now - timedelta(hours=int(match[1]) * {"h": 1, "d": 24, "w": 168}[match[2]])
            return Period(timestamp(start.isoformat()), timestamp(now.isoformat()))
        else:
            return None
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
    if parts[0] == "memories" and len(parts) == 3:
        # A partition file groups records by UTC month; a bare period uses local days.
        if not parts[2].endswith(".jsonl") and (found := period(parts[2], now)):
            return {"prefix": "/".join(parts[:2]), "since": found.since, "until": found.until}
        return {"prefix": f"memories/{parts[1]}/{parts[2].removesuffix('.jsonl')}.jsonl"}
    return {"prefix": path}


def label(brain: str, item: dict[str, object]) -> dict[str, object]:
    """Name the brain and add the portable address of one cached item."""
    ref = str(item["ref"])
    uri = links.address(brain, ref) if item.get("kind") == "record" else links.address(brain, *split_ref(ref))
    return {"brain": brain, **item, "uri": uri}


def _brains(
    stores: list[Store], build: Build, *, strict: bool = True, counted: bool = True, stack: ExitStack | None = None
) -> tuple[list[tuple[str, dict[str, object]]], dict[str, object]]:
    """Build one part per brain; a failing brain is reported unless it is the only one requested."""
    parts: list[tuple[str, dict[str, object]]] = []
    problems: list[dict[str, object]] = []
    stale: list[str] = []
    for store in stores:
        name = store.root.name
        try:
            name = load(store).name
            with ExitStack() as local:
                connection, state = (stack or local).enter_context(index.database(store))
                part = build(store, name, connection)
                skipped = index.problems(connection)
        except (Error, OSError, UnicodeError, sqlite3.DatabaseError) as error:
            if strict and len(stores) == 1:
                if isinstance(error, sqlite3.DatabaseError):
                    raise Error("the search cache is unavailable; run bf build") from error
                raise
            message = str(error) if isinstance(error, Error) else "inaccessible brain or cache; run bf status"
            problems.append({"brain": name, "error": message.replace(str(store.root), "<brain>")})
            continue
        if skipped:
            problems.append({"brain": name, "files": skipped})
        if state != "ready":
            stale.append(name)
        if part is None:
            continue
        problems.extend(cast("list[dict[str, object]]", part.pop("problems", [])))
        if counted:
            usage.note(store, "read", 1)
        parts.append((name, part))
    if strict and not parts and problems:
        raise Error("no selected brain could be read; run bf status with --brain for each brain")
    return parts, {**({"stale": stale} if stale else {}), **({"problems": problems} if problems else {})}


def _gather(parts: list[tuple[str, dict[str, object]]], key: str) -> list[dict[str, object]]:
    return [
        label(name, item) if "ref" in item else {"brain": name, **item}
        for name, part in parts
        for item in cast("list[dict[str, object]]", part.get(key, []))
    ]


def _newest(
    items: list[dict[str, object]], limit: int, *, oldest: bool = False, modified: bool = False
) -> list[dict[str, object]]:
    """Newest event first, or newest modification first; a note's date is both."""

    def key(item: dict[str, object]) -> tuple[str, str]:
        return str((modified and item.get("updated")) or item.get("time", "")), str(item["ref"])

    return sorted(items, key=key, reverse=not oldest)[:limit]


def _listing(
    stores: list[Store],
    where: str,
    params: Mapping[str, object],
    order: str,
    key: Callable[[dict[str, object]], tuple[str, ...]],
    limit: int,
    offset: int,
    *,
    reverse: bool = True,
    review: datetime | None = None,
) -> dict[str, object]:
    """Merge ordered SQLite streams, retaining only the requested page in memory."""

    def build(_store: Store, name: str, connection: sqlite3.Connection) -> dict[str, object]:
        rows, total = index.listing_rows(connection, where, params, order)

        def items() -> Iterator[dict[str, object]]:
            for row in rows:
                yield label(name, row)

        return {"rows": items(), "total": total}

    with ExitStack() as stack:
        parts, extra = _brains(stores, build, counted=False, stack=stack)
        streams = [cast("Iterator[dict[str, object]]", part["rows"]) for _, part in parts]
        try:
            merged = heapq.merge(*streams, key=key, reverse=reverse)
            items = list(islice(islice(merged, offset, None), limit))
        except sqlite3.DatabaseError as error:
            raise Error("the search cache is unavailable; run bf build") from error
        if review:
            for store in stores:
                try:
                    name = load(store).name
                except Error, OSError, UnicodeError:
                    continue  # _brains already reports this unavailable brain.
                selected = [item for item in items if item["brain"] == name]
                if selected:
                    with index.database(store) as (connection, _state):
                        _review(connection, selected, review)
        total = sum(cast("int", part["total"]) for _, part in parts)
    return {
        "items": items,
        "total": total,
        **extra,
        **({"next_offset": offset + len(items)} if offset + len(items) < total else {}),
    }


def _time_key(item: dict[str, object]) -> tuple[str, str]:
    return str(item.get("time", "")), str(item["ref"])


def _next_day(instant: str) -> str:
    return _midnight(datetime.fromisoformat(instant).astimezone().date() + timedelta(days=1))


def _review(connection: sqlite3.Connection, items: list[dict[str, object]], now: datetime) -> None:
    """Mark current project notes whose note is old or has newer linked evidence than its `updated` date."""
    cutoff = timestamp((now - timedelta(days=REVIEW_DAYS)).isoformat())
    for item in items:
        if (
            item.get("type") != "project"
            or item.get("status", "") not in {"", "draft", "stable"}
            or str(item["ref"]).rsplit("/", 1)[-1] in {"index.md", "log.md"}
        ):
            continue
        updated = str(item.get("time", ""))
        # Future-dated notes are not due for review; avoid advancing dates at the calendar limit.
        if updated > timestamp(now.isoformat()):
            continue
        newer = index.newer_links(connection, str(item["ref"]), _next_day(updated)) if updated else 0
        if newer:
            item["new_links"] = newer
        if not updated or updated < cutoff or newer:
            item["review"] = True


def home(stores: list[Store], now: datetime | None = None, *, counted: bool = True) -> dict[str, object]:
    """What needs attention now: current project notes, recent actions and notes, activity and the coming week."""
    now = now or datetime.now(UTC)
    stamp = timestamp(now.isoformat())
    day, week = (timestamp((now - timedelta(days=days)).isoformat()) for days in (1, 7))
    ahead = timestamp((now + timedelta(days=7)).isoformat())

    def build(store: Store, _name: str, connection: sqlite3.Connection) -> dict[str, object]:
        projects, _ = index.listing(
            connection,
            "i.kind='note' AND i.type='project' AND i.status IN ('','draft','stable') "
            "AND i.path NOT GLOB '*/index.md' AND i.path NOT GLOB '*/log.md'",
            {},
            "time DESC,i.ref",
            LISTING,
        )
        _review(connection, projects, now)
        actions, _ = index.listing(connection, index.ACTION, {}, "i.path DESC", 10)
        changed, _ = index.listing(
            connection,
            f"i.kind='note' AND i.time!='' AND ({index.TIME})>=:since AND ({index.TIME})<:until",
            {"since": week, "until": stamp},
            "time DESC",
            SECTION,
        )
        upcoming, _ = index.listing(
            connection,
            index.WITHIN,
            {"since": stamp, "until": ahead},
            "time,i.ref",
            SECTION,
        )
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

    parts, extra = _brains(stores, build, counted=counted)
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
    within = index.WITHIN
    params = {"since": found.since, "until": found.until}

    def build(_store: Store, _brain: str, connection: sqlite3.Connection) -> dict[str, object]:
        changed, _ = index.listing(
            connection,
            f"{index.MODIFIED} AND NOT ({within})",
            params,
            f"({index.UPDATED}) DESC,i.ref",
            SECTION,
        )
        sources = [
            {"source": source, "records": count, "page": f"memories/{source}/{name}"}
            for source, count in index.activity(connection, found.since, found.until)
        ]
        return {"changed": changed, "sources": sources}

    parts, extra = _brains(stores, build, counted=counted)
    reply: dict[str, object] = {
        "page": name,
        "since": found.since,
        "until": found.until,
        **_listing(stores, within, params, "time DESC,i.ref DESC", _time_key, PAGE, offset),
        "changed": _newest(_gather(parts, "changed"), SECTION, modified=True),
        "sources": _gather(parts, "sources"),
    }
    if found.previous:
        reply.update(previous=found.previous, next=found.next)
    for field in ("problems", "stale"):
        if field in extra:
            reply[field] = [*cast("list", reply.get(field, [])), *cast("list", extra[field])]
    return reply


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
        "projects": "i.status IN ('done','deprecated','archived'),time DESC,i.ref DESC",
        "concepts": "i.title,i.ref",
    }.get(top, "i.path DESC")

    def key(item: dict[str, object]) -> tuple[str, ...]:
        if top == "projects":
            return (str(item.get("status") not in _CLOSED), *_time_key(item))
        if top == "concepts":
            return str(item.get("title", "")), str(item["ref"])
        return (str(item["ref"]),)

    reply = _listing(
        stores, where, {"prefix": path}, order, key, LISTING, offset, reverse=top != "concepts", review=now
    )
    if counted:
        for store in stores:
            usage.note(store, "read", 1)
    return {"page": path, **reply}


def memories(
    stores: list[Store], parts: tuple[str, ...], now: datetime | None = None, *, offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """Sources with their coverage, one source with its partitions, or one source's records in a period."""
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

        found, extra = _brains(stores, overview, counted=counted)
        return {"page": "memories", "sources": _gather(found, "sources"), **extra}
    source = parts[1]
    if not re.fullmatch(NAME, source) or len(parts) > 3:
        raise NotFoundError(_MISSING)
    if len(parts) == 2:

        def page(store: Store, _name: str, connection: sqlite3.Connection) -> dict[str, object] | None:
            counts = index.sources(connection)
            if source not in counts and source not in load(store).sensors:
                return None
            health = source_health(store, [source], now=now)[source]
            return {
                "sources": [{"source": source, **counts.get(source, {"records": 0}), **health}],
                "partitions": [{"ref": path, "records": count} for path, count in index.partitions(connection, source)],
            }

        found, extra = _brains(stores, page, counted=counted)
        if not found:
            raise NotFoundError(_MISSING)
        return {
            "page": "/".join(parts),
            "sources": _gather(found, "sources"),
            "partitions": [{"brain": name, **p} for name, part in found for p in cast("list", part["partitions"])],
            **_listing(
                stores,
                "i.kind='record' AND i.source=:source",
                {"source": source},
                "time DESC,i.ref DESC",
                _time_key,
                SECTION,
                offset,
            ),
            **extra,
        }
    name = parts[2].removesuffix(".jsonl")
    window = None if parts[2].endswith(".jsonl") else period(name, now)
    if window:
        where = "i.kind='record' AND i.source=:source AND i.time!='' AND i.time>=:since AND i.time<:until"
        params = {"source": source, "since": window.since, "until": window.until}
    elif name in {"snapshot", "undated"} or re.fullmatch(r"\d{4}-\d{2}", name):
        # A partition file, as listed on the source page: its records match its count exactly.
        where, params = "i.kind='record' AND i.path=:path", {"path": f"memories/{source}/{name}.jsonl"}
    else:
        raise NotFoundError(_MISSING)

    def records(store: Store, _name: str, connection: sqlite3.Connection) -> dict[str, object] | None:
        if source not in index.sources(connection) and source not in load(store).sensors:
            return None
        return {}

    found, extra = _brains(stores, records, counted=counted)
    if not found:
        # Like the source page: an unknown source is a missing page, never an empty answer.
        raise NotFoundError(_MISSING)
    reply: dict[str, object] = {
        "page": "/".join(parts),
        **_listing(stores, where, params, "time DESC,i.ref DESC", _time_key, PAGE, offset),
    }
    if window and window.previous:
        reply.update(previous=f"memories/{source}/{window.previous}", next=f"memories/{source}/{window.next}")
    return {**reply, **extra}


def tags(stores: list[Store], *, offset: int = 0, counted: bool = True) -> dict[str, object]:
    """Enumerate local tag identities, with exact note counts and bounded cross-brain pagination."""

    def build(_store: Store, name: str, connection: sqlite3.Connection) -> dict[str, object]:
        rows = connection.execute("SELECT target,count(*) AS total FROM tags GROUP BY target ORDER BY target")

        def items() -> Iterator[dict[str, object]]:
            for target, total in rows:
                yield {"brain": name, "tag": links.tag(target), "ref": target, "uri": target, "total": total}

        total = connection.execute("SELECT count(DISTINCT target) FROM tags").fetchone()[0]
        return {"rows": items(), "total": total}

    with ExitStack() as stack:
        parts, extra = _brains(stores, build, counted=counted, stack=stack)
        streams = [cast("Iterator[dict[str, object]]", part["rows"]) for _, part in parts]
        try:
            merged = heapq.merge(*streams, key=lambda item: str(item["uri"]))
            items = list(islice(islice(merged, offset, None), LISTING))
        except sqlite3.DatabaseError as error:
            raise Error("the search cache is unavailable; run bf build") from error
        total = sum(cast("int", part["total"]) for _, part in parts)
    return {
        "page": "tags",
        "items": items,
        "total": total,
        **extra,
        **({"next_offset": offset + len(items)} if offset + len(items) < total else {}),
    }


def tagged(stores: list[Store], name: str, *, offset: int = 0, counted: bool = True) -> dict[str, object]:
    """List only explicit frontmatter membership, not prose, aliases or ordinary links to a tag."""
    try:
        tag_name(name)
    except ValueError as error:
        raise Error("invalid tag label; use a ref returned by bf read tags") from error
    targets = []
    for store in stores:
        with suppress(Error, OSError, UnicodeError):
            targets.append(links.address(load(store).name, f"tags/{name}"))
    reply = _listing(
        stores,
        "i.id IN (SELECT item FROM tags WHERE target IN (SELECT value FROM json_each(:targets)))",
        {"targets": json.dumps(targets)},
        "time DESC,i.ref DESC",
        _time_key,
        LISTING,
        offset,
    )
    if counted:
        for store in stores:
            usage.note(store, "read", 1)
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


def _merge(parts: list[tuple[str, dict[str, object]]]) -> list[dict[str, object]]:
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


def context(stores: list[Store], owner: Store, brain: str, reply: dict[str, object]) -> dict[str, object]:
    """Backlinks of a whole note or record and typed claims about it across the selected brains.

    An action's ACTION.md also lists the action's files and the projects it links to.
    """
    ref = str(reply["ref"])
    record = "record" in reply
    path, fragment = ("", "") if record else split_ref(ref)
    if fragment:
        return {}
    identity = links.address(brain, ref) if record else links.address(brain, path)
    try:
        targets, problems = graph.expand(stores, identity)
    except Error:
        # A very long record id has no valid BF address; the item itself still reads.
        return {"problems": [{"brain": brain, "error": "backlinks are unavailable for this reference"}]}

    def build(store: Store, name: str, connection: sqlite3.Connection) -> dict[str, object]:
        local = graph.local_refs(connection, targets)
        claims, truncated = graph.outgoing(connection, local)
        return {
            "backlinks": _backlinks(connection, name, local, ref if store.root == owner.root else ""),
            "claims": [{"brain": name, **claim} for claim in claims],
            "claims_truncated": truncated,
        }

    parts, extra = _brains(stores, build, strict=False, counted=False)
    result: dict[str, object] = {"backlinks": _merge(parts)}
    if claims := [claim for _, part in parts for claim in cast("list[dict[str, object]]", part["claims"])]:
        result["claims"] = claims
    if any(part.get("claims_truncated") for _, part in parts):
        result["claims_truncated"] = True
    issues = [*problems, *cast("list", extra.get("problems", []))]
    if not record and re.fullmatch(r"actions/[^/]+/ACTION\.md", path):
        skipped: dict[str, tuple[int, int, int, int]] = {}
        try:
            result.update(_action(owner, brain, path, str(reply["text"]), skipped))
        except Error, OSError, sqlite3.DatabaseError:
            issues.append({"brain": brain, "error": "the action's files or projects are unavailable; run bf status"})
        if skipped:
            # Never followed, so never offered as the action's files; the owner can replace them.
            issues.append(
                {
                    "brain": brain,
                    "files": [
                        f"{name}: symlinks and special files are not listed" for name in sorted(skipped)[:LISTING]
                    ],
                }
            )
    return {
        **result,
        **({"stale": extra["stale"]} if "stale" in extra else {}),
        **({"problems": issues} if issues else {}),
    }


def _action(
    store: Store, brain: str, path: str, text: str, skipped: dict[str, tuple[int, int, int, int]]
) -> dict[str, object]:
    """The files kept with an action, and the projects its ACTION.md links to."""
    folder = path.rsplit("/", 1)[0]
    files = [name for name in store.files(folder, skipped=skipped) if name != path][:LISTING]
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
        linked, _ = index.listing(
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

    def build(_store: Store, name: str, connection: sqlite3.Connection) -> dict[str, object]:
        local = graph.local_refs(connection, targets)
        claims, truncated = graph.outgoing(connection, local)
        return {
            "backlinks": _backlinks(connection, name, local),
            "claims": [{"brain": name, **claim} for claim in claims],
            "claims_truncated": truncated,
        }

    parts, extra = _brains(stores, build, strict=False, counted=counted)
    backlinks = _merge(parts)
    claims = [claim for _, part in parts for claim in cast("list[dict[str, object]]", part["claims"])]
    issues = [*problems, *cast("list", extra.get("problems", []))]
    if not backlinks and not claims:
        if issues or extra.get("stale"):
            raise Error("identity read is incomplete; run bf status before concluding it is absent")
        return None
    return {
        "page": value,
        "backlinks": backlinks,
        **({"claims": claims} if claims else {}),
        **({"claims_truncated": True} if any(part.get("claims_truncated") for _, part in parts) else {}),
        **({"stale": extra["stale"]} if "stale" in extra else {}),
        **({"problems": issues} if issues else {}),
    }
