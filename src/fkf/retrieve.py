"""Search across bases and read exact notes, sections and records."""

from __future__ import annotations

from fkf import index, records, usage
from fkf.config import load
from fkf.markdown import authored, section
from fkf.models import MAX_REPLY, NOTICE, Error, Query, encode
from fkf.storage import Store, relative


def bounded(value: dict[str, object], limit: int = MAX_REPLY) -> dict[str, object]:
    if len(encode(value)) > limit:
        raise Error("response exceeds its byte limit; narrow the request")
    return value


def search(stores: list[Store], query: Query, *, counted: bool = True) -> dict[str, object]:
    """Merge per-base results: relevance keeps each base's order, a time window interleaves by time."""
    results: list[list[dict[str, object]]] = []
    stale = []
    for store in stores:
        name = load(store).name
        with index.database(store) as (connection, state):
            items = index.search(connection, query)
        if state != "ready":
            stale.append(name)
        if counted:
            usage.note(store, "search", len(items))
        results.append([{"base": name, **item} for item in items])
    if query.recent or not query.text.strip():
        merged = sorted(
            (i for r in results for i in r), key=lambda i: (str(i.get("time", "")), str(i["ref"])), reverse=True
        )
    else:
        # Interleave by rank: bm25 scores from different corpora are not comparable.
        merged = [i for rank in range(query.limit) for r in results if rank < len(r) for i in [r[rank]]]
    reply: dict[str, object] = {"items": merged[: query.limit], "notice": NOTICE}
    if stale:
        reply["stale"] = stale
    return bounded(reply)


def _base(stores: list[Store], name: str) -> list[Store]:
    if not name:
        return stores
    chosen = [store for store in stores if load(store).name == name]
    if not chosen:
        raise Error(f"unknown base {name}; pass one of the searched bases")
    return chosen


def read(stores: list[Store], ref: str, base: str = "") -> dict[str, object]:
    """Resolve a note path, note section, `source:id` record or explicit identity in exactly one base."""
    if not ref or len(ref) > 8192:
        raise Error("expected a reference of at most 8192 characters")
    found = [(store, value) for store in _base(stores, base) if (value := _read(store, ref)) is not None]
    if not found:
        raise Error("reference not found; use fkf search to locate it")
    if len(found) > 1:
        raise Error("reference exists in several bases; pass --base NAME")
    reply = bounded({**found[0][1], "notice": NOTICE})
    usage.note(found[0][0], "read", 1)
    return reply


def _read(store: Store, ref: str) -> dict[str, object] | None:
    name = load(store).name
    path, _, fragment = ref.partition("#")
    if authored(path):
        relative(path)
        try:
            data = store.read(path, 4 << 20)
        except FileNotFoundError:
            return None
        text = section(path, data, fragment) if fragment else data.decode("utf-8")
        return {"base": name, "ref": ref, "text": text}
    with index.database(store) as (connection, _state):
        located = connection.execute("SELECT path FROM items WHERE ref=?", (ref,)).fetchone()
        # An explicit identity (alias) resolves to the note or record that declares it.
        alias = connection.execute(
            "SELECT i.ref FROM names n JOIN items i ON i.id=n.item WHERE n.name=? ORDER BY i.time DESC,i.ref LIMIT 1",
            (ref,),
        ).fetchone()
    source, separator, record_id = ref.partition(":")
    if separator:
        # The cache only locates the partition; the answer always comes from the record file itself.
        hinted = located and next((r for r in records.load(store, located["path"]) if r.id == record_id), None)
        found = (located["path"], hinted) if hinted else records.find(store, source, record_id)
        if found:
            return {"base": name, "ref": ref, "path": found[0], "record": found[1].model_dump(exclude_defaults=True)}
    return _read(store, alias["ref"]) if alias and alias["ref"] != ref else None
