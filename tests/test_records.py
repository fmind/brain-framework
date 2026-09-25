"""Monthly JSON Lines partitions keep exactly one line per source item."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys

import pytest

from bf import index, records, retrieve
from bf.config import may_collect, register
from bf.models import Error, Query, Record
from bf.storage import Store, writer


def lines(store: Store, name: str) -> list[str]:
    return [r.id for r in records.load(store, name)]


def test_window_upsert_adds_updates_moves_and_keeps_unchanged(brain: Store) -> None:
    moved = Record(id="decision-1", title="Preserve durable evidence", time="2026-09-02T00:00:00Z")
    new = Record(id="new", title="New item", time="2026-09-03T00:00:00Z")
    same = Record(id="lunch", title="Lunch plans", text="Meet for lunch on Tuesday.", time="2026-08-30T12:00:00Z")
    counts = records.upsert(brain, "meetings", [moved, new, same], snapshot=False)
    assert counts == {"added": 1, "updated": 1, "unchanged": 1, "removed": 0}
    assert lines(brain, "memories/meetings/2026-08.jsonl") == ["lunch"]
    assert lines(brain, "memories/meetings/2026-09.jsonl") == ["decision-1", "new"]
    before = brain.fingerprint("memories/meetings/2026-08.jsonl")
    assert records.upsert(brain, "meetings", [same], snapshot=False)["unchanged"] == 1
    assert brain.fingerprint("memories/meetings/2026-08.jsonl") == before
    assert records.upsert(brain, "meetings", [], snapshot=False)["added"] == 0
    gone = Record(id="lunch", title="Lunch plans", time="2026-09-01T00:00:00Z")
    records.upsert(brain, "meetings", [gone], snapshot=False)
    assert not (brain.root / "memories/meetings/2026-08.jsonl").exists()
    undated = Record(id="loose", title="No time")
    records.upsert(brain, "meetings", [undated], snapshot=False)
    assert lines(brain, "memories/meetings/undated.jsonl") == ["loose"]


def test_snapshot_replaces_the_complete_catalog(brain: Store) -> None:
    folders = [Record(id="a", title="A"), Record(id="b", title="B")]
    assert records.upsert(brain, "folders", folders, snapshot=True)["added"] == 2
    counts = records.upsert(brain, "folders", [Record(id="b", title="B")], snapshot=True)
    assert counts == {"added": 0, "updated": 0, "unchanged": 1, "removed": 1}
    assert lines(brain, "memories/folders/snapshot.jsonl") == ["b"]
    assert records.upsert(brain, "folders", [], snapshot=True)["removed"] == 1
    assert records.partitions(brain, "folders") == []
    # A source switched to snapshot mode retires its monthly partitions.
    records.upsert(brain, "meetings", [Record(id="only", title="Only")], snapshot=True)
    assert records.partitions(brain, "meetings") == ["memories/meetings/snapshot.jsonl"]


def test_find_reads_records_without_the_cache(brain: Store) -> None:
    found = records.find(brain, "meetings", "decision-1")
    assert found is not None
    assert found[0] == "memories/meetings/2026-08.jsonl"
    assert found[1].aliases == ["meeting:decision-1"]
    assert records.find(brain, "meetings", "absent") is None
    assert records.find(brain, "Bad Source", "x") is None


def test_invalid_lines_name_their_location(brain: Store) -> None:
    brain.write("memories/bad/2026-09.jsonl", b'{"id":"x","title":"ok"}\n\n{"id":"y"}\n')
    with pytest.raises(Error, match=r"2026-09.jsonl:3: invalid record: title"):
        records.load(brain, "memories/bad/2026-09.jsonl")
    brain.write("memories/bad/2026-09.jsonl", b'{"id":"x","id":"y"}\n')
    with pytest.raises(Error, match=r":1: JSON contains a duplicate key"):
        records.load(brain, "memories/bad/2026-09.jsonl")
    brain.write("memories/bad/notes.txt", b"ignored")
    assert records.partitions(brain, "bad") == ["memories/bad/2026-09.jsonl"]


def test_failed_partition_move_preserves_exact_original_bytes(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    name = "memories/meetings/2026-08.jsonl"
    before = brain.read(name)
    original = Store.write

    def fail_new_partition(self: Store, target: str, data: bytes) -> None:
        if target == "memories/meetings/2026-09.jsonl":
            raise OSError(errno.ENOSPC, "synthetic disk full")
        original(self, target, data)

    monkeypatch.setattr(Store, "write", fail_new_partition)
    with pytest.raises(OSError, match="synthetic disk full"):
        records.upsert(
            brain, "meetings", [Record(id="decision-1", title="Moved", time="2026-09-01T00:00:00Z")], snapshot=False
        )
    assert brain.read(name) == before
    assert not (brain.root / "memories/meetings/2026-09.jsonl").exists()


def test_partition_size_is_checked_before_any_changes(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    records.upsert(brain, "bounded", [Record(id="one", title="First", text="x" * 100)], snapshot=False)
    before = brain.read("memories/bounded/undated.jsonl")
    monkeypatch.setattr(records, "MAX_PARTITION", 256)
    with pytest.raises(Error, match=r"partition.*limit"):
        records.upsert(brain, "bounded", [Record(id="two", title="Second", text="x" * 100)], snapshot=False)
    assert brain.read("memories/bounded/undated.jsonl") == before


@pytest.mark.parametrize("completed", [False, True])
def test_interrupted_transaction_recovers_from_durable_files(brain: Store, completed: bool) -> None:
    original = brain.read("memories/meetings/2026-08.jsonl")
    script = """
import json, os, sys
from pathlib import Path
from bf import records
from bf.models import Record
from bf.storage import Store, writer
store = Store(Path(sys.argv[1]))
completed = sys.argv[2] == 'True'
write = Store.write
def interrupted(self, name, data):
    write(self, name, data)
    if completed:
        stop = name == 'memories/.pending/manifest.json' and json.loads(data)['complete']
    else:
        stop = name == 'memories/meetings/2026-08.jsonl'
    if stop:
        os._exit(73)
Store.write = interrupted
with writer(store):
    records.upsert(store, 'meetings', [Record(id='decision-1', title='Moved', time='2026-09-01T00:00:00Z')], snapshot=False)
"""
    child = subprocess.run(  # noqa: S603 - terminate only a synthetic transaction in its temporary brain
        [sys.executable, "-c", script, str(brain.root), str(completed)],
        env=os.environ.copy(),
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert child.returncode == 73, child.stderr.decode()
    assert (brain.root / "memories/.pending/manifest.json").is_file()
    interrupted = {name: brain.read(name) for name in brain.files("memories")}
    with pytest.raises(Error, match="run bf build"):
        records.find(brain, "meetings", "decision-1")
    assert {name: brain.read(name) for name in brain.files("memories")} == interrupted
    index.refresh(brain, full=True)
    found = records.find(brain, "meetings", "decision-1")
    assert found is not None
    assert found[1].title == ("Moved" if completed else "Preserve durable evidence")
    assert not (brain.root / "memories/.pending").exists()
    if not completed:
        assert brain.read("memories/meetings/2026-08.jsonl") == original


def test_pending_manifest_is_confined_and_incomplete_preparation_is_discarded(brain: Store) -> None:
    brain.write("memories/.pending/0.before", b"uncommitted staging")
    brain.write("memories/.pending/.write-" + "a" * 32, b"interrupted atomic staging write")
    with pytest.raises(Error, match="run bf build"):
        records.find(brain, "meetings", "decision-1")
    index.refresh(brain, full=True)
    assert records.find(brain, "meetings", "decision-1") is not None
    assert not (brain.root / "memories/.pending").exists()
    original = brain.read("memories/meetings/2026-08.jsonl")
    brain.write(
        "memories/.pending/manifest.json",
        json.dumps(
            {
                "source": "../escape",
                "changes": [{"name": "undated.jsonl", "existed": False}],
            }
        ).encode(),
    )
    with pytest.raises(Error, match="run bf build"):
        records.find(brain, "meetings", "decision-1")
    with pytest.raises(Error, match=r"invalid.*manifest"):
        index.refresh(brain, full=True)
    assert brain.read("memories/meetings/2026-08.jsonl") == original


def test_observing_an_unchanged_revision_does_not_rewrite_it(brain: Store) -> None:
    first = Record(id="item", title="Same evidence", attributes={"observed": "2026-09-01T00:00:00Z"})
    records.upsert(brain, "observations", [first], snapshot=True)
    fingerprint = brain.fingerprint("memories/observations/snapshot.jsonl")
    repeated = first.model_copy(update={"attributes": {"observed": "2026-09-02T00:00:00.000000Z"}})
    assert records.upsert(brain, "observations", [repeated], snapshot=True)["unchanged"] == 1
    assert brain.fingerprint("memories/observations/snapshot.jsonl") == fingerprint
    found = records.find(brain, "observations", "item")
    assert found is not None
    assert found[1].observed == first.observed


def test_failed_rollback_keeps_originals_for_explicit_build(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    old = "memories/meetings/2026-08.jsonl"
    before = brain.read(old)
    original = Store.write
    changed = False

    def exhausted(self: Store, name: str, data: bytes) -> None:
        nonlocal changed
        if name == "memories/meetings/2026-09.jsonl" or (name == old and changed):
            raise OSError(errno.ENOSPC, "synthetic full disk during write and rollback")
        original(self, name, data)
        if name == old:
            changed = True

    with monkeypatch.context() as patch:
        patch.setattr(Store, "write", exhausted)
        with pytest.raises(Error, match="needs recovery"):
            records.upsert(
                brain, "meetings", [Record(id="decision-1", title="Moved", time="2026-09-01T00:00:00Z")], snapshot=False
            )
    assert (brain.root / "memories/.pending/manifest.json").is_file()
    assert brain.read("memories/.pending/0.before") == before
    with pytest.raises(Error, match="run bf build"):
        records.find(brain, "meetings", "decision-1")
    index.refresh(brain, full=True)
    found = records.find(brain, "meetings", "decision-1")
    assert found is not None
    assert found[1].title == "Preserve durable evidence"
    assert brain.read(old) == before
    assert not (brain.root / "memories/.pending").exists()


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("existed", [False, True])
def test_untrusted_pending_journal_cannot_mutate_evidence_through_reads(
    brain: Store, *, cached: bool, existed: bool
) -> None:
    register(brain, collect=False)
    assert not may_collect(brain)
    if cached:
        index.refresh(brain)
        assert index.fresh(brain) == "ready"
    brain.write("memories/.pending/0.before", b'{"id":"decision-1","title":"Forged replacement"}\n')
    brain.write(
        "memories/.pending/manifest.json",
        json.dumps({"source": "meetings", "changes": [{"name": "2026-08.jsonl", "existed": existed}]}).encode(),
    )
    before = {name: brain.read(name) for name in brain.files("memories")}
    actions = (
        lambda: records.find(brain, "meetings", "decision-1"),
        lambda: retrieve.read([brain], "meetings:decision-1"),
        lambda: retrieve.search([brain], Query(text="durable evidence")),
        lambda: index.refresh(brain),
    )
    for action in actions:
        with pytest.raises(Error, match="run bf build"):
            action()
        assert {name: brain.read(name) for name in brain.files("memories")} == before


@pytest.mark.parametrize("snapshot", [False, True])
def test_older_upstream_revision_cannot_replace_or_move_newer_evidence(brain: Store, snapshot: bool) -> None:
    current = Record(
        id="item",
        title="Current revision",
        time="2026-09-01T00:00:00Z",
        attributes={"updated": "2026-09-02T00:00:00Z"},
    )
    records.upsert(brain, "revisions", [current, Record(id="other", title="Other item")], snapshot=snapshot)
    old = Record(
        id="item",
        title="Historical partial revision",
        time="2026-08-01T00:00:00Z",
        attributes={"updated": "2026-08-02T00:00:00Z"},
    )
    counts = records.upsert(brain, "revisions", [old], snapshot=snapshot)
    assert counts["unchanged"] == 1
    assert counts["updated"] == 0
    found = records.find(brain, "revisions", "item")
    assert found is not None
    assert found[1] == current
    assert found[0] == "memories/revisions/" + ("snapshot" if snapshot else "2026-09") + ".jsonl"
    assert (records.find(brain, "revisions", "other") is None) == snapshot


def test_a_revision_dated_after_its_observation_cannot_freeze_a_record(brain: Store) -> None:
    # A file with a future mtime claims to be modified in 2030 although it was first seen in 2026.
    future = Record(
        id="doc",
        title="v1",
        attributes={"updated": "2030-01-01T00:00:00Z", "observed": "2026-09-01T00:00:00Z"},
    )
    records.upsert(brain, "documents", [future], snapshot=True)
    edited = Record(
        id="doc",
        title="v2",
        attributes={"updated": "2026-09-02T00:00:00Z", "observed": "2026-09-02T00:00:00Z"},
    )
    assert records.upsert(brain, "documents", [edited], snapshot=True)["updated"] == 1
    found = records.find(brain, "documents", "doc")
    assert found is not None
    assert found[1].title == "v2"


def test_aliases_and_absent_sources_do_not_wait_for_writers(brain: Store) -> None:
    with writer(brain):
        # Existing sources still wait for a consistent set of partitions; an alias returns at once.
        assert records.find(brain, "repo", "example/project") is None


@pytest.mark.parametrize("separate_partitions", [False, True])
@pytest.mark.parametrize("snapshot", [False, True])
def test_collection_preserves_conflicting_existing_records(
    brain: Store, *, separate_partitions: bool, snapshot: bool
) -> None:
    first = records.line(Record(id="same", title="First evidence", time="2026-08-01T00:00:00Z"))
    second = records.line(Record(id="same", title="Conflicting evidence", time="2026-09-01T00:00:00Z"))
    brain.write("memories/conflicts/2026-08.jsonl", first if separate_partitions else first + second)
    if separate_partitions:
        brain.write("memories/conflicts/2026-09.jsonl", second)
    before = {name: brain.read(name) for name in records.partitions(brain)}
    with pytest.raises(Error, match=r"duplicate.*bf validate"):
        records.upsert(brain, "conflicts", [Record(id="new", title="New evidence")], snapshot=snapshot)
    assert {name: brain.read(name) for name in records.partitions(brain)} == before
    assert not (brain.root / "memories/.pending").exists()
