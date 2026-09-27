"""Independent JSON record files preserve identity, revisions and transactional recovery."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys

import pytest

from bf import index, records, retrieve
from bf.config import register
from bf.models import Error, Query, Record
from bf.storage import Store, writer


def lines(store: Store, name: str) -> list[str]:
    return [r.id for r in records.load(store, name)]


def test_window_upsert_adds_updates_moves_and_keeps_unchanged(brain: Store) -> None:
    moved = Record(id="decision-1", title="Preserve durable evidence", time="2026-09-02T00:00:00Z")
    new = Record(id="new", title="New item", time="2026-09-03T00:00:00Z")
    same = Record(id="lunch", title="Lunch plans", text="Meet for lunch on Tuesday.", time="2026-08-30T12:00:00Z")
    untouched = brain.fingerprint(records.path("meetings", "lunch"))
    assert records.upsert(brain, "meetings", [moved, new, same], snapshot=False) == {
        "added": 1,
        "updated": 1,
        "unchanged": 1,
        "removed": 0,
    }
    assert brain.fingerprint(records.path("meetings", "lunch")) == untouched
    assert lines(brain, records.path("meetings", "decision-1")) == ["decision-1"]
    found = records.find(brain, "meetings", "decision-1")
    assert found is not None
    assert found[1].time.startswith("2026-09")
    assert records.upsert(brain, "meetings", [same], snapshot=False)["unchanged"] == 1
    assert records.upsert(brain, "meetings", [], snapshot=False)["added"] == 0
    records.upsert(brain, "meetings", [Record(id="loose", title="No time")], snapshot=False)
    assert lines(brain, records.path("meetings", "loose")) == ["loose"]


def test_snapshot_replaces_the_complete_catalog(brain: Store) -> None:
    folders = [Record(id="a", title="A"), Record(id="b", title="B")]
    assert records.upsert(brain, "folders", folders, snapshot=True)["added"] == 2
    counts = records.upsert(brain, "folders", [Record(id="b", title="B")], snapshot=True)
    assert counts == {"added": 0, "updated": 0, "unchanged": 1, "removed": 1}
    assert lines(brain, "memories/folders/3e23e8160039594a33894f6564e1b1348bbd7a0088d42c4acb73eeaed59c009d.json") == [
        "b"
    ]
    assert records.upsert(brain, "folders", [], snapshot=True)["removed"] == 1
    assert records.files(brain, "folders") == []
    # Switching to snapshot mode retires records absent from the complete catalog.
    records.upsert(brain, "meetings", [Record(id="only", title="Only")], snapshot=True)
    assert records.files(brain, "meetings") == [records.path("meetings", "only")]


def test_find_reads_records_without_the_cache(brain: Store) -> None:
    found = records.find(brain, "meetings", "decision-1")
    assert found is not None
    assert found[0] == "memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json"
    assert found[1].aliases == ["meeting:decision-1"]
    assert records.find(brain, "meetings", "absent") is None
    assert records.find(brain, "Bad Source", "x") is None


def test_invalid_record_files_name_their_location(brain: Store) -> None:
    target = records.path("bad", "x")
    brain.write(target, b'{"id":"x","title":"ok"}\n{"id":"y"}\n')
    with pytest.raises(Error, match="invalid JSON document"):
        records.load(brain, target)
    brain.write(target, b'{"id":"x","id":"y"}\n')
    with pytest.raises(Error, match="JSON contains a duplicate key"):
        records.load(brain, target)
    brain.write("memories/bad/notes.txt", b"ignored")
    assert records.files(brain, "bad") == [target]


def test_invalid_record_keys_stay_out_of_diagnostics(brain: Store) -> None:
    path = "memories/bad/2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881.json"
    brain.write(path, b'{"id":"x","title":"ok","private-provider-key":"private-value"}\n')
    with pytest.raises(Error, match=r"invalid record: <key>") as failure:
        records.load(brain, path)
    assert "private" not in str(failure.value)
    reply = retrieve.search([brain], Query(text="absent"))
    assert "<key>" in str(reply["problems"])
    assert "private" not in str(reply["problems"])


def test_failed_partition_move_preserves_exact_original_bytes(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    name = "memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json"
    before = brain.read(name)
    original = Store.write

    def fail_new_partition(self: Store, target: str, data: bytes) -> None:
        if target == "memories/meetings/11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437.json":
            raise OSError(errno.ENOSPC, "synthetic disk full")
        original(self, target, data)

    monkeypatch.setattr(Store, "write", fail_new_partition)
    with pytest.raises(OSError, match="synthetic disk full"):
        records.upsert(
            brain,
            "meetings",
            [Record(id="decision-1", title="Moved", time="2026-09-01T00:00:00Z"), Record(id="new", title="New")],
            snapshot=False,
        )
    assert brain.read(name) == before
    assert not (
        brain.root / "memories/meetings/11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437.json"
    ).exists()


def test_partition_size_is_checked_before_any_changes(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    records.upsert(brain, "bounded", [Record(id="one", title="First", text="x" * 100)], snapshot=False)
    before = brain.read("memories/bounded/7692c3ad3540bb803c020b3aee66cd8887123234ea0c6e7143c0add73ff431ed.json")
    monkeypatch.setattr(records, "MAX_RECORD", 256)
    with pytest.raises(Error, match=r"record.*limit"):
        records.upsert(brain, "bounded", [Record(id="two", title="Second", text="x" * 300)], snapshot=False)
    assert (
        brain.read("memories/bounded/7692c3ad3540bb803c020b3aee66cd8887123234ea0c6e7143c0add73ff431ed.json") == before
    )


@pytest.mark.parametrize("completed", [False, True])
def test_interrupted_transaction_recovers_from_durable_files(brain: Store, completed: bool) -> None:
    original = brain.read("memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json")
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
        stop = name == 'memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json'
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
        assert (
            brain.read("memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json")
            == original
        )


def test_pending_manifest_is_confined_and_incomplete_preparation_is_discarded(brain: Store) -> None:
    brain.write("memories/.pending/0.before", b"uncommitted staging")
    brain.write("memories/.pending/.write-" + "a" * 32, b"interrupted atomic staging write")
    with pytest.raises(Error, match="run bf build"):
        records.find(brain, "meetings", "decision-1")
    index.refresh(brain, full=True)
    assert records.find(brain, "meetings", "decision-1") is not None
    assert not (brain.root / "memories/.pending").exists()
    original = brain.read("memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json")
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
    assert (
        brain.read("memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json")
        == original
    )


def test_observing_an_unchanged_revision_does_not_rewrite_it(brain: Store) -> None:
    first = Record(id="item", title="Same evidence", attributes={"observed": "2026-09-01T00:00:00Z"})
    records.upsert(brain, "observations", [first], snapshot=True)
    fingerprint = brain.fingerprint(
        "memories/observations/4a33eacd5fa65f2b2e2871cd131286b53c415b131666d71173bb6e3fe59361b3.json"
    )
    repeated = first.model_copy(update={"attributes": {"observed": "2026-09-02T00:00:00.000000Z"}})
    assert records.upsert(brain, "observations", [repeated], snapshot=True)["unchanged"] == 1
    assert (
        brain.fingerprint("memories/observations/4a33eacd5fa65f2b2e2871cd131286b53c415b131666d71173bb6e3fe59361b3.json")
        == fingerprint
    )
    found = records.find(brain, "observations", "item")
    assert found is not None
    assert found[1].observed == first.observed


def test_failed_rollback_keeps_originals_for_explicit_build(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    old = "memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json"
    before = brain.read(old)
    original = Store.write
    changed = False

    def exhausted(self: Store, name: str, data: bytes) -> None:
        nonlocal changed
        if name == "memories/meetings/11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437.json" or (
            name == old and changed
        ):
            raise OSError(errno.ENOSPC, "synthetic full disk during write and rollback")
        original(self, name, data)
        if name == old:
            changed = True

    with monkeypatch.context() as patch:
        patch.setattr(Store, "write", exhausted)
        with pytest.raises(Error, match="needs recovery"):
            records.upsert(
                brain,
                "meetings",
                [Record(id="decision-1", title="Moved", time="2026-09-01T00:00:00Z"), Record(id="new", title="New")],
                snapshot=False,
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
    register(brain)
    if cached:
        index.refresh(brain)
        assert index.fresh(brain) == "ready"
    brain.write("memories/.pending/0.before", b'{"id":"decision-1","title":"Forged replacement"}\n')
    brain.write(
        "memories/.pending/manifest.json",
        json.dumps(
            {
                "source": "meetings",
                "changes": [{"name": records.path("meetings", "decision-1").rsplit("/", 1)[1], "existed": existed}],
            }
        ).encode(),
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
    assert found[0] == records.path("revisions", "item")
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
    brain.write(
        "memories/conflicts/0967115f2813a3541eaef77de9d9d5773f1c0c04314b0bbfe4ff3b3b1c55b5d5.json",
        first if separate_partitions else first + second,
    )
    if separate_partitions:
        brain.write("memories/conflicts/d9298a10d1b0735837dc4bd85dac641b0f3cef27a47e5d53a54f2f3f5b2fcffa.json", second)
    before = {name: brain.read(name) for name in records.files(brain)}
    with pytest.raises(Error, match=r"invalid JSON|SHA-256 filename"):
        records.upsert(brain, "conflicts", [Record(id="new", title="New evidence")], snapshot=snapshot)
    assert {name: brain.read(name) for name in records.files(brain)} == before
    assert not (brain.root / "memories/.pending").exists()
