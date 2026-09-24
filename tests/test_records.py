"""Monthly JSON Lines partitions keep exactly one line per source item."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys

import pytest

from fkf import index, records, retrieve
from fkf.config import may_collect, register
from fkf.models import Error, Query, Record
from fkf.storage import Store


def lines(store: Store, name: str) -> list[str]:
    return [r.id for r in records.load(store, name)]


def test_window_upsert_adds_updates_moves_and_keeps_unchanged(base: Store) -> None:
    moved = Record(id="decision-1", title="Preserve durable evidence", time="2026-09-02T00:00:00Z")
    new = Record(id="new", title="New item", time="2026-09-03T00:00:00Z")
    same = Record(id="lunch", title="Lunch plans", text="Meet for lunch on Tuesday.", time="2026-08-30T12:00:00Z")
    counts = records.upsert(base, "meetings", [moved, new, same], snapshot=False)
    assert counts == {"added": 1, "updated": 1, "unchanged": 1, "removed": 0}
    assert lines(base, "records/meetings/2026-08.jsonl") == ["lunch"]
    assert lines(base, "records/meetings/2026-09.jsonl") == ["decision-1", "new"]
    before = base.fingerprint("records/meetings/2026-08.jsonl")
    assert records.upsert(base, "meetings", [same], snapshot=False)["unchanged"] == 1
    assert base.fingerprint("records/meetings/2026-08.jsonl") == before
    assert records.upsert(base, "meetings", [], snapshot=False)["added"] == 0
    gone = Record(id="lunch", title="Lunch plans", time="2026-09-01T00:00:00Z")
    records.upsert(base, "meetings", [gone], snapshot=False)
    assert not (base.root / "records/meetings/2026-08.jsonl").exists()
    undated = Record(id="loose", title="No time")
    records.upsert(base, "meetings", [undated], snapshot=False)
    assert lines(base, "records/meetings/undated.jsonl") == ["loose"]


def test_snapshot_replaces_the_complete_catalog(base: Store) -> None:
    folders = [Record(id="a", title="A"), Record(id="b", title="B")]
    assert records.upsert(base, "folders", folders, snapshot=True)["added"] == 2
    counts = records.upsert(base, "folders", [Record(id="b", title="B")], snapshot=True)
    assert counts == {"added": 0, "updated": 0, "unchanged": 1, "removed": 1}
    assert lines(base, "records/folders/snapshot.jsonl") == ["b"]
    assert records.upsert(base, "folders", [], snapshot=True)["removed"] == 1
    assert records.partitions(base, "folders") == []
    # A source switched to snapshot mode retires its monthly partitions.
    records.upsert(base, "meetings", [Record(id="only", title="Only")], snapshot=True)
    assert records.partitions(base, "meetings") == ["records/meetings/snapshot.jsonl"]


def test_find_reads_records_without_the_cache(base: Store) -> None:
    found = records.find(base, "meetings", "decision-1")
    assert found is not None
    assert found[0] == "records/meetings/2026-08.jsonl"
    assert found[1].aliases == ["meeting:decision-1"]
    assert records.find(base, "meetings", "absent") is None
    assert records.find(base, "Bad Source", "x") is None


def test_invalid_lines_name_their_location(base: Store) -> None:
    base.write("records/bad/2026-09.jsonl", b'{"id":"x","title":"ok"}\n\n{"id":"y"}\n')
    with pytest.raises(Error, match=r"2026-09.jsonl:3: invalid record: title"):
        records.load(base, "records/bad/2026-09.jsonl")
    base.write("records/bad/2026-09.jsonl", b'{"id":"x","id":"y"}\n')
    with pytest.raises(Error, match=r":1: JSON contains a duplicate key"):
        records.load(base, "records/bad/2026-09.jsonl")
    base.write("records/bad/notes.txt", b"ignored")
    assert records.partitions(base, "bad") == ["records/bad/2026-09.jsonl"]


def test_failed_partition_move_preserves_exact_original_bytes(base: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    name = "records/meetings/2026-08.jsonl"
    before = base.read(name)
    original = Store.write

    def fail_new_partition(self: Store, target: str, data: bytes) -> None:
        if target == "records/meetings/2026-09.jsonl":
            raise OSError(errno.ENOSPC, "synthetic disk full")
        original(self, target, data)

    monkeypatch.setattr(Store, "write", fail_new_partition)
    with pytest.raises(OSError, match="synthetic disk full"):
        records.upsert(
            base, "meetings", [Record(id="decision-1", title="Moved", time="2026-09-01T00:00:00Z")], snapshot=False
        )
    assert base.read(name) == before
    assert not (base.root / "records/meetings/2026-09.jsonl").exists()


def test_partition_size_is_checked_before_any_changes(base: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    records.upsert(base, "bounded", [Record(id="one", title="First", text="x" * 100)], snapshot=False)
    before = base.read("records/bounded/undated.jsonl")
    monkeypatch.setattr(records, "MAX_PARTITION", 256)
    with pytest.raises(Error, match=r"partition.*limit"):
        records.upsert(base, "bounded", [Record(id="two", title="Second", text="x" * 100)], snapshot=False)
    assert base.read("records/bounded/undated.jsonl") == before


@pytest.mark.parametrize("completed", [False, True])
def test_interrupted_transaction_recovers_from_durable_files(base: Store, completed: bool) -> None:
    original = base.read("records/meetings/2026-08.jsonl")
    script = """
import json, os, sys
from pathlib import Path
from fkf import records
from fkf.models import Record
from fkf.storage import Store, writer
store = Store(Path(sys.argv[1]))
completed = sys.argv[2] == 'True'
write = Store.write
def interrupted(self, name, data):
    write(self, name, data)
    if completed:
        stop = name == 'records/.pending/manifest.json' and json.loads(data)['complete']
    else:
        stop = name == 'records/meetings/2026-08.jsonl'
    if stop:
        os._exit(73)
Store.write = interrupted
with writer(store):
    records.upsert(store, 'meetings', [Record(id='decision-1', title='Moved', time='2026-09-01T00:00:00Z')], snapshot=False)
"""
    child = subprocess.run(  # noqa: S603 - terminate only a synthetic transaction in its temporary base
        [sys.executable, "-c", script, str(base.root), str(completed)],
        env=os.environ.copy(),
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert child.returncode == 73, child.stderr.decode()
    assert (base.root / "records/.pending/manifest.json").is_file()
    interrupted = {name: base.read(name) for name in base.files("records")}
    with pytest.raises(Error, match="run fkf build"):
        records.find(base, "meetings", "decision-1")
    assert {name: base.read(name) for name in base.files("records")} == interrupted
    index.refresh(base, full=True)
    found = records.find(base, "meetings", "decision-1")
    assert found is not None
    assert found[1].title == ("Moved" if completed else "Preserve durable evidence")
    assert not (base.root / "records/.pending").exists()
    if not completed:
        assert base.read("records/meetings/2026-08.jsonl") == original


def test_pending_manifest_is_confined_and_incomplete_preparation_is_discarded(base: Store) -> None:
    base.write("records/.pending/0.before", b"uncommitted staging")
    base.write("records/.pending/.write-" + "a" * 32, b"interrupted atomic staging write")
    with pytest.raises(Error, match="run fkf build"):
        records.find(base, "meetings", "decision-1")
    index.refresh(base, full=True)
    assert records.find(base, "meetings", "decision-1") is not None
    assert not (base.root / "records/.pending").exists()
    original = base.read("records/meetings/2026-08.jsonl")
    base.write(
        "records/.pending/manifest.json",
        json.dumps(
            {
                "source": "../escape",
                "changes": [{"name": "undated.jsonl", "existed": False}],
            }
        ).encode(),
    )
    with pytest.raises(Error, match="run fkf build"):
        records.find(base, "meetings", "decision-1")
    with pytest.raises(Error, match=r"invalid.*manifest"):
        index.refresh(base, full=True)
    assert base.read("records/meetings/2026-08.jsonl") == original


def test_observing_an_unchanged_revision_does_not_rewrite_it(base: Store) -> None:
    first = Record(id="item", title="Same evidence", attributes={"observed": "2026-09-01T00:00:00Z"})
    records.upsert(base, "observations", [first], snapshot=True)
    fingerprint = base.fingerprint("records/observations/snapshot.jsonl")
    repeated = first.model_copy(update={"attributes": {"observed": "2026-09-02T00:00:00.000000Z"}})
    assert records.upsert(base, "observations", [repeated], snapshot=True)["unchanged"] == 1
    assert base.fingerprint("records/observations/snapshot.jsonl") == fingerprint
    found = records.find(base, "observations", "item")
    assert found is not None
    assert found[1].observed == first.observed


def test_failed_rollback_keeps_originals_for_explicit_build(base: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    old = "records/meetings/2026-08.jsonl"
    before = base.read(old)
    original = Store.write
    changed = False

    def exhausted(self: Store, name: str, data: bytes) -> None:
        nonlocal changed
        if name == "records/meetings/2026-09.jsonl" or (name == old and changed):
            raise OSError(errno.ENOSPC, "synthetic full disk during write and rollback")
        original(self, name, data)
        if name == old:
            changed = True

    with monkeypatch.context() as patch:
        patch.setattr(Store, "write", exhausted)
        with pytest.raises(Error, match="needs recovery"):
            records.upsert(
                base, "meetings", [Record(id="decision-1", title="Moved", time="2026-09-01T00:00:00Z")], snapshot=False
            )
    assert (base.root / "records/.pending/manifest.json").is_file()
    assert base.read("records/.pending/0.before") == before
    with pytest.raises(Error, match="run fkf build"):
        records.find(base, "meetings", "decision-1")
    index.refresh(base, full=True)
    found = records.find(base, "meetings", "decision-1")
    assert found is not None
    assert found[1].title == "Preserve durable evidence"
    assert base.read(old) == before
    assert not (base.root / "records/.pending").exists()


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("existed", [False, True])
def test_untrusted_pending_journal_cannot_mutate_evidence_through_reads(
    base: Store, *, cached: bool, existed: bool
) -> None:
    register(base, collect=False)
    assert not may_collect(base)
    if cached:
        index.refresh(base)
        assert index.fresh(base) == "ready"
    base.write("records/.pending/0.before", b'{"id":"decision-1","title":"Forged replacement"}\n')
    base.write(
        "records/.pending/manifest.json",
        json.dumps({"source": "meetings", "changes": [{"name": "2026-08.jsonl", "existed": existed}]}).encode(),
    )
    before = {name: base.read(name) for name in base.files("records")}
    actions = (
        lambda: records.find(base, "meetings", "decision-1"),
        lambda: retrieve.read([base], "meetings:decision-1"),
        lambda: retrieve.search([base], Query(text="durable evidence")),
        lambda: index.refresh(base),
    )
    for action in actions:
        with pytest.raises(Error, match="run fkf build"):
            action()
        assert {name: base.read(name) for name in base.files("records")} == before


@pytest.mark.parametrize("snapshot", [False, True])
def test_older_upstream_revision_cannot_replace_or_move_newer_evidence(base: Store, snapshot: bool) -> None:
    current = Record(
        id="item",
        title="Current revision",
        time="2026-09-01T00:00:00Z",
        attributes={"updated": "2026-09-02T00:00:00Z"},
    )
    records.upsert(base, "revisions", [current, Record(id="other", title="Other item")], snapshot=snapshot)
    old = Record(
        id="item",
        title="Historical partial revision",
        time="2026-08-01T00:00:00Z",
        attributes={"updated": "2026-08-02T00:00:00Z"},
    )
    counts = records.upsert(base, "revisions", [old], snapshot=snapshot)
    assert counts["unchanged"] == 1
    assert counts["updated"] == 0
    found = records.find(base, "revisions", "item")
    assert found is not None
    assert found[1] == current
    assert found[0] == "records/revisions/" + ("snapshot" if snapshot else "2026-09") + ".jsonl"
    assert (records.find(base, "revisions", "other") is None) == snapshot


@pytest.mark.parametrize("separate_partitions", [False, True])
@pytest.mark.parametrize("snapshot", [False, True])
def test_collection_preserves_conflicting_existing_records(
    base: Store, *, separate_partitions: bool, snapshot: bool
) -> None:
    first = records.line(Record(id="same", title="First evidence", time="2026-08-01T00:00:00Z"))
    second = records.line(Record(id="same", title="Conflicting evidence", time="2026-09-01T00:00:00Z"))
    base.write("records/conflicts/2026-08.jsonl", first if separate_partitions else first + second)
    if separate_partitions:
        base.write("records/conflicts/2026-09.jsonl", second)
    before = {name: base.read(name) for name in records.partitions(base)}
    with pytest.raises(Error, match=r"duplicate.*fkf validate"):
        records.upsert(base, "conflicts", [Record(id="new", title="New evidence")], snapshot=snapshot)
    assert {name: base.read(name) for name in records.partitions(base)} == before
    assert not (base.root / "records/.pending").exists()
