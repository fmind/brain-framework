"""Bounded identity federation and claim explanations over selected SQLite projections."""

from __future__ import annotations

import json
import sqlite3

from bf import index, links
from bf.config import load
from bf.models import Error
from bf.storage import Store


def local_refs(connection: sqlite3.Connection, identities: set[str]) -> set[str]:
    """Add local readable refs only inside the brain that owns a qualified BF address.

    A name that several local items claim identifies none of them: their links never merge.
    """
    qualified = [value for value in identities if links.parse(value)]
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
    neither owner, even when the value itself is a unique, brain-qualified address.
    """
    if not value:
        return set(), []
    value = links.identity(value)
    parsed = links.parse(value)
    matches: list[tuple[Store, int, set[str]]] = []
    problems: list[dict[str, object]] = []
    for store in stores:
        name = store.root.name
        try:
            name = load(store).name
            if parsed and parsed.brain != name:
                continue
            with index.database(store) as (connection, state):
                if state != "ready":
                    problems.append({"brain": name, "error": "identity resolution used a stale cache"})
                rows = connection.execute(
                    "SELECT id FROM items WHERE ref=? UNION SELECT item FROM names WHERE name=? LIMIT 3",
                    (value, value),
                ).fetchall()
                for row in rows:
                    names = {
                        r[0] for r in connection.execute("SELECT name FROM names WHERE item=? LIMIT 1002", (row[0],))
                    }
                    if len(names) > 1001:
                        raise Error("identity expansion exceeds 1001 aliases")
                    matches.append((store, row[0], names))
        except (Error, OSError, sqlite3.DatabaseError) as error:
            problems.append(
                {"brain": name, "error": str(error) if isinstance(error, Error) else "identity cache unavailable"}
            )
    if len(matches) > 1:
        problems.append({"error": "ambiguous identity across selected evidence; use a brain-qualified exact ref"})
        return {value}, problems
    if not matches:
        return {value}, problems
    owner, item, names = matches[0]
    shared: set[str] = set()
    for store in stores:
        try:
            with index.database(store) as (connection, _state):
                shared.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT name FROM names WHERE name IN (SELECT value FROM json_each(?)) "
                        "AND NOT (? AND item=?)",
                        (json.dumps(sorted(names - {value})), store.root == owner.root, item),
                    )
                )
        except Error, OSError, sqlite3.DatabaseError:
            problems.append({"brain": store.root.name, "error": "identity cache unavailable"})
    if shared:
        problems.append({"error": f"{len(shared)} aliases are also claimed elsewhere and were not expanded"})
    return {value} | (names - shared), problems


def explanations(connection: sqlite3.Connection, ref: str, targets: set[str]) -> tuple[list[dict[str, object]], bool]:
    """Claims from one item to the targets; each retains its owning file, even when another file supports it."""
    # Exact ref first: record ids may contain #. Note passage refs resolve to their owning item.
    row = connection.execute("SELECT id FROM items WHERE ref=?", (ref,)).fetchone()
    if row is None:
        row = connection.execute("SELECT id FROM items WHERE ref=?", (ref.rpartition("#")[0],)).fetchone()
    if row is None:
        return [], False
    rows = connection.execute(
        "SELECT subject,relation,target,origin FROM edges WHERE item=? "
        "AND (target IN (SELECT value FROM json_each(?)) OR EXISTS (SELECT 1 FROM json_each(?) s "
        "WHERE target>s.value||'#' AND target<s.value||'$')) "
        "ORDER BY relation,target,origin,subject LIMIT 51",
        (row[0], json.dumps(sorted(targets)), json.dumps(index.sections(targets))),
    ).fetchall()
    return _claims(rows[:50]), len(rows) > 50


def outgoing(connection: sqlite3.Connection, subjects: set[str]) -> list[dict[str, object]]:
    """Typed claims whose explicit subject is one of these identities, wherever they were asserted."""
    rows = connection.execute(
        "SELECT subject,relation,target,origin FROM edges WHERE relation!='' "
        "AND subject IN (SELECT value FROM json_each(?)) "
        "ORDER BY relation,target,origin,subject LIMIT 50",
        (json.dumps(sorted(subjects)),),
    ).fetchall()
    return _claims(rows)


def _claims(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    """Claims as mappings; an untyped link has no relation member."""
    return [{key: val for key, val in dict(row).items() if val != ""} for row in rows]
