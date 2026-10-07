"""A cache generation is reused while index.SCHEMA is unchanged, so the tables, indexes and rows a build stores must
change only with it."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from bf import index, records
from bf.models import digest, encode
from bf.storage import Store

# The digest of the layout and rows a full build of BRAIN stores, recorded with the index.SCHEMA that stores them.
PROJECTION = (34, "a2fcc0d6de161736d06bb55341df9e728c4b758849f7809aecf83224273fc34e")
# Fixed input covering what indexing projects: notes, sections, tasks, links, claims, facts, records and problems.
BRAIN = {
    "bf.yaml": """version: 7
name: golden
fields:
  depends-on: {description: Needs review when the target changes., type: identity, cardinality: many, relation: true}
  kind: {description: Kind of item., type: string, cardinality: one}
""",
    "projects/atlas.md": """---
type: project
status: draft
updated: 2026-09-01
stale_after: 2026-12-01T00:00:00Z
tags: [launch]
aliases: ["repo:example/atlas"]
fields: {kind: plan, depends-on: ["mail:m1"]}
summary: Ship `MAX_FILE_SIZE` limits in config_loader.py.
---

# Atlas

Atlas keeps *durable* evidence; compute 2*3*4.

## Decision

We chose a static site.[^1] See [the guide](../concepts/guide.md#setup) and [mail](bf://golden/mail:m1?rel=depends-on).

### Budget

Approved at 1300 credits.

## Decision

A second decision.

## Notes {#decision-1}

- [ ] Rename `__init__.py` in [the loader](loader.md).
- [x] Review **the** plan.

[^1]: [Meeting](mail:m2)
""",
    "concepts/guide.md": """---
type: concept
---

Guide title line
spanning two lines
===

## Setup

Run `bf build` after `git pull`.
""",
    "actions/2026-09-20_import/ACTION.md": """---
type: action
status: stable
---
# Import

Import the [vendor guide](inputs/vendor.md).

- [ ] Check the console URL.
""",
    "actions/2026-09-20_import/inputs/vendor.md": """---
title: Vendor guide
owner: someone
---
# Vendor

Open [the console](http://[your-host]:8080/admin) or [the docs](https://example.test/docs).
""",
    "concepts/conflict.md": "# Conflict\n\n<<<<<<< ours\n",
    "projects/bad-link.md": "# Bad\n\n[x](http://[)\n",
}
RECORDS = {
    ("mail", "m1"): {
        "id": "m1",
        "title": "Kickoff",
        "text": "Budget approved:  1300 m² of office space…",
        "time": "2026-08-31T12:00:00Z",
        "url": "https://mail.example.test/m1",
        "links": ["repo:example/atlas"],
        "aliases": ["meeting:kickoff"],
        "fields": {"kind": "meeting", "depends-on": ["repo:example/atlas"]},
        "attributes": {"updated": "2026-09-01T08:00:00Z", "observed": "2026-09-01T08:15:00Z", "partial": True},
    },
    ("mail", "m2"): {"id": "m2", "title": "Follow-up", "text": "Short.", "time": "2026-09-02T09:30:00+02:00"},
    ("chat", "c1"): {"id": "c1", "title": "Untimed", "text": "The ﬁnance team."},
}


def projection(path: Path) -> str:
    """Every table and index as created and every stored row, naming items and passages by ref instead of row
    number, without file fingerprints."""
    with closing(sqlite3.connect(path)) as connection:
        # A new column or index changes a cache like a new row. FTS5 creates its own shadow tables, and root pages
        # follow the build's write order.
        layout = connection.execute(
            "SELECT type,name,sql FROM sqlite_schema WHERE name NOT GLOB 'search_*' ORDER BY type,name"
        ).fetchall()
        refs = dict(connection.execute("SELECT id,ref FROM items").fetchall())
        passages = {
            row[0]: [refs[row[1]], row[2]] for row in connection.execute("SELECT id,item,fragment FROM passages")
        }
        tables: dict[str, list[str]] = {}
        # The ontology row holds the configuration signature and the file's inode; FTS5 shadow tables hold its index.
        names = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name!='ontology' AND name NOT GLOB 'search_*'"
        ).fetchall()
        for (name,) in names:
            cursor = connection.execute(f"SELECT {'rowid AS passage,' if name == 'search' else ''}* FROM {name}")  # noqa: S608 - table names from the cache
            columns = [column[0] for column in cursor.description]
            rows = []
            for values in cursor:
                row = dict(zip(columns, values, strict=True))
                for volatile in ("id", "mtime", "ctime", "inode"):
                    row.pop(volatile, None)
                if "item" in row:
                    row["item"] = refs[row["item"]]
                if name == "search":
                    row["passage"] = passages[row["passage"]]
                rows.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
            tables[name] = sorted(rows)
    return digest(encode({"layout": layout, "rows": tables}))


def test_stored_layout_and_rows_change_only_with_the_cache_version(tmp_path: Path) -> None:
    root = tmp_path / "golden"
    root.mkdir()
    store = Store(root)
    for name, text in BRAIN.items():
        store.write(name, text.encode())
    for (source, record_id), record in RECORDS.items():
        store.write(records.path(source, record_id), encode(record))
    assert index.refresh(store, full=True)["skipped"] == 2
    found = projection(root / index.CACHE)
    assert (index.SCHEMA, found) == PROJECTION, (
        "The cache layout, indexing output or index.SCHEMA changed. A cache keeps its tables, indexes and rows while "
        "index.SCHEMA is unchanged, so when they changed, bump index.SCHEMA unless it already changed since the last "
        f"release. Then record PROJECTION = ({index.SCHEMA}, {found!r})."
    )
