"""Confined storage, strict formats and the user registry."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from bf import config
from bf.config import load, may_collect, one, register, select, user_config, user_path, yaml_object
from bf.models import Config, Error, Knowledge, Query, Record, Sensor, UserConfig, decode, moment, timestamp
from bf.storage import BusyError, Store, collecting, reader, relative, state_store, writer


def test_path_grammar() -> None:
    assert relative("concepts/a.md") == ("concepts", "a.md")
    for bad in ["/etc/passwd", "../x", "a//b", "a/./b", "a\\b", "a\x00b", ""]:
        with pytest.raises(Error):
            relative(bad)


def test_no_follow_reads_writes_and_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = Store(root)
    (tmp_path / "outside").write_text("secret")
    (root / "concepts").mkdir()
    (root / "concepts/link.md").symlink_to(tmp_path / "outside")
    with pytest.raises(OSError, match="symbolic links"):
        store.read("concepts/link.md")
    with pytest.raises(Error, match="symlinks"):
        store.files("concepts")
    (root / "concepts/link.md").unlink()
    (root / "linked").symlink_to(tmp_path)
    with pytest.raises(OSError, match=r"Not a directory|symbolic links"):
        store.write("linked/escape.md", b"x")
    assert not (tmp_path / "escape.md").exists()
    store.write("concepts/a.md", b"one")
    store.write("concepts/a.md", b"two")
    assert store.read("concepts/a.md") == b"two"
    with pytest.raises(Error, match="exceeds"):
        store.read("concepts/a.md", 2)
    (root / "concepts/dir.md").mkdir()
    with pytest.raises(Error, match="non-regular"):
        store.write("concepts/dir.md", b"x")
    with pytest.raises(Error):
        store.delete("concepts/dir.md")
    store.delete("concepts/a.md")
    assert store.files("concepts") == []
    assert store.files("missing") == []
    with pytest.raises(Error, match="expected a regular file"):
        store.fingerprint("concepts/dir.md")


def test_missing_brain_is_named(tmp_path: Path) -> None:
    with pytest.raises(Error, match="does not exist"):
        Store(tmp_path / "absent")
    (tmp_path / "file").write_text("")
    with pytest.raises(Error, match="directory"):
        Store(tmp_path / "file")


def test_writer_lock_is_exclusive_and_waits(brain: Store) -> None:
    with writer(brain), pytest.raises(BusyError), writer(brain, wait=0.1):
        pass
    with writer(brain):
        pass


def test_shared_read_locks_and_independent_collector_lock(brain: Store) -> None:
    with reader(brain), reader(brain), pytest.raises(BusyError), writer(brain):
        pass
    with writer(brain), pytest.raises(BusyError), reader(brain, wait=0):
        pass
    with (
        collecting(brain, "source"),
        writer(brain),
        collecting(brain, "other"),
        pytest.raises(BusyError),
        collecting(brain, "source"),
    ):
        pass


def test_rmdir_refuses_files_and_symlinks(brain: Store) -> None:
    brain.write("empty/item", b"data")
    with pytest.raises(Error, match="directory"):
        brain.rmdir("empty/item")
    (brain.root / "redirect").symlink_to(brain.root / "empty", target_is_directory=True)
    with pytest.raises(Error, match="directory"):
        brain.rmdir("redirect")
    brain.delete("empty/item")
    brain.rmdir("empty")
    assert not (brain.root / "empty").exists()


def test_state_stays_outside_the_brain_and_private(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    state = state_store(brain.root)
    assert not state.root.is_relative_to(brain.root)
    assert state.root.stat().st_mode & 0o077 == 0
    monkeypatch.setenv("XDG_STATE_HOME", str(brain.root / "state"))
    with pytest.raises(Error, match="outside the brain"):
        state_store(brain.root)
    target = brain.root.parent / "elsewhere"
    target.mkdir()
    (brain.root.parent / "redirect").symlink_to(target)
    monkeypatch.setenv("XDG_STATE_HOME", str(brain.root.parent / "redirect"))
    with pytest.raises(Error, match="symlinks"):
        state_store(brain.root)


def test_json_and_yaml_reject_ambiguity() -> None:
    for bad in [b'{"a":1,"a":2}', b"[NaN]", b"{"]:
        with pytest.raises(Error):
            decode(bad)
    assert decode('{"text":"\u2028 日本"}') == {"text": "\u2028 日本"}
    for bad in [b"a: 1\na: 2\n", b"a: &x 1\nb: *x\n", b"- 1\n", b"[" * 40 + b"]" * 40, b"\xff"]:
        with pytest.raises(Error):
            yaml_object(bad)
    assert yaml_object(b"") == {}
    assert yaml_object(b"day: 2026-09-01\n") == {"day": "2026-09-01"}


def test_models_are_strict_and_canonical() -> None:
    record = Record(id="x", title="T", time="2026-09-01T02:00:00+02:00", links=["b", "a", "a"])
    assert record.time == "2026-09-01T00:00:00.000000Z"
    assert record.links == ["a", "b"]
    for bad in [
        {"id": "x", "title": "T", "time": "2026-09-01T00:00:00"},
        {"id": "", "title": "T"},
        {"id": "x", "title": "bad\x07"},
        {"id": "x", "title": "T", "extra": 1},
        {"id": 1, "title": "T"},
    ]:
        with pytest.raises(ValidationError):
            Record.model_validate(bad)
    with pytest.raises(ValidationError):
        Knowledge.model_validate({"updated": "2026-9-1"})
    with pytest.raises(ValidationError):
        Knowledge.model_validate({"status": "current"})
    for command in [["{{start}}"], ["x", "{{secret}}"], ["x", "a\x00"]]:
        with pytest.raises(ValidationError):
            Sensor(command=command)
    with pytest.raises(ValidationError):
        Config.model_validate({"version": 3, "name": "Bad Name"})
    with pytest.raises(ValidationError, match="time window"):
        Query(text="  ")
    assert Query(status="active").text == ""
    with pytest.raises(ValidationError, match="earlier"):
        Query(since=timestamp("2026-09-02T00:00:00Z"), until=timestamp("2026-09-01T00:00:00Z"))


def test_relative_moments_resolve_deterministically() -> None:
    now = datetime(2026, 9, 22, 15, 30, tzinfo=UTC)
    assert moment("now", now) == "2026-09-22T15:30:00.000000Z"
    assert moment("7d", now) == "2026-09-15T15:30:00.000000Z"
    assert moment("12h", now) == "2026-09-22T03:30:00.000000Z"
    assert moment("2w", now) == "2026-09-08T15:30:00.000000Z"
    assert moment("2026-09-01T00:00:00+02:00", now) == "2026-08-31T22:00:00.000000Z"
    assert moment("today", now) < moment("now", now)
    assert moment("yesterday", now) < moment("today", now)
    assert moment("2026-09-01", now).startswith(("2026-08-31", "2026-09-01"))
    for bad in ["soon", "2026-13-01", "2026-09-01T00:00:00"]:
        with pytest.raises(Error):
            moment(bad, now)


def test_configuration_is_strict_and_version_3(brain: Store) -> None:
    assert load(brain).name == "fixture"
    brain.write("bf.yaml", b"version: 1\nid: x\nname: old\n")
    with pytest.raises(Error, match="version"):
        load(brain)
    brain.write("bf.yaml", b"version: 3\nname: fixture\nunknown: 1\n")
    with pytest.raises(Error, match="unknown"):
        load(brain)


def test_registry_selection_and_collection_trust(brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert may_collect(brain)
    assert user_config().brains["fixture"].collect
    assert user_path().read_text().startswith("# https://fmind.github.io/brain-framework/")
    other = tmp_path / "team"
    other.mkdir()
    team = Store(other)
    team.write("bf.yaml", b"version: 3\nname: team\n")
    assert not may_collect(team)
    register(team, collect=False)
    assert not may_collect(team)
    assert [s.root for s in select()] == [brain.root, team.root]
    assert one("team").root == team.root
    assert one(str(other)).root == team.root
    with pytest.raises(Error, match="several brains"):
        one()
    monkeypatch.setenv("BF_BRAIN", "fixture")
    assert [s.root for s in select()] == [brain.root]
    assert one("team").root == team.root
    monkeypatch.chdir(team.root)
    assert [s.root for s in select()] == [brain.root]
    monkeypatch.delenv("BF_BRAIN")
    monkeypatch.chdir(brain.root / "projects")
    assert [s.root for s in select()] == [brain.root]
    clone = tmp_path / "clone"
    clone.mkdir()
    Store(clone).write("bf.yaml", b"version: 3\nname: team\n")
    with pytest.raises(Error, match="already registered as team"):
        register(Store(clone), collect=False)
    brain.write("bf.yaml", b"version: 3\nname: renamed\n")
    with pytest.raises(Error, match="already registered as fixture"):
        register(brain, collect=True)
    monkeypatch.chdir(tmp_path)
    user_path().write_text(f"brains:\n  fixture:\n    path: {brain.root}\n  gone:\n    path: {tmp_path / 'gone'}\n")
    assert [s.root for s in select()] == [brain.root]
    with pytest.raises(Error, match="does not exist"):
        select("gone")
    user_path().write_text("brains:\n  bad:\n    path: 1\n")
    with pytest.raises(Error, match="invalid"):
        user_config()


def test_nothing_selected_is_actionable(tmp_path: Path) -> None:
    os.chdir(tmp_path)
    with pytest.raises(Error, match="bf register"):
        select()


def test_concurrent_registrations_keep_every_brain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stores = []
    for number in range(4):
        root = tmp_path / f"brain-{number}"
        root.mkdir()
        store = Store(root)
        store.write("bf.yaml", f"version: 3\nname: brain-{number}\n".encode())
        stores.append(store)
    original = config.user_config
    start = Barrier(len(stores))

    def slow_read() -> UserConfig:
        registry = original()
        # Widen the read/modify/write race without changing the result of a registry read.
        time.sleep(0.05)
        return registry

    def enroll(store: Store) -> None:
        start.wait(timeout=10)
        register(store, collect=False)

    monkeypatch.setattr(config, "user_config", slow_read)
    with ThreadPoolExecutor(max_workers=len(stores)) as executor:
        list(executor.map(enroll, stores))
    assert set(user_config().brains) == {f"brain-{number}" for number in range(4)}
    assert user_path().stat().st_mode & 0o077 == 0
