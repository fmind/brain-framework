"""Independent JSON record files preserve identity, revisions and transactional recovery."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from bf import index, records, retrieve
from bf.config import register
from bf.models import MAX_FIELDS, MAX_RECORD, Error, Query, Record
from bf.storage import Store, writer
from bf.update import update

DECISION = records.path("meetings", "decision-1")
NEW = records.path("meetings", "new")


def test_fields_beside_text_stay_within_a_readable_size() -> None:
    # An exact read pages only a record's text, so every other field must fit its first page.
    half = "x" * (MAX_FIELDS // 2)
    assert Record(id="long", title="Long text", text=half * 4).text
    with pytest.raises(ValidationError, match="fields other than text exceed 2 MiB"):
        Record(id="bulky", title="Bulky", attributes={"a": half, "b": half})
    with pytest.raises(ValidationError, match="fields other than text exceed 2 MiB"):
        Record(id="linked", title="Linked", links=[f"repo:example/{n}-" + "y" * 4000 for n in range(600)])


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
    assert records.load(brain, DECISION).id == "decision-1"
    found = records.find(brain, "meetings", "decision-1")
    assert found is not None
    assert found[1].time.startswith("2026-09")
    assert records.upsert(brain, "meetings", [same], snapshot=False)["unchanged"] == 1
    assert records.upsert(brain, "meetings", [], snapshot=False)["added"] == 0
    records.upsert(brain, "meetings", [Record(id="loose", title="No time")], snapshot=False)
    assert records.load(brain, records.path("meetings", "loose")).id == "loose"


def test_snapshot_replaces_the_complete_catalog(brain: Store) -> None:
    folders = [Record(id="a", title="A"), Record(id="b", title="B")]
    assert records.upsert(brain, "folders", folders, snapshot=True)["added"] == 2
    counts = records.upsert(brain, "folders", [Record(id="b", title="B")], snapshot=True)
    assert counts == {"added": 0, "updated": 0, "unchanged": 1, "removed": 1}
    assert records.files(brain, "folders") == [records.path("folders", "b")]
    assert records.upsert(brain, "folders", [], snapshot=True)["removed"] == 1
    assert records.files(brain, "folders") == []
    # Switching to snapshot mode retires records absent from the complete catalog.
    records.upsert(brain, "meetings", [Record(id="only", title="Only")], snapshot=True)
    assert records.files(brain, "meetings") == [records.path("meetings", "only")]


def test_collection_parses_only_the_files_it_replaces_or_removes(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    records.upsert(brain, "catalog", [Record(id=str(n), title=f"Item {n}") for n in range(20)], snapshot=True)
    loaded: list[str] = []
    original = records.load

    def counted(store: Store, name: str) -> Record:
        loaded.append(name)
        return original(store, name)

    monkeypatch.setattr(records, "load", counted)
    records.upsert(brain, "catalog", [Record(id="3", title="Changed")], snapshot=False)
    assert loaded == [records.path("catalog", "3")]
    loaded.clear()
    kept = [Record(id=str(n), title=f"Item {n}") for n in range(18)]
    assert records.upsert(brain, "catalog", kept, snapshot=True)["removed"] == 2
    # A snapshot parses what it keeps and removes; unchanged files stay byte-identical.
    assert sorted(loaded) == sorted(records.path("catalog", str(n)) for n in range(20))


def test_absent_record_parses_the_source_unless_a_ready_cache_already_did(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    loaded: list[str] = []
    original = brain.stamped

    def counted(name: str, limit: int = MAX_RECORD) -> tuple[bytes, int]:
        # The record files a lookup opens; other reads, such as bf.yaml, prove nothing about absence.
        if name.startswith("memories/"):
            loaded.append(name)
        return original(name, limit)

    monkeypatch.setattr(brain, "stamped", counted)
    absent = records.path("meetings", "absent")
    assert records.find(brain, "meetings", "absent") is None
    assert loaded == [absent, DECISION, records.path("meetings", "lunch")]
    loaded.clear()
    assert records.find(brain, "meetings", "absent", complete=True) is None
    assert loaded == [absent]
    loaded.clear()
    # Exact reads pass `complete` only from a ready cache that reported no problem in the source.
    with pytest.raises(Error, match="not found"):
        retrieve.read([brain], "meetings:absent")
    assert loaded == [absent]
    brain.write(records.path("meetings", "misnamed"), b'{"id":"elsewhere","title":"Misnamed"}\n')
    with pytest.raises(Error, match="SHA-256 filename"):
        retrieve.read([brain], "meetings:elsewhere")


def test_find_reads_records_without_the_cache(brain: Store) -> None:
    found = records.find(brain, "meetings", "decision-1")
    assert found is not None
    assert found[0] == DECISION
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
    path = records.path("bad", "x")
    brain.write(path, b'{"id":"x","title":"ok","private-provider-key":"private-value"}\n')
    with pytest.raises(Error, match=r"invalid record: <key>") as failure:
        records.load(brain, path)
    assert "private" not in str(failure.value)
    reply = retrieve.search([brain], Query(text="absent"))
    assert "<key>" in str(reply["problems"])
    assert "private" not in str(reply["problems"])


def test_failed_record_write_preserves_exact_original_bytes(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    before = brain.read(DECISION)
    original = Store.write

    def fail_new_record(self: Store, target: str, data: bytes, *, durable: bool = True) -> None:
        if target == NEW:
            raise OSError(errno.ENOSPC, "synthetic disk full")
        original(self, target, data, durable=durable)

    monkeypatch.setattr(Store, "write", fail_new_record)
    with pytest.raises(OSError, match="synthetic disk full"):
        records.upsert(
            brain,
            "meetings",
            [Record(id="decision-1", title="Moved", time="2026-09-01T00:00:00Z"), Record(id="new", title="New")],
            snapshot=False,
        )
    assert brain.read(DECISION) == before
    assert not (brain.root / NEW).exists()


def test_record_size_is_checked_before_any_changes(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    records.upsert(brain, "bounded", [Record(id="one", title="First", text="x" * 100)], snapshot=False)
    before = brain.read(records.path("bounded", "one"))
    monkeypatch.setattr(records, "MAX_RECORD", 256)
    with pytest.raises(Error, match=r"record.*limit"):
        records.upsert(brain, "bounded", [Record(id="two", title="Second", text="x" * 300)], snapshot=False)
    assert brain.read(records.path("bounded", "one")) == before


def test_a_commit_syncs_each_directory_once(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    fsync = os.fsync
    calls = 0

    def counted(fd: int) -> None:
        nonlocal calls
        calls += 1
        fsync(fd)

    records.upsert(brain, "bulk", [Record(id=str(n), title="Old") for n in range(20)], snapshot=False)
    monkeypatch.setattr(os, "fsync", counted)
    records.upsert(brain, "bulk", [Record(id=str(n), title="New") for n in range(20)], snapshot=False)
    # One sync per backup and per record file, plus a few directory and manifest syncs, not one per entry.
    assert calls <= 2 * 20 + 8
    assert all(records.load(brain, records.path("bulk", str(n))).title == "New" for n in range(20))


def test_a_rollback_syncs_each_directory_once(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    records.upsert(brain, "bulk", [Record(id=str(n), title="Old") for n in range(20)], snapshot=False)
    before = {name: brain.read(name) for name in records.files(brain, "bulk")}
    fsync, write = os.fsync, Store.write
    failed, directories = False, 0

    def counted(fd: int) -> None:
        nonlocal directories
        directories += failed and stat.S_ISDIR(os.fstat(fd).st_mode)
        fsync(fd)

    def exhausted(self: Store, name: str, data: bytes, *, durable: bool = True) -> None:
        nonlocal failed
        if name == records.path("bulk", "19") and not failed:
            failed = True
            raise OSError(errno.ENOSPC, "synthetic full disk")
        write(self, name, data, durable=durable)

    monkeypatch.setattr(os, "fsync", counted)
    monkeypatch.setattr(Store, "write", exhausted)
    with pytest.raises(OSError, match="synthetic full disk"):
        records.upsert(brain, "bulk", [Record(id=str(n), title="New") for n in range(20)], snapshot=False)
    # Restoring 19 replaced records syncs the source, the completed manifest and the removed journal once each.
    assert directories == 3
    assert {name: brain.read(name) for name in records.files(brain, "bulk")} == before
    assert not (brain.root / "memories/.pending").exists()


def test_update_recovers_an_interrupted_transaction_that_reads_refuse(brain: Store) -> None:
    original = brain.read(DECISION)
    brain.write("memories/.pending/0.before", original)
    brain.write(DECISION, b'{"id":"decision-1","title":"Half committed"}\n')
    brain.write(
        "memories/.pending/manifest.json",
        json.dumps({"source": "meetings", "changes": [{"name": DECISION.rsplit("/", 1)[1], "existed": True}]}).encode(),
    )
    orphan = "memories/meetings/.write-" + "a" * 32
    brain.write(orphan, b"a write killed before its rename")
    with pytest.raises(Error, match="run bf build or bf update"):
        retrieve.search([brain], Query(text="durable evidence"))
    assert brain.read(DECISION) != original
    # Update holds the writer lock like build: it rolls the journal back before refreshing the cache.
    report = update(brain)
    assert report["ok"], report
    assert brain.read(DECISION) == original
    assert not (brain.root / "memories/.pending").exists()
    assert not (brain.root / orphan).exists()
    assert retrieve.search([brain], Query(text="durable evidence"))["items"]


def test_temporary_files_of_killed_writes_are_swept_and_never_count_as_records(brain: Store, tmp_path: Path) -> None:
    orphan, other = "memories/meetings/.write-" + "b" * 32, "memories/meetings/.write-notes"
    brain.write(orphan, b"a write killed before its rename")
    brain.write(other, b"not a name Store.write chooses")
    assert records.files(brain, "meetings") == sorted([DECISION, records.path("meetings", "lunch")])
    (brain.root / f"memories/meetings/.write-{'c' * 32}").symlink_to(tmp_path)
    records.upsert(brain, "meetings", [], snapshot=False)
    assert not (brain.root / orphan).exists()
    assert brain.read(other) == b"not a name Store.write chooses"
    assert (brain.root / f"memories/meetings/.write-{'c' * 32}").is_symlink()
    # Absent or linked directories have nothing to sweep; their own reads name a link.
    (brain.root / "memories/linked").symlink_to(tmp_path, target_is_directory=True)
    brain.sweep("memories/linked")
    brain.sweep("memories/absent")


@pytest.mark.parametrize("completed", [False, True])
def test_interrupted_transaction_recovers_from_durable_files(brain: Store, completed: bool) -> None:
    original = brain.read(DECISION)
    script = f"""
import json, os, sys
from pathlib import Path
from bf import records
from bf.models import Record
from bf.storage import Store, writer
store = Store(Path(sys.argv[1]))
completed = sys.argv[2] == 'True'
write = Store.write
def interrupted(self, name, data, *, durable=True):
    write(self, name, data, durable=durable)
    if completed:
        stop = name == 'memories/.pending/manifest.json' and json.loads(data)['complete']
    else:
        stop = name == {DECISION!r}
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
        assert brain.read(DECISION) == original


def test_pending_manifest_is_confined_and_incomplete_preparation_is_discarded(brain: Store) -> None:
    brain.write("memories/.pending/0.before", b"uncommitted staging")
    brain.write("memories/.pending/.write-" + "a" * 32, b"interrupted atomic staging write")
    with pytest.raises(Error, match="run bf build"):
        records.find(brain, "meetings", "decision-1")
    index.refresh(brain, full=True)
    assert records.find(brain, "meetings", "decision-1") is not None
    assert not (brain.root / "memories/.pending").exists()


@pytest.mark.parametrize(
    ("manifest", "problem"),
    [
        ({"source": "../escape", "changes": [{"name": DECISION.rsplit("/", 1)[1], "existed": True}]}, "invalid"),
        ({"source": "meetings", "changes": [{"name": "undated.json", "existed": False}]}, "invalid"),
        ({"source": "meetings", "changes": [{"name": "../../bf.yaml", "existed": False}]}, "invalid"),
        (
            {
                "source": "meetings",
                "changes": [{"name": DECISION.rsplit("/", 1)[1], "existed": flag} for flag in (True, False)],
            },
            "duplicate paths",
        ),
    ],
    ids=["traversal-source", "invalid-name", "traversal-name", "duplicate-names"],
)
def test_untrusted_manifests_are_refused_without_changing_evidence(
    brain: Store, manifest: dict[str, object], problem: str
) -> None:
    brain.write("memories/.pending/0.before", b'{"id":"decision-1","title":"Forged replacement"}\n')
    brain.write("memories/.pending/manifest.json", json.dumps(manifest).encode())
    before = {name: brain.read(name) for name in brain.files("memories")}
    with pytest.raises(Error, match=problem):
        index.refresh(brain, full=True)
    assert {name: brain.read(name) for name in brain.files("memories")} == before


def test_unexpected_pending_files_are_preserved_for_repair(brain: Store) -> None:
    brain.write("memories/.pending/notes.txt", b"an operator's note")
    with pytest.raises(Error, match=r"unexpected file in memories/\.pending"):
        index.refresh(brain, full=True)
    assert brain.read("memories/.pending/notes.txt") == b"an operator's note"
    assert brain.read(DECISION)


def test_observing_an_unchanged_revision_does_not_rewrite_it(brain: Store) -> None:
    first = Record(id="item", title="Same evidence", attributes={"observed": "2026-09-01T00:00:00Z"})
    records.upsert(brain, "observations", [first], snapshot=True)
    fingerprint = brain.fingerprint(records.path("observations", "item"))
    repeated = first.model_copy(update={"attributes": {"observed": "2026-09-02T00:00:00.000000Z"}})
    assert records.upsert(brain, "observations", [repeated], snapshot=True)["unchanged"] == 1
    assert brain.fingerprint(records.path("observations", "item")) == fingerprint
    found = records.find(brain, "observations", "item")
    assert found is not None
    assert found[1].observed == first.observed


def test_failed_rollback_keeps_originals_for_explicit_build(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    before = brain.read(DECISION)
    original = Store.write
    changed = False

    def exhausted(self: Store, name: str, data: bytes, *, durable: bool = True) -> None:
        nonlocal changed
        if name == NEW or (name == DECISION and changed):
            raise OSError(errno.ENOSPC, "synthetic full disk during write and rollback")
        original(self, name, data, durable=durable)
        if name == DECISION:
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
    assert brain.read(DECISION) == before
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
            {"source": "meetings", "changes": [{"name": DECISION.rsplit("/", 1)[1], "existed": existed}]}
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
def test_older_upstream_revision_cannot_replace_newer_evidence(brain: Store, snapshot: bool) -> None:
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
        # Existing sources still wait for a consistent set of record files; an alias returns at once.
        assert records.find(brain, "repo", "example/project") is None


@pytest.mark.parametrize("incoming", ["same", "new"])
def test_collection_never_replaces_or_removes_misnamed_evidence(brain: Store, incoming: str) -> None:
    # A file named for one id holds another: collection cannot tell which evidence to keep.
    misnamed = records.path("conflicts", "same")
    brain.write(misnamed, records.serialize(Record(id="elsewhere", title="Conflicting evidence")))
    before = brain.read(misnamed)
    with pytest.raises(Error, match="SHA-256 filename"):
        records.upsert(brain, "conflicts", [Record(id=incoming, title="Incoming")], snapshot=True)
    assert records.files(brain, "conflicts") == [misnamed]
    if incoming == "same":
        with pytest.raises(Error, match="SHA-256 filename"):
            records.upsert(brain, "conflicts", [Record(id=incoming, title="Incoming")], snapshot=False)
    else:
        # A window leaves unrelated files alone; bf validate still reports the misnamed one.
        assert records.upsert(brain, "conflicts", [Record(id=incoming, title="Incoming")], snapshot=False)["added"]
    assert brain.read(misnamed) == before
    assert not (brain.root / "memories/.pending").exists()


def test_collection_replaces_its_own_records_that_break_current_rules(brain: Store) -> None:
    # A stored record that breaks the rules, such as with a display-name alias or an over-long title, holds its
    # own id: the corrected sensor replaces it, and a snapshot that no longer returns it removes it.
    kept, gone = records.path("legacy", "w-legacy"), records.path("legacy", "gone")
    brain.write(kept, b'{"id":"w-legacy","title":"Old","aliases":["Alice Example"]}\n')
    brain.write(gone, b'{"id":"gone","title":"' + b"G" * 4097 + b'"}\n')
    corrected = [Record(id="w-legacy", title="New", aliases=["person:alice"])]
    assert records.upsert(brain, "legacy", corrected, snapshot=False)["updated"] == 1
    assert records.load(brain, kept).aliases == ["person:alice"]
    assert records.upsert(brain, "legacy", corrected, snapshot=True)["removed"] == 1
    assert records.files(brain, "legacy") == [kept]
