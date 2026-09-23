"""Failures found by reviewing durability and incomplete-result boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fkf import index, records
from fkf.cli import app
from fkf.models import Error
from fkf.storage import Store, writer
from fkf.update import update
from fkf.validate import validate


def test_health_check_rejects_a_stale_cache(base: Store) -> None:
    index.refresh(base)
    with writer(base):
        result = CliRunner().invoke(app, ["status", "--check", "--base", str(base.root)])
    assert result.exit_code == 1
    reply = json.loads(result.stdout)
    assert reply["bases"][0]["index"] == "stale"
    assert not reply["healthy"]


def test_update_reports_skipped_evidence_as_failure(base: Store) -> None:
    base.write("projects/broken.md", b"---\nstatus: invalid\n---\n# Broken\n")
    report = update([base])
    assert isinstance(report["bases"], list)
    assert report["bases"][0]["index"]["problems"] == 1
    assert not report["ok"]


def test_missing_record_in_corrupt_source_is_not_proven_absent(base: Store) -> None:
    base.write("records/meetings/2026-09.jsonl", b"broken partition\n")
    assert records.find(base, "meetings", "decision-1") is not None
    with pytest.raises(Error, match="unreadable partitions"):
        records.find(base, "meetings", "possibly-in-broken-file")
    assert records.find(base, "other", "absent") is None


@pytest.mark.parametrize("path", ["records/orphan.jsonl", "records/Bad/2026-09.jsonl", "records/mail/2026-99.jsonl"])
def test_invalid_record_paths_never_produce_unreadable_refs(base: Store, path: str) -> None:
    from fkf.models import Query
    from fkf.retrieve import search

    base.write(path, b'{"id":"item","title":"Misplacedneedle"}\n')
    reply = search([base], Query(text="misplacedneedle"), counted=False)
    assert reply["items"] == []
    assert reply["problems"]
    assert not validate(base)["valid"]


def test_unreadable_note_is_skipped_without_hiding_healthy_files(base: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    from fkf.models import Query
    from fkf.retrieve import search

    base.write("projects/denied.md", b"# Restricted\n")
    original = Store.read

    def denied(self: Store, name: str, limit: int = 16 << 20) -> bytes:
        if name == "projects/denied.md":
            raise PermissionError("private operating-system detail")
        return original(self, name, limit)

    monkeypatch.setattr(Store, "read", denied)
    reply = search([base], Query(text="offline"), counted=False)
    assert reply["items"]
    assert "inaccessible file" in str(reply["problems"])
    assert "private operating-system detail" not in str(reply)
    assert not validate(base)["valid"]


def test_validation_refuses_links_through_unindexed_symlinks(base: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private evidence")
    (base.root / "inputs").mkdir()
    (base.root / "inputs/direct.txt").symlink_to(outside / "secret.txt")
    (base.root / "inputs/redirect").symlink_to(outside, target_is_directory=True)
    base.write(
        "projects/links.md",
        b"# Links\n\n[direct](../inputs/direct.txt) [parent](../inputs/redirect/secret.txt)\n",
    )
    report = validate(base)
    assert not report["valid"]
    assert isinstance(report["problems"], list)
    assert len([p for p in report["problems"] if "broken link" in p]) == 2


def test_new_directories_are_durable_before_their_files(base: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[tuple[int, int]] = []
    fsync = os.fsync

    def record_sync(fd: int) -> None:
        info = os.fstat(fd)
        synced.append((info.st_dev, info.st_ino))
        fsync(fd)

    monkeypatch.setattr(os, "fsync", record_sync)
    base.write("new-parent/journal/original", b"durable original")
    expected = []
    for directory in (base.root, base.root / "new-parent", base.root / "new-parent/journal"):
        info = directory.stat()
        expected.append((info.st_dev, info.st_ino))
    assert all(identity in synced for identity in expected)
    assert synced.index(expected[0]) < synced.index(expected[1]) < synced.index(expected[2])
