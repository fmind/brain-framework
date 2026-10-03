"""The search cache: incremental refreshes match a full rebuild, survive bf.yaml edits and bound their work."""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from bf import index, records, retrieve
from bf.cli import app
from bf.models import Config, Error, Query, Record
from bf.retrieve import read, search
from bf.storage import Store, building
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
