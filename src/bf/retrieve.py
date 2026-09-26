"""Search across brains, and read pages, exact notes, sections, records and identities."""

from __future__ import annotations

import heapq
import re
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, suppress
from itertools import islice, zip_longest
from typing import cast

from bf import graph, index, links, pages, records, usage
from bf.config import load, related
from bf.health import source_health
from bf.markdown import authored, section, split_ref
from bf.models import MAX_NOTE, MAX_REPLY, NOTICE, Error, NotFoundError, Query, digest, encode
from bf.storage import Store, relative


def bounded(value: dict[str, object], limit: int = MAX_REPLY) -> dict[str, object]:
    if len(encode(value)) > limit:
        raise Error("response exceeds its byte limit; narrow the request")
    return value


def search(stores: list[Store], query: Query, *, counted: bool = True) -> dict[str, object]:
    """Merge per-brain results: relevance keeps each brain's order, identities interleave by time.

    An identity-shaped query is an identity search when a selected brain knows it; otherwise its words rank.
    """
    with index.session(), ExitStack() as stack:
        try:
            return _search(stores, query, stack, counted=counted)
        except sqlite3.DatabaseError as error:
            raise Error("the search cache is unavailable; run bf build") from error


def _search(stores: list[Store], query: Query, stack: ExitStack, *, counted: bool) -> dict[str, object]:
    stores, scope_problems = related(stores)
    results: list[tuple[bool, Iterator[dict[str, object]]]] = []
    stale = []
    brains = []
    problems: list[dict[str, object]] = list(scope_problems)
    last_error: Exception | None = None
    shaped = index.identity(query.text)
    identities, identity_problems = graph.expand(stores, query.text.strip()) if shaped else (set(), [])
    targets, target_problems = graph.expand(stores, query.target)
    problems.extend([*identity_problems, *target_problems])
    for store in stores:
        name = store.root.name
        try:
            name = load(store).name
            connection, state = stack.enter_context(index.database(store))
            local_targets = graph.local_refs(connection, targets)
            local_identities = graph.local_refs(connection, identities)
            exact = shaped and index.known(connection, local_identities | {query.text.strip()})
            rows = index.search(
                connection, query, identities=local_identities, targets=local_targets, exact=exact, limit=-1
            )
            items = _search_items(
                connection,
                rows,
                store,
                name,
                local_targets if query.target else local_identities,
                exact or bool(query.target),
            )
            skipped = index.problems(connection)
        except (Error, OSError, UnicodeError, sqlite3.DatabaseError) as error:
            if len(stores) == 1:
                if isinstance(error, sqlite3.DatabaseError):
                    # A damaged cache is a brain problem to report, not a crash.
                    raise Error("the search cache is unavailable; run bf build") from error
                raise
            last_error = error
            message = str(error) if isinstance(error, Error) else "inaccessible brain or cache; run bf status"
            problems.append({"brain": name, "error": message.replace(str(store.root), "<brain>")})
            continue
        if skipped:
            problems.append({"brain": name, "files": skipped})
        brains.append((store, name, set(index.sources(connection))))
        if state != "ready":
            stale.append(name)
        results.append((exact, items))
    if not results and scope_problems:
        raise Error("no unambiguous brain could be searched; check bf.yaml brain references")
    if not results:
        raise Error("no selected brain could be searched; run bf status with --brain for each brain") from last_error
    identity = any(exact for exact, _ in results)
    # A brain that does not know a known identity ranked its words instead: that is not an identity answer.
    ranked = [items for exact, items in results if exact or not identity]
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
    selected = window[: query.limit]
    for item in selected:
        item.pop("_rank", None)
    reply: dict[str, object] = {"items": selected, "notice": NOTICE}
    if len(window) > query.limit:
        reply.update(more=True, next_offset=query.offset + query.limit)
    if shaped and not identity:
        # Nothing is, names or links to it: say so, since its items only share words with the query.
        reply["identity"] = "unknown"
    scoped = query.prefix.split("/")[1] if query.prefix.startswith("memories/") else ""
    coverage = []
    for store, name, sources in brains:
        if counted:
            usage.note(store, "search", sum(item["brain"] == name for item in selected))
        # Coverage describes what was searched, including sources with no matching evidence.
        names = {scoped} if scoped else sources | set(load(store).sensors)
        if query.prefix and not query.prefix.startswith("memories"):
            names = set()
        if names:
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
    if stale:
        reply["stale"] = stale
    if problems:
        reply["problems"] = problems
    return bounded(reply)


def _search_items(
    connection: sqlite3.Connection,
    rows: Iterator[dict[str, object]],
    store: Store,
    name: str,
    targets: set[str],
    explain: bool,
) -> Iterator[dict[str, object]]:
    sources = pages.owned(store)
    for row in rows:
        item = pages.label(name, pages.guard(row, sources, excerpts=True))
        if explain:
            claims, truncated = graph.explanations(connection, str(item["ref"]), targets)
            if claims:
                item["relations"] = claims
            if truncated:
                item["relations_truncated"] = True
        yield item


def _configured(stores: list[Store], problems: list[dict[str, object]]) -> list[Store]:
    """Skip a brain whose bf.yaml does not load, unless it is the only one: the others still answer."""
    if len(stores) == 1:
        return stores
    result = []
    for store in stores:
        try:
            load(store)
        except Error, OSError, UnicodeError:
            problems.append({"brain": store.root.name, "error": "bf.yaml is invalid or inaccessible; run bf validate"})
            continue
        result.append(store)
    return result


def _brain(stores: list[Store], name: str) -> list[Store]:
    if not name:
        return stores
    chosen = [store for store in stores if load(store).name == name]
    if not chosen:
        raise Error(f"unknown brain {name}; pass one of the searched brains")
    return chosen


def read(
    stores: list[Store], ref: str = "", brain: str = "", *, offset: int = 0, counted: bool = True
) -> dict[str, object]:
    """Resolve a page, a note, a note section, a `source:id` record or an explicit identity.

    Without a ref, read returns the home page. Notes and records resolve in exactly one brain and
    carry their backlinks across the selected brains; pages and identities combine every brain.
    """
    if type(offset) is not int or not 0 <= offset <= 2**63 - 1:
        raise Error("offset must be a non-negative integer below 2**63")
    with index.session():
        return _resolve(stores, ref, brain, offset=offset, counted=counted)


def _resolve(stores: list[Store], ref: str, brain: str, *, offset: int, counted: bool) -> dict[str, object]:
    if len(ref) > 8192:
        raise Error("expected a reference of at most 8192 characters")
    stores, problems = related(stores)
    stores = _configured(stores, problems)
    # A qualified address resolves in its brain; backlinks and identities still span the whole selection.
    selected = everywhere = _brain(stores, brain)
    ref = ref.strip()
    if home := re.fullmatch(r"bf://([a-z][a-z0-9-]{0,63})/?", ref):
        selected, ref = _brain(selected, home[1]), ""
    parsed = links.parse(ref) if ref else None
    if parsed:
        selected = _brain(selected, parsed.brain)
    path = parsed.path if parsed else ref
    if not (parsed and parsed.fragment):
        try:
            view = pages.page(selected, path, offset=offset, counted=counted)
        except Error as error:
            if problems and isinstance(error, NotFoundError):
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
        if offset:
            raise Error("offset applies to listing pages or exact reads; use search for identity backlinks")
        view = pages.identity(everywhere, ref, counted=counted)
        if view is not None:
            return bounded({**view, "notice": NOTICE, **_problems(problems, view)})
        if problems:
            raise Error("read is incomplete; run bf status before concluding a reference is absent")
        raise NotFoundError("reference not found; use bf search to locate it, or bf read to browse pages")
    if len(found) > 1:
        raise Error("reference exists in several brains; use a brain-qualified bf:// address")
    store, value = found[0]
    extra = pages.context(everywhere, store, str(value["brain"]), value)
    reply = _exact_reply({**value, **extra, "notice": NOTICE, **_problems(problems, extra)}, offset)
    if counted:
        usage.note(store, "read", 1)
    return reply


def _exact_reply(value: dict[str, object], offset: int) -> dict[str, object]:
    """Oversized exact replies are lossless JSON chunks; offsets count Unicode characters."""
    data = encode(value)
    if not offset and len(data) <= MAX_REPLY:
        return value
    text = data.decode()
    if offset >= len(text):
        raise Error("offset is beyond this exact reply; restart the read at offset 0")
    end = min(offset + 65536, len(text))
    return bounded(
        {
            "brain": value["brain"],
            "ref": value["ref"],
            "format": "json",
            "chunk": text[offset:end],
            "offset": offset,
            "total_characters": len(text),
            "sha256": digest(data),
            "notice": NOTICE,
            **({"external": True} if value.get("external") else {}),
            **({"problems": value["problems"]} if value.get("problems") else {}),
            **({"stale": value["stale"]} if value.get("stale") else {}),
            **({"next_offset": end} if end < len(text) else {}),
        }
    )


def _problems(scope: list[dict[str, object]], reply: dict[str, object]) -> dict[str, object]:
    combined = [*scope, *cast("list[dict[str, object]]", reply.get("problems", []))]
    return {"problems": combined} if combined else {}


def _read(store: Store, ref: str) -> dict[str, object] | None:
    name = load(store).name
    if parsed := links.parse(ref):
        if parsed.brain != name:
            return None
        # BF paths address authored files, source:id records, or explicitly owned entity aliases.
        if authored(parsed.path) or ":" in parsed.path.split("/")[0]:
            value = _read(store, parsed.path)
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
        except FileNotFoundError:
            return None
        text = section(path, data, fragment) if fragment else data.decode("utf-8")
        return {"brain": name, "ref": ref, "text": text}
    located, aliases, cache_error = None, [], None
    try:
        with index.database(store) as (connection, _state):
            located = connection.execute("SELECT path FROM items WHERE ref=?", (ref,)).fetchone()
            aliases = connection.execute(
                "SELECT i.ref FROM names n JOIN items i ON i.id=n.item WHERE n.name=? ORDER BY i.ref LIMIT 2",
                (ref,),
            ).fetchall()
    except (Error, OSError, sqlite3.DatabaseError) as error:
        # An exact source:id still has a file recovery path when its disposable cache is unavailable.
        cache_error = error
    source, separator, record_id = ref.partition(":")
    if separator:
        # The cache only locates the partition; the answer always comes from the record file itself.
        found = None
        if located:
            hint = str(located["path"])
            parts: tuple[str, ...] = ()
            with suppress(Error):
                parts = relative(hint)
            if len(parts) == 3 and parts[:2] == ("memories", source) and parts[2].endswith(".jsonl"):
                # Journal checks run before suppression; only an unusable cache hint is ignored.
                with records.reading(store), suppress(Error, OSError, UnicodeError):
                    hinted = next((r for r in records.load(store, hint) if r.id == record_id), None)
                    if hinted:
                        found = hint, hinted
        found = found or records.find(store, source, record_id)
        if found:
            collection = source_health(store, [source])[source]
            return {
                "brain": name,
                "ref": ref,
                "path": found[0],
                "record": found[1].model_dump(exclude_defaults=True),
                "collection": collection,
                **({"external": True} if collection["trust"] == "external" else {}),
            }
    if cache_error:
        raise Error("the search cache is unavailable; run bf build to resolve identities") from cache_error
    if len(aliases) > 1:
        raise Error("ambiguous identity; use bf search and read an exact ref")
    return _read(store, aliases[0]["ref"]) if aliases and aliases[0]["ref"] != ref else None
