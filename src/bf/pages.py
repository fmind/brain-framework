"""Computed pages: bounded, linkable views over the cache; every entry carries a readable ref."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TypedDict, cast

from bf import graph, index, links, usage
from bf.config import load
from bf.health import attention, source_health
from bf.markdown import authored, note, reference, split_ref
from bf.models import AUTHORED, NAME, Error, timestamp
from bf.storage import Store, relative

# Active or blocked projects whose note is older than this are marked for review; a reminder, never a failure.
REVIEW_DAYS = 14
SECTION = 20
PAGE = 50
LISTING = 200
BROWSE = ["projects", "concepts", "actions", "memories", "today", "7d"]
_SCOPE = "scope accepts a folder (projects, memories/gmail), a period (today, 7d, 2026-09, 2026-09-25) or an identity"
_MISSING = "page not found; read projects, concepts, actions or memories to browse the brain"
_BELOW = "(i.path=:prefix OR substr(i.path,1,length(:prefix)+1)=:prefix||'/')"
_CLOSED = {"done", "deprecated", "archived"}
_ORDER = {
    "projects": "i.status IN ('done','deprecated','archived'),time DESC,i.ref",
    "concepts": "i.title,i.ref",
    "actions": "i.path DESC",
}

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


def owned(store: Store) -> set[str]:
    """Sources whose text the owner writes; every other record source is external."""
    return {name for name, sensor in load(store).sensors.items() if sensor.trust == "owner"}


def guard[T](value: T, sources: set[str], *, excerpts: bool = False) -> T:
    """Mark external records wherever they appear; pages also drop their excerpts."""
    if isinstance(value, list):
        for child in value:
            guard(child, sources, excerpts=excerpts)
    elif isinstance(value, dict):
        if value.get("kind") == "record" and value.get("source") not in sources:
            value["external"] = True
            if not excerpts:
                value.pop("excerpt", None)
        for child in value.values():
            guard(child, sources, excerpts=excerpts)
    return value


def label(brain: str, item: dict[str, object]) -> dict[str, object]:
    """Name the brain and add the portable address of one cached item."""
    ref = str(item["ref"])
    uri = links.address(brain, ref) if item.get("kind") == "record" else links.address(brain, *split_ref(ref))
    return {"brain": brain, **item, "uri": uri}


def _brains(
    stores: list[Store], build: Build, *, strict: bool = True, counted: bool = True
) -> tuple[list[tuple[str, dict[str, object]]], dict[str, object]]:
    """Build one part per brain; a failing brain is reported unless it is the only one requested."""
    parts: list[tuple[str, dict[str, object]]] = []
    problems: list[dict[str, object]] = []
    stale: list[str] = []
    for store in stores:
        name = store.root.name
        try:
            name = load(store).name
            with index.database(store) as (connection, state):
                part = guard(build(store, name, connection), owned(store))
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


def _next_day(instant: str) -> str:
    return _midnight(datetime.fromisoformat(instant).astimezone().date() + timedelta(days=1))


def _review(connection: sqlite3.Connection, items: list[dict[str, object]], now: datetime) -> None:
    """Mark active projects whose note is old or has newer linked evidence than its `updated` date."""
    cutoff = timestamp((now - timedelta(days=REVIEW_DAYS)).isoformat())
    for item in items:
        if item.get("type") != "project" or item.get("status") not in {"active", "blocked"}:
            continue
        updated = str(item.get("time", ""))
        newer = index.newer_links(connection, str(item["ref"]), _next_day(updated)) if updated else 0
        if newer:
            item["new_links"] = newer
        if not updated or updated < cutoff or newer:
            item["review"] = True


def home(stores: list[Store], now: datetime | None = None, *, counted: bool = True) -> dict[str, object]:
    """What needs attention now: active projects, recent actions and notes, activity and the coming week."""
    now = now or datetime.now(UTC)
    stamp = timestamp(now.isoformat())
    day, week = (timestamp((now - timedelta(days=days)).isoformat()) for days in (1, 7))
    ahead = timestamp((now + timedelta(days=7)).isoformat())

    def build(store: Store, _name: str, connection: sqlite3.Connection) -> dict[str, object]:
        projects, _ = index.listing(
            connection,
            "i.kind='note' AND i.type='project' AND i.status IN ('active','blocked')",
            {},
            "time DESC,i.ref",
            LISTING,
        )
        _review(connection, projects, now)
        actions, _ = index.listing(connection, index.ACTION, {}, "i.path DESC", 10)
        changed, _ = index.listing(
            connection,
            f"i.kind='note' AND i.time!='' AND ({index.TIME})>=:since",
            {"since": week},
            "time DESC",
            SECTION,
        )
        upcoming, _ = index.listing(
            connection,
            f"i.time!='' AND ({index.TIME})>=:since AND ({index.TIME})<:until",
            {"since": stamp, "until": ahead},
            "time,i.ref",
            SECTION,
        )
        activity = [
            {"source": source, "records": count, "page": f"memories/{source}/24h"}
            for source, count in index.activity(connection, day, stamp)
        ]
        return {
            "projects": projects,
            "actions": actions,
            "changed": changed,
            "activity": activity,
            "upcoming": upcoming,
            "attention": attention(store, now),
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


def timeline(stores: list[Store], name: str, found: Period, *, counted: bool = True) -> dict[str, object]:
    """Items dated within a period, items modified within it, and each source's share of the period."""
    within = f"i.time!='' AND ({index.TIME})>=:since AND ({index.TIME})<:until"
    params = {"since": found.since, "until": found.until}

    def build(_store: Store, _brain: str, connection: sqlite3.Connection) -> dict[str, object]:
        items, total = index.listing(connection, within, params, "time DESC,i.ref", PAGE)
        changed, _ = index.listing(
            connection,
            f"({index.UPDATED})>=:since AND ({index.UPDATED})<:until AND NOT ({within})",
            params,
            f"({index.UPDATED}) DESC,i.ref",
            SECTION,
        )
        sources = [
            {"source": source, "records": count, "page": f"memories/{source}/{name}"}
            for source, count in index.activity(connection, found.since, found.until)
        ]
        return {"items": items, "total": total, "changed": changed, "sources": sources}

    parts, extra = _brains(stores, build, counted=counted)
    reply: dict[str, object] = {
        "page": name,
        "since": found.since,
        "until": found.until,
        "items": _newest(_gather(parts, "items"), PAGE),
        "total": sum(cast("int", part["total"]) for _, part in parts),
        "changed": _newest(_gather(parts, "changed"), SECTION, modified=True),
        "sources": _gather(parts, "sources"),
    }
    if found.previous:
        reply.update(previous=found.previous, next=found.next)
    return {**reply, **extra}


def _directory(store: Store, path: str) -> bool:
    try:
        relative(path)
        with store.parent(path) as (parent, leaf):
            return stat.S_ISDIR(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode)
    except Error, OSError:
        return False


def folder(stores: list[Store], path: str, now: datetime | None = None, *, counted: bool = True) -> dict[str, object]:
    """Notes below an authored folder; the actions folder lists one ACTION.md per action, newest first."""
    now = now or datetime.now(UTC)
    top = path.split("/")[0]
    where = index.ACTION if path == "actions" else f"i.kind='note' AND {_BELOW}"
    order = _ORDER.get(top, "i.path")

    def build(_store: Store, _name: str, connection: sqlite3.Connection) -> dict[str, object]:
        items, total = index.listing(connection, where, {"prefix": path}, order, LISTING)
        _review(connection, items, now)
        return {"items": items, "total": total}

    parts, extra = _brains(stores, build, counted=counted)
    items = sorted(_gather(parts, "items"), key=lambda item: str(item["ref"]))
    if top == "projects":
        items = sorted(_newest(items, len(items)), key=lambda item: item.get("status") in _CLOSED)
    elif top == "concepts":
        items.sort(key=lambda item: str(item.get("title", "")))
    elif top == "actions":
        items.reverse()
    return {
        "page": path,
        "items": items,
        "total": sum(cast("int", part["total"]) for _, part in parts),
        **extra,
    }


def memories(
    stores: list[Store], parts: tuple[str, ...], now: datetime | None = None, *, counted: bool = True
) -> dict[str, object]:
    """Sources with their coverage, one source with its partitions, or one source's records in a period."""
    now = now or datetime.now(UTC)
    if len(parts) == 1:

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
        raise Error(_MISSING)
    if len(parts) == 2:

        def page(store: Store, _name: str, connection: sqlite3.Connection) -> dict[str, object] | None:
            counts = index.sources(connection)
            if source not in counts and source not in load(store).sensors:
                return None
            health = source_health(store, [source], now=now)[source]
            items, total = index.listing(
                connection, "i.kind='record' AND i.source=:source", {"source": source}, "time DESC,i.ref", SECTION
            )
            return {
                "sources": [{"source": source, **counts.get(source, {"records": 0}), **health}],
                "partitions": [{"ref": path, "records": count} for path, count in index.partitions(connection, source)],
                "items": items,
                "total": total,
            }

        found, extra = _brains(stores, page, counted=counted)
        if not found:
            raise Error(_MISSING)
        return {
            "page": "/".join(parts),
            "sources": _gather(found, "sources"),
            "partitions": [{"brain": name, **p} for name, part in found for p in cast("list", part["partitions"])],
            "items": _newest(_gather(found, "items"), SECTION),
            "total": sum(cast("int", part["total"]) for _, part in found),
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
        raise Error(_MISSING)

    def records(store: Store, _name: str, connection: sqlite3.Connection) -> dict[str, object] | None:
        if source not in index.sources(connection) and source not in load(store).sensors:
            return None
        items, total = index.listing(connection, where, params, "time DESC,i.ref", PAGE)
        return {"items": items, "total": total}

    found, extra = _brains(stores, records, counted=counted)
    if not found:
        # Like the source page: an unknown source is a missing page, never an empty answer.
        raise Error(_MISSING)
    reply: dict[str, object] = {
        "page": "/".join(parts),
        "items": _newest(_gather(found, "items"), PAGE),
        "total": sum(cast("int", part["total"]) for _, part in found),
    }
    if window and window.previous:
        reply.update(previous=f"memories/{source}/{window.previous}", next=f"memories/{source}/{window.next}")
    return {**reply, **extra}


def page(
    stores: list[Store], path: str, now: datetime | None = None, *, counted: bool = True
) -> dict[str, object] | None:
    """The page a ref names, or None when it names a note, a record or an identity."""
    if not path:
        return home(stores, now, counted=counted)
    if found := period(path, now):
        return timeline(stores, path, found, counted=counted)
    parts = tuple(path.rstrip("/").split("/"))
    if parts[0] == "memories":
        relative("/".join(parts))
        return memories(stores, parts, now, counted=counted)
    # Notes and their sections (`path#heading`) are items, not folders.
    if authored(split_ref(path)[0]):
        return None
    if parts[0] in AUTHORED and not (parts[0] == "actions" and len(parts) == 2):
        name = "/".join(parts)
        relative(name)
        # Entity names such as projects/archive are logical: only an existing folder is a page.
        if name in AUTHORED or any(_directory(store, name) for store in stores):
            return folder(stores, name, now, counted=counted)
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
        return {
            "backlinks": _backlinks(connection, name, local, ref if store.root == owner.root else ""),
            "claims": [{"brain": name, **claim} for claim in graph.outgoing(connection, local)],
        }

    parts, extra = _brains(stores, build, strict=False, counted=False)
    result: dict[str, object] = {"backlinks": _merge(parts)}
    if claims := [claim for _, part in parts for claim in cast("list[dict[str, object]]", part["claims"])]:
        result["claims"] = claims
    issues = [*problems, *cast("list", extra.get("problems", []))]
    if not record and re.fullmatch(r"actions/[^/]+/ACTION\.md", path):
        try:
            result.update(_action(owner, brain, path, str(reply["text"])))
        except Error, OSError, sqlite3.DatabaseError:
            issues.append({"brain": brain, "error": "the action's files or projects are unavailable; run bf status"})
    return {
        **result,
        **({"stale": extra["stale"]} if "stale" in extra else {}),
        **({"problems": issues} if issues else {}),
    }


def _action(store: Store, brain: str, path: str, text: str) -> dict[str, object]:
    """The files kept with an action, and the projects its ACTION.md links to."""
    folder = path.rsplit("/", 1)[0]
    files = [name for name in store.files(folder) if name != path][:LISTING]
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
        claims = [{"brain": name, **claim} for claim in graph.outgoing(connection, local)]
        return {"backlinks": _backlinks(connection, name, local), "claims": claims}

    parts, extra = _brains(stores, build, strict=False, counted=counted)
    backlinks = _merge(parts)
    claims = [claim for _, part in parts for claim in cast("list[dict[str, object]]", part["claims"])]
    if not backlinks and not claims:
        return None
    issues = [*problems, *cast("list", extra.get("problems", []))]
    return {
        "page": value,
        "backlinks": backlinks,
        **({"claims": claims} if claims else {}),
        **({"stale": extra["stale"]} if "stale" in extra else {}),
        **({"problems": issues} if issues else {}),
    }
