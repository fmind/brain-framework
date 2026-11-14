"""Computed pages: bounded, linkable views over the cache; every entry carries a readable ref."""

from __future__ import annotations

import json
import re
import sqlite3
import stat
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, TypedDict, cast

from pydantic import ValidationError

from bf import graph, index, links, usage
from bf.config import load
from bf.health import attention, source_health
from bf.markdown import Markdown, action_note, authored, editor_lock, entry_note, note, preview, reference, split_ref
from bf.models import (
    AUTHORED,
    CACHE_ERROR,
    NAME,
    Bounds,
    Error,
    InputError,
    NotFoundError,
    Query,
    addressable,
    encode,
    present,
    suggest,
    tag_name,
    timestamp,
)
from bf.storage import FileAncestorError, Store, relative, temporary, unread

REVIEW_DAYS = 14
NEWER = 5
SECTION = 20
PAGE = 50
LISTING = 200
PREVIEW = 5
# Reply byte budget (items or exact text page) so continuation advances via next_offset.
BUDGET = 32 << 10
BROWSE = ["projects", "concepts", "actions", "review", "tasks", "memories", "tags", "today", "7d"]
_SCOPE = (
    "scope accepts a folder or a whole note (projects, memories/gmail, projects/atlas.md), a period (today, 7d, "
    "2026-09, 2026-09-25, 2026-09-21..2026-09-25), a source period or its undated records (memories/gmail/7d, "
    "memories/gmail/undated), an identity or a tag (bf://NAME/tags/LABEL)"
)
_WHOLE = "scope takes a whole note without its #section; search the note, then read the section"
_MISSING = "page not found; read projects, concepts, actions or memories to browse the brain"
_BELOW = "(i.path=:prefix OR substr(i.path,1,length(:prefix)+1)=:prefix||'/')"
_LOUD = "i.source NOT IN (SELECT value FROM json_each(:low))"


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
    return timestamp(datetime.combine(day, time.min).astimezone().isoformat())


def period(value: str, now: datetime | None = None) -> Period | None:
    """Parse a local day, range of days, month, or trailing window; None for other values."""
    if links.PERIOD.fullmatch(value) is None:
        return None
    now = (now or datetime.now(UTC)).astimezone()
    try:
        if value in {"today", "yesterday"}:
            day = now.date() - timedelta(days=value == "yesterday")
        elif value[-1] in "hdw":
            if not int(value[:-1]):
                raise ValueError
            start = now - timedelta(hours=int(value[:-1]) * {"h": 1, "d": 24, "w": 168}[value[-1]])
            return Period(timestamp(start.isoformat()), timestamp(now.isoformat()))
        elif ".." in value:
            first, last = (date.fromisoformat(part) for part in value.split(".."))
            if last < first:
                raise ValueError
            span, following = last - first + timedelta(days=1), last + timedelta(days=1)
            return Period(
                _midnight(first),
                _midnight(following),
                f"{first - span}..{first - timedelta(days=1)}",
                f"{following}..{last + span}",
            )
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
        raise InputError(f"invalid period: {value}") from error


@dataclass(frozen=True)
class Route:
    """Syntax classification of a brain-relative path."""

    kind: Literal["period", "review", "tasks", "tags", "tag", "memories", "action", "note", "folder", "other"]
    name: str
    window: Period | None = None


def route(path: str, now: datetime | None = None) -> Route:
    name = path.rstrip("/")
    if found := period(name, now):
        return Route("period", name, found)
    top = name.split("/")[0]
    if name in {"review", "tasks"}:
        return Route(name, name)
    if top == "tags":
        return Route("tags" if name == "tags" else "tag", name)
    if top == "memories":
        return Route("memories", name)
    base, fragment = split_ref(path)
    if authored(base):
        return Route("note", path)
    action = base.rstrip("/")
    if re.fullmatch(r"actions/[^/#]+", action) and "/" not in fragment:
        return Route("action", action + "/ACTION.md" + (f"#{fragment}" if fragment else ""))
    return Route("folder", name) if top in AUTHORED else Route("other", path)


def scope(value: str, now: datetime | None = None) -> Scope:
    """Parse search bounds without opening a brain; invalid input names `scope`."""
    try:
        return _scope(value.strip(), now)
    except Error as error:
        raise InputError(str(error), "scope") from None


def _scope(value: str, now: datetime | None) -> Scope:
    if not value:
        return {}
    if (found := route(value, now).window) is not None:
        return {"since": found.since, "until": found.until}
    head, _, rest = value.partition(" ")
    if rest.strip() and (window := period(head, now)) is not None:
        inner = _scope(rest.strip(), now)
        if {"since", "until", "undated"} & inner.keys():
            raise Error("combine a period with a folder, note, source, identity or tag, not with another period")
        return {**inner, "since": window.since, "until": window.until}
    if index.identity(value) or value.lower().startswith("bf:"):
        tagged = links.tag(value)
        _syntax(value)
        parsed = links.parse(value)
        if not tagged and parsed and links.reserved(parsed.path):
            raise Error(_SCOPE)
        if parsed and parsed.fragment:
            raise Error(_WHOLE)
        return {"target": links.identity(value)}
    path = value.rstrip("/")
    try:
        parts = relative(path)
    except Error:
        raise Error(_SCOPE) from None
    if parts[0] not in (*AUTHORED, "memories"):
        raise Error(_SCOPE)
    if parts[0] == "memories" and len(parts) > 1 and "#" in parts[1]:
        raise Error(_SCOPE)
    note = split_ref(path)[0]
    if (authored(note) or re.fullmatch(r"actions/[^/#]+", note.rstrip("/"))) and note != path:
        raise Error(_WHOLE)
    if parts[0] == "memories" and len(parts) >= 3:
        if len(parts) == 3 and not parts[2].endswith(".json") and (found := period(parts[2], now)):
            return {"prefix": "/".join(parts[:2]), "since": found.since, "until": found.until}
        if len(parts) == 3 and parts[2] == "undated":
            return {"prefix": "/".join(parts[:2]), "undated": True}
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{64}\.json", parts[2]):
            raise Error(_SCOPE)
    return {"prefix": path}


def query(text: str, bounds: str = "", *, limit: int = 10, offset: int = 0) -> Query:
    """Validate search text and scope bounds without a brain."""
    window = scope(bounds)
    if index.identity(text):
        try:
            links.tag(links.identity(text.strip()))
        except Error as error:
            raise InputError(str(error), "query") from None
    # Check query words alone first so empty queries report `query` before `scope`.
    _request(Query, "query", text=text)
    return _request(Query, "scope", text=text, limit=limit, offset=offset, **window)


def bounds(value: str) -> Bounds:
    """Validate a scope without query text."""
    return _request(Bounds, "scope", **scope(value))


def _request[T: Bounds](model: type[T], argument: str, **values: object) -> T:
    try:
        return model.model_validate(values)
    except ValidationError as error:
        reasons = sorted({item["msg"].removeprefix("Value error, ") for item in error.errors(include_input=False)})
        raise InputError("; ".join(reasons), argument) from None


def sources(store: Store, connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Configured and indexed record sources with their indexed counts."""
    return {**{name: {"records": 0} for name in load(store).record_sources()}, **index.sources(connection)}


def low(store: Store) -> list[str]:
    """Low-priority record sources, resolved at query time without rebuilding."""
    return sorted(name for name, (_, job) in load(store).record_sources().items() if job.is_low_priority)


def _counts(store: Store, rows: list[tuple[str, int]], page: str) -> list[dict[str, object]]:
    quiet = set(low(store))
    return [
        {
            "source": source,
            "records": count,
            "page": f"memories/{source}/{page}",
            **({"priority": "low"} if source in quiet else {}),
        }
        for source, count in rows
    ]


def address(brain: str, item: Mapping[str, object]) -> str:
    """Portable BF address of a cached item."""
    ref = str(item["ref"])
    return links.address(brain, ref) if item.get("kind") == "record" else links.address(brain, *split_ref(ref))


@contextmanager
def _open(store: Store) -> Generator[tuple[str, sqlite3.Connection, dict[str, object]]]:
    """Load brain config and open its cache with staleness and problems."""
    try:
        name = load(store).name
        with index.database(store) as (connection, state):
            problems = index.problems(connection)
            extra: dict[str, object] = {
                **({"stale": [name]} if state != "ready" else {}),
                **({"problems": problems} if problems else {}),
            }
            yield name, connection, extra
    except sqlite3.DatabaseError as error:
        raise Error(CACHE_ERROR) from error


def absent(extra: Mapping[str, object], message: str = _MISSING) -> Error:
    """Return NotFoundError only when the cache answered cleanly without stale or problem items."""
    if extra.get("problems") or extra.get("stale"):
        return Error("not found in the readable evidence; run bf status before concluding it is absent")
    return NotFoundError(message)


def unique(problems: list[dict[str, object]]) -> list[dict[str, object]]:
    """Deduplicate problem dicts while preserving order."""
    return list({encode(problem): problem for problem in problems}.values())


def fitting(
    items: list[dict[str, object]], budget: int = BUDGET, shape: Callable[[object], object] = present
) -> list[dict[str, object]]:
    """Leading items fitting `budget`, keeping at least one so pagination always advances."""
    size = 0
    for count, item in enumerate(items):
        size += len(encode(shape(item)))
        if count and size > budget:
            return items[:count]
    return items


def _rows(
    connection: sqlite3.Connection, where: str, params: Mapping[str, object], order: str, limit: int
) -> list[dict[str, object]]:
    return list(index.listing(connection, where, params, order, limit, counted=False)[0])


def _paged(
    items: list[dict[str, object]],
    total: int,
    offset: int,
    extra: Mapping[str, object],
    *,
    reserved: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Fit one SQL page within the reply budget and add `next_offset` when rows remain."""
    space = BUDGET - (len(encode(present(dict(reserved)))) if reserved else 0)
    selected = fitting(items, space)
    return {
        "items": selected,
        "total": total,
        **extra,
        **({"next_offset": offset + len(selected)} if offset + len(selected) < total else {}),
    }


def _listing(
    store: Store,
    where: str,
    params: Mapping[str, object],
    order: str,
    limit: int,
    offset: int,
    *,
    counted: bool,
    kind: str,
    review: datetime | None = None,
) -> dict[str, object]:
    with _open(store) as (_name, connection, extra):
        rows, total = index.listing(connection, where, params, order, limit, offset)
        items = list(rows)
        if review:
            _review(connection, items, review)
        if counted:
            usage.note(store, "read", 1, kind=kind)
        return _paged(items, total, offset, extra)


def _review(connection: sqlite3.Connection, items: list[dict[str, object]], now: datetime) -> None:
    """Derive review reminders from edits, explicit deadlines, and newer linked evidence."""
    rows = {
        row["ref"]: row
        for row in connection.execute(
            "SELECT i.id,i.ref,i.stale_after,f.mtime FROM items i JOIN files f ON f.path=i.path "
            "WHERE i.ref IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted({str(item["ref"]) for item in items})),),
        )
    }
    stamp = timestamp(now.isoformat())
    reviewed: list[tuple[dict[str, object], int, list[str]]] = []
    edits: dict[int, str] = {}
    for item in items:
        row = rows.get(item["ref"])
        if (
            row is None
            or item.get("status") == "deprecated"
            or not entry_note(str(item["ref"]))
            or not (item.get("type") == "project" or row["stale_after"])
        ):
            continue
        reasons, modified, due = [], "", ""
        try:
            instant = datetime.fromtimestamp(row["mtime"] / 1_000_000_000, UTC)
            modified = timestamp(instant.isoformat())
            item["modified"] = modified
            if modified > stamp:
                reasons.append("future_modified")
            if not row["stale_after"]:
                due = timestamp((instant + timedelta(days=REVIEW_DAYS)).isoformat())
        except ValueError, OverflowError, OSError:
            reasons.append("unknown_modified")
        if row["stale_after"]:
            due = row["stale_after"]
        item["review_source"] = "stale_after" if row["stale_after"] else "modified"
        if due:
            review = datetime.fromisoformat(due)
            with suppress(OverflowError):
                review = review.astimezone()
            item["review_due"] = review.date().isoformat()
            if due <= stamp:
                reasons.append("due")
        if modified:
            edits[row["id"]] = modified
        reviewed.append((item, row["id"], reasons))
    newer = index.newer_links(connection, edits, stamp, NEWER)
    for item, item_id, reasons in reviewed:
        if item_id in newer:
            item["new_links"], item["newer"] = newer[item_id]
            reasons.append("newer_evidence")
        if reasons:
            item["review"], item["review_reasons"] = True, reasons


def _brief(item: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in item.items() if key != "excerpt"}


_CHANGED = ("ref", "kind", "title", "time", "date", "modified", "review")
_UPDATED = ("ref", "kind", "title", "time", "updated")


def _only(item: dict[str, object], keys: tuple[str, ...]) -> dict[str, object]:
    return {key: item[key] for key in keys if key in item}


def _alerts(
    store: Store, connection: sqlite3.Connection, now: datetime, extra: Mapping[str, object]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    issues = list(cast("list[dict[str, object]]", extra.get("problems", [])))
    try:
        # Use this connection; opening the cache again would wait behind a publish waiting on this read.
        return attention(store, now, index.sources(connection)), issues
    except Error, OSError, UnicodeError:
        return [], [*issues, {"error": "operational status is unavailable; run bf status"}]


def home(store: Store, now: datetime | None = None, *, counted: bool = True) -> dict[str, object]:
    """Home page: projects needing review, open draft actions, weekly changes, activity, and upcoming items."""
    now = now or datetime.now(UTC)
    stamp = timestamp(now.isoformat())
    day, week = (timestamp((now - timedelta(days=days)).isoformat()) for days in (1, 7))
    ahead = timestamp((now + timedelta(days=7)).isoformat())
    current = "i.status!='deprecated'"
    project = f"{index.ENTRY} AND i.type='project'"
    with _open(store) as (_name, connection, extra):
        projects = _rows(connection, f"{project} AND {current}", {}, "time DESC,i.ref DESC", -1)
        actions = _rows(connection, f"{index.ACTION} AND i.status='draft'", {}, "i.path DESC", 10)
        changed = _rows(
            connection,
            f"i.kind='note' AND {current} AND NOT ({project}) AND i.time!='' "
            f"AND ({index.TIME})>=:since AND ({index.TIME})<:until",
            {"since": week, "until": stamp},
            "time DESC,i.ref DESC",
            SECTION,
        )
        upcoming = _rows(
            connection,
            f"{index.WITHIN} AND {_LOUD} AND {current}",
            {"since": stamp, "until": ahead, "low": json.dumps(low(store))},
            "time,i.ref",
            SECTION,
        )
        _review(connection, [*projects, *actions, *changed, *upcoming], now)
        activity = _counts(store, index.activity(connection, day, stamp), "24h")
        alerts, issues = _alerts(store, connection, now, extra)
        if counted:
            usage.note(store, "read", 1, kind="home")
    reply: dict[str, object] = {
        "page": "",
        "projects": [],
        "projects_total": len(projects),
        "actions": actions,
        "changed": [_only(item, _CHANGED) for item in changed],
        "activity": activity,
        "upcoming": [_brief(item) for item in upcoming],
        "attention": alerts,
        "pages": BROWSE,
        **{k: v for k, v in extra.items() if k != "problems"},
        **({"problems": issues} if issues else {}),
    }
    reply["projects"] = fitting(
        sorted(projects, key=lambda item: not item.get("review")), BUDGET - len(encode(present(reply)))
    )
    return reply


_REVIEW_REASONS = frozenset({"due", "newer_evidence", "future_modified", "unknown_modified"})


def _blocked_by(connection: sqlite3.Connection, actions: list[dict[str, object]]) -> None:
    """Populate `blocked_by` on draft actions from unresolved draft `depends-on` targets."""
    if not actions:
        return
    refs = [str(item["ref"]) for item in actions]
    rows = connection.execute(
        "SELECT i.ref,e.target FROM edges e JOIN items i ON i.id=e.item "
        "WHERE e.relation='depends-on' AND i.ref IN (SELECT value FROM json_each(?))",
        (json.dumps(refs),),
    ).fetchall()
    if not rows:
        return
    owners = graph.owners(connection, {str(row["target"]) for row in rows})
    resolved_refs = sorted({ref for ref, _ in owners.values()})
    if not resolved_refs:
        return
    draft_actions = {
        str(row[0])
        for row in connection.execute(
            f"SELECT i.ref FROM items i WHERE {index.ACTION} AND i.status='draft' "  # noqa: S608 - fixed SQL
            "AND i.ref IN (SELECT value FROM json_each(?))",
            (json.dumps(resolved_refs),),
        )
    }
    by_action: dict[str, set[str]] = {}
    for row in rows:
        owner = owners.get(str(row["target"]))
        if owner and owner[0] in draft_actions and owner[0] != row["ref"]:
            by_action.setdefault(str(row["ref"]), set()).add(owner[0])
    for item in actions:
        if blockers := by_action.get(str(item["ref"])):
            item["blocked_by"] = sorted(blockers)[:10]


def review_page(
    store: Store, now: datetime | None = None, *, offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """Triage view of notes needing review, draft actions, and attention items."""
    now = now or datetime.now(UTC)
    with _open(store) as (_name, connection, extra):
        candidates = _rows(connection, f"{index.ENTRY} AND i.status!='deprecated'", {}, "time DESC,i.ref DESC", -1)
        _review(connection, candidates, now)
        due_items = [
            item
            for item in candidates
            if bool(set(cast("list[str]", item.get("review_reasons", ()))) & _REVIEW_REASONS)
        ]
        drafts = sorted(
            (dict(item) for item in candidates if action_note(str(item["ref"])) and item.get("status") == "draft"),
            key=lambda item: str(item["ref"]),
            reverse=True,
        )
        previewed = drafts[:SECTION]
        _blocked_by(connection, previewed)
        alerts, issues = _alerts(store, connection, now, extra)
        if counted:
            usage.note(store, "read", 1, kind="review")
    ordered = sorted(due_items, key=lambda item: (str(item.get("review_due", "9999-99-99")), str(item["ref"])))
    total = len(ordered)
    reply: dict[str, object] = {
        "page": "review",
        "items": [],
        "total": total,
        "actions": previewed,
        "actions_total": len(drafts),
        "attention": alerts,
        **{k: v for k, v in extra.items() if k != "problems"},
        **({"problems": issues} if issues else {}),
    }
    selected = fitting(ordered[offset : offset + PAGE], BUDGET - len(encode(present(reply))))
    reply["items"] = selected
    if offset + len(selected) < total:
        reply["next_offset"] = offset + len(selected)
    return reply


def timeline(store: Store, name: str, found: Period, *, offset: int = 0, counted: bool = True) -> dict[str, object]:
    """Items dated or modified within a period plus per-source activity counts."""
    with _open(store) as (_brain, connection, extra):
        params = {"since": found.since, "until": found.until, "low": json.dumps(low(store))}
        changed = [
            _only(item, _UPDATED)
            for item in _rows(
                connection,
                f"{index.MODIFIED} AND NOT ({index.PERIOD_WITHIN}) AND {_LOUD}",
                params,
                f"({index.UPDATED}) DESC,i.ref DESC",
                SECTION,
            )
        ]
        sources = _counts(store, index.activity(connection, found.since, found.until), name)
        rows, total = index.listing(
            connection,
            f"{index.PERIOD_WITHIN} AND {_LOUD}",
            params,
            f"({index.PERIOD_TIME}) DESC,i.ref DESC",
            PAGE,
            offset,
        )
        items = list(rows)
        if counted:
            usage.note(store, "read", 1, kind="period")
    reply = _paged(items, total, offset, extra, reserved={"changed": changed, "sources": sources})
    return {
        "page": name,
        "since": found.since,
        "until": found.until,
        **reply,
        "changed": changed,
        "sources": sources,
        **({"previous": found.previous, "next": found.next} if found.previous else {}),
    }


def _kind(store: Store, path: str) -> int:
    try:
        return stat.S_IFMT(store.mode(path))
    except FileAncestorError, OSError:
        return 0


def folder(
    store: Store, path: str, now: datetime | None = None, *, offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """Notes below an authored folder."""
    now = now or datetime.now(UTC)
    top = path.split("/")[0]
    where = index.ACTION if path == "actions" else f"i.kind='note' AND {_BELOW}"
    order = {"projects": "i.status='deprecated',time DESC,i.ref DESC", "concepts": "i.title,i.ref"}.get(
        top, "i.path DESC"
    )
    reply = _listing(store, where, {"prefix": path}, order, LISTING, offset, counted=counted, kind="folder", review=now)
    return {"page": path, **reply}


def memories(
    store: Store, parts: tuple[str, ...], now: datetime | None = None, *, offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """Source coverage overview or records for one source, period, `undated`, or record file."""
    now = now or datetime.now(UTC)
    if len(parts) == 1:
        if offset:
            raise InputError("offset applies to a source's records; read memories/SOURCE", "offset")
        with _open(store) as (_name, connection, extra):
            counts = index.sources(connection)
            health = source_health(store, counts, now=now)
            if counted:
                usage.note(store, "read", 1, kind="memories")
        return {
            "page": "memories",
            "sources": [
                {"source": name, "page": f"memories/{name}", **counts.get(name, {"records": 0}), **value}
                for name, value in health.items()
            ],
            **extra,
        }
    source = parts[1]
    if not re.fullmatch(NAME, source) or len(parts) > 3:
        raise NotFoundError(_MISSING)
    window = None
    if len(parts) == 2:
        where, params = "i.kind='record' AND i.source=:source", {"source": source}
        order, budget, kind = "i.time DESC,i.ref DESC", SECTION, "source"
    else:
        name = parts[2]
        window = None if name.endswith(".json") else period(name, now)
        if window:
            where = f"i.kind='record' AND i.source=:source AND {index.PERIOD_WITHIN}"
            params = {"source": source, "since": window.since, "until": window.until}
        elif name == "undated":
            where, params = "i.kind='record' AND i.source=:source AND i.time=''", {"source": source}
        elif re.fullmatch(r"[0-9a-f]{64}\.json", name):
            where, params = "i.kind='record' AND i.path=:path", {"path": f"memories/{source}/{name}"}
        else:
            raise NotFoundError(_MISSING)
        order = f"({index.PERIOD_TIME}) DESC,i.ref DESC" if window else "i.time DESC,i.ref DESC"
        budget = PAGE
        kind = "source-period" if window or name == "undated" else "record"
    with _open(store) as (_name, connection, extra):
        counts = sources(store, connection)
        if (count := counts.get(source)) is None:
            raise absent(extra, _MISSING + suggest(source, set(counts), prefix="memories/"))
        coverage = [{"source": source, **count, **source_health(store, {source: count}, now=now)[source]}]
        rows, total = index.listing(connection, where, params, order, budget, offset)
        items = list(rows)
        if counted:
            usage.note(store, "read", 1, kind=kind)
    reply = _paged(items, total, offset, extra, reserved={"sources": coverage})
    return {
        "page": "/".join(parts),
        "sources": coverage,
        **reply,
        **(
            {"previous": f"memories/{source}/{window.previous}", "next": f"memories/{source}/{window.next}"}
            if window and window.previous
            else {}
        ),
    }


def tasks(store: Store, *, offset: int = 0, counted: bool = True) -> dict[str, object]:
    """Open checkboxes in current entry notes with aggregate counts."""
    eligible = f"{index.ENTRY} AND i.status!='deprecated'"
    with _open(store) as (_name, connection, extra):
        summary = connection.execute(
            "SELECT coalesce(sum(t.done=0),0),coalesce(sum(t.done=1),0),count(DISTINCT t.item) "  # noqa: S608 - fixed SQL
            f"FROM tasks t JOIN items i ON i.id=t.item WHERE {eligible}"
        ).fetchone()
        rows = connection.execute(
            "SELECT i.ref,i.title,t.line,t.fragment,t.text FROM tasks t JOIN items i ON i.id=t.item "  # noqa: S608 - fixed SQL
            f"WHERE t.done=0 AND {eligible} ORDER BY i.ref,t.line LIMIT ? OFFSET ?",
            (PAGE, offset),
        ).fetchall()
        if counted:
            usage.note(store, "read", 1, kind="tasks")
    items = [
        {
            "ref": ref + ("#" + fragment if fragment else ""),
            "note": ref,
            "title": preview(title, index.TITLE),
            "line": line,
            "text": text,
        }
        for ref, title, line, fragment, text in rows
    ]
    counts = dict(zip(("open", "done", "notes"), summary, strict=True))
    return {
        "page": "tasks",
        **_paged(items, counts["open"], offset, extra, reserved={"summary": counts}),
        "summary": counts,
    }


def tags(store: Store, *, offset: int = 0, counted: bool = True) -> dict[str, object]:
    """Enumerate local tag identities with note counts."""
    with _open(store) as (_name, connection, extra):
        rows = connection.execute(
            "SELECT target,count(*) AS total FROM tags GROUP BY target ORDER BY target LIMIT ? OFFSET ?",
            (LISTING, offset),
        ).fetchall()
        total = int(connection.execute("SELECT count(DISTINCT target) FROM tags").fetchone()[0])
        if counted:
            usage.note(store, "read", 1, kind="tags")
    items = [{"tag": links.tag(target), "ref": target, "total": count} for target, count in rows]
    return {"page": "tags", **_paged(items, total, offset, extra)}


def _label(name: str) -> None:
    try:
        tag_name(name)
    except ValueError as error:
        # Both interfaces show this: MCP would rename a `bf read tags` hint to "the read tool tags".
        raise Error("invalid tag label; read the tags page and use one of its refs") from error


def tagged(store: Store, name: str, *, offset: int = 0, counted: bool = True) -> dict[str, object]:
    """List items declaring frontmatter tag `name`."""
    _label(name)
    with _open(store) as (brain, connection, extra):
        target = links.address(brain, f"tags/{name}")
        rows, total = index.listing(
            connection,
            "i.id IN (SELECT item FROM tags WHERE target=:target)",
            {"target": target},
            "time DESC,i.ref DESC",
            LISTING,
            offset,
        )
        items = list(rows)
        if counted:
            usage.note(store, "read", 1, kind="tag")
    return {"page": f"tags/{name}", **_paged(items, total, offset, extra)}


def page(
    store: Store, path: str, now: datetime | None = None, *, offset: int = 0, counted: bool = True
) -> dict[str, object] | None:
    """Return the page named by `path`, or None for a note, record, or identity."""
    if not path:
        if offset:
            raise InputError(
                "offset applies to listing pages; follow a home page's projects, actions or period link", "offset"
            )
        return home(store, now, counted=counted)
    target = route(path, now)
    match target.kind:
        case "period":
            return timeline(store, target.name, cast("Period", target.window), offset=offset, counted=counted)
        case "review":
            return review_page(store, now, offset=offset, counted=counted)
        case "tasks":
            return tasks(store, offset=offset, counted=counted)
        case "tags":
            return tags(store, offset=offset, counted=counted)
        case "tag":
            return tagged(store, target.name.removeprefix("tags/"), offset=offset, counted=counted)
        case "memories":
            return memories(store, relative(target.name), now, offset=offset, counted=counted)
    if name := folder_page(store, path):
        return folder(store, name, now, offset=offset, counted=counted)
    return None


def folder_page(store: Store, path: str) -> str | None:
    """Return the authored folder page named by `path`, or None when it is not a folder."""
    if (target := route(path)).kind != "folder":
        return None
    name = target.name
    relative(name)
    if name in AUTHORED:
        return name
    kind = _kind(store, name)
    if kind == stat.S_IFDIR:
        return name
    if kind not in {0, stat.S_IFREG}:
        raise Error(f"{name}: expected a directory; symlinks and special files are not followed")
    return None


RELATIONLESS = "rel lists the links to a note, record or identity, not a page"


def readable(ref: str, rel: str = "") -> str:
    """Validate read `ref` and optional `rel` syntax without opening a brain."""
    try:
        path, section = _syntax(ref)
    except Error as error:
        raise InputError(str(error), "ref") from None
    if rel and links.reserved(path.rstrip("/")):
        raise InputError(RELATIONLESS, "rel")
    if rel and section:
        raise InputError("rel lists the links to a whole note; read the note without its #section", "rel")
    return ref


_SECTIONLESS = "pages have no sections; read the page without its #fragment"
_TAGGED = "tag pages have no sections; for a label holding #, read the tags page and use its ref"


def _syntax(ref: str) -> tuple[str, bool]:
    """Validate read ref syntax and return `(path, has_section)`."""
    if len(ref) > 8192:
        raise Error("expected a reference of at most 8192 characters")
    try:
        ref.encode()
    except UnicodeEncodeError:
        raise Error("expected UTF-8 text") from None
    value = ref.strip()
    if parsed := links.parse(value):
        links.tag(parsed.identity)
        if parsed.fragment:
            if links.reserved(parsed.path.rstrip("/")):
                raise Error(_SECTIONLESS)
            return parsed.path, True
    path = parsed.path if parsed else value
    note, fragment = split_ref(path)
    bare = parsed is None and note != path and not fragment
    if (fragment or bare) and links.reserved(note.rstrip("/")):
        raise Error(_TAGGED if note.startswith("tags/") else _SECTIONLESS)
    target = route(path)
    if target.kind == "tag":
        _label(target.name.removeprefix("tags/"))
    elif target.kind in {"note", "action"}:
        if bare:
            raise Error("a ref ending in # names no section; read the whole note without it, or add the section")
        relative(note.rstrip("/") if target.kind == "action" else note)
        return path, bool(fragment)
    elif target.kind in {"memories", "folder"}:
        parts = relative(target.name)
        if parts[0] == "memories" and len(parts) == 3:
            period(parts[2])
    return path, False


_PREVIEWED = ("ref", "title", "time", "date", "kind", "source", "status", "type", "@type", "excerpt")
GLIMPSE = 160


def _glimpse(item: dict[str, object]) -> dict[str, object]:
    if isinstance(excerpt := item.get("excerpt"), str):
        item["excerpt"] = preview(excerpt, GLIMPSE)
    return item


def _backlinks(connection: sqlite3.Connection, targets: set[str], exclude: str = "") -> list[dict[str, object]]:
    groups = index.incoming(connection, targets, exclude=exclude, limit=PREVIEW)
    for group in groups:
        group["items"] = [
            _glimpse({key: item[key] for key in _PREVIEWED if key in item})
            for item in cast("list[dict[str, object]]", group["items"])
        ]
    return groups


def relation_page(
    store: Store, targets: set[str], relation: str, *, ref: str = "", offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """List items linking to expanded `targets` through `relation` (or narrower ones), newest first."""
    with _open(store) as (name, connection, extra):
        relations = [relation, *load(store).narrower(relation)]
        local = graph.local_refs(connection, targets, name)
        rows, total = index.linking(connection, local, relations, exclude=ref, limit=PAGE, offset=offset)
        items = [{k: v for k, v in row.items() if (k, v) != ("relation", relation)} for row in rows]
        if counted:
            usage.note(store, "read", 1, kind="relation")
    return {"page": "relation", "relation": relation, **_paged(items, total, offset, extra)}


def _graph(
    store: Store,
    targets: set[str],
    ref: str = "",
    now: datetime | None = None,
    *,
    counted: bool = False,
    kind: str = "",
) -> dict[str, object]:
    """Backlinks, typed claims, and optional whole-note reminders for expanded `targets`."""
    try:
        with _open(store) as (name, connection, extra):
            local = graph.local_refs(connection, targets, name)
            claims, truncated = graph.outgoing(connection, local)
            groups = _backlinks(connection, local, ref)
            reminders = _reminders(connection, ref, now) if now else {}
            if counted:
                usage.note(store, "read", 1, kind=kind)
    except (Error, OSError, UnicodeError) as error:
        return {"problems": [graph.problem(store, error)]}
    return {
        "backlinks": groups[: index.GROUPS],
        **({"backlinks_omitted": len(groups) - index.GROUPS} if len(groups) > index.GROUPS else {}),
        **({"claims": claims} if claims else {}),
        **({"claims_truncated": True} if truncated else {}),
        **reminders,
        **extra,
    }


_REMINDERS = ("tasks", "next", "review", "review_due", "review_source", "review_reasons", "new_links", "newer")


def _reminders(connection: sqlite3.Connection, ref: str, now: datetime) -> dict[str, object]:
    items = _rows(connection, "i.ref=:ref", {"ref": ref}, "i.ref", 1)
    _review(connection, items, now)
    if not items or "review_source" not in items[0]:
        return {}
    return {key: items[0][key] for key in _REMINDERS if key in items[0]}


def _summary(reply: Mapping[str, object]) -> dict[str, object]:
    try:
        path, data, markdown = cast("tuple[str, bytes, Markdown | None]", reply["_source"])
        found = note(path, markdown or data)
    except Error:
        return {}
    values = {
        "title": found.title,
        "type": found.knowledge.type,
        "status": found.knowledge.status,
        "date": found.knowledge.updated,
    }
    return {key: value for key, value in values.items() if value}


def context(store: Store, reply: dict[str, object]) -> dict[str, object]:
    """Backlinks, typed claims, reminders, and action attachments/projects for a read reply."""
    brain, ref = str(reply["brain"]), str(reply["ref"])
    record = "record" in reply
    path, fragment = ("", "") if record else split_ref(ref)
    if fragment:
        return _summary(reply)
    if not record and not addressable(path):
        return {"problems": [{"error": "backlinks are unavailable for a path this long; shorten it"}]}
    try:
        targets, problems = graph.expand(store, links.address(brain, ref if record else path))
    except Error:
        return {"problems": [{"file": path, "error": "backlinks are unavailable for this path; rename it"}]}
    result = _graph(store, targets, ref, None if record else datetime.now(UTC))
    issues: list[dict[str, object]] = [*problems, *cast("list[dict[str, object]]", result.pop("problems", []))]
    if not record and action_note(path):
        skipped: dict[str, tuple[int, int, int, int]] = {}
        try:
            result.update(_action(store, brain, path, str(reply["text"]), skipped))
        except Error, OSError, sqlite3.DatabaseError:
            issues.append({"error": "the action's files or projects are unavailable; run bf status"})
        reported = {problem.get("file") for problem in issues}
        unlisted = sorted(name for name in skipped if not editor_lock(name) and name not in reported)
        issues.extend({"file": name, "error": unread(store, name)} for name in unlisted[:LISTING])
    return {**result, **({"problems": unique(issues)} if issues else {})}


def _action(
    store: Store, brain: str, path: str, text: str, skipped: dict[str, tuple[int, int, int, int]]
) -> dict[str, object]:
    folder = path.rsplit("/", 1)[0]
    files = [
        name
        for name in store.files(folder, skipped=skipped)
        if name != path and not editor_lock(name) and not temporary(name)
    ][:LISTING]
    projects, names = set(), set()
    for target in note(path, text.encode()).targets:
        resolved = reference(path, target)
        parsed = links.parse(resolved) if resolved.lower().startswith("bf:") else None
        if parsed and parsed.brain != brain:
            continue
        name = split_ref(parsed.path if parsed else resolved)[0]
        if name.startswith("projects/") and authored(name):
            projects.add(name)
        elif parsed or index.identity(resolved):
            names.add(links.address(parsed.brain, parsed.path) if parsed else resolved)
    with index.database(store) as (connection, _state):
        linked = _rows(
            connection,
            "i.ref IN (SELECT value FROM json_each(:refs)) OR (substr(i.ref,1,9)='projects/' AND i.kind='note' AND "  # noqa: S608 - fixed SQL
            f"i.id IN (SELECT item FROM ({index.OWNED})))",
            {"refs": json.dumps(sorted(projects)), "names": json.dumps(sorted(names))},
            "i.ref",
            SECTION,
        )
    return {"files": files, "projects": linked}


def identity(store: Store, value: str, *, counted: bool = True) -> dict[str, object] | None:
    """Backlinks and typed claims for an unowned identity."""
    if not (index.identity(value) or value.lower().startswith("bf:")):
        return None
    value = links.identity(value)
    targets, problems = graph.expand(store, value)
    result = _graph(store, targets, counted=counted, kind="identity")
    issues: list[dict[str, object]] = [*problems, *cast("list[dict[str, object]]", result.pop("problems", []))]
    if not result.get("backlinks") and "claims" not in result:
        if issues or result.get("stale"):
            raise Error("identity read is incomplete; run bf status before concluding it is absent")
        return None
    return {"page": value, **result, **({"problems": unique(issues)} if issues else {})}
