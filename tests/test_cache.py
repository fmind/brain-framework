"""The search cache: incremental refreshes match a full rebuild, survive bf.yaml edits and bound their work."""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from bf import index, records, retrieve, storage
from bf.cli import app
from bf.models import Config, Error, Query, Record
from bf.retrieve import read, search
from bf.storage import BusyError, Store, building, writer
from bf.validate import validate
from conftest import records_file

FIELDS = b"""version: 7
name: fixture
sensors: {}
fields:
  owner:
    description: Who answers for the work.
    type: identity
    relation: true
  stage:
    description: Delivery stage.
    type: string
    cardinality: one
"""
REVIEWER = b"""  reviewer:
    description: Who checks the work.
    type: identity
    relation: true
"""
# Documentation, `broader`, `targets` and sensors leave the cached rows as they are: no rebuild follows.
REWORDED = b"""version: 7
name: fixture
sensors:
  tickets:
    command: [true-command]
    refresh: 0
fields:
  owner:
    description: The accountable owner.
    type: identity
    relation: true
    examples: [person:ada]
  stage:
    description: Delivery stage.
    type: string
    cardinality: one
  reviewer:
    description: Who reviews the work.
    type: identity
    relation: true
    broader: owner
    targets: ["person:"]
"""
ATLAS = b"""---
type: project
status: draft
updated: 2026-09-10
tags: [atlas, retention]
aliases: ["project:atlas"]
fields:
  owner: person:ada
  stage: build
---

# Atlas

Atlas keeps zircon evidence offline: see [evidence](../concepts/evidence.md) and [the decision](meetings:decision-1).

## Budget

The atlas budget review needs [Ada](bf://fixture/concepts/ada.md?rel=owner).

## Next actions

- [ ] Draft the atlas budget.
- [x] Collect zircon samples.
"""
QUERIES = ("atlas budget", "zircon", "offline retrieval", "person:ada", "project:atlas", "repo:example/project")
PAGES = (
    "projects",
    "tasks",
    "tags",
    "memories/tickets",
    "2026-09",
    "projects/offline.md",
    "meetings:decision-1",
    "person:ada",
)


def refs(store: Store, text: str) -> list[str]:
    items = search([store], Query(text=text), counted=False)["items"]
    return [str(item["ref"]) for item in cast("list[dict[str, object]]", items)]


def rows(store: Store) -> dict[str, object]:
    """Every cache row, keyed by refs: an incremental refresh numbers new items and passages after the existing ones.

    File fingerprints are left out, since a copied brain's files have their own.
    """
    owned = {
        "tasks": "SELECT item,line,fragment,text,done FROM tasks",
        "names": "SELECT item,name FROM names",
        "links": "SELECT item,target FROM links",
        "tags": "SELECT item,target FROM tags",
        "edges": "SELECT item,subject,relation,target,origin FROM edges",
    }
    with closing(sqlite3.connect(store.root / index.CACHE)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # FTS5 checks that its full-text index still matches the rows incremental deletes and inserts left.
        connection.execute("INSERT INTO search(search) VALUES('integrity-check')")
        item = dict(connection.execute("SELECT id,ref FROM items").fetchall())
        passage = {
            key: (item.get(owner, f"orphan {owner}"), fragment)
            for key, owner, fragment in connection.execute("SELECT id,item,fragment FROM passages")
        }
        columns = "ref,path,kind,source,title,time,type,status,lead,url,updated,observed,partial,tasks_open,tasks_done"
        return {
            "signature": connection.execute("SELECT signature FROM ontology").fetchall(),
            "files": sorted(connection.execute("SELECT path,error FROM files")),
            "items": sorted(connection.execute(f"SELECT {columns},next,weight,stale_after,fields FROM items")),  # noqa: S608 - fixed columns
            "passages": sorted(
                (passage[key], title, original)
                for key, title, original in connection.execute("SELECT id,title,original FROM passages")
            ),
            "search": sorted(
                (passage.get(key, (f"orphan {key}", "")), *text)
                for key, *text in connection.execute("SELECT rowid,title,text,names,context FROM search")
            ),
            **{
                table: sorted((item.get(owner, f"orphan {owner}"), *rest) for owner, *rest in connection.execute(sql))
                for table, sql in owned.items()
            },
        }


def snapshot(store: Store) -> dict[str, object]:
    """What search, read and export answer from a brain's cache, and the rows they answer from."""
    answers: dict[str, object] = {}
    for text in QUERIES:
        answers[f"search {text}"] = search([store], Query(text=text, limit=50), counted=False)
    for ref, relation in [*((page, "") for page in PAGES), ("person:ada", "owner")]:
        try:
            answers[f"read {ref} {relation}"] = read([store], ref, rel=relation, counted=False)
        except Error as error:
            answers[f"read {ref} {relation}"] = str(error)
    with ExitStack() as stack:
        for kind, export in (("edges", retrieve.edges), ("identities", retrieve.identities)):
            stream, extra = export([store], stack)
            answers[f"export {kind}"] = [list(stream), extra]
    return {**answers, **rows(store)}


def test_incremental_refreshes_match_a_full_rebuild(
    brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    fills: list[tuple[bool, int, int]] = []
    filling = index._fill  # noqa: SLF001 - observe what each refresh indexes

    def observed(
        store: Store,
        connection: sqlite3.Connection,
        config: Config,
        *,
        new: bool,
        compared: tuple[index.Fingerprints, index.Fingerprints] | None = None,
    ) -> dict[str, object]:
        result = filling(store, connection, config, new=new, compared=compared)
        if store.root == brain.root:
            fills.append((new, cast("int", result["changed"]), cast("int", result["removed"])))
        return result

    monkeypatch.setattr(index, "_fill", observed)

    def check(step: str, *refreshes: tuple[bool, int, int]) -> None:
        """The brain's own cache, refreshed by its first search, answers like a full build of a copy."""
        fills.clear()
        incremental = snapshot(brain)
        assert fills == list(refreshes), step
        copy = tmp_path / f"rebuilt {step}"
        expected = snapshot(Store(shutil.copytree(brain.root, copy, ignore=shutil.ignore_patterns(".bf"))))
        assert incremental.keys() == expected.keys()
        for key, value in expected.items():
            assert incremental[key] == value, f"{step}: {key}"

    brain.write("bf.yaml", FIELDS)
    check("declare fields", (True, 4, 0))
    brain.write("projects/atlas.md", ATLAS)
    brain.write(
        "concepts/ada.md", b"---\ntype: concept\nentity: person:ada\n---\n# Ada\n\nAda owns the atlas budget.\n"
    )
    records_file(
        brain,
        "tickets",
        [
            Record(
                id="T-1",
                title="Atlas budget",
                text="Budget review for zircon.",
                time="2026-09-12T09:00:00Z",
                url="https://tickets.example/T-1",
                links=["project:atlas"],
                aliases=["jira:T-1"],
                fields={"owner": "person:bob", "stage": "review"},
            ),
            Record(
                id="T-2",
                title="Atlas samples",
                text="Zircon samples arrived.",
                time="2026-09-14T09:00:00Z",
                links=["bf://fixture/projects/atlas.md#budget"],
                fields={"stage": "done"},
            ),
        ],
    )
    check("add notes and records", (False, 4, 0))
    edited = ATLAS.replace(b"# Atlas\n", b"# Atlas program\n").replace(b"[ ] Draft", b"[x] Draft")
    edited = edited.replace(b"owner: person:ada", b"owner: person:carol").replace(b"[atlas, retention]", b"[atlas]")
    brain.write("projects/atlas.md", edited.replace(edited[edited.index(b"## Budget") : edited.index(b"## Next")], b""))
    brain.write(
        "concepts/evidence.md", b"---\ntype: concept\nstatus: stable\n---\n\n# Durable evidence\n\nZircon audits.\n"
    )
    check("edit notes", (False, 2, 0))
    records_file(
        brain,
        "tickets",
        [
            Record(
                id="T-1",
                title="Atlas budget approved",
                time="2026-09-15T09:00:00Z",
                links=["project:atlas", "meetings:decision-1"],
                fields={"owner": "person:ada", "stage": "done"},
            )
        ],
    )
    brain.delete(records.path("tickets", "T-2"))
    brain.delete(records.path("meetings", "lunch"))
    check("edit and remove records", (False, 1, 2))
    brain.write("projects/broken.md", b"---\nstale_after: soon\n---\n# Broken zircon\n")
    brain.write(records.path("tickets", "T-3"), b"{broken")
    check("break files", (False, 2, 0))
    brain.write("projects/broken.md", b"---\ntype: project\nstatus: draft\n---\n# Repaired zircon\n\nRepaired.\n")
    records_file(brain, "tickets", [Record(id="T-3", title="Zircon follow-up", time="2026-09-16T09:00:00Z")])
    check("repair files", (False, 2, 0))
    brain.write("bf.yaml", FIELDS + REVIEWER)
    check("declare another relation", (True, 8, 0))
    brain.write("bf.yaml", REWORDED)
    brain.write(
        "projects/review.md", b"---\ntype: project\nfields:\n  reviewer: person:dan\n---\n# Review\n\nZircon.\n"
    )
    check("reword documentation, broader and targets", (False, 1, 0))
    moved = brain.read("projects/atlas.md")
    brain.delete("projects/atlas.md")
    brain.write("projects/atlas-program.md", moved)
    # A same-size edit that keeps the modification time: the change time still differs.
    ada = brain.root / "concepts/ada.md"
    before = ada.stat()
    ada.write_bytes(ada.read_bytes().replace(b"Ada owns", b"Ada runs"))
    os.utime(ada, ns=(before.st_atime_ns, before.st_mtime_ns))
    check("move and touch notes", (False, 2, 1))


def test_bf_yaml_broken_during_a_refresh_never_skips_the_files_it_indexes(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    original = brain.read("bf.yaml")
    for number in range(6):
        brain.write(f"concepts/{number}.md", f"---\ntype: concept\n---\n# Note {number}\n\nZircon.\n".encode())
    indexing, calls = index._index, 0  # noqa: SLF001 - edit bf.yaml while files are being indexed

    def editing(connection: sqlite3.Connection, store: Store, path: str, config: Config) -> None:
        nonlocal calls
        calls += 1
        # An editor autosaves a half-typed line, then the owner finishes the edit.
        if calls == 2:
            brain.write("bf.yaml", original + b"fields: [unfinished\n")
        elif calls == 5:
            brain.write("bf.yaml", original)
        indexing(connection, store, path, config)

    with monkeypatch.context() as patch:
        patch.setattr(index, "_index", editing)
        # Loaded per file, the broken bf.yaml left each file indexed meanwhile skipped as `invalid YAML` until bf build.
        assert index.refresh(brain) == {"files": 10, "changed": 6, "removed": 0, "skipped": 0}
    assert calls == 6
    assert index.refresh(brain)["changed"] == 0
    assert len(refs(brain, "zircon")) == 6
    assert index.status(brain)["problems"] == []
    assert validate(brain)["valid"]


def test_bf_yaml_changed_during_a_refresh_rebuilds_at_the_next_one(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    brain.write("concepts/owned.md", b"---\ntype: concept\nfields:\n  owner: person:ada\n---\n# Owned\n")
    indexing = index._index  # noqa: SLF001 - declare the relation while the note is indexed

    def declaring(connection: sqlite3.Connection, store: Store, path: str, config: Config) -> None:
        brain.write("bf.yaml", FIELDS)
        indexing(connection, store, path, config)

    with monkeypatch.context() as patch:
        patch.setattr(index, "_index", declaring)
        assert index.refresh(brain)["skipped"] == 0
    # The generation keeps the configuration it was indexed with; the next refresh sees the new one and rebuilds.
    assert index.refresh(brain) == {"files": 5, "changed": 5, "removed": 0, "skipped": 0}
    backlinks = cast("list[dict[str, object]]", read([brain], "person:ada", counted=False)["backlinks"])
    assert [(group["relation"], [i["ref"] for i in cast("list[dict]", group["items"])]) for group in backlinks] == [
        ("owner", ["concepts/owned.md"])
    ]


def test_a_search_or_status_scans_the_brain_once_even_when_it_refreshes(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    scans: list[Path] = []
    scanning = index.inputs

    def counted(store: Store, counts: dict[str, int] | None = None) -> index.Fingerprints:
        scans.append(store.root)
        return scanning(store, counts)

    monkeypatch.setattr(index, "inputs", counted)
    brain.write("concepts/late.md", b"# Late\n\nOsmium evidence.\n")
    # The freshness check's scan indexes the changed file instead of a second scan.
    assert refs(brain, "osmium") == ["concepts/late.md"]
    assert scans == [brain.root]
    # Status also counts each scanned tree's entries from it.
    brain.write("concepts/later.md", b"# Later\n\nIridium evidence.\n")
    scans.clear()
    monkeypatch.setattr(index, "CROWDED", 1)
    report = index.status(brain)
    assert (report["cache"], scans) == ("ready", [brain.root])
    assert cast("list[dict[str, object]]", report["warnings"])[0]["directory"] == "concepts"
    assert refs(brain, "iridium") == ["concepts/later.md"]


def test_a_scan_compared_before_another_refresh_committed_is_scanned_again(brain: Store) -> None:
    index.refresh(brain)
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        known = index._known(connection)  # noqa: SLF001 - the files table a freshness check compared
    brain.write("concepts/evidence.md", b"---\ntype: concept\n---\n# Durable evidence\n\nEdited.\n")
    scanned = index.inputs(brain)
    # Before that check's refresh takes the writer lock, another one, as by bf update, indexes a newer note.
    brain.write("concepts/newer.md", b"# Newer\n\nOsmium.\n")
    assert index.refresh(brain)["changed"] == 2
    # The cache no longer holds what the check compared: reusing its scan would drop the newer note as removed.
    assert index.refresh(brain, compared=(known, scanned)) == {"files": 5, "changed": 0, "removed": 0, "skipped": 0}
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        assert connection.execute("SELECT count(*) FROM items WHERE ref='concepts/newer.md'").fetchone() == (1,)


def test_reads_remove_the_file_a_killed_build_left_once_no_build_holds_it(brain: Store) -> None:
    index.refresh(brain)
    before = sorted(path.name for path in (brain.root / ".bf").iterdir())
    leftovers = [brain.root / (index.BUILD + suffix) for suffix in ("", "-journal")]
    for path in leftovers:
        path.write_bytes(b"abandoned build")
    # A running build holds the build lock: its file stays while searches keep answering.
    with building(brain):
        assert refs(brain, "offline retrieval")[0] == "projects/offline.md"
        assert all(path.exists() for path in leftovers)
    # Once no build holds it, a read removes the leftover, as large as the cache, instead of the next bf build.
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"
    assert sorted(path.name for path in (brain.root / ".bf").iterdir()) == before
    assert index.status(brain)["cache"] == "ready"


def test_long_problems_are_cut_so_replies_stay_within_their_limit(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One path is itself longer than a problem may be: its reason must survive the cut.
    deep = "projects/" + "/".join(["d" * 200] * 6) + "/deep.md"
    hostile = sorted([deep, *(f"projects/hostile{number}.md" for number in range(4))])
    for path in hostile:
        brain.write(path, b"# Hostile\n")
    indexing = index._index  # noqa: SLF001 - some parse errors name every invalid value

    def failing(connection: sqlite3.Connection, store: Store, path: str, config: Config) -> None:
        if path in hostile:
            # About 1 MB each, as for thousands of malformed aliases: five would exceed a reply's 4 MiB.
            raise Error(f"{path}: aliases: " + "é is invalid; " * 70_000)
        indexing(connection, store, path, config)

    monkeypatch.setattr(index, "_index", failing)
    reply = search([brain], Query(text="offline"), counted=False)
    assert cast("list[dict[str, object]]", reply["items"])[0]["ref"] == "projects/offline.md"
    problems = cast("list[dict[str, str]]", reply["problems"])
    assert [problem["file"] for problem in problems] == hostile
    for problem in problems:
        assert problem["error"].startswith("aliases: é is invalid; ")
        assert problem["error"].endswith("…")
        assert len(problem["error"].encode()) <= 1024
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        stored = [error for (error,) in connection.execute("SELECT error FROM files WHERE error!=''")]
    assert len(stored) == 5
    assert all(error.startswith("aliases: ") and len(error.encode()) <= 1024 for error in stored)
    # A cut never splits a character, and a short message stays whole.
    assert index._capped("é" * 1024) == "é" * 510 + "…"  # noqa: SLF001
    assert index._capped("x" * 1024) == "x" * 1024  # noqa: SLF001


def test_a_build_commits_its_schema_rows_and_journal_mode_in_three_transactions(brain: Store) -> None:
    assert index.refresh(brain, full=True)["changed"] == 4
    # SQLite's header counts the rollback-journal transactions that wrote the file: each schema statement was one.
    header = (brain.root / index.CACHE).read_bytes()[:100]
    assert header.startswith(b"SQLite format 3\x00")
    assert int.from_bytes(header[24:28], "big") <= 3


def test_an_sqlite_too_old_for_the_cache_is_named_instead_of_a_full_disk(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index._supported.cache_clear()  # noqa: SLF001 - a passing check is kept for the process
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 34, 1))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.34.1")
    expected = r"needs SQLite 3\.35\.0 or newer with FTS5 and JSON functions, which this Python's SQLite 3\.34\.1 lacks"
    with pytest.raises(Error, match=expected):
        index.refresh(brain, full=True)
    result = CliRunner().invoke(app, ["status", "--brain", str(brain.root)])
    assert result.exit_code == 1
    assert "uv tool install --reinstall --managed-python" in str(result.exception)
    assert not (brain.root / index.CACHE).exists()
    assert not (brain.root / index.BUILD).exists()


def test_an_sqlite_build_without_cache_features_is_named(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    index.refresh(brain)
    index._supported.cache_clear()  # noqa: SLF001 - a passing check is kept for the process
    # A context, not undo(), which would also restore the real HOME and XDG directories the suite isolates.
    with monkeypatch.context() as patch:
        # As a build without FTS5 or JSON functions answers the probe; the version alone would pass.
        patch.setattr(index, "_PROBE", "SELECT value FROM absent_table_function('[1]')")
        with pytest.raises(Error, match=r"needs SQLite .* which this Python's SQLite \S+ lacks"):
            search([brain], Query(text="offline"), counted=False)
    # A failed check is not kept: the next connection checks again.
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"


def test_a_transient_read_failure_is_retried_on_the_next_refresh(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write("projects/atlas.md", b"# Atlas\n\nBilling migration.\n")
    index.refresh(brain, full=True)
    os.utime(brain.root / "projects/atlas.md", (1, 1))
    original = Store.read

    def failing(self: Store, name: str, *limit: int) -> bytes:
        if name == "projects/atlas.md":
            raise OSError(errno.EIO, "Input/output error")
        return original(self, name, *limit)

    # An I/O error on a network mount leaves the file unchanged: the failure used to stay cached until it changed.
    monkeypatch.setattr(Store, "read", failing)
    assert index.refresh(brain)["skipped"] == 1
    monkeypatch.setattr(Store, "read", original)
    assert index.refresh(brain)["skipped"] == 0
    assert [item["ref"] for item in cast("list[dict]", search([brain], Query(text="billing"))["items"])] == [
        "projects/atlas.md"
    ]


def test_a_cache_another_program_locks_names_the_lock(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    index.refresh(brain, full=True)
    brain.write("projects/new.md", b"# New\n")
    with closing(sqlite3.connect(brain.root / ".bf/index.sqlite", timeout=0)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        # Without SQLite's 30-second busy wait: it used to end in advice to check free space and run bf build.
        connect = sqlite3.connect

        def impatient(database: Path, timeout: float = 30) -> sqlite3.Connection:
            return connect(database, timeout=min(timeout, 0))

        monkeypatch.setattr(sqlite3, "connect", impatient)
        with pytest.raises(Error, match="another process holds the search cache; close it and retry"):
            index.refresh(brain, wait=0)


def plant(store: Store, *statements: str) -> None:
    """Add rows no brain file supports, as a cloned or extracted cache could carry, and optional schema objects."""
    with closing(sqlite3.connect(store.root / index.CACHE)) as connection, connection:
        connection.execute(
            "INSERT INTO items(id,ref,path,kind,source,title,time,type,status,lead,url,updated,observed,partial,"
            "tasks_open,tasks_done,next,weight,stale_after,fields) VALUES(999,'projects/roadmap.md',"
            "'projects/roadmap.md','note','','Roadmap','','project','','planted','','','',0,0,0,'',1,'','')"
        )
        connection.execute("INSERT INTO passages(id,item,fragment,title,original) VALUES(999,999,'','Roadmap','')")
        connection.execute("INSERT INTO search(rowid,title,text,names) VALUES(999,'roadmap','zanzibar planted','')")
        for statement in statements:
            connection.execute(statement)


def cache_file(store: Store) -> str:
    with closing(sqlite3.connect(store.root / index.CACHE)) as connection:
        return connection.execute("SELECT file FROM ontology").fetchone()[0]


def test_cache_follows_edits_additions_removals_and_touches(brain: Store) -> None:
    assert refs(brain, "zirconium") == []
    brain.write("concepts/new.md", b"---\ntype: concept\n---\n# New\n\nZirconium matters.\n")
    assert refs(brain, "zirconium") == ["concepts/new.md"]
    stat = (brain.root / "concepts/new.md").stat()
    # A same-size edit that preserves mtime is still noticed through ctime.
    with (brain.root / "concepts/new.md").open("r+b") as stream:
        stream.write(b"---\ntype: concept\n---\n# New\n\nHafniums matters.\n")
    os.utime(brain.root / "concepts/new.md", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert refs(brain, "hafniums") == ["concepts/new.md"]
    (brain.root / "concepts/new.md").unlink()
    assert refs(brain, "hafniums") == []
    result = index.refresh(brain)
    assert result["changed"] == 0
    assert index.refresh(brain, full=True)["changed"] == 4


def test_full_build_creates_every_index_after_loading_and_serves_in_wal_mode(brain: Store) -> None:
    index.refresh(brain, full=True)
    brain.write("concepts/late.md", b"# Late\n\nOsmium evidence.\n")
    assert refs(brain, "osmium") == ["concepts/late.md"]
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        created = {row[0] for row in connection.execute("SELECT sql FROM sqlite_schema WHERE type='index'") if row[0]}
    assert created == set(index._INDEXES)  # noqa: SLF001 - deferred secondary indexes


def test_replies_find_skipped_files_through_their_own_index(brain: Store) -> None:
    brain.write("projects/broken.md", b"---\nstale_after: soon\n---\n# Broken\n")
    index.refresh(brain, full=True)
    statements: list[str] = []
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        connection.row_factory = sqlite3.Row
        connection.set_trace_callback(statements.append)
        assert [problem["file"] for problem in index.problems(connection)] == ["projects/broken.md"]
        assert retrieve._complete(connection, "ready", "meetings")  # noqa: SLF001 - an exact record read's check
        connection.set_trace_callback(None)
        # Every reply checks for skipped files: only their rows are read, not the row of every file.
        assert len(statements) == 2
        for statement in statements:
            plan = " ".join(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}"))
            assert "files_problems" in plan, plan


def test_one_reply_compares_files_with_the_cache_once_per_brain(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[Path] = []
    original = index.fresh

    def counted(store: Store, counts: dict[str, int] | None = None) -> str:
        checked.append(store.root)
        return original(store, counts)

    monkeypatch.setattr(index, "fresh", counted)
    # A note with backlinks opens the cache for identities, backlinks and claims.
    read([brain], "projects/offline.md")
    assert checked == [brain.root]
    search([brain], Query(text="offline"))
    assert checked == [brain.root, brain.root]


def test_outdated_or_corrupt_cache_is_rebuilt(brain: Store) -> None:
    refs(brain, "offline")
    cache = brain.root / index.CACHE
    with sqlite3.connect(cache) as connection:
        connection.execute("PRAGMA user_version=1")
    connection.close()
    assert refs(brain, "offline")[0] == "projects/offline.md"
    for suffix in ("-wal", "-shm"):
        cache.with_name(cache.name + suffix).unlink(missing_ok=True)
    cache.write_bytes(b"not a database")
    assert refs(brain, "offline")[0] == "projects/offline.md"
    cache.unlink()
    (brain.root / ".bf/index.sqlite").symlink_to(brain.root / "bf.yaml")
    with pytest.raises(Error, match="regular file"):
        refs(brain, "offline")


def test_a_cache_copied_with_its_brain_is_rebuilt_instead_of_trusted(brain: Store, tmp_path: Path) -> None:
    # A copy, clone or archive can carry a cache whose rows no brain file supports; only its file identity differs.
    refs(brain, "offline")
    plant(brain)
    copy = Store(shutil.copytree(brain.root, tmp_path / "copy", symlinks=True))
    assert refs(copy, "zanzibar") == []
    assert refs(copy, "offline")[0] == "projects/offline.md"
    assert cache_file(copy) != cache_file(brain)


@pytest.mark.parametrize(
    "statement",
    ["CREATE TRIGGER planted AFTER INSERT ON files BEGIN SELECT 1; END", "CREATE VIEW planted AS SELECT 1"],
    ids=["trigger", "view"],
)
def test_a_cache_holding_triggers_or_views_is_rebuilt_in_place(brain: Store, statement: str) -> None:
    # bf never creates them: a trigger could add rows on refresh that no brain file supports.
    refs(brain, "offline")
    plant(brain, statement)
    assert refs(brain, "zanzibar") == []
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        found = connection.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()
    assert found == (0,)


def test_a_remount_that_changes_the_device_number_keeps_the_cache(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # btrfs subvolumes, NFS and FUSE mounts can get another device number after a remount or reboot.
    refs(brain, "offline")
    cache, lstat = brain.root / index.CACHE, Path.lstat

    def remounted(self: Path) -> os.stat_result:
        info = lstat(self)
        if self != cache:
            return info
        values = list(info)
        values[2] += 1  # st_dev
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", remounted)
    assert index.refresh(brain)["changed"] == 0


def test_a_busy_writer_serves_the_current_cache_as_stale(brain: Store) -> None:
    refs(brain, "offline")
    brain.write("concepts/late.md", b"# Late\n\nLatecomer.\n")
    with writer(brain):
        reply = search([brain], Query(text="latecomer"))
    # The stale cache does not hold the word yet: `stale` marks that answer, and its unmatched word, as outdated.
    assert reply == {
        "items": [],
        "notice": reply["notice"],
        "stale": ["fixture"],
        "sources": reply["sources"],
        "unmatched": ["latecomer"],
    }
    assert cast(list[dict], reply["sources"])[0]["source"] == "meetings"
    assert refs(brain, "latecomer") == ["concepts/late.md"]


def test_a_missing_cache_behind_a_busy_writer_fails_after_one_wait(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        (brain.root / (index.CACHE + suffix)).unlink(missing_ok=True)
    waits: list[float] = []

    def busy(_store: Store, *, wait: float = 30, **_options: object) -> dict[str, object]:
        waits.append(wait)
        raise BusyError("another writer holds the brain")

    monkeypatch.setattr(index, "refresh", busy)
    # With nothing to serve, the read waits once for the writer, then names it instead of retrying the wait.
    with pytest.raises(BusyError, match="another writer is building the search cache"), index.database(brain):
        pass
    assert waits == [120]


@pytest.mark.parametrize("outdated", [False, True])
def test_concurrent_first_search_waits_for_a_complete_cache(
    brain: Store, monkeypatch: pytest.MonkeyPatch, outdated: bool
) -> None:
    if outdated:
        index.refresh(brain)
        with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
            connection.execute(f"PRAGMA user_version={index.SCHEMA - 1}")
    ingest_started, release_ingest, reader_refreshing = Event(), Event(), Event()
    original_index, original_refresh = index._index, index.refresh  # noqa: SLF001 - coordinated ingestion failure boundary
    waits: list[float] = []

    def paused(connection: sqlite3.Connection, store: Store, path: str, config: Config) -> None:
        ingest_started.set()
        assert release_ingest.wait(10)
        return original_index(connection, store, path, config)

    def observe_refresh(store: Store, *, wait: float = 30, **options: Any) -> dict[str, object]:
        waits.append(wait)
        reader_refreshing.set()
        return original_refresh(store, wait=wait, **options)

    monkeypatch.setattr(index, "_index", paused)
    monkeypatch.setattr(index, "refresh", observe_refresh)
    with ThreadPoolExecutor(max_workers=2) as executor:
        build = executor.submit(original_refresh, brain)
        try:
            assert ingest_started.wait(10)
            result = executor.submit(search, [brain], Query(text="offline retrieval"), counted=False)
            assert reader_refreshing.wait(10)
            assert waits == [120]  # Incomplete generations cannot be served as stale evidence.
        finally:
            release_ingest.set()
        assert build.result(timeout=10)["files"] == 4
        reply = result.result(timeout=10)
    assert "stale" not in reply
    assert [item["ref"] for item in cast("list[dict[str, object]]", reply["items"])][:2] == [
        "projects/offline.md",
        "meetings:decision-1",
    ]


def test_failed_or_abandoned_full_rebuilds_keep_the_live_cache(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    index.refresh(brain)
    live = cache_file(brain)
    original_index = index._index  # noqa: SLF001 - inject failure after a file was indexed
    indexed: list[str] = []

    def fail_second(connection: sqlite3.Connection, store: Store, path: str, config: Config) -> None:
        if indexed:
            raise sqlite3.OperationalError("simulated disk full")
        indexed.append(path)
        return original_index(connection, store, path, config)

    with monkeypatch.context() as patch:
        patch.setattr(index, "_index", fail_second)
        with pytest.raises(Error, match="check free space"):
            index.refresh(brain, full=True)
    assert len(indexed) == 1
    assert not (brain.root / index.BUILD).exists()
    assert cache_file(brain) == live
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == index.SCHEMA
        assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 4
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"
    # A killed build leaves its partial file and journal beside the cache; the next build replaces them. A read
    # would remove them first (tests/test_cache.py), so the build runs next.
    for suffix in ("", "-journal"):
        (brain.root / (index.BUILD + suffix)).write_bytes(b"abandoned build")
    assert index.refresh(brain, full=True)["files"] == 4
    assert sorted(path.name for path in (brain.root / ".bf").iterdir()) == ["index.sqlite"]
    assert cache_file(brain) != live


def test_a_full_build_keeps_serving_and_refreshing_the_live_cache_until_it_publishes(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    live = cache_file(brain)
    ingesting, finish = Event(), Event()
    original_index = index._index  # noqa: SLF001 - pause the build after its scan

    def paused(connection: sqlite3.Connection, store: Store, path: str, config: Config) -> None:
        if not ingesting.is_set():
            ingesting.set()
            assert finish.wait(10)
        return original_index(connection, store, path, config)

    monkeypatch.setattr(index, "_index", paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        build = executor.submit(index.refresh, brain, full=True)
        try:
            assert ingesting.wait(10)
            # The build holds no writer lock: a collection commits while searches read and refresh the live cache.
            with writer(brain):
                brain.write("concepts/late.md", b"# Late\n\nOsmium evidence.\n")
                brain.delete(records.path("meetings", "lunch"))
            assert refs(brain, "osmium") == ["concepts/late.md"]
            assert cache_file(brain) == live
        finally:
            finish.set()
        # The build fingerprinted its files before the commit: the removed record is neither indexed nor skipped.
        assert build.result(timeout=10) == {"files": 4, "changed": 4, "removed": 0, "skipped": 0}
    assert cache_file(brain) != live
    # The file added after the build's scan has no fingerprint in the new generation: the next refresh indexes it.
    reply = search([brain], Query(text="osmium lunch"), counted=False)
    assert [item["ref"] for item in cast("list[dict[str, object]]", reply["items"])] == ["concepts/late.md"]
    assert "problems" not in reply


def test_a_build_publishes_after_readers_close_the_generation_it_replaces(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    waiting = Event()

    def sleep(seconds: float) -> None:
        waiting.set()
        time.sleep(seconds)

    # The publish waits in the generation lock that readers hold while connected.
    monkeypatch.setattr(storage, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    with ThreadPoolExecutor(max_workers=1) as executor:
        with index.database(brain) as (connection, _state):
            build = executor.submit(index.refresh, brain, full=True)
            # SQLite finds -wal and -shm files by name: no reader of the old generation may outlive the rename.
            assert waiting.wait(10)
            assert not build.done()
            assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 4
        assert build.result(timeout=10)["files"] == 4
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"
    # A reader that outlasts the wait fails the build visibly; the live cache stays in service.
    live = cache_file(brain)
    monkeypatch.setattr(index, "_PUBLISH", 0)
    with index.database(brain) as (connection, _state), pytest.raises(BusyError, match="readers kept"):
        index.refresh(brain, full=True)
    assert not (brain.root / index.BUILD).exists()
    assert cache_file(brain) == live
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"


def test_rewording_schema_documentation_keeps_the_cache(brain: Store) -> None:
    schema = b"version: 7\nname: fixture\nfields:\n  owner:\n    description: Who owns it.\n    type: identity\n"
    brain.write("bf.yaml", schema + b"    relation: true\n")
    index.refresh(brain)
    generation = cache_file(brain)
    reworded = schema.replace(b"Who owns it.", b"The accountable owner.")
    brain.write("bf.yaml", reworded + b"    relation: true\n    examples: [person:ada]\n")
    assert index.refresh(brain)["changed"] == 0
    assert cache_file(brain) == generation
    # A structural change still rebuilds: edges follow relation fields.
    brain.write("bf.yaml", reworded)
    assert index.refresh(brain)["changed"] == 4
    assert cache_file(brain) != generation


def test_live_pending_commit_serves_stale_cache_but_abandoned_commit_errors(brain: Store) -> None:
    expected = refs(brain, "offline retrieval")
    with writer(brain):
        brain.write("memories/.pending/0.before", b"staged original")
        reply = search([brain], Query(text="offline retrieval"), counted=False)
    assert reply["stale"] == ["fixture"]
    assert [item["ref"] for item in cast("list[dict[str, object]]", reply["items"])] == expected
    with pytest.raises(Error, match="interrupted transaction"):
        search([brain], Query(text="offline retrieval"), counted=False)
    assert brain.read("memories/.pending/0.before") == b"staged original"


def test_current_schema_cache_with_missing_table_rebuilds(brain: Store) -> None:
    refs(brain, "offline")
    with sqlite3.connect(brain.root / index.CACHE) as connection:
        connection.execute("DROP TABLE files")
    connection.close()
    assert refs(brain, "offline")[0] == "projects/offline.md"
    assert read([brain], "meetings:decision-1")["ref"] == "meetings:decision-1"


def test_direct_record_read_survives_unavailable_cache(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_store: Store, _counts: dict[str, int] | None = None) -> str:
        raise Error("cache unavailable")

    monkeypatch.setattr(index, "fresh", unavailable)
    brain.write(
        "memories/meetings/11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437.json",
        b"malformed unrelated newer record\n",
    )
    assert read([brain], "meetings:decision-1")["ref"] == "meetings:decision-1"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_cache_sidecars_cannot_redirect_reads(brain: Store, suffix: str) -> None:
    cache = brain.root / index.CACHE
    cache.parent.mkdir()
    cache.with_name(cache.name + suffix).symlink_to(brain.root / "bf.yaml")
    before = brain.read("bf.yaml")
    with pytest.raises(Error, match="regular file"):
        refs(brain, "offline")
    assert brain.read("bf.yaml") == before


def test_cache_write_failure_is_a_safe_actionable_error(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_store: Store, _config: Config) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(index, "_create", fail)
    with pytest.raises(Error, match="check free space") as raised:
        index.refresh(brain)
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)


def test_a_search_opens_a_generation_published_after_its_freshness_check(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    live = cache_file(brain)
    checked, reopen = Event(), Event()
    original_fresh = index.fresh

    def paused_fresh(store: Store, counts: dict[str, int] | None = None) -> str:
        state = original_fresh(store, counts)
        checked.set()
        assert reopen.wait(10)
        return state

    monkeypatch.setattr(index, "fresh", paused_fresh)
    with ThreadPoolExecutor(max_workers=1) as executor:
        query = executor.submit(search, [brain], Query(text="offline"), counted=False)
        try:
            assert checked.wait(10)
            # A checked reader holds no connection yet, so the build publishes without waiting for it.
            assert index.refresh(brain, full=True)["files"] == 4
        finally:
            reopen.set()
        reply = query.result(timeout=10)
    assert cache_file(brain) != live
    assert isinstance(reply["items"], list)
    assert reply["items"][0]["ref"] == "projects/offline.md"
    assert "problems" not in reply


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_read_only_brains_name_the_cache_they_need(brain: Store) -> None:
    index.refresh(brain)
    folders = [brain.root / ".bf", brain.root]
    for folder in folders:
        folder.chmod(0o500)
    try:
        with pytest.raises(Error, match=r"write access to the brain.s \.bf cache"):
            search([brain], Query(text="offline"))
    finally:
        for folder in folders:
            folder.chmod(0o700)


def test_hashed_inode_numbers_beyond_signed_64_bits_still_index(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    original = storage._fingerprint  # noqa: SLF001 - the one place every fingerprint passes through

    def hashed(info: os.stat_result) -> tuple[int, int, int, int]:
        # mergerfs and some FUSE mounts report inode numbers with the top bit set.
        return original(
            cast(
                "os.stat_result",
                SimpleNamespace(
                    st_size=info.st_size,
                    st_mtime_ns=info.st_mtime_ns,
                    st_ctime_ns=info.st_ctime_ns,
                    st_ino=info.st_ino | 1 << 63,
                ),
            )
        )

    monkeypatch.setattr(storage, "_fingerprint", hashed)
    # Before 17 SQLite rejected the inode and every refresh failed with a traceback.
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"


def test_the_cache_directory_must_be_your_own_directory(
    brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = brain.root / ".bf"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o755)
    cache.symlink_to(elsewhere)
    with pytest.raises(Error, match=r"^\.bf: expected a directory"):
        search([brain], Query(text="offline"))
    # The check never follows the link, so it tightens nothing elsewhere.
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755
    cache.unlink()
    cache.write_bytes(b"not a directory")
    with pytest.raises(Error, match=r"^\.bf: expected a directory"):
        search([brain], Query(text="offline"))
    cache.unlink()
    cache.mkdir()
    monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 1)
    with pytest.raises(Error, match="belongs to another user"):
        search([brain], Query(text="offline"))


def test_the_cache_is_private_and_distrusts_its_own_schema(brain: Store) -> None:
    (brain.root / ".bf").mkdir()
    (brain.root / ".bf").chmod(0o755)
    refs(brain, "offline")
    assert stat.S_IMODE((brain.root / ".bf").stat().st_mode) == 0o700
    assert stat.S_IMODE((brain.root / index.CACHE).stat().st_mode) == 0o600
    with index.database(brain) as (connection, _state):
        assert not connection.getconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA)
        assert connection.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE)
