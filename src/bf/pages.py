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

# The automatic reminder interval for project files; modification time is not evidence of verification.
REVIEW_DAYS = 14
# The refs a reminder names beside its count of newer linked evidence, newest first.
NEWER = 5
SECTION = 20
PAGE = 50
LISTING = 200
# The newest items previewed in each backlink group; its relation page lists them all.
PREVIEW = 5
# Serialized bytes of a page's items, or of an exact read's text page. Agents read a reply whole: a larger one
# ends early and continues at next_offset.
BUDGET = 32 << 10
BROWSE = ["projects", "concepts", "actions", "review", "tasks", "memories", "tags", "today", "7d"]
_SCOPE = (
    "scope accepts a folder or a whole note (projects, memories/gmail, projects/atlas.md), a period (today, 7d, "
    "2026-09, 2026-09-25, "
    "2026-09-21..2026-09-25), "
    "a source period or its undated records (memories/gmail/7d, memories/gmail/undated), an identity or a tag "
    "(bf://NAME/tags/LABEL)"
)
_WHOLE = "scope takes a whole note without its #section; search the note, then read the section"
_MISSING = "page not found; read projects, concepts, actions or memories to browse the brain"
_BELOW = "(i.path=:prefix OR substr(i.path,1,length(:prefix)+1)=:prefix||'/')"
# Items outside the :low sources; notes have no source. Period and home lists keep those sources as counts.
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
    # A naive datetime resolves the local offset at that date, including DST transitions.
    return timestamp(datetime.combine(day, time.min).astimezone().isoformat())


def period(value: str, now: datetime | None = None) -> Period | None:
    """A local day, inclusive range of days or month, or a trailing window ending now; None for another value.

    A range's previous and next pages are the ranges of the same length around it. links.PERIOD is the one period
    syntax, the page namespace links.reserved() keeps from entities.
    """
    if links.PERIOD.fullmatch(value) is None:
        return None
    now = (now or datetime.now(UTC)).astimezone()
    try:
        if value in {"today", "yesterday"}:
            day = now.date() - timedelta(days=value == "yesterday")
        elif value[-1] in "hdw":
            # An empty window holds nothing: `0d` would be a page no search scope could match.
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
    """What a brain-relative path names by its syntax alone: the one grammar of pages, folders, actions and notes.

    `name` drops a trailing slash, as shell completion writes `projects/` or `7d/`; an action folder names its
    ACTION.md. A note keeps its `#section`. `other` is a record, an identity or nothing; a `folder` below a root
    is a page only where it exists.
    """

    kind: Literal["period", "review", "tasks", "tags", "tag", "memories", "action", "note", "folder", "other"]
    name: str
    window: Period | None = None


def route(path: str, now: datetime | None = None) -> Route:
    name = path.rstrip("/")
    if found := period(name, now):
        return Route("period", name, found)
    top = name.split("/")[0]
    if name == "review":
        return Route("review", name)
    if name == "tasks":
        return Route("tasks", name)
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
    """Search bounds for one scope: a period, an explicit identity, or a brain folder or file.

    A scope is checked without a brain, so any failure is invalid input.
    """
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
        # A period, a space, then one other scope, such as `7d repo:github.com/owner/name`: no folder, identity or
        # tag starts with a period followed by a space, so this form widens what a scope accepts.
        inner = _scope(rest.strip(), now)
        if {"since", "until", "undated"} & inner.keys():
            raise Error("combine a period with a folder, note, source, identity or tag, not with another period")
        return {**inner, "since": window.since, "until": window.until}
    if index.identity(value) or value.lower().startswith("bf:"):
        # A tag scope takes no relation, unlike a read. Otherwise as `bf read` takes it: a page's #fragment would
        # scope nothing, a complete-looking empty reply.
        tagged = links.tag(value)
        _syntax(value)
        parsed = links.parse(value)
        if not tagged and parsed and links.reserved(parsed.path):
            # A page's address, such as bf://brain/projects or bf://brain/7d, is never a link target: it would
            # scope nothing. Its brain-relative form, such as projects or 7d, scopes the brain.
            raise Error(_SCOPE)
        if parsed and parsed.fragment:
            # A section's address, as a result's uri gives it, would scope only the links naming that section, an
            # empty-looking answer about the note: scope the whole note, as for its path.
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
        # A source has no sections, and no source name holds `#`: memories/gmail#x would fail as an unknown source.
        raise Error(_SCOPE)
    note = split_ref(path)[0]
    if (authored(note) or re.fullmatch(r"actions/[^/#]+", note.rstrip("/"))) and note != path:
        # Items carry a note's path, never a section's: a section ref, as search returns it, would scope nothing.
        raise Error(_WHOLE)
    if parts[0] == "memories" and len(parts) >= 3:
        # The source pages below memories/SOURCE, and nothing else: another segment would scope nothing.
        if len(parts) == 3 and not parts[2].endswith(".json") and (found := period(parts[2], now)):
            return {"prefix": "/".join(parts[:2]), "since": found.since, "until": found.until}
        if len(parts) == 3 and parts[2] == "undated":
            return {"prefix": "/".join(parts[:2]), "undated": True}
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{64}\.json", parts[2]):
            raise Error(_SCOPE)
    return {"prefix": path}


def query(text: str, bounds: str = "", *, limit: int = 10, offset: int = 0) -> Query:
    """A search checked without a brain, as the CLI, MCP and eval take it; invalid input names `query` or `scope`.

    An identity-shaped query must be a well-formed identity, like a read ref. Each interface bounds `limit` and
    `offset` itself, before calling.
    """
    window = scope(bounds)
    if index.identity(text):
        try:
            links.tag(links.identity(text.strip()))
        except Error as error:
            raise InputError(str(error), "query") from None
    # The words alone first: a query without words names the query, and any other reason the scope's bounds.
    _request(Query, "query", text=text)
    return _request(Query, "scope", text=text, limit=limit, offset=offset, **window)


def bounds(value: str) -> Bounds:
    """A scope checked without a brain, as search checks it, for commands that take a scope without words."""
    return _request(Bounds, "scope", **scope(value))


def _request[T: Bounds](model: type[T], argument: str, **values: object) -> T:
    try:
        return model.model_validate(values)
    except ValidationError as error:
        # pydantic's reasons without its field names, which are not the arguments a caller wrote.
        reasons = sorted({item["msg"].removeprefix("Value error, ") for item in error.errors(include_input=False)})
        raise InputError("; ".join(reasons), argument) from None


def sources(store: Store, connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Each source a brain indexes or configures via a `records` job, with its indexed record count."""
    config = load(store)
    return {**{name: {"records": 0} for name in config.record_sources()}, **index.sources(connection)}


def low(store: Store) -> list[str]:
    """Record sources whose job is low-priority (including prompt jobs), read at query time without rebuilding."""
    config = load(store)
    return sorted(name for name, (_, job) in config.record_sources().items() if job.is_low_priority)


def _counts(store: Store, rows: list[tuple[str, int]], page: str) -> list[dict[str, object]]:
    """Records per source with the page listing them; a low-priority source is marked, its items listed only there."""
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
    """The portable address of one cached item: a record id may contain `#`, a note ref names its section."""
    ref = str(item["ref"])
    return links.address(brain, ref) if item.get("kind") == "record" else links.address(brain, *split_ref(ref))


@contextmanager
def _open(store: Store) -> Generator[tuple[str, sqlite3.Connection, dict[str, object]]]:
    """Load the brain config and open its cache, collecting cache problems and staleness."""
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
    """A missing page or source is NotFoundError only when the brain answered from a complete cache."""
    if extra.get("problems") or extra.get("stale"):
        return Error("not found in the readable evidence; run bf status before concluding it is absent")
    return NotFoundError(message)


def unique(problems: list[dict[str, object]]) -> list[dict[str, object]]:
    """Each problem once: identity expansion and the page itself can meet the same unreadable cache."""
    return list({encode(problem): problem for problem in problems}.values())


def fitting(
    items: list[dict[str, object]], budget: int = BUDGET, shape: Callable[[object], object] = present
) -> list[dict[str, object]]:
    """The leading items within the reply budget; at least one, so a continuation always advances."""
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
    """Fit one SQL page within the reply budget and emit its continuation offset when rows remain."""
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
    """Derive reminders from local edits, explicit deadlines and newer linked evidence, without asserting a review.

    The same note may appear in several of `items`, such as a home section's: each copy gets the same signals.
    """
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
            if not row["stale_after"]:
                due = timestamp((instant + timedelta(days=REVIEW_DAYS)).isoformat())
        except ValueError, OverflowError, OSError:
            reasons.append("unknown_modified")
        if row["stale_after"]:
            due = row["stale_after"]
        item["review_source"] = "stale_after" if row["stale_after"] else "modified"
        if due:
            # A review is due on a local day: state that date, compare its first instant. A far-future
            # placeholder, such as 9999-12-31T23:00:00Z, can pass the last local day; it keeps its UTC date.
            review = datetime.fromisoformat(due)
            with suppress(OverflowError):
                review = review.astimezone()
            item["review_due"] = review.date().isoformat()
            if due <= stamp:
                reasons.append("due")
        if modified:
            edits[row["id"]] = modified
        reviewed.append((item, row["id"], reasons))
    # Linked evidence that happened or changed upstream since the edit; copying another note is not new evidence.
    newer = index.newer_links(connection, edits, stamp, NEWER)
    for item, item_id, reasons in reviewed:
        if item_id in newer:
            item["new_links"], item["newer"] = newer[item_id]
            reasons.append("newer_evidence")
        if reasons:
            item["review"] = True
            item["review_reasons"] = reasons


def _brief(item: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in item.items() if key != "excerpt"}


# A recently changed note on the home page: which note, when, and whether it needs review; its read holds the rest.
_CHANGED = ("ref", "kind", "title", "time", "date", "modified", "review")
# A record changed upstream on a period page: which record, when it happened and when it changed. Each continuation
# page repeats the list, so its read holds the rest and the page budget goes to the items.
_UPDATED = ("ref", "kind", "title", "time", "updated")


def _only(item: dict[str, object], keys: tuple[str, ...]) -> dict[str, object]:
    return {key: item[key] for key in keys if key in item}


def home(store: Store, now: datetime | None = None, *, counted: bool = True) -> dict[str, object]:
    """What needs attention now: projects needing review first, recent actions and notes, activity and the coming week.

    Every current project is reviewed; those that fit the page budget are listed, and `projects_total` counts all.
    `changed` names the other notes changed this week. A deprecated note no longer applies, so it leaves every
    section. The budget is measured on the reply as printed.
    """
    now = now or datetime.now(UTC)
    stamp = timestamp(now.isoformat())
    day, week = (timestamp((now - timedelta(days=days)).isoformat()) for days in (1, 7))
    ahead = timestamp((now + timedelta(days=7)).isoformat())
    current = "i.status!='deprecated'"
    project = f"{index.ENTRY} AND i.type='project'"
    with _open(store) as (_name, connection, extra):
        projects = _rows(connection, f"{project} AND {current}", {}, "time DESC,i.ref DESC", -1)
        actions = _rows(connection, f"{index.ACTION} AND i.status='draft'", {}, "i.path DESC", 10)
        # Projects carry their own review signals in `projects`.
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
        # A note listed in several sections carries the same signals in each.
        _review(connection, [*projects, *actions, *changed, *upcoming], now)
        activity = _counts(store, index.activity(connection, day, stamp), "24h")
        issues = list(cast("list[dict[str, object]]", extra.get("problems", [])))
        try:
            # From this connection: opening the cache again would wait behind a publish that waits for this read.
            alerts = attention(store, now, index.sources(connection))
        except Error, OSError, UnicodeError:
            alerts = []
            issues.append({"error": "operational status is unavailable; run bf status"})
        if counted:
            usage.note(store, "read", 1, kind="home")
    reply: dict[str, object] = {
        "page": "",
        "projects": [],
        "projects_total": len(projects),
        "actions": actions,
        # Orientation previews: titles and times; read an item for its text.
        "changed": [_only(item, _CHANGED) for item in changed],
        "activity": activity,
        "upcoming": [_brief(item) for item in upcoming],
        "attention": alerts,
        "pages": BROWSE,
        **({k: v for k, v in extra.items() if k != "problems"}),
        **({"problems": issues} if issues else {}),
    }
    # Projects needing review first, each group newest first, in what the bounded previews leave of the budget.
    reply["projects"] = fitting(
        sorted(projects, key=lambda item: not item.get("review")),
        BUDGET - len(encode(present(reply))),
    )
    return reply


_REVIEW_REASONS = frozenset({"due", "newer_evidence", "future_modified", "unknown_modified"})


def _blocked_by(connection: sqlite3.Connection, actions: list[dict[str, object]]) -> None:
    """Populate `blocked_by` on open draft actions from unresolved draft `depends-on` targets in the same brain."""
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
    """Computed triage view: paginated non-deprecated notes needing review, open draft actions and attention items."""
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
        issues = list(cast("list[dict[str, object]]", extra.get("problems", [])))
        try:
            alerts = attention(store, now, index.sources(connection))
        except Error, OSError, UnicodeError:
            alerts = []
            issues.append({"error": "operational status is unavailable; run bf status"})
        if counted:
            usage.note(store, "read", 1, kind="review")
    ordered = sorted(
        due_items,
        key=lambda item: (str(item.get("review_due", "9999-99-99")), str(item["ref"])),
    )
    total = len(ordered)
    reply: dict[str, object] = {
        "page": "review",
        "items": [],
        "total": total,
        "actions": previewed,
        "actions_total": len(drafts),
        "attention": alerts,
        **({k: v for k, v in extra.items() if k != "problems"}),
        **({"problems": issues} if issues else {}),
    }
    selected = fitting(ordered[offset : offset + PAGE], BUDGET - len(encode(present(reply))))
    reply["items"] = selected
    if offset + len(selected) < total:
        reply["next_offset"] = offset + len(selected)
    return reply


def timeline(store: Store, name: str, found: Period, *, offset: int = 0, counted: bool = True) -> dict[str, object]:
    """Items dated within a period, items modified within it, and each source's share of the period.

    Low-priority sources, such as a news feed, appear only in `sources`: their records would crowd out the rest.
    """
    with _open(store) as (_brain, connection, extra):
        params = {"since": found.since, "until": found.until, "low": json.dumps(low(store))}
        # Brief before the page measures what its summaries leave the items.
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
    """An entry's own file type, never a link target's; 0 when it is absent, unreadable or below a file. A link or
    special file above it raises, naming that entry."""
    try:
        return stat.S_IFMT(store.mode(path))
    except FileAncestorError, OSError:
        return 0


def folder(
    store: Store, path: str, now: datetime | None = None, *, offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """Notes below an authored folder, with a continuation through the complete order."""
    now = now or datetime.now(UTC)
    top = path.split("/")[0]
    where = index.ACTION if path == "actions" else f"i.kind='note' AND {_BELOW}"
    order = {
        "projects": "i.status='deprecated',time DESC,i.ref DESC",
        "concepts": "i.title,i.ref",
    }.get(top, "i.path DESC")
    reply = _listing(
        store,
        where,
        {"prefix": path},
        order,
        LISTING,
        offset,
        counted=counted,
        kind="folder",
        review=now,
    )
    return {"page": path, **reply}


def memories(
    store: Store, parts: tuple[str, ...], now: datetime | None = None, *, offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """Sources with their coverage, or one source's coverage with its records: all of them, those in a period, the
    undated ones or one record file's."""
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
        # A record's time is its stored instant: ordering by the column walks items_kind_source_time.
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
            # An unknown source is a missing page, never an empty answer.
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
    """Open checkboxes in current entry notes, with source lines and complete aggregate counts."""
    # Working attachments can quote source checkboxes: only authored entry notes join the task queue.
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
    """Enumerate local tag identities, with exact note counts and bounded pagination."""
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
    """List only explicit frontmatter membership, not prose, aliases or ordinary links to a tag."""
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
    store: Store,
    path: str,
    now: datetime | None = None,
    *,
    offset: int = 0,
    counted: bool = True,
) -> dict[str, object] | None:
    """The page a ref names, or None when it names a note, a record or an identity.

    An offset on a page that is never paged is invalid input, decided by the ref alone.
    """
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
    """The folder page an authored path names: a root, or an existing folder below one other than an action's,
    which reads as its ACTION.md. Notes and their sections (`path#heading`) are items, not folders; a symlink or
    special file where the folder would be, or above it, fails, naming it."""
    if (target := route(path)).kind != "folder":
        return None
    name = target.name
    relative(name)
    if name in AUTHORED:
        return name
    # Entity names such as projects/archive are logical: only an existing folder is a page.
    kind = _kind(store, name)
    if kind == stat.S_IFDIR:
        return name
    if kind not in {0, stat.S_IFREG}:
        # Skipped and reported like a path below it: not found would claim the folder absent.
        raise Error(f"{name}: expected a directory; symlinks and special files are not followed")
    return None


# A page has no relation page: by its syntax here, or once retrieve finds an existing folder below a root.
RELATIONLESS = "rel lists the links to a note, record or identity, not a page"


def readable(ref: str, rel: str = "") -> str:
    """Check a read's syntax without a brain, as `page` and `retrieve.read` apply it; a failure is invalid input.

    A malformed ref, or a relation page of a page or a #section, fails here; a well-formed ref can still name
    nothing in the brain.
    """
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
# A label may hold `#`, which only its address encodes: dropping the fragment of tags/C# would read the label C.
_TAGGED = "tag pages have no sections; for a label holding #, read the tags page and use its ref"


def _syntax(ref: str) -> tuple[str, bool]:
    """A read ref's path, once its syntax is checked, and whether it names a note's section."""
    if len(ref) > 8192:
        raise Error("expected a reference of at most 8192 characters")
    try:
        ref.encode()
    except UnicodeEncodeError:
        # Undecodable argument bytes arrive as lone surrogates, which no note path, record id or identity holds.
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
    # A trailing `#` names no section: like a scope, a read rejects it instead of echoing it as part of the ref, as
    # links.parse rejects a BF address's. A BF address's decoded path, like a record id, may itself end in `#`.
    bare = parsed is None and note != path and not fragment
    # A page, such as tasks or 7d, has no sections; a folder below a root may hold `#` in its name.
    if (fragment or bare) and links.reserved(note.rstrip("/")):
        raise Error(_TAGGED if note.startswith("tags/") else _SECTIONLESS)
    target = route(path)
    if target.kind == "tag":
        _label(target.name.removeprefix("tags/"))
    elif target.kind in {"note", "action"}:
        if bare:
            raise Error("a ref ending in # names no section; read the whole note without it, or add the section")
        # A note's section fragment is free text; only its path must be normalized.
        relative(note.rstrip("/") if target.kind == "action" else note)
        return path, bool(fragment)
    elif target.kind in {"memories", "folder"}:
        # Pages and folders check the whole path, including any `#`.
        parts = relative(target.name)
        if parts[0] == "memories" and len(parts) == 3:
            # A source's period page, such as memories/mail/7d.
            period(parts[2])
    return path, False


# A backlink preview names an item; its relation page and exact read hold the rest.
_PREVIEWED = ("ref", "title", "time", "date", "kind", "source", "status", "type", "@type", "excerpt")
# A backlink preview's excerpt: enough to see what each linking item says without reading it.
GLIMPSE = 160


def _glimpse(item: dict[str, object]) -> dict[str, object]:
    if isinstance(excerpt := item.get("excerpt"), str):
        # Cut at a word, as other previews are.
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
    store: Store,
    targets: set[str],
    relation: str,
    *,
    ref: str = "",
    offset: int = 0,
    counted: bool = True,
) -> dict[str, object]:
    """Every item linking to the expanded targets through one relation, newest first, 50 per page.

    `relation` is a RelationName, CITES or LINKS; the owning item is not its own backlink. Also lists the links of
    relations declaring this one `broader`; an item carries its `relation` only when it differs from the requested one.
    """
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
    """Backlinks and typed claims about the expanded targets; the owning item is not its own backlink.

    With `now`, also gives the whole note's reminders.
    """
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


# What a whole read adds from the note's listing entry when the note gets reminders.
_REMINDERS = ("tasks", "next", "review", "review_due", "review_source", "review_reasons", "new_links", "newer")


def _reminders(connection: sqlite3.Connection, ref: str, now: datetime) -> dict[str, object]:
    """A note's tasks and review signals as listings show them; nothing for a note without reminders."""
    items = _rows(connection, "i.ref=:ref", {"ref": ref}, "i.ref", 1)
    _review(connection, items, now)
    if not items or "review_source" not in items[0]:
        return {}
    return {key: items[0][key] for key in _REMINDERS if key in items[0]}


def _summary(reply: Mapping[str, object]) -> dict[str, object]:
    """A section's note title, type, status and date, from the file it was read from: its text omits them."""
    try:
        path, data, markdown = cast("tuple[str, bytes, Markdown | None]", reply["_source"])
        found = note(path, markdown or data)
    except Error:
        # The section still reads; only a note that search can index states its metadata.
        return {}
    values = {
        "title": found.title,
        "type": found.knowledge.type,
        "status": found.knowledge.status,
        "date": found.knowledge.updated,
    }
    return {key: value for key, value in values.items() if value}


def context(store: Store, reply: dict[str, object]) -> dict[str, object]:
    """Backlinks of a whole note or record and typed claims about it.

    A whole note that gets reminders adds its tasks and review signals, and an action's ACTION.md the action's files
    and the projects it links to. A section names its note's title, type, status and date instead.
    """
    brain = str(reply["brain"])
    ref = str(reply["ref"])
    record = "record" in reply
    path, fragment = ("", "") if record else split_ref(ref)
    if fragment:
        return _summary(reply)
    if not record and not addressable(path):
        # Record ids are bounded to fit a BF address; a note path is not, but the note itself still reads.
        return {"problems": [{"error": "backlinks are unavailable for a path this long; shorten it"}]}
    try:
        targets, problems = graph.expand(store, links.address(brain, ref if record else path))
    except Error:
        # Only an invalid address raises, such as a note path with a control character; the note itself reads.
        return {"problems": [{"file": path, "error": "backlinks are unavailable for this path; rename it"}]}
    reminded = None if record else datetime.now(UTC)
    result = _graph(store, targets, ref, reminded)
    issues: list[dict[str, object]] = [*problems, *cast("list[dict[str, object]]", result.pop("problems", []))]
    if not record and action_note(path):
        skipped: dict[str, tuple[int, int, int, int]] = {}
        try:
            result.update(_action(store, brain, path, str(reply["text"]), skipped))
        except Error, OSError, sqlite3.DatabaseError:
            issues.append({"error": "the action's files or projects are unavailable; run bf status"})
        # Never followed, so never offered as the action's files; the owner can replace or rename them. The cache
        # names each entry it scanned with its cause: only those it did not report, such as past its 200, are added,
        # with the cause bf validate names.
        reported = {problem.get("file") for problem in issues}
        unlisted = sorted(name for name in skipped if not editor_lock(name) and name not in reported)
        issues.extend({"file": name, "error": unread(store, name)} for name in unlisted[:LISTING])
    return {**result, **({"problems": unique(issues)} if issues else {})}


def _action(
    store: Store, brain: str, path: str, text: str, skipped: dict[str, tuple[int, int, int, int]]
) -> dict[str, object]:
    """The files kept with an action, and the projects its ACTION.md links to by path, address, entity or alias."""
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
            # An identity names its one local owner, as backlinks do; a name several items claim names none.
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
    """Everything that links to an identity without an owning note, and the claims made about it."""
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
