"""Search across brains, and read pages, exact notes, sections, records, identities and relation pages."""

from __future__ import annotations

import heapq
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, suppress
from datetime import UTC, datetime
from itertools import accumulate, islice, zip_longest
from typing import NoReturn, cast

from bf import graph, index, links, pages, records, usage
from bf.config import brain_name, load, related
from bf.health import source_health
from bf.markdown import authored, lines, parse, section, split_ref
from bf.models import (
    AUTHORED,
    CITES,
    LINKS,
    MAX_NOTE,
    MAX_OFFSET,
    MAX_REPLY,
    NOTICE,
    Error,
    NotFoundError,
    Query,
    addressable,
    digest,
    encode,
    suggest,
    timestamp,
)
from bf.storage import FileAncestorError, Store, relative

# What a later page of an exact read repeats beside its text: identity, digest, notice and completeness.
_TEXT = ("brain", "ref", "uri", "sha256", "modified", "notice", "problems", "stale")
# Freshness that keeps a searched source in a search's coverage even when none of its records returned.
_ATTENTION = {"overdue", "never"}


def bounded(value: dict[str, object], limit: int = MAX_REPLY) -> dict[str, object]:
    if len(encode(value)) > limit:
        raise Error("response exceeds its byte limit; narrow the request")
    return value


def search(stores: list[Store], query: Query, *, counted: bool = True) -> dict[str, object]:
    """Merge per-brain results: words alternate by each brain's rank; identities list owners, then newest links.

    An identity-shaped query is an identity search when a selected brain knows it; otherwise its words rank.
    """
    with index.session(), ExitStack() as stack:
        try:
            return _search(stores, query, stack, counted=counted)
        except sqlite3.DatabaseError as error:
            raise Error(pages.CACHE) from error


def _search(stores: list[Store], query: Query, stack: ExitStack, *, counted: bool) -> dict[str, object]:
    text = query.text.strip()
    shaped = index.identity(text)
    stores, problems = related(stores)
    # A missing or conflicting brain still counts as selected: the shape of a reply never depends on its absence.
    single = len(stores) == 1 and not problems
    identities, identity_problems = graph.expand(stores, text) if shaped else (set(), [])
    targets, target_problems = graph.expand(stores, query.target)
    problems.extend([*identity_problems, *target_problems])
    # No brain can place more than this many items up to the end of the requested window.
    bound = query.offset + query.limit + 1
    scoped = query.prefix.split("/")[1] if query.prefix.startswith("memories/") else ""
    # Coverage describes the sources searched, including those without matches; notes are not collected.
    collected = query.prefix.split("/")[0] in {"", "memories"}

    def build(store: Store, name: str, connection: sqlite3.Connection) -> dict[str, object]:
        local_targets = graph.local_refs(connection, targets)
        local_identities = graph.local_refs(connection, identities)
        exact = shaped and (links.tag(text) is not None or index.known(connection, local_identities | {text}))
        rows = index.search(
            connection,
            query,
            identities=local_identities,
            targets=local_targets,
            exact=exact,
            limit=bound,
            low=pages.low(store),
        )
        known = set(index.sources(connection)) | set(load(store).sensors)
        return {
            "store": store,
            "connection": connection,
            "exact": exact,
            "explain": (local_targets if query.target else local_identities) if exact or query.target else None,
            "rows": (pages.label(name, row) for row in rows),
            "sources": ({scoped} & known if scoped else known) if collected else set(),
        }

    parts, extra = pages.brains(stores, build, counted=False, stack=stack)
    problems.extend(cast("list[dict[str, object]]", extra.get("problems", [])))
    if not parts:
        raise Error("no unambiguous brain could be searched; check bf.yaml brain references")
    if scoped and not any(part["sources"] for _, part in parts):
        # Like the source page: a source no brain indexes or configures is unknown, never an empty answer.
        raise pages.absent({**extra, "problems": problems}, "unknown source; read memories to list sources")
    identity = any(part["exact"] for _, part in parts)
    # A brain that does not know a known identity ranked its words instead: that is not an identity answer.
    ranked = [cast("Iterator[dict[str, object]]", part["rows"]) for _, part in parts if part["exact"] or not identity]
    if identity:
        # Every local stream has the same owner priority and descending time/ref order.
        merged = heapq.merge(
            *ranked,
            key=lambda item: (cast("int", item.get("_rank", 0)), str(item.get("time", "")), str(item["ref"])),
            reverse=True,
        )
    else:
        # Interleave by rank: scores from independent corpora are not comparable.
        merged = (item for row in zip_longest(*ranked) for item in _tied(row))
    window = list(islice(islice(merged, query.offset, None), query.limit + 1))
    context = dict(parts)
    for item in window[: query.limit]:
        _finish(item, text, context[str(item["brain"])])
    selected = pages.fitting(window[: query.limit])
    reply: dict[str, object] = {"items": selected, "notice": NOTICE}
    if len(selected) < len(window):
        reply["next_offset"] = query.offset + len(selected)
    if shaped and not identity:
        # Nothing is, names or links to it: say so, since its items only share words with the query.
        reply["identity"] = "unknown"
    if not identity:
        # A misspelled word would otherwise match nothing silently; a term any selected brain holds is known.
        missing = [index.unmatched(cast("sqlite3.Connection", part["connection"]), text) for _, part in parts]
        if unmatched := [term for term in missing[0] if all(term in other for other in missing[1:])]:
            reply["unmatched"] = unmatched
    coverage = []
    for name, part in parts:
        store = cast("Store", part["store"])
        if counted:
            usage.note(store, "search", sum(item["brain"] == name for item in selected))
        if names := cast("set[str]", part["sources"]):
            try:
                coverage.extend(
                    {"brain": name, "source": source, **health}
                    for source, health in source_health(store, names).items()
                    if source in names
                )
            except Error, OSError, UnicodeError:
                problems.append({"brain": name, "error": "collection coverage is unavailable; run bf status"})
    reply.update(_coverage(coverage, selected, whole=not selected or query.prefix.split("/")[0] == "memories"))
    if "stale" in extra:
        reply["stale"] = extra["stale"]
    if problems:
        reply["problems"] = pages.unique(problems)
    if not single:
        # With several brains a plain ref can be ambiguous: name `also` records by address, like each item's uri.
        for item in selected:
            if also := cast("list[str] | None", item.get("also")):
                item["also"] = [links.address(str(item["brain"]), ref) for ref in also]
            if others := cast("list[str] | None", item.get("sections")):
                item["sections"] = [links.address(str(item["brain"]), *split_ref(ref)) for ref in others]
    return bounded(pages.local(reply) if single else reply)


def _tied(row: tuple[dict[str, object] | None, ...]) -> list[dict[str, object]]:
    """One rank of several brains, newest first, then by ref, like equal scores within one brain."""
    items = sorted((item for item in row if item is not None), key=lambda item: str(item["ref"]))
    return sorted(items, key=lambda item: str(item.get("time", "")), reverse=True)


def _coverage(coverage: list[dict[str, object]], items: list[dict[str, object]], *, whole: bool) -> dict[str, object]:
    """The sources of returned records and those needing attention; `sources_omitted` counts the other searched ones.

    An empty reply, or a search of collected evidence alone, keeps every searched source: coverage is its answer.
    """
    returned = {(item["brain"], item.get("source")) for item in items if item.get("kind") == "record"}
    kept = [
        entry
        for entry in coverage
        if whole
        or (entry["brain"], entry["source"]) in returned
        or entry.get("failed")
        or entry.get("freshness") in _ATTENTION
    ]
    omitted = len(coverage) - len(kept)
    return {**({"sources": kept} if kept else {}), **({"sources_omitted": omitted} if omitted else {})}


def _finish(item: dict[str, object], text: str, part: dict[str, object]) -> None:
    """Excerpts and relations cost far more than ranking: compute them only for the returned items."""
    connection = cast("sqlite3.Connection", part["connection"])
    item.pop("_rank", None)
    passage = item.pop("_passage", None)
    if passage is not None and (excerpt := index.excerpt(connection, text, cast("int", passage))):
        item["excerpt"] = excerpt
    if (targets := cast("set[str] | None", part["explain"])) is not None:
        claims, truncated = graph.explanations(connection, str(item["ref"]), targets)
        if claims:
            item["relations"] = claims
        if truncated:
            item["relations_truncated"] = True


def _configured(stores: list[Store], problems: list[dict[str, object]]) -> list[Store]:
    """Skip a brain whose bf.yaml does not load, unless it is the only one: the others still answer."""
    if len(stores) == 1:
        return stores
    result = []
    for store in stores:
        try:
            load(store)
        except Error, OSError, UnicodeError:
            problems.append(
                {"brain": brain_name(store), "error": "bf.yaml is invalid or inaccessible; run bf validate"}
            )
            continue
        result.append(store)
    return result


def _brain(stores: list[Store], name: str, problems: list[dict[str, object]]) -> list[Store]:
    chosen = [store for store in stores if load(store).name == name]
    if not chosen:
        # A declared, registered or ambiguous name is not unknown: its problem explains why it is missing.
        if any(str(problem.get("error", "")).startswith(f"brains.{name}:") for problem in problems):
            raise Error(f"referenced brain {name} is unavailable; check brains.{name} in bf.yaml")
        if any(problem.get("brain") == name for problem in problems):
            raise Error(f"brain {name} is ambiguous or unavailable; run bf status")
        raise Error(f"unknown brain {name}; select it or a brain whose bf.yaml references it")
    return chosen


# Every claim in a stable order of its exported fields; `time` is the event time of the item asserting it.
_EDGES = f"""SELECT e.subject,coalesce(nullif(e.relation,''),:links) AS relation,e.target,e.origin,{index.TIME} AS time,{index.DATE} AS date,
  i.observed FROM edges e JOIN items i ON i.id=e.item ORDER BY 1,2,3,4"""  # noqa: S608 - fixed SQL


# Every note or record declaring names beyond its own address: an entity, aliases, or a record's provider aliases.
_IDENTITIES = """SELECT i.ref,i.kind,i.type,i.source,i.status,json_group_array(n.name) AS names FROM names n
  JOIN items i ON i.id=n.item GROUP BY i.id HAVING count(*)>1 ORDER BY i.ref"""


def edges(stores: list[Store], stack: ExitStack) -> tuple[Iterator[dict[str, object]], dict[str, object]]:
    """Stream the claims of the selected brains and their direct references from their caches, like search."""
    return _export(
        stores,
        stack,
        lambda name, connection: (_edge(name, row) for row in connection.execute(_EDGES, {"links": LINKS})),
    )


def identities(stores: list[Store], stack: ExitStack) -> tuple[Iterator[dict[str, object]], dict[str, object]]:
    """Stream each note or record with every identity it answers to, so duplicates of one subject stand out."""
    return _export(
        stores, stack, lambda name, connection: (_identity(name, row) for row in connection.execute(_IDENTITIES))
    )


def _export(
    stores: list[Store],
    stack: ExitStack,
    rows: Callable[[str, sqlite3.Connection], Iterator[dict[str, object]]],
) -> tuple[Iterator[dict[str, object]], dict[str, object]]:
    """Stream rows of the selected brains and their direct references from their caches, like search.

    Returns the stream, open while `stack` is, and the selection's `problems` and `stale` brains. The problems
    of a brain are known before its first row: skipped files and unavailable brains make an export incomplete.
    """
    stores, problems = related(stores)

    def build(_store: Store, name: str, connection: sqlite3.Connection) -> dict[str, object]:
        return {"rows": rows(name, connection)}

    parts, extra = pages.brains(stores, build, counted=False, stack=stack)
    problems.extend(cast("list[dict[str, object]]", extra.get("problems", [])))

    def stream() -> Iterator[dict[str, object]]:
        try:
            for _, part in parts:
                yield from cast("Iterator[dict[str, object]]", part["rows"])
        except sqlite3.DatabaseError as error:
            raise Error(pages.CACHE) from error

    return stream(), {**({"stale": extra["stale"]} if "stale" in extra else {}), **_problems(problems, {})}


def _edge(brain: str, row: sqlite3.Row) -> dict[str, object]:
    """One claim; an undated origin has no `time`, and only a collected record has an `observed` time."""
    return {"brain": brain, **{key: value for key, value in dict(row).items() if value != ""}}


def _identity(brain: str, row: sqlite3.Row) -> dict[str, object]:
    """One note or record: a note's type and status, or a record's source, and its sorted names."""
    value = {key: row[key] for key in ("ref", "kind", "status") if row[key]}
    value.update({"type": row["type"]} if row["kind"] == "note" else {"source": row["source"]})
    return {"brain": brain, **value, "names": sorted(json.loads(row["names"]))}


class RelationError(Error):
    """A relation page names a relation that no selected brain declares: invalid input, not a failed read."""


def _declared(stores: list[Store]) -> set[str]:
    """The relations a link may name: the built-in `cites` and each relation a selected brain declares."""
    return {CITES} | {name for store in stores for name, field in load(store).ontology.items() if field.relation}


def relation(stores: list[Store], value: str) -> str:
    """A relation page's relation: LINKS, CITES or a relation a selected brain declares; others are invalid input."""
    stores, problems = related(stores)
    return _role(_configured(stores, problems), value)


def _role(stores: list[Store], value: str) -> str:
    declared = sorted(_declared(stores) - {CITES})
    if value not in {LINKS, CITES, *declared}:
        # Name what is valid, never the rejected value, and bound the list like other replies' previews.
        shown = [LINKS, CITES, *declared[:20], *(["…"] if len(declared) > 20 else [])]
        raise RelationError(f"undeclared relation; use {', '.join(shown)}")
    return value


def read(
    stores: list[Store], ref: str = "", *, rel: str = "", offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """Resolve a page, a note, a note section, a `source:id` record or an explicit identity.

    Without a ref, read returns the home page. Notes and records resolve in exactly one brain, which a
    bf:// address names, and carry their backlinks across the selected brains; pages and identities
    combine every brain. With `rel`, read lists every item linking to a note, record or identity through
    that relationship, or through untyped links with LINKS.
    """
    if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
        raise Error(f"offset must be an integer from 0 to {MAX_OFFSET}")
    with index.session():
        return _resolve(stores, ref, rel=rel, offset=offset, counted=counted)


def _resolve(stores: list[Store], ref: str, *, rel: str, offset: int, counted: bool) -> dict[str, object]:
    if len(ref) > 8192:
        raise Error("expected a reference of at most 8192 characters")
    stores, problems = related(stores)
    single = len(stores) == 1 and not problems
    stores = _configured(stores, problems)
    ref = ref.strip()
    # links.parse reads bf://NAME/ as that brain's home, an empty path.
    parsed = links.parse(ref) if ref else None
    reply = (
        _related(stores, ref, parsed, _role(stores, rel), problems, offset=offset, counted=counted)
        if rel
        else _lookup(stores, ref, parsed, problems, offset=offset, counted=counted)
    )
    if parsed and parsed.relation and parsed.relation not in _declared(stores):
        # A read follows a link's target whatever its role; an undeclared role is the link's problem, not the read's.
        issue: dict[str, object] = {"error": f"undeclared relation {parsed.relation}; declare it in bf.yaml fields"}
        reply["problems"] = pages.unique([*cast("list[dict[str, object]]", reply.get("problems", [])), issue])
    return bounded(pages.local(reply) if single else reply)


def _lookup(
    stores: list[Store],
    ref: str,
    parsed: links.Address | None,
    problems: list[dict[str, object]],
    *,
    offset: int,
    counted: bool,
) -> dict[str, object]:
    # A qualified address resolves in its brain; backlinks and identities still span the whole selection.
    selected = stores
    if parsed:
        # Reads follow a link's target; its relationship does not change the tag page.
        links.tag(parsed.identity)
        selected = _brain(selected, parsed.brain, problems)
    path = parsed.path if parsed else ref
    if not (parsed and parsed.fragment):
        try:
            view = pages.page(selected, path, offset=offset, counted=counted)
        except NotFoundError as error:
            if problems:
                raise Error("read is incomplete; run bf status before concluding a reference is absent") from error
            raise
        if view is not None:
            return {**view, "notice": NOTICE, **_problems(problems, view)}
        ref = _action(ref, parsed, path)
    found = [(store, value) for store in selected if (value := _read(store, ref)) is not None]
    if not found:
        # An identity with no owner reads as its links; a typed link reads as its target identity.
        view = pages.identity(stores, parsed.identity if parsed else ref, counted=counted)
        if view is not None:
            if offset:
                raise Error("offset applies to listing pages, exact reads and relation pages; page a relation with rel")
            return {**view, "notice": NOTICE, **_problems(problems, view)}
        _absent(problems, ref, selected)
    if len(found) > 1:
        raise Error("reference exists in several brains; use a brain-qualified bf:// address")
    store, value = found[0]
    extra = pages.context(stores, store, str(value["brain"]), value)
    stale = [*cast("list[str]", value.get("stale", [])), *cast("list[str]", extra.get("stale", []))]
    marked = {"stale": list(dict.fromkeys(stale))} if stale else {}
    reply = _exact_reply({**value, **extra, **marked, "notice": NOTICE, **_problems(problems, extra)}, offset)
    if counted:
        usage.note(store, "read", 1)
    return reply


def _action(ref: str, parsed: links.Address | None, path: str) -> str:
    """An action folder reads as its ACTION.md with the action's files and linked projects."""
    if re.fullmatch(r"actions/[^/#]+", path.rstrip("/")) and not authored(path):
        path = path.rstrip("/") + "/ACTION.md"
        return links.address(parsed.brain, path) if parsed else path
    return ref


def _absent(problems: list[dict[str, object]], ref: str = "", stores: list[Store] | None = None) -> NoReturn:
    if problems:
        raise Error("read is incomplete; run bf status before concluding a reference is absent")
    hint = ""
    if ref and ":" not in ref.split("/")[0] and stores:
        # A mistyped page or note path: suggest what exists, from the pages and each brain's authored files.
        names = {"tasks", "tags", "memories", "today", "7d", *AUTHORED}
        for store in stores:
            with suppress(Error, OSError):
                names.update(name for directory in AUTHORED for name in store.files(directory) if authored(name))
        hint = suggest(ref.partition("#")[0], names)
    raise NotFoundError(f"reference not found; use bf search to locate it, or bf read to browse pages{hint}")


def _related(
    stores: list[Store],
    ref: str,
    parsed: links.Address | None,
    role: str,
    problems: list[dict[str, object]],
    *,
    offset: int,
    counted: bool,
) -> dict[str, object]:
    """The items linking to a whole note, record or identity through one relation, as a paged listing."""
    selected = _brain(stores, parsed.brain, problems) if parsed else stores
    path = parsed.path if parsed else ref
    if links.reserved(path):
        raise Error("rel lists the links to a note, record or identity, not a page")
    if parsed.fragment if parsed else authored(split_ref(path)[0]) and split_ref(path)[1]:
        raise Error("rel lists the links to a whole note; read the note without its #section")
    found = [(store, value) for store in selected if (value := _read(store, _action(ref, parsed, path))) is not None]
    if len(found) > 1:
        raise Error("reference exists in several brains; use a brain-qualified bf:// address")
    if found:
        owner, value = found[0]
        owned = str(value["ref"])
        subject = owned if "record" in value else split_ref(owned)[0]
        if not addressable(subject):
            raise Error("links are unavailable for a path this long; shorten it")
        try:
            targets, issues = graph.expand(stores, links.address(str(value["brain"]), subject))
        except Error:
            raise Error("links are unavailable for this path; rename it") from None
        view = pages.role(stores, targets, role, owner=owner, ref=owned, offset=offset, counted=counted)
    else:
        identity = parsed.identity if parsed else ref
        if not index.identity(identity):
            _absent(problems)
        targets, issues = graph.expand(stores, identity)
        view = pages.role(stores, targets, role, offset=offset, counted=counted)
        # Like an identity read, an identity nothing owns, links to or makes claims about is not found.
        if not view["total"] and pages.identity(stores, identity, counted=False) is None:
            _absent([*problems, *issues])
    return {**view, "ref": ref, "notice": NOTICE, **_problems([*problems, *issues], view)}


# The text bytes a large note's first page returns beside its outline and graph context.
OPENING = 4096


def _exact_reply(value: dict[str, object], offset: int) -> dict[str, object]:
    """A reply above the page budget returns its note or record text in pages; offsets count Unicode characters.

    The first page carries everything else, with the note's outline and only its opening text; later pages carry
    only their text slice.
    """
    source = cast("tuple[str, bytes] | None", value.pop("_source", None))
    record = cast("dict[str, object] | None", value.get("record"))
    text = str(record.get("text", "")) if record is not None else str(value["text"])
    if len(encode(value)) <= pages.BUDGET:
        if offset:
            raise Error("this exact reply is not paged; read it without an offset")
        return value
    if offset and offset >= len(text):
        raise Error("offset is beyond this exact text; restart the read at offset 0")
    if offset:
        frame = {key: value[key] for key in _TEXT if key in value}
        if record is not None:
            frame["record"] = {"text": ""}
    else:
        frame = {**value, **(_outline(value, *source) if source else {})}
        if record is not None:
            frame["record"] = {**record, "text": ""}
    frame |= {"offset": offset, "total_characters": len(text), "next_offset": len(text)}
    if record is None:
        frame["text"] = ""
    # A first page with a large context still returns some text, so every continuation advances. A large note's
    # first page returns only its opening: its outline names the sections to read by ref instead of paging them all.
    room = max(pages.BUDGET - len(encode(frame)), pages.BUDGET // 4)
    if not offset and record is None and frame.get("outline"):
        room = min(room, OPENING)
    end = _cut(text, offset, room)
    if end == len(text):
        del frame["next_offset"]
    else:
        frame["next_offset"] = end
    piece = text[offset:end]
    if record is None:
        frame["text"] = piece
    else:
        # Like a whole record, a record without text has no `text` field.
        fields = {k: v for k, v in cast("dict[str, object]", frame["record"]).items() if k != "text"}
        frame["record"] = {**fields, "text": piece} if piece else fields
    return frame


def _cut(text: str, start: int, room: int) -> int:
    """The end of the longest slice from `start` whose serialized form fits `room` bytes, preferring a line end
    in the slice's second half, so a page rarely splits a line."""
    end = min(len(text), start + room)
    while end > start + 1 and (size := len(encode(text[start:end]))) > room:
        end = start + max(1, (end - start) * room // size)
    if end < len(text) and (newline := text.rfind("\n", start + (end - start) // 2, end)) >= 0:
        end = newline + 1
    return end


def _outline(value: dict[str, object], path: str, data: bytes) -> dict[str, object]:
    """The sections within the returned text, each with its ref and length, up to 200."""
    try:
        markdown = parse(path, data)
    except Error:
        # A note that search skips, such as one with a merge conflict marker, still reads as text.
        return {}
    # A section ends at the next heading of the same or a higher level, as `section` reads it.
    headings = markdown.headings
    offsets = [0, *accumulate(map(len, lines(markdown.text)))]
    ends = [len(offsets) - 1] * len(headings)
    opened: list[int] = []
    for number, heading in enumerate(headings):
        while opened and headings[opened[-1]].level >= heading.level:
            ends[opened.pop()] = heading.line
        opened.append(number)
    fragment = split_ref(str(value["ref"]))[1]
    start, stop = next(
        ((h.line, end) for h, end in zip(headings, ends, strict=True) if h.slug == fragment), (0, len(offsets) - 1)
    )
    entries = [
        {"ref": f"{path}#{h.slug}", "title": h.title, "characters": offsets[end] - offsets[h.line]}
        for h, end in zip(headings, ends, strict=True)
        if start <= h.line < stop
    ]
    return {
        "outline": entries[: pages.LISTING],
        **({"outline_truncated": True} if len(entries) > pages.LISTING else {}),
    }


def _problems(scope: list[dict[str, object]], reply: dict[str, object]) -> dict[str, object]:
    combined = [*scope, *cast("list[dict[str, object]]", reply.get("problems", []))]
    return {"problems": pages.unique(combined)} if combined else {}


def _stamp(data: bytes, nanoseconds: int) -> dict[str, object]:
    """A file's digest and modification instant; an unrepresentable time is left out."""
    try:
        modified = {"modified": timestamp(datetime.fromtimestamp(nanoseconds / 1_000_000_000, UTC).isoformat())}
    except ValueError, OverflowError, OSError:
        modified = {}
    return {"sha256": digest(data), **modified}


def _record(
    store: Store, name: str, ref: str, source: str, record_id: str, *, complete: bool = False
) -> dict[str, object] | None:
    found = records.find(store, source, record_id, complete=complete)
    if not found:
        return None
    path, record, data, nanoseconds, busy = found
    return {
        "brain": name,
        "ref": ref,
        "path": path,
        "record": record.model_dump(exclude_defaults=True),
        "collection": source_health(store, [source])[source],
        **_stamp(data, nanoseconds),
        # Read whole while a writer held the brain: the record may be changing, like a busy brain's cache.
        **({"stale": [name]} if busy else {}),
    }


def _read(store: Store, ref: str) -> dict[str, object] | None:
    name = load(store).name
    if parsed := links.parse(ref):
        if parsed.brain != name:
            return None
        source, separator, record_id = parsed.path.partition(":")
        # BF paths address authored files, records, or explicitly owned entity aliases. A first path segment
        # with ':' names a record, as validate and backlinks read it, never another scheme's alias.
        if authored(parsed.path):
            value = _read(store, parsed.path)
        elif separator and "/" not in source:
            value = _record(store, name, parsed.path, source, record_id)
        else:
            with index.database(store) as (connection, _state):
                owners = connection.execute(
                    "SELECT i.ref FROM names n JOIN items i ON n.item=i.id WHERE n.name=? LIMIT 2",
                    (links.address(name, parsed.path),),
                ).fetchall()
            if len(owners) > 1:
                raise Error("ambiguous identity; read an exact file ref")
            value = _read(store, owners[0][0]) if owners else None
        if value and parsed.fragment:
            if "text" not in value:
                raise Error("fragments select Markdown sections, not record fields")
            value = {
                **value,
                "text": section(str(value["ref"]), str(value["text"]).encode(), parsed.fragment),
                "ref": str(value["ref"]) + "#" + parsed.fragment,
            }
        return {**value, "uri": parsed.identity} if value else None
    path, fragment = split_ref(ref)
    if authored(path):
        relative(path)
        try:
            data, nanoseconds = store.stamped(path, MAX_NOTE)
        except FileNotFoundError, FileAncestorError:
            # A file where the note's folder would be means this brain cannot hold it; another brain may.
            return None
        try:
            text = section(path, data, fragment) if fragment else data.decode("utf-8")
        except UnicodeDecodeError:
            raise Error(f"{path}: note is not UTF-8") from None
        # The digest covers the whole file, even for a section: a writer compares it before replacing the file.
        return {"brain": name, "ref": ref, "text": text, **_stamp(data, nanoseconds), "_source": (path, data)}
    aliases, cache_error, complete = [], None, False
    source, separator, record_id = ref.partition(":")
    try:
        with index.database(store) as (connection, state):
            aliases = connection.execute(
                "SELECT i.ref FROM names n JOIN items i ON i.id=n.item WHERE n.name=? ORDER BY i.ref LIMIT 2",
                (ref,),
            ).fetchall()
            # A ready cache has parsed every file of the source: without a problem there, a missing
            # record file proves absence, so the read need not parse the whole source again.
            complete = (
                state == "ready"
                and not connection.execute(
                    "SELECT 1 FROM files WHERE path>? AND path<? AND error!='' LIMIT 1",
                    (f"memories/{source}/", f"memories/{source}0"),
                ).fetchone()
            )
    except (Error, OSError, sqlite3.DatabaseError) as error:
        # An exact source:id still has a file recovery path when its disposable cache is unavailable.
        cache_error = error
    if separator and (value := _record(store, name, ref, source, record_id, complete=complete)):
        return value
    if cache_error:
        raise Error("the search cache is unavailable; run bf build to resolve identities") from cache_error
    if len(aliases) > 1:
        raise Error("ambiguous identity; use bf search and read an exact ref")
    return _read(store, aliases[0]["ref"]) if aliases and aliases[0]["ref"] != ref else None
