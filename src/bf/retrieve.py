"""Search across brains, and read pages, exact notes, sections, records and identities."""

from __future__ import annotations

import re
import sqlite3
from contextlib import suppress
from typing import cast

from bf import graph, index, links, pages, records, usage
from bf.config import load, related
from bf.health import source_health
from bf.markdown import authored, section, split_ref
from bf.models import MAX_REPLY, NOTICE, Error, Query, encode
from bf.storage import Store, relative


def bounded(value: dict[str, object], limit: int = MAX_REPLY) -> dict[str, object]:
    if len(encode(value)) > limit:
        raise Error("response exceeds its byte limit; narrow the request")
    return value


def search(stores: list[Store], query: Query, *, counted: bool = True) -> dict[str, object]:
    """Merge per-brain results: relevance keeps each brain's order, identities interleave by time."""
    stores, scope_problems = related(stores)
    results: list[list[dict[str, object]]] = []
    stale = []
    brains = []
    problems: list[dict[str, object]] = list(scope_problems)
    last_error: Exception | None = None
    owners: set[tuple[str, str]] = set()
    identity = index.identity(query.text)
    identities, identity_problems = graph.expand(stores, query.text.strip()) if identity else (set(), [])
    targets, target_problems = graph.expand(stores, query.target)
    problems.extend([*identity_problems, *target_problems])
    for store in stores:
        name = store.root.name
        try:
            name = load(store).name
            with index.database(store) as (connection, state):
                local_targets = graph.local_refs(connection, targets)
                local_identities = graph.local_refs(connection, identities)
                items = index.search(connection, query, identities=local_identities, targets=local_targets)
                # Searches keep excerpts: the agent asked. External records stay labeled as such.
                pages.guard(items, pages.owned(store), excerpts=True)
                for position, item in enumerate(items):
                    claims, truncated = (
                        graph.explanations(
                            connection, str(item["ref"]), local_targets if query.target else local_identities
                        )
                        if identity or query.target
                        else ([], False)
                    )
                    item = items[position] = pages.label(name, item)
                    if claims:
                        item["relations"] = claims
                    if truncated:
                        item["relations_truncated"] = True
                skipped = index.problems(connection)
                if identity:
                    owners.update((name, ref) for ref in index.identity_owners(connection, query.text))
        except (Error, OSError, UnicodeError, sqlite3.DatabaseError) as error:
            if len(stores) == 1:
                raise
            last_error = error
            message = str(error) if isinstance(error, Error) else "inaccessible brain or cache; run bf status"
            problems.append({"brain": name, "error": message.replace(str(store.root), "<brain>")})
            continue
        if skipped:
            problems.append({"brain": name, "files": skipped})
        brains.append((store, name))
        if state != "ready":
            stale.append(name)
        if counted:
            usage.note(store, "search", len(items))
        results.append(items)
    if not results and scope_problems:
        raise Error("no unambiguous brain could be searched; check bf.yaml brain references")
    if not results:
        raise Error("no selected brain could be searched; run bf status with --brain for each brain") from last_error
    if identity:
        # Keep canonical refs, alias owners and related evidence in that order, then newest first.
        merged = sorted(
            (i for r in results for i in r), key=lambda i: (str(i.get("time", "")), str(i["ref"])), reverse=True
        )
        merged.sort(
            key=lambda item: (
                str(item["ref"]) != query.text.strip(),
                (str(item["brain"]), str(item["ref"])) not in owners,
            )
        )
    else:
        # Interleave by rank: bm25 scores from different corpora are not comparable.
        merged = [i for rank in range(query.limit) for r in results if rank < len(r) for i in [r[rank]]]
    selected = merged[: query.limit]
    reply: dict[str, object] = {"items": selected, "notice": NOTICE}
    scoped = query.prefix.split("/")[1] if query.prefix.startswith("memories/") else ""
    coverage = []
    for store, name in brains:
        names = {str(item["source"]) for item in selected if item["brain"] == name and "source" in item}
        if scoped:
            names.add(scoped)
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


def _brain(stores: list[Store], name: str) -> list[Store]:
    if not name:
        return stores
    chosen = [store for store in stores if load(store).name == name]
    if not chosen:
        raise Error(f"unknown brain {name}; pass one of the searched brains")
    return chosen


def read(stores: list[Store], ref: str = "", brain: str = "", *, counted: bool = True) -> dict[str, object]:
    """Resolve a page, a note, a note section, a `source:id` record or an explicit identity.

    Without a ref, read returns the home page. Notes and records resolve in exactly one brain and
    carry their backlinks across the selected brains; pages and identities combine every brain.
    """
    if len(ref) > 8192:
        raise Error("expected a reference of at most 8192 characters")
    stores, problems = related(stores)
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
        view = pages.page(selected, path, counted=counted)
        if view is not None:
            return bounded({**view, "notice": NOTICE, **_problems(problems, view)})
        if re.fullmatch(r"actions/[^/#]+", path.rstrip("/")) and not authored(path):
            # An action folder reads as its ACTION.md with the action's files and linked projects.
            path = path.rstrip("/") + "/ACTION.md"
            ref = links.address(parsed.brain, path) if parsed else path
    found = [(store, value) for store in selected if (value := _read(store, ref)) is not None]
    if not found:
        view = pages.identity(everywhere, ref, counted=counted)
        if view is not None:
            return bounded({**view, "notice": NOTICE, **_problems(problems, view)})
        raise Error("reference not found; use bf search to locate it, or bf read to browse pages")
    if len(found) > 1:
        raise Error("reference exists in several brains; use a brain-qualified bf:// address")
    store, value = found[0]
    extra = pages.context(everywhere, store, str(value["brain"]), value)
    reply = bounded({**value, **extra, "notice": NOTICE, **_problems(problems, extra)})
    if counted:
        usage.note(store, "read", 1)
    return reply


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
            data = store.read(path, 4 << 20)
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
