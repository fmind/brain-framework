"""Search across brains, and read pages, exact notes, sections, records and identities."""

from __future__ import annotations

import heapq
import re
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack
from itertools import islice, zip_longest
from typing import cast

from bf import graph, index, links, pages, records, usage
from bf.config import brain_name, load, related
from bf.health import source_health
from bf.markdown import authored, section, split_ref
from bf.models import MAX_NOTE, MAX_REPLY, NOTICE, Error, NotFoundError, Query, digest, encode
from bf.storage import FileAncestorError, Store, relative

# Exact replies longer than this many serialized characters return chunks of this length, from offset 0.
CHUNK = 64 << 10


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
            limit=bound if bound <= 2**63 - 1 else -1,
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
        merged = (item for row in zip_longest(*ranked) for item in row if item is not None)
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
    if coverage:
        reply["sources"] = coverage
    if "stale" in extra:
        reply["stale"] = extra["stale"]
    if problems:
        reply["problems"] = pages.unique(problems)
    return bounded(reply)


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


def read(stores: list[Store], ref: str = "", *, offset: int = 0, counted: bool = True) -> dict[str, object]:
    """Resolve a page, a note, a note section, a `source:id` record or an explicit identity.

    Without a ref, read returns the home page. Notes and records resolve in exactly one brain, which a
    bf:// address names, and carry their backlinks across the selected brains; pages and identities
    combine every brain.
    """
    if type(offset) is not int or not 0 <= offset <= 2**63 - 1:
        raise Error("offset must be a non-negative integer below 2**63")
    with index.session():
        return _resolve(stores, ref, offset=offset, counted=counted)


def _resolve(stores: list[Store], ref: str, *, offset: int, counted: bool) -> dict[str, object]:
    if len(ref) > 8192:
        raise Error("expected a reference of at most 8192 characters")
    stores, problems = related(stores)
    stores = _configured(stores, problems)
    # A qualified address resolves in its brain; backlinks and identities still span the whole selection.
    selected = stores
    ref = ref.strip()
    # links.parse reads bf://NAME/ as that brain's home, an empty path.
    parsed = links.parse(ref) if ref else None
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
            return bounded({**view, "notice": NOTICE, **_problems(problems, view)})
        if re.fullmatch(r"actions/[^/#]+", path.rstrip("/")) and not authored(path):
            # An action folder reads as its ACTION.md with the action's files and linked projects.
            path = path.rstrip("/") + "/ACTION.md"
            ref = links.address(parsed.brain, path) if parsed else path
    found = [(store, value) for store in selected if (value := _read(store, ref)) is not None]
    if not found:
        # An identity with no owner reads as its links; a typed link reads as its target identity.
        view = pages.identity(stores, parsed.identity if parsed else ref, counted=counted)
        if view is not None:
            if offset:
                raise Error("offset applies to listing pages or exact reads; use search for identity backlinks")
            return bounded({**view, "notice": NOTICE, **_problems(problems, view)})
        if problems:
            raise Error("read is incomplete; run bf status before concluding a reference is absent")
        raise NotFoundError("reference not found; use bf search to locate it, or bf read to browse pages")
    if len(found) > 1:
        raise Error("reference exists in several brains; use a brain-qualified bf:// address")
    store, value = found[0]
    extra = pages.context(stores, store, str(value["brain"]), value)
    reply = _exact_reply({**value, **extra, "notice": NOTICE, **_problems(problems, extra)}, offset)
    if counted:
        usage.note(store, "read", 1)
    return reply


def _exact_reply(value: dict[str, object], offset: int) -> dict[str, object]:
    """Replies above one chunk are lossless JSON chunks from offset 0; offsets count Unicode characters."""
    text = encode(value).decode()
    if len(text) <= CHUNK:
        if offset:
            raise Error("this exact reply is not chunked; read it without an offset")
        return value
    if offset >= len(text):
        raise Error("offset is beyond this exact reply; restart the read at offset 0")
    end = min(offset + CHUNK, len(text))
    return bounded(
        {
            "brain": value["brain"],
            "ref": value["ref"],
            "format": "json",
            "chunk": text[offset:end],
            "offset": offset,
            "total_characters": len(text),
            "sha256": digest(text.encode()),
            "notice": NOTICE,
            **({"problems": value["problems"]} if value.get("problems") else {}),
            **({"stale": value["stale"]} if value.get("stale") else {}),
            **({"next_offset": end} if end < len(text) else {}),
        }
    )


def _problems(scope: list[dict[str, object]], reply: dict[str, object]) -> dict[str, object]:
    combined = [*scope, *cast("list[dict[str, object]]", reply.get("problems", []))]
    return {"problems": pages.unique(combined)} if combined else {}


def _record(
    store: Store, name: str, ref: str, source: str, record_id: str, *, complete: bool = False
) -> dict[str, object] | None:
    found = records.find(store, source, record_id, complete=complete)
    if not found:
        return None
    return {
        "brain": name,
        "ref": ref,
        "path": found[0],
        "record": found[1].model_dump(exclude_defaults=True),
        "collection": source_health(store, [source])[source],
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
            data = store.read(path, MAX_NOTE)
        except FileNotFoundError, FileAncestorError:
            # A file where the note's folder would be means this brain cannot hold it; another brain may.
            return None
        try:
            text = section(path, data, fragment) if fragment else data.decode("utf-8")
        except UnicodeDecodeError:
            raise Error(f"{path}: note is not UTF-8") from None
        return {"brain": name, "ref": ref, "text": text}
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
