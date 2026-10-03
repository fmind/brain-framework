"""Confined storage, strict formats and the user registry."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from bf import config, storage
from bf.config import load, one, register, related, select, user_config, user_path, yaml_object
from bf.markdown import note
from bf.models import (
    RECORD_KEYS,
    Config,
    Error,
    Knowledge,
    Query,
    Record,
    Registration,
    Sensor,
    UserConfig,
    decode,
    digest,
    explain,
    moment,
    timestamp,
)
from bf.retrieve import search
from bf.storage import BusyError, Store, collecting, generation, lock_file, reader, relative, state_store, writer
from bf.watch_settings import settings


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
    with pytest.raises(Error, match="symlinks are not followed"):
        store.read("concepts/link.md")
    with pytest.raises(Error, match="symlinks"):
        store.files("concepts")
    # A caller that reports skipped entries gets them, fingerprinted without following the link.
    os.mkfifo(root / "concepts/pipe.md")
    skipped: dict[str, tuple[int, int, int, int]] = {}
    assert store.files("concepts", skipped=skipped) == []
    assert sorted(skipped) == ["concepts/link.md", "concepts/pipe.md"]
    assert skipped["concepts/link.md"][3] == (root / "concepts/link.md").lstat().st_ino
    (root / "concepts/pipe.md").unlink()
    (root / "concepts/link.md").unlink()
    (root / "linked").symlink_to(tmp_path)
    (tmp_path / "escape.md").write_text("outside")
    # A redirected folder is named relative to the brain, without its target.
    for access in (lambda: store.write("linked/escape.md", b"x"), lambda: store.read("linked/escape.md")):
        with pytest.raises(Error, match=r"^linked: expected a directory; symlinks and special files are not followed$"):
            access()
    assert (tmp_path / "escape.md").read_text() == "outside"
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
    store.write("concepts/nested/b.md", b"three")
    assert store.files("concepts") == ["concepts/a.md", "concepts/nested/b.md"]
    assert store.scan("concepts") == {name: store.fingerprint(name) for name in store.files("concepts")}
    assert store.scan("missing") == {}
    store.delete("concepts/nested/b.md")
    store.rmdir("concepts/nested")
    store.delete("concepts/a.md")
    assert store.files("concepts") == []
    assert store.files("missing") == []
    with pytest.raises(Error, match="expected a regular file"):
        store.fingerprint("concepts/dir.md")


def test_sockets_are_named_like_other_special_files(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # A relative name keeps the socket path within the AF_UNIX limit, as short as 104 bytes on macOS.
    monkeypatch.chdir(brain.root / "concepts")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind("talk.md")
        # Opening a socket fails before the type check: ENXIO on Linux, EOPNOTSUPP on macOS.
        with pytest.raises(Error, match=r"^concepts/talk\.md: expected a regular file$"):
            brain.read("concepts/talk.md")
        skipped: dict[str, tuple[int, int, int, int]] = {}
        assert brain.files("concepts", skipped=skipped) == ["concepts/evidence.md"]
        assert list(skipped) == ["concepts/talk.md"]
        problems = search([brain], Query(text="durable"))["problems"]
        assert problems == [{"brain": "fixture", "file": "concepts/talk.md", "error": "expected a regular file"}]


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
@pytest.mark.parametrize(("mode", "unreadable"), [(0, "memories"), (0o600, "memories/meetings")])
def test_an_unreadable_scanned_folder_is_skipped_whole(brain: Store, mode: int, unreadable: str) -> None:
    # Without read permission the scan cannot open memories/; without search permission, it cannot reach a source.
    (brain.root / "memories").chmod(mode)
    try:
        skipped: dict[str, tuple[int, int, int, int]] = {}
        assert brain.scan("memories/meetings", skipped=skipped) == {}
        assert list(skipped) == [unreadable]
        with pytest.raises(PermissionError):
            brain.scan("memories/meetings")
    finally:
        (brain.root / "memories").chmod(0o700)


def test_a_scan_without_fingerprints_lists_the_same_entries(brain: Store, tmp_path: Path) -> None:
    brain.write("memories/meetings/nested/item.json", b"{}")
    (brain.root / "memories/meetings/a\\b.json").write_bytes(b"{}")
    (brain.root / "memories/meetings/link.json").symlink_to(tmp_path)
    os.mkfifo(brain.root / "memories/meetings/pipe.json")
    skipped, passed, counts, listed_counts = {}, {}, {}, {}
    found = brain.scan("memories", skipped=skipped, split=True, counts=counts)
    listed = brain.scan("memories", skipped=passed, split=True, counts=listed_counts, fingerprints=False)
    # Counting a source before a commit, or listing its record names, once cost one stat per record.
    assert (listed.keys(), passed.keys(), listed_counts) == (found.keys(), skipped.keys(), counts)
    assert set(listed.values()) == {(0, 0, 0, 0)}
    assert len(skipped) == 3
    # Links and special files still fail a scan that does not report them.
    (brain.root / "memories/meetings/a\\b.json").unlink()
    with pytest.raises(Error, match=r"(link|pipe)\.json: symlinks and special files are forbidden"):
        brain.scan("memories/meetings", fingerprints=False)


def test_explanations_name_the_first_reasons_and_count_the_rest() -> None:
    with pytest.raises(ValidationError) as invalid:
        TypeAdapter(list[Record]).validate_python([{"id": str(n), "title": ""} for n in range(1000)])
    # One invalid item per printed record once built a message larger than the output it described.
    reasons = [f"{n}.title: String should have at least 1 character" for n in range(5)]
    assert explain(invalid.value) == "; ".join([*reasons, "and 995 more"])
    with pytest.raises(ValidationError) as redacted:
        Record.model_validate({"id": "x", "title": "T", "private-a": 1, "private-b": 2})
    assert explain(redacted.value, RECORD_KEYS) == "<key>: Extra inputs are not permitted"


def test_missing_brain_is_named(tmp_path: Path) -> None:
    with pytest.raises(Error, match="does not exist"):
        Store(tmp_path / "absent")
    (tmp_path / "file").write_text("")
    with pytest.raises(Error, match="directory"):
        Store(tmp_path / "file")


@pytest.mark.parametrize("extra", [b"more", b"too many bytes"])
def test_reads_handle_in_place_growth_without_returning_a_partial_file(
    brain: Store, monkeypatch: pytest.MonkeyPatch, extra: bytes
) -> None:
    brain.write("growing.txt", b"first")
    path = brain.root / "growing.txt"
    inode = path.stat().st_ino
    original = os.fstat

    def grow(fd: int) -> os.stat_result:
        info = original(fd)
        if info.st_ino == inode:
            with path.open("ab") as stream:
                stream.write(extra)
        return info

    monkeypatch.setattr(os, "fstat", grow)
    if len(b"first" + extra) <= 10:
        assert brain.read("growing.txt", 10) == b"first" + extra
    else:
        with pytest.raises(Error, match="exceeds 10 bytes"):
            brain.read("growing.txt", 10)


def test_writer_lock_is_exclusive_and_waits(brain: Store) -> None:
    with writer(brain), pytest.raises(BusyError), writer(brain, wait=0.1):
        pass
    with writer(brain):
        pass


@pytest.mark.parametrize(
    ("lock", "name"),
    [(writer, "write.lock"), (reader, "write.lock"), (partial(collecting, sensor="source"), "collect-source.lock")],
    ids=["writer", "reader", "collector"],
)
def test_lock_creation_race_keeps_the_contenders_inode(
    brain: Store,
    monkeypatch: pytest.MonkeyPatch,
    lock: Callable[..., AbstractContextManager[None]],
    name: str,
) -> None:
    folder, name = lock_file(brain, name)
    original = os.open
    contender: list[int] = []
    opened: list[int] = []
    calls: list[tuple[int, int | None]] = []

    def raced_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == name:
            calls.append((flags, dir_fd))
            if len(calls) == 1:
                # Reproduce the observed macOS first-create failure after a contender creates the leaf.
                fd = original(path, flags, mode, dir_fd=dir_fd)
                contender.append(fd)
                os.write(fd, b"contender")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise FileNotFoundError(errno.ENOENT, "concurrent first creation", path)
        fd = original(path, flags, mode, dir_fd=dir_fd)
        if path == name:
            opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", raced_open)
    try:
        with pytest.raises(BusyError), lock(brain, wait=0):
            pass
        assert len(calls) == 2
        assert calls[0][1] == calls[1][1]
        assert calls[1][0] == calls[0][0] & ~os.O_CREAT
        assert len(opened) == 1
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(opened[0])
        inode = os.fstat(contender[0]).st_ino
        path = folder.root / name
        assert path.stat().st_ino == inode
        assert path.read_bytes() == b"contender"
    finally:
        for fd in contender:
            os.close(fd)
    with lock(brain, wait=0):
        assert path.stat().st_ino == inode
        assert path.read_bytes() == b"contender"


@pytest.mark.parametrize("failure", [errno.ENOENT, errno.EACCES, errno.ELOOP])
def test_lock_creation_errors_do_not_create_a_replacement(
    brain: Store, monkeypatch: pytest.MonkeyPatch, failure: int
) -> None:
    folder, name = lock_file(brain, "write.lock")
    original = os.open
    calls: list[int] = []

    def failed_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == name:
            calls.append(flags)
            if len(calls) == 1:
                raise OSError(failure, "creation failed", path)
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", failed_open)
    with pytest.raises(OSError, match=name) as caught, writer(brain):
        pass
    assert caught.value.errno == failure
    assert len(calls) == (2 if failure == errno.ENOENT else 1)
    assert not (folder.root / name).exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_lock_creation_fallback_rejects_unsafe_leaves(brain: Store, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    folder, name = lock_file(brain, "write.lock")
    original = os.open
    path = folder.root / name
    target = brain.root.parent / "lock-target"
    target.write_bytes(b"untouched")
    calls = 0

    def raced_open(leaf: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal calls
        if leaf == name:
            calls += 1
            if calls == 1:
                if kind == "symlink":
                    path.symlink_to(target)
                elif kind == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    path.mkdir()
                raise FileNotFoundError(errno.ENOENT, "concurrent first creation", leaf)
        return original(leaf, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", raced_open)
    match = {"symlink": "symbolic links", "fifo": "invalid writer lock", "directory": "Is a directory"}[kind]
    with pytest.raises((Error, OSError), match=match), writer(brain):
        pass
    assert calls == 2
    assert target.read_bytes() == b"untouched"


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


def test_a_waiting_cache_replacement_goes_before_new_readers(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    waiting = Event()

    def sleep(seconds: float) -> None:
        waiting.set()
        time.sleep(seconds)

    monkeypatch.setattr(storage, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    replaced = Event()

    def replace() -> None:
        with generation(brain, shared=False, wait=10):
            replaced.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with generation(brain, shared=True, wait=0):
            publish = executor.submit(replace)
            assert waiting.wait(10)
            # Overlapping readers once kept a publish waiting until it failed: a new one now waits behind it.
            with pytest.raises(BusyError, match="being replaced"), generation(brain, shared=True, wait=0):
                pass
            assert not replaced.is_set()
        publish.result(timeout=10)
    assert replaced.is_set()
    with generation(brain, shared=True, wait=0):
        pass


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_an_inaccessible_state_directory_is_named(
    brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = tmp_path / "read-only-state"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setenv("XDG_STATE_HOME", str(locked))
    try:
        # Locks live there too: searches name the directory instead of a generic inaccessible path.
        for action in (lambda: state_store(brain.root), lambda: search([brain], Query(text="offline"))):
            with pytest.raises(Error, match=r"state directory .*read-only-state/bf is inaccessible.*XDG_STATE_HOME"):
                action()
    finally:
        locked.chmod(0o700)


def test_links_share_regular_files_without_following_or_replacing_links(brain: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (brain.root / "linked").symlink_to(outside)
    with pytest.raises(Error, match="linked: expected a regular file"):
        brain.link("linked", "copy")
    brain.write("original", b"bytes")
    (brain.root / "target").symlink_to(outside)
    with pytest.raises(Error, match="target: refusing to replace a non-regular file"):
        brain.link("original", "target")
    brain.link("original", "copy")
    brain.link("original", "nested/copy")
    assert os.path.samestat(brain.lstat("copy"), brain.lstat("original"))
    assert brain.read("nested/copy") == b"bytes"
    assert outside.read_text() == "private"
    assert (brain.root / "target").is_symlink()
    assert not [path for path in brain.root.iterdir() if path.name.startswith(".write-")]


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
    # mkdir's mode applies to the last directory only; the shared bf/ root is private too.
    assert state.root.stat().st_mode & 0o077 == state.root.parent.stat().st_mode & 0o077 == 0
    monkeypatch.setenv("XDG_STATE_HOME", str(brain.root / "state"))
    with pytest.raises(Error, match="outside the brain"):
        state_store(brain.root)
    target = brain.root.parent / "elsewhere"
    target.mkdir()
    (brain.root.parent / "redirect").symlink_to(target)
    monkeypatch.setenv("XDG_STATE_HOME", str(brain.root.parent / "redirect"))
    with pytest.raises(Error, match="symlinks"):
        state_store(brain.root)
    # A linked ancestor, such as /home -> /var/home on some systems, is part of the machine, not a redirect.
    (brain.root.parent / "linked-home").symlink_to(target)
    monkeypatch.setenv("XDG_STATE_HOME", str(brain.root.parent / "linked-home" / "state"))
    assert state_store(brain.root).root.is_relative_to(target.resolve() / "state" / "bf")


def test_existing_state_must_be_private(brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "private-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    shared = home / "bf" / digest(os.fsencode(brain.root))
    shared.mkdir(parents=True, mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(Error, match="owned by you with mode 700"):
        state_store(brain.root)
    shared.chmod(0o700)
    folder, _name = lock_file(brain, "write.lock")
    # The shared root created with the default umask is restricted too, so others cannot list brain digests.
    for directory in (state_store(brain.root).root, folder.root, home / "bf"):
        assert directory.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("value", ["", "relative/state", "~nonexistent-user-7f3a/state"])
def test_empty_or_relative_xdg_directories_are_ignored(
    brain: Store, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # The XDG specification treats empty and relative values as unset.
    monkeypatch.setenv("XDG_STATE_HOME", value)
    monkeypatch.setenv("XDG_CONFIG_HOME", value)
    home = Path.home()
    assert state_store(brain.root).root.is_relative_to(home.resolve() / ".local/state/bf")
    assert user_path() == home / ".config/bf/config.yaml"
    register(brain)
    assert not (brain.root.parent / "relative").exists()


def test_scan_skips_entries_removed_after_listing(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write("concepts/kept.md", b"# Kept\n")
    brain.write("concepts/.tmp-save", b"editor temporary file")
    brain.write("concepts/gone/draft.md", b"# Gone\n")
    original = os.scandir

    def racing(fd: int) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        entries = list(original(fd))
        # An editor's atomic save or a checkout removes entries right after the listing.
        for entry in entries:
            if entry.name == ".tmp-save":
                os.unlink(entry.name, dir_fd=fd)
            elif entry.name == "gone":
                os.unlink("gone/draft.md", dir_fd=fd)
                os.rmdir("gone", dir_fd=fd)
        return nullcontext(iter(entries))

    monkeypatch.setattr(os, "scandir", racing)
    assert brain.files("concepts") == ["concepts/evidence.md", "concepts/kept.md"]


def test_scan_bounds_name_the_directory(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    deep = "concepts/" + "d/" * 65
    brain.write(deep + "x.md", b"# Deep\n")
    with pytest.raises(Error, match=r"^concepts/(?:d/){64}d: directory exceeds the 64-level depth limit$"):
        brain.files("concepts")
    brain.write("projects/a.md", b"# A\n")
    monkeypatch.setattr(storage, "MAX_FILES", 2)
    brain.write("projects/b.md", b"# B\n")
    with pytest.raises(Error, match=r"^projects exceeds the 2-entry scan limit; keep bulky files in the brain's root"):
        brain.files("projects")


def test_the_scan_limit_bounds_each_source_of_memories(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage, "MAX_FILES", 3)
    for name in ("mail/1.json", "mail/2.json", "notes/1.json", "notes/2.json", "notes/3.json"):
        brain.write(f"memories/{name}", b"{}")
    with pytest.raises(Error, match=r"^memories exceeds the 3-entry scan limit"):
        brain.files("memories")
    counts: dict[str, int] = {}
    assert len(brain.scan("memories", split=True, counts=counts)) == 7
    assert counts == {"memories": 3, "memories/meetings": 2, "memories/mail": 2, "memories/notes": 3}
    brain.write("memories/notes/4.json", b"{}")
    with pytest.raises(
        Error, match=r"^memories/notes exceeds the 3-entry scan limit; split its sensor into several sources"
    ):
        brain.files("memories", split=True)


def test_program_locks_follow_the_physical_brain(brain: Store, tmp_path: Path) -> None:
    # A bind mount, container mount or case variant reaches one directory through another path.
    alias = tmp_path / "alias"
    alias.symlink_to(brain.root, target_is_directory=True)
    other = Store(brain.root)
    other.root = alias
    assert other.identity == brain.identity
    assert state_store(other.root).root != state_store(brain.root).root
    for name in (".update", "mail"):
        with (
            collecting(brain, name),
            pytest.raises(BusyError, match=f"^{re.escape(name)} is already running"),
            collecting(other, name),
        ):
            pass
    stores, problems = related([brain, other])
    assert stores == [brain]
    assert not problems


def test_writers_of_one_physical_brain_share_a_lock_whatever_the_path(brain: Store, tmp_path: Path) -> None:
    (tmp_path / "alias").symlink_to(brain.root, target_is_directory=True)
    with writer(brain), pytest.raises(BusyError), writer(Store(tmp_path / "alias")):
        pass
    # The lock stays in private state: nothing is created in the brain.
    folder, name = lock_file(brain, "write.lock")
    assert (folder.root / name).is_file()
    assert not folder.root.is_relative_to(brain.root)


def test_json_and_yaml_reject_ambiguity() -> None:
    for bad in [b'{"a":1,"a":2}', b"[NaN]", b"{"]:
        with pytest.raises(Error):
            decode(bad)
    assert decode('{"text":"\u2028 日本"}') == {"text": "\u2028 日本"}
    for bad in [b"a: 1\na: 2\n", b"a: &x 1\nb: *x\n", b"- 1\n", b"\xff"]:
        with pytest.raises(Error):
            yaml_object(bad)
    # A mapping holds at most 31 nested lists, or 19,991 scalars in one list: each limit, not the shape, rejects more.
    for depth, count, valid in ((31, 1, True), (32, 1, False), (1, 19_991, True), (1, 19_992, False)):
        data = b"a: " + b"[" * depth + b",".join([b"1"] * count) + b"]" * depth
        if valid:
            assert yaml_object(data)
        else:
            with pytest.raises(Error, match=r"^YAML structure exceeds its limit$"):
                yaml_object(data)
    # The same limits protect the frontmatter of notes from shared brains.
    with pytest.raises(Error, match=r"^concepts/deep\.md: YAML structure exceeds its limit$"):
        note("concepts/deep.md", b"---\na: " + b"[" * 32 + b"]" * 32 + b"\n---\n# Deep\n")
    assert yaml_object(b"") == {}
    # Syntax errors name the file and the position, never the content.
    with pytest.raises(Error, match=r"^suite.yaml: invalid YAML at line 2, column 4$"):
        yaml_object(b"a: 1\n  b: secret\n", "suite.yaml")
    with pytest.raises(Error, match=r"^projects/x.md: invalid YAML at line 3, column 9$"):
        note("projects/x.md", b"---\ntype: project\n  status: secret\n---\n# X\n")
    # A duplicate key is named by its position, like a syntax error, never by its text.
    with pytest.raises(Error, match=r"^suite.yaml: YAML mapping keys must be unique strings at line 2, column 1$"):
        yaml_object(b"a: 1\na: 2\n", "suite.yaml")
    with pytest.raises(Error, match=r"^projects/x.md: YAML mapping keys must be unique strings at line 3, column 1$"):
        note("projects/x.md", b"---\ntype: project\ntype: secret\n---\n# X\n")
    assert yaml_object(b"day: 2026-09-01\n") == {"day": "2026-09-01"}


def test_yaml_follows_the_1_2_core_schema(brain: Store) -> None:
    # Editors validate YAML 1.2: these stay strings, and a leading zero stays decimal.
    assert yaml_object(b"on: yes\nno: off\ntags: [yes, no, 2026]\nt: [true, 017, 0o17, 0x1f, 1:30, 1.5, ~]\n") == {
        "on": "yes",
        "no": "off",
        "tags": ["yes", "no", 2026],
        "t": [True, 17, 15, 31, "1:30", 1.5, None],
    }
    assert note("concepts/x.md", b"---\ntags: [yes, 2026]\n---\n# X\n").knowledge.tags == ["yes", "2026"]
    brain.write("bf.yaml", b"version: 7\nname: fixture\nsensors:\n  on:\n    command: [echo]\n    timeout: 1:30\n")
    with pytest.raises(Error, match=r"sensors\.on\.timeout"):
        load(brain)
    brain.write("bf.yaml", b"version: 7\nname: fixture\nwatch:\n  notifications: off\n")
    assert settings(brain).notifications == "off"


def test_yaml_scalar_conversion_errors_are_safe_file_diagnostics() -> None:
    # Hex and octal integers keep the digit limit of decimal ones: replies and the cache print them in decimal.
    for value in (b"9" * 5000, b"0x" + b"f" * 3600, b"0o" + b"7" * 4800):
        with pytest.raises(Error, match=r"^note\.md: invalid YAML at line 2, column 16$") as failure:
            yaml_object(b"ok: 1\nprivate-field: " + value, "note.md")
        assert "private-field" not in str(failure.value)
    assert yaml_object(b"n: 0x" + b"f" * 3500) == {"n": int("f" * 3500, 16)}


def test_registry_fifo_fails_without_waiting_for_a_writer(tmp_path: Path) -> None:
    path = user_path()
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    result = subprocess.run(
        [sys.executable, "-m", "bf", "read"], cwd=tmp_path, capture_output=True, text=True, timeout=5, check=False
    )
    assert result.returncode == 1
    assert not result.stdout
    assert "regular file" in result.stderr
    assert "Traceback" not in result.stderr


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
    # OKF notes use draft, stable or deprecated (bf validate); attachments keep their own bounded words.
    assert Knowledge.model_validate({"status": "current"}).status == "current"
    with pytest.raises(ValidationError):
        Knowledge.model_validate({"status": "x" * 129})
    # Only namespaced aliases name a note; `bf validate` rejects display names in OKF notes.
    assert Knowledge.model_validate({"aliases": ["repo:x/y", "Website"]}).names == ["repo:x/y"]
    for bad in [
        {"id": "x", "title": "T", "url": "https://example.test/\x1b[2J"},
        {"id": "x", "title": "T", "url": "https://example.test/" + "x" * 8192},
        {"id": "x", "title": "T", "aliases": ["Plain Name"]},
        {"id": "x", "title": "T", "attributes": {"path": "caf\udce9.txt"}},
        {"id": "x", "title": "T", "fields": {"kind": ["\ud800"]}},
        {"id": "x", "title": "T", "links": ["repo:\ud800"]},
        {"id": "x", "title": "T", "aliases": ["file:caf\udce9"]},
    ]:
        with pytest.raises(ValidationError, match=r"url|aliases|attributes|fields|links"):
            Record.model_validate(bad)
    assert Record(id="x", title="T", url="https://example.test/a").url == "https://example.test/a"
    for command in [["{{start}}"], ["x", "{{secret}}"], ["x", "a\x00"], ["/usr/bin/env"], ["sensors/../x.sh"]]:
        with pytest.raises(ValidationError):
            Sensor(command=command)
    assert Sensor(command=["sensors/mail.py", "{{start}}"]).command[0] == "sensors/mail.py"
    with pytest.raises(ValidationError):
        Config.model_validate({"version": 3, "name": "Bad Name"})
    with pytest.raises(ValidationError, match="give words"):
        Query(text="  ")
    with pytest.raises(ValidationError, match="earlier"):
        Query(text="x", since=timestamp("2026-09-02T00:00:00Z"), until=timestamp("2026-09-01T00:00:00Z"))
    with pytest.raises(ValidationError, match="identity"):
        Query(text="x", target="Alice")


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
    # Like periods, windows and dates use ASCII digits only: Arabic-Indic 12h is not a time.
    for bad in ["soon", "2026-13-01", "2026-09-01T00:00:00", "\u0661\u0662h"]:
        with pytest.raises(Error):
            moment(bad, now)


def test_configuration_is_strict_and_version_7(brain: Store) -> None:
    assert load(brain).name == "fixture"
    # One diagnostic names the supported format instead of every field a format change renamed.
    for data, problem in (
        (b"name: fixture\n", "has no version"),
        (b"version: 4\nid: x\nname: old\n", "declares version 4"),
        (b"version: 8\nname: fixture\nfuture: {}\n", "declares version 8"),
        (b'version: "7"\nname: fixture\n', "declares an invalid version"),
    ):
        brain.write("bf.yaml", data)
        with pytest.raises(Error, match=rf"^bf\.yaml {problem}; this release reads version: 7$"):
            load(brain)
    brain.write("bf.yaml", b"version: 7\nname: fixture\nunknown: 1\n")
    with pytest.raises(Error, match="unknown"):
        load(brain)


def test_registry_selection(brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert user_path().read_text().startswith("# https://fmind.github.io/brain-framework/")
    other = tmp_path / "team"
    other.mkdir()
    team = Store(other)
    team.write("bf.yaml", b"version: 7\nname: team\n")
    register(team)
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
    Store(clone).write("bf.yaml", b"version: 7\nname: team\n")
    with pytest.raises(Error, match="already registered as team"):
        register(Store(clone))
    brain.write("bf.yaml", b"version: 7\nname: renamed\n")
    with pytest.raises(Error, match="already registered as fixture"):
        register(brain)
    monkeypatch.chdir(tmp_path)
    user_path().write_text(f"brains:\n  fixture:\n    path: {brain.root}\n  gone:\n    path: {tmp_path / 'gone'}\n")
    assert [s.root for s in select()] == [brain.root]
    with pytest.raises(Error, match=r"^gone: registered brain directory is absent on this machine; restore it"):
        select("gone")
    user_path().write_text("brains:\n  bad:\n    path: 1\n")
    with pytest.raises(Error, match="invalid"):
        user_config()
    user_path().write_text("brains: [oops\n")
    with pytest.raises(Error, match=f"^{user_path()}: invalid YAML at line 2"):
        user_config()


def test_registering_a_moved_brain_replaces_its_absent_entry(brain: Store, tmp_path: Path) -> None:
    moved = tmp_path / "moved"
    brain.root.rename(moved)
    with pytest.raises(Error, match=r"^fixture: registered brain directory is absent on this machine"):
        one("fixture")
    # Registration once refused, advising to rename one of two brains while only one existed: a rename breaks addresses.
    reply = register(Store(moved))
    assert reply == {"brain": "fixture", "path": str(moved), "config": str(user_path()), "replaced": str(brain.root)}
    assert one("fixture").root == moved
    assert "replaced" not in register(Store(moved))
    # An entry reaching the same physical brain through another path moves too.
    alias = tmp_path / "alias"
    alias.symlink_to(moved, target_is_directory=True)
    user_path().write_text(f"brains:\n  fixture:\n    path: {alias}\n")
    assert register(Store(moved))["replaced"] == str(alias)
    assert user_config().brains["fixture"].path == str(moved)


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_registering_keeps_an_entry_whose_directory_cannot_be_reached(brain: Store, tmp_path: Path) -> None:
    # A locked folder or a disconnected mount may still hold the registered brain: only a missing path frees its name.
    locked = tmp_path / "locked"
    locked.mkdir()
    brain.root.rename(locked / "brain")
    user_path().write_text(f"brains:\n  fixture:\n    path: {locked / 'brain'}\n")
    clone, other = tmp_path / "clone", tmp_path / "other"
    for root, name in ((clone, "fixture"), (other, "other")):
        root.mkdir()
        Store(root).write("bf.yaml", f"version: 7\nname: {name}\n".encode())
    locked.chmod(0)
    try:
        with pytest.raises(
            Error,
            match=rf"^cannot reach the brain registered as fixture at {re.escape(str(locked / 'brain'))}; restore "
            rf"access to it, or remove that entry from {re.escape(str(user_path()))}$",
        ):
            register(Store(clone))
        # Another name still registers: the unreachable entry blocks only its own.
        assert "replaced" not in register(Store(other))
    finally:
        locked.chmod(0o700)
    assert {name: entry.path for name, entry in user_config().brains.items()} == {
        "fixture": str(locked / "brain"),
        "other": str(other),
    }


def test_registration_conflicts_name_the_entry_to_change(brain: Store, tmp_path: Path) -> None:
    registry = re.escape(str(user_path()))
    clone = tmp_path / "clone"
    clone.mkdir()
    Store(clone).write("bf.yaml", b"version: 7\nname: fixture\n")
    # Two present brains claim one name: the owner chooses which entry to keep.
    with pytest.raises(
        Error,
        match=rf"^another brain is already registered as fixture at {re.escape(str(brain.root))}; remove that entry "
        rf"from {registry}, or rename one of the brains in its bf\.yaml$",
    ):
        register(Store(clone))
    brain.write("bf.yaml", b"version: 7\nname: renamed\n")
    with pytest.raises(
        Error, match=rf"^this brain is already registered as fixture; remove that entry from {registry}, then run"
    ):
        register(brain)
    assert user_config().brains == {"fixture": Registration(path=str(brain.root))}


def test_an_emptied_registry_still_registers(brain: Store) -> None:
    # Deleting the last entry, as the configuration guide suggests, leaves `brains:` null.
    user_path().write_text("# Machine-local brains\nbrains:\n")
    assert user_config().brains == {}
    with pytest.raises(Error, match="no brain selected"):
        select()
    register(brain)
    assert user_path().read_text().startswith("# Machine-local brains\nbrains:\n  fixture:\n")


def test_registration_keeps_the_owner_header(brain: Store) -> None:
    header = "# Machine-local registry; use bf register.\n# Second line\n"
    user_path().write_text(header + user_path().read_text().split("\n", 1)[1] + "# trailing note\n")
    register(brain)
    written = user_path().read_text()
    assert written.startswith(header + "brains:\n")
    assert "trailing note" not in written
    assert user_config().brains["fixture"].model_dump() == {"path": str(brain.root)}


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
        store.write("bf.yaml", f"version: 7\nname: brain-{number}\n".encode())
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
        register(store)

    monkeypatch.setattr(config, "user_config", slow_read)
    with ThreadPoolExecutor(max_workers=len(stores)) as executor:
        list(executor.map(enroll, stores))
    assert set(user_config().brains) == {f"brain-{number}" for number in range(4)}
    assert user_path().stat().st_mode & 0o077 == 0
