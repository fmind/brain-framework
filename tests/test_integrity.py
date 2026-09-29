"""Failures found by reviewing durability and incomplete-result boundaries."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from bf import index, records
from bf.cli import app
from bf.evaluate import evaluate
from bf.models import Error, Query, Record
from bf.retrieve import read, search
from bf.storage import BusyError, Store, writer
from bf.update import update
from bf.validate import validate

DECISION = records.path("meetings", "decision-1")


def _cross_device(*_: object, **__: object) -> None:
    raise OSError(errno.EXDEV, "cross-device link")


def test_health_check_passes_while_a_live_writer_holds_the_brain(brain: Store) -> None:
    index.refresh(brain)
    brain.write("concepts/late.md", b"# Late\n\nLatecomer.\n")
    with writer(brain):
        result = CliRunner().invoke(app, ["status", "--check", "--brain", str(brain.root)])
        # Retrieval keeps serving the previous generation and says so.
        assert search([brain], Query(text="latecomer"), counted=False)["stale"] == ["fixture"]
    assert result.exit_code == 0, result.output
    reply = json.loads(result.stdout)
    assert reply["brains"][0]["cache"] == "busy"
    assert reply["healthy"]
    assert (
        json.loads(CliRunner().invoke(app, ["status", "--brain", str(brain.root)]).stdout)["brains"][0]["cache"]
        == "ready"
    )


@pytest.mark.parametrize("linkable", [True, False])
def test_a_cancelled_commit_restores_only_the_files_it_replaced(
    brain: Store, monkeypatch: pytest.MonkeyPatch, *, linkable: bool
) -> None:
    records.upsert(brain, "bulk", [Record(id=str(n), title="Old") for n in range(10)], snapshot=False)
    before = {name: brain.read(name) for name in records.files(brain, "bulk")}
    if not linkable:
        monkeypatch.setattr(os, "link", _cross_device)
    write, link = Store.write, Store.link
    replaced: list[str] = []
    restored: list[str] = []

    def committing(self: Store, name: str, data: bytes, *, durable: bool = True) -> None:
        if name.startswith("memories/bulk/"):
            if len(replaced) == 3 and not restored:
                # Ctrl-C, or watch's SIGTERM, after three of ten replacements.
                restored.append("")
                raise KeyboardInterrupt
            (restored or replaced).append(name)
        write(self, name, data, durable=durable)

    def linking(self: Store, name: str, target: str) -> None:
        link(self, name, target)
        if target.startswith("memories/bulk/"):
            restored.append(target)

    monkeypatch.setattr(Store, "write", committing)
    monkeypatch.setattr(Store, "link", linking)
    with pytest.raises(KeyboardInterrupt):
        records.upsert(brain, "bulk", [Record(id=str(n), title="New") for n in range(10)], snapshot=False)
    # Files the commit never reached stay as they were: a large cancelled commit rolls back only its progress.
    assert sorted(restored[1:]) == sorted(replaced)
    assert len(replaced) == 3
    assert {name: brain.read(name) for name in records.files(brain, "bulk")} == before
    assert not (brain.root / "memories/.pending").exists()


@pytest.mark.parametrize("linkable", [True, False])
def test_commit_backups_link_the_replaced_file_unless_the_filesystem_cannot(
    brain: Store, monkeypatch: pytest.MonkeyPatch, *, linkable: bool
) -> None:
    original, data = (brain.root / DECISION).stat(), brain.read(DECISION)
    if not linkable:
        monkeypatch.setattr(os, "link", _cross_device)
    write = Store.write
    backups: list[tuple[bool, bytes]] = []

    def observed(self: Store, name: str, content: bytes, *, durable: bool = True) -> None:
        if name == DECISION:
            backup = brain.root / "memories/.pending/0.before"
            backups.append((os.path.samestat(backup.stat(), original), backup.read_bytes()))
        write(self, name, content, durable=durable)

    monkeypatch.setattr(Store, "write", observed)
    records.upsert(brain, "meetings", [Record(id="decision-1", title="Moved")], snapshot=False)
    # A link keeps the replaced inode, which the commit renames a new file over and never writes.
    assert backups == [(linkable, data)]
    assert records.load(brain, DECISION).title == "Moved"
    assert not (brain.root / "memories/.pending").exists()


def test_exact_reads_do_not_wait_out_a_writer(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(records, "WAIT", 0)
    index.refresh(brain)
    with writer(brain):
        # One file reads whole without the lock, marked like a busy brain's cache.
        reply = read([brain], "meetings:decision-1", counted=False)
        assert cast("dict[str, object]", reply["record"])["title"] == "Preserve durable evidence"
        assert reply["stale"] == ["fixture"]
        # A missing file proves nothing while records change: fail at once instead of claiming absence.
        with pytest.raises(BusyError, match="retry the read shortly"):
            read([brain], "meetings:absent", counted=False)
    assert "stale" not in read([brain], "meetings:decision-1", counted=False)
    with pytest.raises(Error, match="not found"):
        read([brain], "meetings:absent", counted=False)


def _intact(brain: Store) -> bool:
    """Whether the cache passes SQLite's own page check; a rebuilt file may reuse the damaged one's inode number."""
    try:
        with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
            return connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        return False


def _damage(brain: Store, table: str) -> None:
    """Overwrite the root page of one cache table, as a bad disk block would."""
    path = brain.root / index.CACHE
    with closing(sqlite3.connect(path)) as connection:
        size = connection.execute("PRAGMA page_size").fetchone()[0]
        page = connection.execute("SELECT rootpage FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
    with path.open("r+b") as stream:
        stream.seek((page - 1) * size)
        stream.write(b"\xff" * size)


@pytest.mark.parametrize("table", ["items", "files"])
def test_a_damaged_cache_is_discarded_and_rebuilt(brain: Store, table: str) -> None:
    index.refresh(brain)
    _damage(brain, table)
    assert not _intact(brain)
    if table == "items":
        # Freshness reads only file fingerprints: the first query to meet the damage fails and discards it.
        with pytest.raises(Error, match="search cache is unavailable"):
            search([brain], Query(text="offline"), counted=False)
        assert not (brain.root / index.CACHE).exists()
    reply = search([brain], Query(text="offline retrieval"), counted=False)
    assert cast("list[dict[str, object]]", reply["items"])[0]["ref"] == "projects/offline.md"
    assert "stale" not in reply
    assert _intact(brain)


def test_damage_in_one_brain_discards_only_its_own_cache(brain: Store, tmp_path: Path) -> None:
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 7\nname: team\n")
    team.write("concepts/team.md", b"---\ntype: concept\nstatus: stable\n---\n# Team offline notes\n")
    for store in (brain, team):
        index.refresh(store)
    kept = (brain.root / index.CACHE).stat().st_ino
    _damage(team, "items")
    reply = search([brain, team], Query(text="offline"), counted=False)
    assert reply["problems"] == [{"brain": "team", "error": "inaccessible brain or cache; run bf status"}]
    assert not (team.root / index.CACHE).exists()
    assert (brain.root / index.CACHE).stat().st_ino == kept
    # An intact generation survives a discard request, as when another brain's error reached it.
    index._discard(brain, 0)  # noqa: SLF001 - the guard every damage report passes through
    assert (brain.root / index.CACHE).stat().st_ino == kept
    found = search([brain, team], Query(text="offline"), counted=False)["items"]
    refs = [item["ref"] for item in cast("list[dict[str, object]]", found)]
    assert "concepts/team.md" in refs


def test_recovery_refuses_an_oversized_backup_before_restoring(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    before = brain.read(DECISION)
    brain.write(DECISION, b'{"id":"decision-1","title":"Half committed"}\n')
    brain.write("memories/.pending/0.before", b"x" * 64)
    brain.write(
        "memories/.pending/manifest.json",
        json.dumps({"source": "meetings", "changes": [{"name": DECISION.rsplit("/", 1)[1], "existed": True}]}).encode(),
    )
    monkeypatch.setattr(records, "MAX_RECORD", 32)
    with writer(brain), pytest.raises(Error, match=r"0\.before: file exceeds 32 bytes"):
        records.recover(brain)
    assert brain.read(DECISION) != before
    assert brain.read("memories/.pending/0.before") == b"x" * 64


@pytest.mark.parametrize("table", ["items", "search_data"])
def test_update_rebuilds_a_damaged_cache_instead_of_reporting_ok(brain: Store, table: str) -> None:
    index.refresh(brain)
    _damage(brain, table)
    assert not _intact(brain)
    report = update(brain)
    assert report["ok"], report
    assert _intact(brain)
    assert search([brain], Query(text="offline retrieval"), counted=False)["items"]


def test_reads_ignore_a_completed_commit_whose_cleanup_was_interrupted(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(_store: Store) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(records, "_clear", interrupted)
        with pytest.raises(KeyboardInterrupt):
            records.upsert(brain, "meetings", [Record(id="decision-1", title="Moved")], snapshot=False)
    assert json.loads(brain.read("memories/.pending/manifest.json"))["complete"]
    # Every record was committed: reads, searches and validation need not wait for the next writer's cleanup.
    assert cast("dict[str, object]", read([brain], "meetings:decision-1")["record"])["title"] == "Moved"
    assert search([brain], Query(text="moved"), counted=False)["items"]
    assert validate(brain)["valid"]
    report = update(brain)
    assert report["ok"], report
    assert not (brain.root / "memories/.pending").exists()


def test_update_reports_skipped_evidence_as_failure(brain: Store) -> None:
    brain.write("projects/broken.md", b"---\nstale_after: invalid\n---\n# Broken\n")
    report = update(brain)
    assert cast("dict[str, int]", report["index"])["skipped"] == 1
    assert not report["ok"]


def test_malformed_yaml_scalar_does_not_hide_healthy_evidence(brain: Store) -> None:
    from bf.models import Query
    from bf.retrieve import search

    brain.write("projects/oversized.md", b"---\nprivate-field: " + b"9" * 5000 + b"\n---\n# Broken\n")
    reply = search([brain], Query(text="offline"), counted=False)
    assert reply["items"]
    assert "'file': 'projects/oversized.md', 'error': 'invalid YAML" in str(reply["problems"])
    assert "private-field" not in str(reply)
    assert not validate(brain)["valid"]


def test_cache_read_snapshot_survives_a_concurrent_refresh(brain: Store) -> None:
    index.refresh(brain)
    with index.database(brain) as (connection, _state):
        before = connection.execute("SELECT ref,title FROM items ORDER BY ref").fetchall()
        brain.write("projects/offline.md", b"---\ntype: project\n---\n# Updated\n")
        brain.write("projects/added.md", b"---\ntype: project\n---\n# Added\n")
        index.refresh(brain)
        after = connection.execute("SELECT ref,title FROM items ORDER BY ref").fetchall()
        assert [tuple(row) for row in after] == [tuple(row) for row in before]
    with index.database(brain) as (connection, _state):
        current = dict(connection.execute("SELECT ref,title FROM items"))
        assert current["projects/offline.md"] == "Updated"
        assert current["projects/added.md"] == "Added"


def test_missing_record_in_corrupt_source_is_not_proven_absent(brain: Store) -> None:
    brain.write(
        "memories/meetings/11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437.json", b"broken record\n"
    )
    assert records.find(brain, "meetings", "decision-1") is not None
    with pytest.raises(Error, match="invalid JSON document"):
        records.find(brain, "meetings", "possibly-in-broken-file")
    assert records.find(brain, "other", "absent") is None


def test_missing_identity_in_skipped_evidence_is_not_proven_absent(brain: Store) -> None:
    brain.write("projects/broken.md", b"---\nstale_after: invalid\n---\n# Broken\n")
    brain.write(
        "evals/retrieval.yaml",
        b"version: 7\ncases:\n- name: absent\n  read: 'repo:example/absent'\n  empty: true\n",
    )
    assert not evaluate(brain)["passed"]
    with pytest.raises(Error, match="incomplete"):
        read([brain], "repo:example/absent")


@pytest.mark.parametrize("path", ["memories/orphan.json", "memories/Bad/item.json", "memories/mail/2026-99.json"])
def test_invalid_record_paths_never_produce_unreadable_refs(brain: Store, path: str) -> None:
    from bf.models import Query
    from bf.retrieve import search

    brain.write(path, b'{"id":"item","title":"Misplacedneedle"}\n')
    reply = search([brain], Query(text="misplacedneedle"), counted=False)
    assert reply["items"] == []
    assert reply["problems"]
    assert not validate(brain)["valid"]


def test_unreadable_note_is_skipped_without_hiding_healthy_files(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    from bf.models import Query
    from bf.retrieve import search

    brain.write("projects/denied.md", b"# Restricted\n")
    original = Store.read

    def denied(self: Store, name: str, limit: int = 16 << 20) -> bytes:
        if name == "projects/denied.md":
            raise PermissionError("private operating-system detail")
        return original(self, name, limit)

    monkeypatch.setattr(Store, "read", denied)
    reply = search([brain], Query(text="offline"), counted=False)
    assert reply["items"]
    assert "inaccessible file" in str(reply["problems"])
    assert "private operating-system detail" not in str(reply)
    assert not validate(brain)["valid"]


def test_validation_refuses_links_through_unindexed_symlinks(brain: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private evidence")
    (brain.root / "inputs").mkdir()
    (brain.root / "inputs/direct.txt").symlink_to(outside / "secret.txt")
    (brain.root / "inputs/redirect").symlink_to(outside, target_is_directory=True)
    brain.write(
        "projects/links.md",
        b"# Links\n\n[direct](../inputs/direct.txt) [parent](../inputs/redirect/secret.txt)\n",
    )
    report = validate(brain)
    assert not report["valid"]
    assert isinstance(report["problems"], list)
    assert len([p for p in report["problems"] if "broken link" in p["error"]]) == 2


def test_new_directories_are_durable_before_their_files(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[tuple[int, int]] = []
    fsync = os.fsync

    def record_sync(fd: int) -> None:
        info = os.fstat(fd)
        synced.append((info.st_dev, info.st_ino))
        fsync(fd)

    monkeypatch.setattr(os, "fsync", record_sync)
    brain.write("new-parent/journal/original", b"durable original")
    expected = []
    for directory in (brain.root, brain.root / "new-parent", brain.root / "new-parent/journal"):
        info = directory.stat()
        expected.append((info.st_dev, info.st_ino))
    assert all(identity in synced for identity in expected)
    assert synced.index(expected[0]) < synced.index(expected[1]) < synced.index(expected[2])
