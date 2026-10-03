"""Bounded identity federation and claim explanations over selected SQLite projections."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bf import index, links
from bf.config import brain_name, load
from bf.models import Error
from bf.storage import Store

# A read lists at most CLAIMS outgoing claims, and at most RELATION of any one relation.
CLAIMS = 50
RELATION = 20
# The most names one item holds: a note's path, entity, resource and 1,000 aliases; a record's ref and 1,000 aliases.
NAMES = 1003


def local_refs(connection: sqlite3.Connection, identities: set[str], brain: str) -> set[str]:
    """Add local readable refs only inside the brain that owns a qualified BF address.

    Validation keeps each BF name in its own brain's namespace, so another brain's address adds nothing here, even
    from a cache built before a rule rejected it. A name that several local items claim identifies none of them: their
    links never merge.
    """
    qualified = [value for value in identities if (parsed := links.parse(value)) and parsed.brain == brain]
    rows = connection.execute(
        "SELECT DISTINCT i.ref FROM items i JOIN (SELECT name,min(item) AS item FROM names "
        "WHERE name IN (SELECT value FROM json_each(?)) GROUP BY name HAVING count(*)=1) o ON o.item=i.id LIMIT 1002",
        (json.dumps(qualified),),
    ).fetchall()
    if len(rows) > 1001:
        raise Error("local identity expansion exceeds 1001 owners")
    return identities | {row[0] for row in rows}


def expand(stores: list[Store], value: str) -> tuple[set[str], list[dict[str, object]]]:
    """One explicit owner expands aliases; conflicting owners never merge their identities.

    An alias that another item in any selected brain also claims is not expanded: it identifies
    neither owner, even when the value itself is a unique, brain-qualified address. A record also expands to its
    bare `source:id`, by which other brains link it, unless another item holds that ref too, such as the same record
    collected by another brain: each brain's links then name its own copy, which is no problem.
    """
    if not value:
        return set(), []
    value = links.identity(value)
    if links.tag(value) is not None:
        return {value}, []
    parsed = links.parse(value)
    matches: list[tuple[Store, int, set[str], str]] = []
    problems: list[dict[str, object]] = []
    labels: dict[Path, str] = {}
    failed: set[Path] = set()
    for store in stores:
        name = ""
        try:
            name = labels[store.root] = load(store).name
            if parsed and parsed.brain != name:
                continue
            with index.database(store) as (connection, state):
                if state != "ready":
                    problems.append({"brain": name, "error": "identity resolution used a stale cache"})
                rows = connection.execute(
                    "SELECT id,ref,kind FROM items WHERE id IN "
                    "(SELECT id FROM items WHERE ref=?1 UNION SELECT item FROM names WHERE name=?1) LIMIT 3",
                    (value,),
                ).fetchall()
                for item, ref, kind in rows:
                    names = {
                        r[0]
                        for r in connection.execute("SELECT name FROM names WHERE item=? LIMIT ?", (item, NAMES + 1))
                    }
                    if len(names) > NAMES:
                        raise Error(f"identity expansion exceeds {NAMES} aliases")
                    matches.append((store, item, names, ref if kind == "record" else ""))
        except (Error, OSError, sqlite3.DatabaseError) as error:
            # Named and worded as the page reports the same brain, so a reply lists an unloadable brain once.
            message = str(error) if isinstance(error, Error) else "inaccessible brain or cache; run bf status"
            problems.append({"brain": name or brain_name(store), "error": message.replace(str(store.root), "<brain>")})
            failed.add(store.root)
    if len(matches) > 1:
        problems.append(
            {"error": "ambiguous identity across selected evidence; read the owning note's bf://NAME/path.md"}
        )
        return {value}, problems
    if not matches:
        return {value}, problems
    owner, item, names, bare = matches[0]
    # Another brain never holds a name in the owner's namespace: like local_refs, ignore a cache built before a rule
    # rejected one. The owner's claim to another brain's address still counts as shared.
    namespace = links.address(labels[owner.root], "")
    shared: set[str] = set()
    for store in stores:
        if store.root not in labels or store.root in failed:
            # Its bf.yaml or cache did not load: already reported once. It cannot share an alias, but it may hold the
            # record's ref.
            bare = ""
            continue
        own = store.root == owner.root
        candidates = sorted(name for name in (names | {bare}) - {value, ""} if own or not name.startswith(namespace))
        try:
            with index.database(store) as (connection, _state):
                # A record's ref already names it: a note alias repeating one is shared, never expanded.
                found = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM names WHERE name IN (SELECT value FROM json_each(?1)) AND NOT (?2 AND item=?3) "
                        "UNION SELECT ref FROM items WHERE ref IN (SELECT value FROM json_each(?1)) "
                        "AND NOT (?2 AND id=?3)",
                        (json.dumps(candidates), own, item),
                    )
                }
        except (Error, OSError, sqlite3.DatabaseError) as error:
            message = str(error) if isinstance(error, Error) else "inaccessible brain or cache; run bf status"
            problems.append({"brain": labels[store.root], "error": message.replace(str(store.root), "<brain>")})
            bare = ""
            continue
        if bare in found:
            found.remove(bare)
            bare = ""
        shared |= found
    if shared:
        problems.append({"error": f"{len(shared)} aliases are also claimed elsewhere and were not expanded"})
    return {value} | (names - shared) | ({bare} if bare else set()), problems


def explanations(connection: sqlite3.Connection, ref: str, targets: set[str]) -> tuple[list[dict[str, object]], bool]:
    """Claims from one item to the targets; each retains its owning file, even when another file supports it."""
    # Exact ref first: record ids may contain #. Note passage refs resolve to their owning item.
    row = connection.execute("SELECT id FROM items WHERE ref=?", (ref,)).fetchone()
    if row is None:
        row = connection.execute("SELECT id FROM items WHERE ref=?", (ref.rpartition("#")[0],)).fetchone()
    if row is None:
        return [], False
    # An untyped link beside a typed one from the same origin to the same target repeats it: backlinks list it once.
    rows = connection.execute(
        "SELECT subject,relation,target,origin FROM (SELECT subject,relation,target,origin,"
        "max(relation!='') OVER (PARTITION BY target,origin) AS typed FROM edges WHERE item=? "
        "AND (target IN (SELECT value FROM json_each(?)) OR EXISTS (SELECT 1 FROM json_each(?) s "
        "WHERE target>s.value||'#' AND target<s.value||'$'))) "
        "WHERE relation!='' OR NOT typed ORDER BY relation,target,origin,subject LIMIT 51",
        (row[0], json.dumps(sorted(targets)), json.dumps(index.sections(targets))),
    ).fetchall()
    return _claims(rows[:50]), len(rows) > 50


def outgoing(connection: sqlite3.Connection, subjects: set[str]) -> tuple[list[dict[str, object]], bool]:
    """Typed claims whose explicit subject is one of these identities, wherever they were asserted.

    Each relation keeps up to RELATION claims, so a crowded one, such as a meeting's attendees, never hides another,
    such as its organizer; at most CLAIMS in all. Each claim carries the `time` or `date` of the asserting item.
    """
    rows = connection.execute(
        f"""SELECT subject,relation,target,origin,time,date FROM (
              SELECT e.subject,e.relation,e.target,e.origin,{index.TIME} AS time,{index.DATE} AS date,
                row_number() OVER (PARTITION BY e.relation ORDER BY e.target,e.origin,e.subject) AS n
              FROM edges e JOIN items i ON i.id=e.item
              WHERE e.relation!='' AND e.subject IN (SELECT value FROM json_each(?)))
            WHERE n<=? ORDER BY relation,target,origin,subject""",  # noqa: S608 - fixed SQL
        (json.dumps(sorted(subjects)), RELATION + 1),
    ).fetchall()
    kept, seen = [], {}
    for row in rows:
        seen[row["relation"]] = seen.get(row["relation"], 0) + 1
        if seen[row["relation"]] <= RELATION:
            kept.append(row)
    truncated = any(count > RELATION for count in seen.values()) or len(kept) > CLAIMS
    return _claims(kept[:CLAIMS]), truncated


def _claims(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    """Claims as mappings; an untyped link has no relation member."""
    return [{key: val for key, val in dict(row).items() if val != ""} for row in rows]
