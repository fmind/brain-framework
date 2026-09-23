"""Confined storage, strict formats and the user registry."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from fkf.config import load, may_collect, one, register, select, user_config, user_path, yaml_object
from fkf.models import Config, Error, Knowledge, Query, Record, Source, decode, moment, timestamp
from fkf.storage import BusyError, Store, relative, state_store, writer


def test_path_grammar() -> None:
    assert relative("wiki/a.md") == ("wiki", "a.md")
    for bad in ["/etc/passwd", "../x", "a//b", "a/./b", "a\\b", "a\x00b", ""]:
        with pytest.raises(Error):
            relative(bad)


def test_no_follow_reads_writes_and_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = Store(root)
    (tmp_path / "outside").write_text("secret")
    (root / "wiki").mkdir()
    (root / "wiki/link.md").symlink_to(tmp_path / "outside")
    with pytest.raises(OSError, match="symbolic links"):
        store.read("wiki/link.md")
    with pytest.raises(Error, match="symlinks"):
        store.files("wiki")
    (root / "wiki/link.md").unlink()
    (root / "linked").symlink_to(tmp_path)
    with pytest.raises(OSError, match=r"Not a directory|symbolic links"):
        store.write("linked/escape.md", b"x")
    assert not (tmp_path / "escape.md").exists()
    store.write("wiki/a.md", b"one")
    store.write("wiki/a.md", b"two")
    assert store.read("wiki/a.md") == b"two"
    with pytest.raises(Error, match="exceeds"):
        store.read("wiki/a.md", 2)
    (root / "wiki/dir.md").mkdir()
    with pytest.raises(Error, match="non-regular"):
        store.write("wiki/dir.md", b"x")
    with pytest.raises(Error):
        store.delete("wiki/dir.md")
    store.delete("wiki/a.md")
    assert store.files("wiki") == []
    assert store.files("missing") == []
    with pytest.raises(Error, match="expected a regular file"):
        store.fingerprint("wiki/dir.md")


def test_missing_base_is_named(tmp_path: Path) -> None:
    with pytest.raises(Error, match="does not exist"):
        Store(tmp_path / "absent")
    (tmp_path / "file").write_text("")
    with pytest.raises(Error, match="directory"):
        Store(tmp_path / "file")


def test_writer_lock_is_exclusive_and_waits(base: Store) -> None:
    with writer(base), pytest.raises(BusyError), writer(base, wait=0.1):
        pass
    with writer(base):
        pass


def test_state_stays_outside_the_base_and_private(base: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    state = state_store(base.root)
    assert not state.root.is_relative_to(base.root)
    assert state.root.stat().st_mode & 0o077 == 0
    monkeypatch.setenv("XDG_STATE_HOME", str(base.root / "state"))
    with pytest.raises(Error, match="outside the base"):
        state_store(base.root)
    target = base.root.parent / "elsewhere"
    target.mkdir()
    (base.root.parent / "redirect").symlink_to(target)
    monkeypatch.setenv("XDG_STATE_HOME", str(base.root.parent / "redirect"))
    with pytest.raises(Error, match="symlinks"):
        state_store(base.root)


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
            Source(command=command)
    with pytest.raises(ValidationError):
        Config.model_validate({"version": 2, "name": "Bad Name"})
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


def test_configuration_is_strict_and_version_2(base: Store) -> None:
    assert load(base).name == "fixture"
    base.write("fkf.yaml", b"version: 1\nid: x\nname: old\n")
    with pytest.raises(Error, match="FKF 7"):
        load(base)
    base.write("fkf.yaml", b"version: 2\nname: fixture\nunknown: 1\n")
    with pytest.raises(Error, match="unknown"):
        load(base)


def test_registry_selection_and_collection_trust(base: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert may_collect(base)
    assert user_config().bases["fixture"].collect
    assert user_path().read_text().startswith("# https://fmind.github.io/fkf/")
    other = tmp_path / "team"
    other.mkdir()
    team = Store(other)
    team.write("fkf.yaml", b"version: 2\nname: team\n")
    assert not may_collect(team)
    register(team, collect=False)
    assert not may_collect(team)
    assert [s.root for s in select()] == [base.root, team.root]
    assert one("team").root == team.root
    assert one(str(other)).root == team.root
    with pytest.raises(Error, match="several bases"):
        one()
    monkeypatch.setenv("FKF_BASE", "fixture")
    assert [s.root for s in select()] == [base.root]
    monkeypatch.delenv("FKF_BASE")
    monkeypatch.chdir(base.root / "projects")
    assert [s.root for s in select()] == [base.root]
    clone = tmp_path / "clone"
    clone.mkdir()
    Store(clone).write("fkf.yaml", b"version: 2\nname: team\n")
    with pytest.raises(Error, match="already registered as team"):
        register(Store(clone), collect=False)
    base.write("fkf.yaml", b"version: 2\nname: renamed\n")
    with pytest.raises(Error, match="already registered as fixture"):
        register(base, collect=True)
    monkeypatch.chdir(tmp_path)
    user_path().write_text(f"bases:\n  fixture:\n    path: {base.root}\n  gone:\n    path: {tmp_path / 'gone'}\n")
    assert [s.root for s in select()] == [base.root]
    with pytest.raises(Error, match="does not exist"):
        select("gone")
    user_path().write_text("bases:\n  bad:\n    path: 1\n")
    with pytest.raises(Error, match="invalid"):
        user_config()


def test_nothing_selected_is_actionable(tmp_path: Path) -> None:
    os.chdir(tmp_path)
    with pytest.raises(Error, match="fkf register"):
        select()
