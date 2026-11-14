"""Bounded identity expansion and claim explanations over a brain's SQLite projection."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import suppress

from bf import index, links
from bf.config import load
from bf.markdown import preview
from bf.models import Error, reason
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
        f"SELECT DISTINCT i.ref FROM items i JOIN ({index.OWNED}) o ON o.item=i.id",  # noqa: S608 - fixed SQL
        {"names": json.dumps(qualified)},
    ).fetchall()
    local = {str(row[0]) for row in rows}
    shorthands = {ref.removesuffix("/ACTION.md") for ref in local if re.fullmatch(r"actions/[^/#]+/ACTION\.md", ref)}
    return identities | local | shorthands


# Why a selected brain answered nothing, when its failure is no Error naming its own cause.
UNAVAILABLE = "inaccessible brain or cache; run bf status"


def problem(store: Store, error: BaseException, fallback: str = UNAVAILABLE) -> dict[str, object]:
    """A brain failure as a reply problem, without leaking the brain's path."""
    message = reason(error, fallback)
    return {"error": message.replace(str(store.root), "<brain>")}


def expand(store: Store, value: str) -> tuple[set[str], list[dict[str, object]]]:
    """One explicit owner expands aliases; conflicting owners never merge their identities.

    An alias that another item in the brain also claims is not expanded: it identifies neither owner, even when the
    value itself is a unique, brain-qualified address. A record also expands to its bare `source:id`, unless another
    item holds that ref as an alias.
    """
    if not value:
        return set(), []
    value = links.identity(value)
    if links.tag(value) is not None:
        return {value}, []
    parsed = links.parse(value)
    lookup = (
        links.address(parsed.brain, parsed.path + "/ACTION.md")
        if parsed and re.fullmatch(r"actions/[^/#]+", parsed.path)
        else value
    )
    problems: list[dict[str, object]] = []
    try:
        name = load(store).name
        if parsed and parsed.brain != name:
            return {value}, []
        with index.database(store) as (connection, state):
            if state != "ready":
                problems.append({"error": "identity resolution used a stale cache"})
            rows = connection.execute(
                "SELECT id,ref,kind FROM items WHERE id IN "
                "(SELECT id FROM items WHERE ref=?1 UNION SELECT item FROM names WHERE name=?1) LIMIT 3",
                (lookup,),
            ).fetchall()
            if len(rows) > 1:
                problems.append({"error": "ambiguous identity; read an owner's bf://NAME/... address"})
                return {value}, problems
            if not rows:
                return {value}, problems
            item, ref, kind = rows[0]
            names = {r[0] for r in connection.execute("SELECT name FROM names WHERE item=? LIMIT ?", (item, NAMES + 1))}
            if len(names) > NAMES:
                raise Error(f"identity expansion exceeds {NAMES} aliases")
            bare = ref if kind == "record" else ""
            if re.fullmatch(r"actions/[^/#]+/ACTION\.md", ref):
                names = names | {links.address(name, ref.removesuffix("/ACTION.md"))}
            candidates = sorted(candidate for candidate in (names | {bare}) - {value, ""} if candidate)
            found = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM names WHERE name IN (SELECT value FROM json_each(?1)) AND item!=?2 "
                    "UNION SELECT ref FROM items WHERE ref IN (SELECT value FROM json_each(?1)) AND ref!=?3",
                    (json.dumps(candidates), item, ref),
                )
            }
    except (Error, OSError, sqlite3.DatabaseError) as error:
        problems.append(problem(store, error))
        return {value}, problems
    if bare in found:
        found.remove(bare)
        bare = ""
    if found:
        problems.append({"error": f"aliases also claimed elsewhere were not expanded: {len(found)}"})
    return {value} | (names - found) | ({bare} if bare else set()), problems


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
        "SELECT subject,relation,target,origin FROM (SELECT subject,relation,target,origin,"  # noqa: S608 - fixed SQL
        "max(relation!='') OVER (PARTITION BY target,origin) AS typed FROM edges WHERE item=? "
        "AND (target IN (SELECT value FROM json_each(?)) OR EXISTS (SELECT 1 FROM json_each(?) s "
        f"WHERE {index.within('target', 's.value')}))) "
        "WHERE relation!='' OR NOT typed ORDER BY relation,target,origin,subject LIMIT ?",
        (row[0], json.dumps(sorted(targets)), json.dumps(index.sections(targets)), CLAIMS + 1),
    ).fetchall()
    return _claims(rows[:CLAIMS]), len(rows) > CLAIMS


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
    claims = _claims(kept[:CLAIMS])
    claim_owners = owners(connection, {str(claim["target"]) for claim in claims})
    for claim in claims:
        if owner := claim_owners.get(str(claim["target"])):
            claim["target_ref"], claim["target_title"] = owner
    return claims, truncated


def owners(connection: sqlite3.Connection, targets: set[str]) -> dict[str, tuple[str, str]]:
    """The one local item each target names, by its ref or a name, so a reader can follow a claim without searching;
    a target several items claim, by ref or name, names none. A note section's address names its note: the claim's
    target keeps the section, which `bf read` opens. Titles are previews, as in listings."""
    wholes: dict[str, str] = {}
    for target in targets:
        parsed = None
        with suppress(Error):
            parsed = links.parse(target)
        if parsed:
            path = parsed.path
            if re.fullmatch(r"actions/[^/#]+", path.rstrip("/")):
                path = path.rstrip("/") + "/ACTION.md"
            wholes[target] = links.address(parsed.brain, path) if parsed.fragment or path != parsed.path else target
        else:
            wholes[target] = target
    rows = connection.execute(
        """SELECT target,min(ref) AS ref,min(title) AS title,min(kind) AS kind FROM (
             SELECT ref AS target,ref,title,kind FROM items WHERE ref IN (SELECT value FROM json_each(:names))
             UNION ALL SELECT n.name,i.ref,i.title,i.kind FROM names n JOIN items i ON i.id=n.item
               WHERE n.name IN (SELECT value FROM json_each(:names)))
           GROUP BY target HAVING count(DISTINCT ref)=1""",
        {"names": json.dumps(sorted(set(wholes.values())))},
    ).fetchall()
    owned = {row["target"]: row for row in rows}
    return {
        target: (row["ref"], preview(row["title"], index.TITLE))
        for target, whole in wholes.items()
        if (row := owned.get(whole)) is not None and (whole == target or row["kind"] == "note")
    }


def _claims(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    """Claims as mappings; an untyped link has no relation member."""
    return [{key: val for key, val in dict(row).items() if val != ""} for row in rows]
