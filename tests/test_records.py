"""Monthly JSON Lines partitions keep exactly one line per source item."""

from __future__ import annotations

import pytest

from fkf import records
from fkf.models import Error, Record
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
