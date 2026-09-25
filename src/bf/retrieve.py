"""Search across brains and read exact notes, sections and records."""

from __future__ import annotations

import sqlite3
from contextlib import suppress

from bf import graph, index, links, records, usage
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
    """Merge per-brain results: relevance keeps each brain's order, a time window interleaves by time."""
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
    subjects, subject_problems = graph.expand(stores, query.subject)
    problems.extend([*identity_problems, *target_problems, *subject_problems])
    for store in stores:
        name = store.root.name
        try:
            config = load(store)
            name = config.name
            with index.database(store) as (connection, state):
                local_targets = graph.local_refs(connection, targets)
                local_subjects = graph.local_refs(connection, subjects)
                local_identities = graph.local_refs(connection, identities)
                items = index.search(
                    connection,
                    query,
                    active_sources={n for n, sensor in config.sensors.items() if sensor.enabled},
                    identities=local_identities,
                    targets=local_targets,
                    subjects=local_subjects,
                )
                for item in items:
                    item["uri"] = (
                        links.address(name, str(item["ref"]))
                        if item["kind"] == "record"
                        else links.address(name, *split_ref(str(item["ref"])))
                    )
                    claims, truncated = (
                        graph.explanations(
                            connection, str(item["ref"]), query, local_targets, local_subjects, local_identities
                        )
                        if identity or query.target or query.subject
                        else ([], False)
                    )
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
        results.append([{"brain": name, **item} for item in items])
    if not results and scope_problems:
        raise Error("no unambiguous brain could be searched; check bf.yaml brain references")
    if not results:
        raise Error("no selected brain could be searched; run bf status with --brain for each brain") from last_error
    if identity or query.recent or not query.text.strip():
        merged = sorted(
            (i for r in results for i in r), key=lambda i: (str(i.get("time", "")), str(i["ref"])), reverse=True
        )
        if identity:
            # Keep canonical refs, alias owners and related evidence in that order, then newest first.
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
    coverage = []
    for store, name in brains:
        names = {str(item["source"]) for item in selected if item["brain"] == name and "source" in item}
        if query.source:
            names.add(query.source)
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


def read(stores: list[Store], ref: str, brain: str = "") -> dict[str, object]:
    """Resolve a note path, note section, `source:id` record or explicit identity in exactly one brain."""
    if not ref or len(ref) > 8192:
        raise Error("expected a reference of at most 8192 characters")
    stores, problems = related(stores)
    selected = _brain(stores, brain)
    parsed = links.parse(ref)
    if parsed:
        selected = _brain(selected, parsed.brain)
        ref = parsed.identity
    found = [(store, value) for store in selected if (value := _read(store, ref)) is not None]
    if not found:
        raise Error("reference not found; use bf search to locate it")
    if len(found) > 1:
        raise Error("reference exists in several brains; use a brain-qualified bf:// address")
    reply = bounded({**found[0][1], "notice": NOTICE, **({"problems": problems} if problems else {})})
    usage.note(found[0][0], "read", 1)
    return reply


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
            return {
                "brain": name,
                "ref": ref,
                "path": found[0],
                "record": found[1].model_dump(exclude_defaults=True),
                "collection": source_health(store, [source])[source],
            }
    if cache_error:
        raise Error("the search cache is unavailable; run bf build to resolve identities") from cache_error
    if len(aliases) > 1:
        raise Error("ambiguous identity; use bf search and read an exact ref")
    return _read(store, aliases[0]["ref"]) if aliases and aliases[0]["ref"] != ref else None
