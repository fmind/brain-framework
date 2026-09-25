"""Bounded identity federation and claim explanations over selected SQLite projections."""

from __future__ import annotations

import json
import sqlite3

from bf import index, links
from bf.config import load
from bf.models import Error, Query
from bf.storage import Store


def local_refs(connection: sqlite3.Connection, identities: set[str]) -> set[str]:
    """Add local readable refs only inside the brain that owns a qualified BF address."""
    qualified = [value for value in identities if links.parse(value)]
    rows = connection.execute(
        "SELECT DISTINCT i.ref FROM names n JOIN items i ON n.item=i.id "
        "WHERE n.name IN (SELECT value FROM json_each(?)) LIMIT 1002",
        (json.dumps(qualified),),
    ).fetchall()
    if len(rows) > 1001:
        raise Error("local identity expansion exceeds 1001 owners")
    return identities | {row[0] for row in rows}


def expand(stores: list[Store], value: str) -> tuple[set[str], list[dict[str, object]]]:
    """One explicit owner expands aliases; conflicting owners never merge their identities."""
    if not value:
        return set(), []
    value = links.identity(value)
    parsed = links.parse(value)
    matches: list[set[str]] = []
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
                    matches.append(names)
        except (Error, OSError, sqlite3.DatabaseError) as error:
            problems.append(
                {"brain": name, "error": str(error) if isinstance(error, Error) else "identity cache unavailable"}
            )
    if len(matches) > 1:
        problems.append({"error": "ambiguous identity across selected evidence; use a brain-qualified exact ref"})
        return {value}, problems
    return ({value} | matches[0] if matches else {value}), problems


def explanations(
    connection: sqlite3.Connection, ref: str, query: Query, targets: set[str], subjects: set[str], identities: set[str]
) -> tuple[list[dict[str, object]], bool]:
    """Each claim retains its owning file, even when another file supports the same triple."""
    # Exact ref first: record ids may contain #. Note passage refs resolve to their owning item.
    row = connection.execute("SELECT id FROM items WHERE ref=?", (ref,)).fetchone()
    if row is None:
        row = connection.execute("SELECT id FROM items WHERE ref=?", (ref.rpartition("#")[0],)).fetchone()
    if row is None:
        return [], False
    rows = connection.execute(
        "SELECT subject,relation,target,evidence,asserted_by,attributes,origin FROM edges WHERE item=? "
        "AND (?='' OR relation=?) "
        "AND (?='' OR target IN (SELECT value FROM json_each(?))) "
        "AND (?='' OR subject IN (SELECT value FROM json_each(?))) "
        "AND (?=0 OR target IN (SELECT value FROM json_each(?))) "
        "ORDER BY relation,target,evidence,subject,asserted_by,attributes,origin LIMIT 51",
        (
            row[0],
            query.relation,
            query.relation,
            query.target,
            json.dumps(sorted(targets)),
            query.subject,
            json.dumps(sorted(subjects)),
            int(bool(identities) and not (query.target or query.subject)),
            json.dumps(sorted(identities)),
        ),
    ).fetchall()
    result = []
    for row in rows[:50]:
        claim = dict(row)
        claim["attributes"] = json.loads(claim["attributes"])
        result.append({key: val for key, val in claim.items() if val not in ("", {})})
    return result, len(rows) > 50
