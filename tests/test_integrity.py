"""Failures found by reviewing durability and incomplete-result boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bf import index, records
from bf.cli import app
from bf.models import Error
from bf.storage import Store, writer
from bf.update import update
from bf.validate import validate


def test_health_check_rejects_a_stale_cache(brain: Store) -> None:
    index.refresh(brain)
    with writer(brain):
        result = CliRunner().invoke(app, ["status", "--check", "--brain", str(brain.root)])
    assert result.exit_code == 1
    reply = json.loads(result.stdout)
    assert reply["brains"][0]["index"] == "stale"
    assert not reply["healthy"]


def test_update_reports_skipped_evidence_as_failure(brain: Store) -> None:
    brain.write("projects/broken.md", b"---\nstatus: invalid\n---\n# Broken\n")
    report = update([brain])
    assert isinstance(report["brains"], list)
    assert report["brains"][0]["index"]["problems"] == 1
    assert not report["ok"]


def test_missing_record_in_corrupt_source_is_not_proven_absent(brain: Store) -> None:
    brain.write("memories/meetings/2026-09.jsonl", b"broken partition\n")
    assert records.find(brain, "meetings", "decision-1") is not None
    with pytest.raises(Error, match="unreadable partitions"):
        records.find(brain, "meetings", "possibly-in-broken-file")
    assert records.find(brain, "other", "absent") is None


@pytest.mark.parametrize("path", ["memories/orphan.jsonl", "memories/Bad/2026-09.jsonl", "memories/mail/2026-99.jsonl"])
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
    assert len([p for p in report["problems"] if "broken link" in p]) == 2


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
