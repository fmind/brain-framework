"""Tests for models, confined O_NOFOLLOW storage, stdlib cron, YAML 1.2 parsing, and brain selection."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from bf import config, cron, models, yaml_io
from bf.models import BusyError, Error, FormatError
from bf.storage import Store, expand, private_dir, relative, tail_lines, writer


def test_models_sanitization_json_and_dates() -> None:
    assert models.clean("hello") == "hello"
    for bad in ("", "   ", "line\nbreak", "bell\x07"):
        with pytest.raises(Error, match="nonempty and contain no control"):
            models.clean(bad)

    assert models.clean_text("line 1\n\tline 2\r\n") == "line 1\n\tline 2\r\n"
    with pytest.raises(Error, match="forbidden control"):
        models.clean_text("bad\x00byte")

    assert models.FORMAT == 19
    assert not models.invisible("plain-ascii")
    assert not models.invisible("éàü")
    assert models.invisible("bidir\u202eattack")
    assert models.invisible("tag\U000e0041char")
    assert models.invisible("vs\ufe0fchar")
    assert "\\u001b" in models.printable("esc\x1b[31m")
    assert b"\\u202e" in models.terminal({"msg": "rtl\u202e"})
    assert models.terminal({"msg": "ok"}) == b'{"msg":"ok"}\n'
    line = models.log_line("evt.ok", severity="warn", time_iso="2026-11-15T10:00:00Z", drop=None, msg="rtl\u202e")
    assert line.endswith(b"\n")
    assert b"\\u202e" in line
    log_obj = json.loads(line)
    assert log_obj == {"time": "2026-11-15T10:00:00Z", "severity": "WARN", "message": "evt.ok", "msg": "rtl\u202e"}
    assert "drop" not in log_obj

    with pytest.raises(Error, match="finite UTF-8"):
        models.encode({"bad": float("nan")})
    with pytest.raises(Error, match="duplicate key"):
        models.decode('{"a": 1, "a": 2}')
    with pytest.raises(Error, match="must be finite"):
        models.decode('{"a": NaN}')
    assert models.decode('{"a": 1, "a": 2}', check_duplicates=False) == {"a": 2}
    redacted = models.redact_secrets(
        "Bearer tok_12345678 ya29.a0AfH6SMC_secret_token DB_PASSWORD=pw1 GOOGLE_CLIENT_SECRET=cs1 "
        'AWS_SECRET_ACCESS_KEY=ak1 MY_API_KEY=mk1 "db_password": "pw2" exact_custom_secret_value',
        secrets=("exact_custom_secret_value", "short"),
    )
    for leaked in ("ya29.", "pw1", "cs1", "ak1", "mk1", "pw2", "exact_custom_secret_value"):
        assert leaked not in redacted
    assert redacted.startswith("[REDACTED] [REDACTED]")
    with pytest.raises(Error, match="invalid JSON"):
        models.decode("{broken")

    assert models.normalize_date("2026-11-15") == "2026-11-15"
    for bad_date in ("2026-2-3", "2026-02-29", "0001-01-01", "9999-12-31", "not-a-date"):
        with pytest.raises(Error, match="YYYY-MM-DD"):
            models.normalize_date(bad_date)

    assert models.normalize_time("2026-11-15T10:30:00+02:00") == "2026-11-15T08:30:00Z"
    with pytest.raises(Error, match="ISO 8601"):
        models.normalize_time("nope")
    with pytest.raises(Error, match="timezone offset or Z"):
        models.normalize_time("2026-11-15T10:30:00")
    with pytest.raises(Error, match="outside the supported UTC range"):
        models.normalize_time("0001-01-01T01:00:00+05:00")

    fixed = datetime(2026, 11, 15, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert models.now_utc(fixed) == "2026-11-15T10:00:00Z"
    assert models.now_utc().endswith("Z")

    assert models.failure(Error("safe msg")) == "safe msg"
    assert "not valid UTF-8" in models.failure(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"))
    assert "locale must use UTF-8" in models.failure(UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate"))
    assert "Permission denied" in models.failure(PermissionError(13, "Permission denied"))
    assert models.failure(OSError()).startswith("inaccessible file")
    assert models.failure(RuntimeError("secret")) == "unexpected failure"

    # Explicit duration units only (bare positive integers rejected; 0 / "0" allowed with allow_zero=True).
    assert models.parse_duration("30s") == 30
    assert models.parse_duration("0", allow_zero=True) == 0
    assert models.parse_duration(0, allow_zero=True) == 0
    assert models.parse_duration("15m") == 900
    assert models.parse_duration("1h30m") == 5400
    assert models.parse_duration("2h") == 7200
    assert models.parse_duration("1d") == 86400
    assert models.parse_duration("1w") == 604800
    assert models.clean_env("") == ""
    assert models.clean_env("ok-value") == "ok-value"
    with pytest.raises(Error, match="no control characters"):
        models.clean_env("bad\nline")
    for bad_dur in (True, 0, 30, -5, float("nan"), float("inf"), "bad", "0s", "150ms", [], "999y", "30s\n"):
        with pytest.raises(Error):
            models.parse_duration(bad_dur)


def test_stdlib_cron_parsing_macros_and_dst() -> None:
    assert cron.is_valid_cron("0 8 * * *")
    assert cron.is_valid_cron("*/15 9-17 * * mon-fri")
    assert cron.is_valid_cron("@daily")
    assert cron.is_valid_cron("@hourly")
    assert cron.is_valid_cron("@weekly")
    assert cron.is_valid_cron("@monthly")
    assert cron.is_valid_cron("@yearly")
    assert cron.is_valid_cron("0 0 1 jan,jun 5-7")
    assert cron.is_valid_cron("0 0 1 1 7")
    for bad_c in ("not a cron", "0 0 31 2 *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "5-2 * * * *", "0 0 * * 8"):
        assert not cron.is_valid_cron(bad_c)

    # Next & prev UTC
    t_ref = datetime(2026, 11, 15, 7, 30, tzinfo=UTC)
    assert cron.next_cron("0 8 * * *", t_ref) == datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
    assert cron.prev_cron("0 8 * * *", t_ref) == datetime(2026, 11, 14, 8, 0, tzinfo=UTC)

    # DST spring-forward (Europe/Paris 2026-03-29: 02:00 jumps to 03:00 CEST = 01:00Z).
    paris = ZoneInfo("Europe/Paris")
    pre_dst = datetime(2026, 3, 29, 0, 59, tzinfo=UTC).astimezone(paris)
    assert cron.prev_cron("0 2 * * *", pre_dst).astimezone(UTC) == datetime(2026, 3, 28, 1, 0, tzinfo=UTC)
    post_dst = datetime(2026, 3, 29, 1, 5, tzinfo=UTC).astimezone(paris)
    assert cron.prev_cron("0 2 * * *", post_dst).astimezone(UTC) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)

    # DST fall-back (America/New_York 2026-11-01: 01:00 occurs at 05:00Z EDT and 06:00Z EST).
    ny = ZoneInfo("America/New_York")
    mid_edt = datetime(2026, 11, 1, 5, 30, tzinfo=UTC).astimezone(ny)
    assert cron.next_cron("0 * * * *", mid_edt).astimezone(UTC) == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    mid_est = datetime(2026, 11, 1, 6, 15, tzinfo=UTC).astimezone(ny)
    assert cron.prev_cron("0 * * * *", mid_est).astimezone(UTC) == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)


def test_check_version_rejects_missing_bool_and_wrong_versions() -> None:
    models.check_version({"version": models.FORMAT}, "bf.yaml")
    with pytest.raises(FormatError, match="has no version"):
        models.check_version({}, "bf.yaml")
    for legacy in (11, 13, 14, 15, 16, 17, 18):
        with pytest.raises(FormatError, match=rf"declares version {legacy}"):
            models.check_version({"version": legacy}, "bf.yaml")
    with pytest.raises(FormatError, match="declares an invalid version"):
        models.check_version({"version": True}, "bf.yaml")
    with pytest.raises(FormatError, match="declares an invalid version"):
        models.check_version({"version": 5000}, "bf.yaml")


def test_store_containment_symlinks_special_files_and_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Error, match="does not exist"):
        Store(tmp_path / "missing")
    regular = tmp_path / "file.txt"
    regular.write_text("hi")
    with pytest.raises(Error, match="must be a directory"):
        Store(regular)

    for bad_rel in ("", "/abs", "a/../b", "a/./b", "a//b", "a\\b", "a\x00b"):
        with pytest.raises(Error, match="normalized brain-relative path"):
            relative(bad_rel)

    root = tmp_path / "store"
    root.mkdir()
    store = Store(root)
    store.write("notes/a.txt", b"alpha", durable=False)
    assert store.is_dir("notes")
    assert not store.is_dir("notes/a.txt")
    assert not store.is_dir("notes/missing")
    assert store.exists("notes/a.txt")
    assert not store.exists("notes/missing.txt")
    assert store.read("notes/a.txt") == b"alpha"
    with pytest.raises(Error, match="exceeds 3 bytes"):
        store.read("notes/a.txt", limit=3)

    # Full-line `iter_lines` never splits a line across internal read chunks!
    big_ln = b"{" + (b"x" * 70_000) + b"}\n{" + (b"y" * 70_000) + b"}\n"
    store.write("notes/biglines.jsonl", big_ln, durable=False)
    got_lines = list(store.iter_lines("notes/biglines.jsonl"))
    assert len(got_lines) == 2
    assert b"".join(got_lines) == big_ln
    store.delete("notes/biglines.jsonl", durable=False)

    # Parent path traverses through a regular file or symlink -> rejected.
    with pytest.raises(Error, match="expected a directory, found a file"):
        store.read("notes/a.txt/child")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "linked-dir").symlink_to(outside)
    with pytest.raises(Error, match="symlinks and special files are not followed"):
        store.read("linked-dir/secret.txt")

    # Leaf symlink rejected on read, write replacement, exists, and delete.
    (root / "notes" / "sym.txt").symlink_to(outside / "secret.txt")
    assert not store.exists("notes/sym.txt")
    with pytest.raises(Error, match="symlinks are not followed"):
        store.read("notes/sym.txt")
    with pytest.raises(Error, match="refusing to replace a non-regular file"):
        store.write("notes/sym.txt", b"overwrite")
    with pytest.raises(Error, match="refusing to delete a non-regular file"):
        store.delete("notes/sym.txt")

    # Special FIFO rejected on read and recorded in subdirs(skipped=...).
    fifo = root / "notes" / "pipe.fifo"
    os.mkfifo(fifo)
    with pytest.raises(Error, match="expected a regular file"):
        store.read("notes/pipe.fifo")
    (root / "notes" / ".write-0123456789abcdef0123456789abcdef").write_text("tmp")
    (root / "notes" / "bad\\slash.txt").write_text("backslash")

    with pytest.raises(Error):
        store.subdirs("notes")
    skipped: list[str] = []
    sub_d, listed = store.subdirs("notes", skipped=skipped)
    assert sub_d == []
    assert listed == ["notes/a.txt"]
    assert len(skipped) == 3

    assert store.subdirs("nonexistent", skipped=[]) == ([], [])
    skipped_top: list[str] = []
    assert store.subdirs("linked-dir", skipped=skipped_top) == ([], [])
    assert skipped_top
    assert "symlinks" in skipped_top[0]
    (root / "plain-file").write_text("x")
    skipped_file: list[str] = []
    assert store.subdirs("plain-file", skipped=skipped_file) == ([], [])
    assert skipped_file
    assert "expected a directory" in skipped_file[0]

    store.delete("notes/a.txt", durable=False)
    assert not store.exists("notes/a.txt")
    store.sync_dirs([])
    store.sync_dirs(["notes"])

    # Scan limits (MAX_FILES).
    store.write("many/f1.txt", b"1", durable=False)
    store.write("many/f2.txt", b"2", durable=False)
    store.sync_dirs(["many", "many", ""])
    with pytest.raises(Error, match="symlinks and special files"):
        store.sync_dirs(["linked-dir"])
    with pytest.raises(Error, match="expected a directory, found a file"):
        store.sync_dirs(["plain-file"])
    with pytest.raises(Error, match="normalized brain-relative path"):
        store.sync_dirs(["../escape"])
    with pytest.raises(FileNotFoundError):
        store.sync_dirs(["missing-dir"])
    monkeypatch.setattr("bf.storage.MAX_FILES", 1)
    with pytest.raises(Error, match="1-entry scan limit"):
        store.subdirs("many")
    monkeypatch.setattr("bf.storage.MAX_FILES", 100_000)

    # Private `.bf` state, nested private `delete_tree`, and writer lock contention.
    with pytest.raises(Error, match="invalid private state path"):
        private_dir(store, "../escape")
    bf_dir = private_dir(store)
    assert bf_dir.root == root / ".bf"
    bf_dir.write("sub/nested/file.txt", b"ok", mode=0o600)
    assert oct((root / ".bf" / "sub").stat().st_mode & 0o777) == "0o700"
    bf_dir.delete_tree("sub")
    assert not (root / ".bf" / "sub").exists()
    with pytest.raises(Error, match="restricted to private stores"):
        store.delete_tree("many")

    (root / ".bf").chmod(0o755)
    with pytest.raises(Error, match="mode 700"):
        private_dir(store)
    (root / ".bf").chmod(0o700)

    (root / "bad-state").symlink_to(outside)
    with pytest.raises(Error, match="real directory without symlinks"):
        private_dir(store, "bad-state")
    (root / "bad-state").unlink()
    (root / "bad-state").mkdir(mode=0o755)
    (root / "bad-state").chmod(0o755)
    with pytest.raises(Error, match="bad-state must be owned by you with mode 700"):
        private_dir(store, "bad-state")
    with monkeypatch.context() as mp:
        mp.setattr(os, "geteuid", lambda: os.getuid() + 1)
        with pytest.raises(Error, match="owned by you with mode 700"):
            private_dir(store)

    with writer(store, wait=0.1):
        lock_file = root / ".bf" / "write.lock"
        assert lock_file.is_file()
    lock_fd = os.open(root / ".bf" / "write.lock", os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BusyError, match="another writer is active"), writer(store, wait=0.06):
            pass
    finally:
        os.close(lock_fd)

    # expand helper.
    assert expand(Path("~/sub")).is_absolute()
    monkeypatch.delenv("HOME")
    monkeypatch.setattr(Path, "expanduser", lambda _self: (_ for _ in ()).throw(RuntimeError("no home")))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: (_ for _ in ()).throw(RuntimeError("no home"))))
    with pytest.raises(Error, match="set HOME"):
        expand(Path("~/x"))
    with pytest.raises(Error, match="check the user name"):
        expand(Path("~nosuchuser/x"))


def test_yaml_object_and_brain_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = yaml_io.yaml_object(b"oct: 0o10\nhex: 0x10\ndec: 42\ndate: 2026-11-15\nmixed: TrUe\nempty:\n")
    assert parsed == {"oct": 8, "hex": 16, "dec": 42, "date": "2026-11-15", "mixed": "TrUe", "empty": None}
    assert yaml_io.yaml_object(b"") == {}

    # Pure-Python SafeLoader fallback works identically when CSafeLoader is absent (Python 3.15+ compatibility).
    with monkeypatch.context() as mp:
        mp.setattr(yaml, "CSafeLoader", None)
        yaml_io._strict_loader.cache_clear()  # noqa: SLF001
        try:
            assert yaml_io.yaml_object(b"a: 1\nflag: on\n") == {"a": 1, "flag": "on"}
            with pytest.raises(Error, match="anchors and aliases"):
                yaml_io.yaml_object(b"a: &anc 1\nb: *anc\n")
        finally:
            yaml_io._strict_loader.cache_clear()  # noqa: SLF001

    for payload, match in (
        (b"x" * ((1 << 20) + 1), "exceeds 1 MiB"),
        (b"\xff\xfe", "not UTF-8"),
        (b"a: &anchor 1\nb: *anchor\n", "anchors and aliases"),
        (b"a: 1\na: 2\n", "unique strings"),
        (b"1: int-key\n", "unique strings"),
        (b"- item\n", "one mapping"),
        (b"a: [unclosed\n", "invalid YAML"),
        (b"deep: " + b"[" * 35 + b"]" * 35, "structure exceeds"),
    ):
        with pytest.raises(Error, match=match):
            yaml_io.yaml_object(payload, "test.yaml")

    # Brain selection precedence: --brain > BF_BRAIN > nearest enclosing bf.yaml > ~/brain -> Error (BRAIN_LOCATION dropped).
    monkeypatch.delenv("BF_BRAIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Error, match="no brain selected"):
        config.select()

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(Error, match=r"has no bf\.yaml"):
        config.select(str(empty_dir))
    with pytest.raises(Error, match=r"bf\.yaml is missing"):
        config.load(Store(empty_dir))

    b1 = tmp_path / "b1"
    b1.mkdir()
    sib_path = tmp_path / "sib"
    (b1 / "bf.yaml").write_text(f"version: {models.FORMAT}\nname: alpha\nbrains:\n  sib: {sib_path}\n")
    b2 = tmp_path / "b2"
    (b2 / "projects").mkdir(parents=True)
    (b2 / "bf.yaml").write_text(f"version: {models.FORMAT}\nname: beta\n")

    cfg1 = config.load(Store(b1))
    assert cfg1 == config.Config(name="alpha", brains={"sib": str(sib_path)})
    assert config.load(Store(b1)) is cfg1  # cached by SHA-256

    bad_b = tmp_path / "bad"
    bad_b.mkdir()
    (bad_b / "bf.yaml").write_text(f"version: {models.FORMAT}\nname: Bad_Upper\n")
    with pytest.raises(Error, match=r"bf\.yaml: name"):
        config.load(Store(bad_b))

    monkeypatch.chdir(b2 / "projects")
    assert config.inside_brain() is not None
    assert config.load(config.select()).name == "beta"
    # BF_BRAIN beats enclosing bf.yaml; --brain beats BF_BRAIN!
    monkeypatch.setenv("BF_BRAIN", str(b1))
    assert config.load(config.select()).name == "alpha"
    assert config.load(config.select(str(b2))).name == "beta"
    monkeypatch.chdir(tmp_path)
    assert config.inside_brain() is None
    assert config.load(config.select()).name == "alpha"
    monkeypatch.setenv("BF_BRAIN", str(tmp_path / "missing-brain"))
    with pytest.raises(Error, match="does not exist"):
        config.select()
    monkeypatch.delenv("BF_BRAIN")
    home_brain = tmp_path / "empty-home" / "brain"
    home_brain.mkdir(parents=True)
    (home_brain / "bf.yaml").write_text(f"version: {models.FORMAT}\nname: homebrain\n")
    assert config.load(config.select()).name == "homebrain"  # ~/brain fallback
    assert config.load(config.select(str(b2))).name == "beta"  # --brain beats all

    # Cross-brain resolution (`resolve_brain`): self or declared `brains:` only (fails closed on undeclared).
    sib_path.mkdir()
    (sib_path / "bf.yaml").write_text(f"version: {models.FORMAT}\nname: sib\n")
    s1 = Store(b1)
    assert config.resolve_brain(s1, cfg1, "").root == b1.resolve()
    assert config.resolve_brain(s1, cfg1, "alpha").root == b1.resolve()
    assert config.resolve_brain(s1, cfg1, "sib").root == sib_path.resolve()
    with pytest.raises(Error, match=r"not declared in bf\.yaml brains:"):
        config.resolve_brain(s1, cfg1, "b2")
    with pytest.raises(Error, match=r"not declared in bf\.yaml brains:"):
        config.resolve_brain(s1, cfg1, "nonexistent-brain")

    # Unified v19 loops (`notes`, `search.exclude`, `concurrency`, `env`, per-loop `env_from`, flat/object `when`, `window`, `ingest`, `emit`).
    rich_b = tmp_path / "rich"
    rich_b.mkdir()
    (rich_b / "bf.yaml").write_text(
        f"version: {models.FORMAT}\nname: rich\ndescription: Rich test brain.\n"
        "timezone: Europe/Paris\nconcurrency: 8\n"
        "notes: [concepts, projects, actions, research]\n"
        "search:\n  exclude: [agent-telemetry]\n"
        "env:\n  GWS_DIR: /var/lib/gws\n  EMPTY_VAR: ''\n"
        "loops:\n"
        "  sync:\n"
        "    description: Sync records.\n"
        "    window:\n      backfill: 2d\n      overlap: 5m\n"
        "    when:\n      - 10m\n      - event: records.ingested\n"
        "    run: [echo, base, hi]\n"
        "    ingest:\n      source: sync-src\n      mode: snapshot\n      schema: custom-schema\n"
        "    timeout: 2m\n"
        "    env:\n      EXTRA_VAR: custom\n"
        "    env_from: [GH_TOKEN, LOCAL_SECRET]\n"
        "  digest:\n"
        "    window:\n      backfill: 1w\n      overlap: 5m\n"
        "    when:\n"
        "      - 1h\n"
        "      - cron: '0 8 * * *'\n        backfill: 2d\n        overlap: 10m\n"
        "      - event:\n"
        "          kind: loop.message\n"
        "          source: chat\n"
        "          where:\n"
        "            data.direct: true\n"
        "            data.channel: {in: [ops, eng]}\n"
        "            data.topic: {prefix: inc-}\n"
        "            data.note: {exists: true}\n"
        "    run: [claude, -p, prompts/digest.md]\n"
        "    emit: loop.event\n"
        "  short-ing:\n    run: [echo, '[]']\n    ingest: upsert\n"
        "  short-snap:\n    run: [echo, '[]']\n    ingest: snapshot\n"
        "  chat:\n    when: daemon\n    run: [echo, listen]\n    emit: loop.message\n"
        "  daemon:\n    when: daemon\n    run: [echo, serve]\n"
        "  conv-only:\n    when: 15m\n"
        "  bare-manual:\n"
        "  spaced-cron:\n    when: '0  8   * * *'\n"
    )
    rich_store = Store(rich_b)
    rich_cfg = config.load(rich_store)
    assert rich_cfg.description == "Rich test brain."
    assert rich_cfg.timezone == "Europe/Paris"
    assert rich_cfg.concurrency == 8
    assert rich_cfg.notes == ("concepts", "projects", "actions", "research")
    assert rich_cfg.search_exclude == ("agent-telemetry",)
    assert rich_cfg.env == {"GWS_DIR": "/var/lib/gws", "EMPTY_VAR": ""}
    assert rich_cfg.loops["sync"].description == "Sync records."
    assert rich_cfg.loops["sync"].run == ("echo", "base", "hi")
    assert rich_cfg.loops["sync"].schedules[0].every == 600
    assert rich_cfg.loops["sync"].events == (config.EventMatch(kind="records.ingested"),)
    assert rich_cfg.loops["sync"].env_from == ("GH_TOKEN", "LOCAL_SECRET")
    assert [(s.every, s.cron, s.backfill, s.overlap) for s in rich_cfg.loops["digest"].schedules] == [
        (3600, "", None, None),
        (None, "0 8 * * *", 172_800, 600),
    ]
    assert rich_cfg.loops["digest"].emit == "loop.event"
    assert rich_cfg.loops["sync"].backfill == 172_800
    assert rich_cfg.loops["sync"].overlap == 300
    assert rich_cfg.loops["sync"].timeout == 120
    assert (rich_cfg.loops["sync"].ingest, rich_cfg.loops["sync"].mode, rich_cfg.loops["sync"].schema_name) == (
        "sync-src",
        "snapshot",
        "custom-schema",
    )
    assert rich_cfg.loops["sync"].env == {"EXTRA_VAR": "custom"}
    assert (rich_cfg.loops["short-ing"].ingest, rich_cfg.loops["short-ing"].mode) == ("short-ing", "upsert")
    assert (rich_cfg.loops["short-snap"].ingest, rich_cfg.loops["short-snap"].mode) == ("short-snap", "snapshot")
    assert rich_cfg.loops["chat"].daemon is True
    assert rich_cfg.loops["chat"].emit == "loop.message"
    assert rich_cfg.loops["daemon"].daemon is True
    assert rich_cfg.loops["daemon"].emit == ""
    assert rich_cfg.loops["conv-only"].run == ()
    assert rich_cfg.loops["conv-only"].timeout == 300
    assert rich_cfg.loops["bare-manual"].run == ()
    assert rich_cfg.loops["bare-manual"].schedules == ()
    assert rich_cfg.loops["bare-manual"].events == ()
    assert rich_cfg.loops["bare-manual"].enabled is True
    assert rich_cfg.loops["spaced-cron"].schedules[0].cron == "0 8 * * *"
    assert rich_cfg.loops["spaced-cron"].schedules[0].raw == "cron:0 8 * * *"

    # Optional gitignored `.env` loading + edge cases:
    assert config.load_dotenv(rich_store) == {}
    rich_store.write(
        ".env",
        b"# local overrides\n\nexport API_TOKEN = 'lit\"quote' # single-quote comment\n"
        b'QUOTED="a\\"b\\\\c" # double-quote comment\n'
        b'SLASH="trailing\\\\"\n'
        b"PLAIN=val # inline comment\nEMPTY=\n",
        mode=0o600,
    )
    assert config.load_dotenv(rich_store) == {
        "API_TOKEN": 'lit"quote',
        "QUOTED": 'a"b\\c',
        "SLASH": "trailing\\",
        "PLAIN": "val",
        "EMPTY": "",
    }
    rich_store.write(".env", b"PLAIN=val\n", mode=0o644)
    with pytest.raises(Error, match="mode 600"):
        config.load_dotenv(rich_store)
    for bad_env_bytes, env_err in (
        (b"\xff\xfe", "not valid UTF-8"),
        (b"NO_EQUALS\n", "invalid environment assignment"),
        (b"1BAD=val\n", "invalid environment assignment"),
        (b"BF_BRAIN=/evil\n", "cannot override reserved or protected"),
        (b"LD_PRELOAD=/evil.so\n", "cannot override reserved or protected"),
        (b"PYTHONSTARTUP=/evil.py\n", "cannot override reserved or protected"),
        (b'UNCLOSED="abc\n', "unclosed quote"),
        (b'ESCAPED_END="abc\\"\n', "unclosed quote"),
        (b'MID_QUOTE="a" "b"\n', "unclosed quote"),
        (b"SINGLE_MID='a' 'b'\n", "unclosed quote"),
    ):
        rich_store.write(".env", bad_env_bytes, mode=0o600)
        with pytest.raises(Error, match=env_err):
            config.load_dotenv(rich_store)

    for idx, (bad_yaml, err_match) in enumerate(
        (
            (f"version: {models.FORMAT}\nname: badtz\ntimezone: Not/AZone\n", "unknown IANA timezone"),
            (f"version: {models.FORMAT}\nname: baddef\ndefaults:\n  timeout: 5m\n", "unknown property 'defaults'"),
            (
                f"version: {models.FORMAT}\nname: badtpl\ntemplates:\n  s:\n    run: [echo]\n",
                "unknown property 'templates'",
            ),
            (f"version: {models.FORMAT}\nname: badwatch\nwatch:\n  interval: 1s\n", "unknown property 'watch'"),
            (
                f"version: {models.FORMAT}\nname: badcron\nloops:\n  l1:\n    when: 'not a cron'\n    run: [echo]\n",
                "failed schema rule anyOf",
            ),
            (
                f"version: {models.FORMAT}\nname: bareint\nloops:\n  l1:\n    when: 60\n    run: [echo]\n",
                "failed schema rule anyOf",
            ),
            (
                f"version: {models.FORMAT}\nname: legon\nloops:\n  l1:\n    on:\n      schedules: 5m\n    run: [echo]\n",
                "unknown property 'on'",
            ),
            (
                f"version: {models.FORMAT}\nname: legnestedev\nloops:\n  l1:\n    when:\n      kind: records.ingested\n    run: [echo]\n",
                "unknown property 'kind'",
            ),
            (
                f"version: {models.FORMAT}\nname: legtarget\nloops:\n  l1:\n    run: [echo]\n    ingest:\n      target: usages\n",
                "unknown property 'target'",
            ),
            (
                f"version: {models.FORMAT}\nname: resing\nloops:\n  l1:\n    run: [echo]\n    ingest:\n      source: concepts\n",
                "reserved",
            ),
            (
                f"version: {models.FORMAT}\nname: resing2\nloops:\n  concepts:\n    run: [echo]\n    ingest: upsert\n",
                "reserved",
            ),
            (
                f"version: {models.FORMAT}\nname: rescustom\nnotes: [wiki]\nloops:\n  wiki:\n    run: [echo]\n    ingest: upsert\n",
                "reserved",
            ),
            (
                f"version: {models.FORMAT}\nname: ingemit\nloops:\n  l1:\n    run: [echo]\n    ingest: upsert\n    emit: loop.event\n",
                "conflicting properties",
            ),
            (
                f"version: {models.FORMAT}\nname: dwin\nloops:\n  l1:\n    when: daemon\n    window:\n      backfill: 1h\n    run: [echo]\n",
                "conflicting properties",
            ),
            (
                f"version: {models.FORMAT}\nname: ding\nloops:\n  l1:\n    when: daemon\n    ingest: upsert\n    run: [echo]\n",
                "conflicting properties",
            ),
            (
                f"version: {models.FORMAT}\nname: dto\nloops:\n  l1:\n    when: daemon\n    timeout: 1m\n    run: [echo]\n",
                "conflicting properties",
            ),
            (
                f"version: {models.FORMAT}\nname: emptywin\nloops:\n  l1:\n    window: {{}}\n    run: [echo]\n",
                "violates minProperties 1",
            ),
            (
                f"version: {models.FORMAT}\nname: reslogs\nloops:\n  logs:\n    run: [echo]\n    ingest: upsert\n",
                "reserved",
            ),
            (
                f"version: {models.FORMAT}\nname: resenv\nenv:\n  BF_BRAIN: /x\n",
                "cannot override reserved or protected",
            ),
            (
                f"version: {models.FORMAT}\nname: ldenv\nenv:\n  LD_PRELOAD: /evil.so\n",
                "cannot override reserved or protected",
            ),
            (
                f"version: {models.FORMAT}\nname: pyenv\nenv:\n  PYTHONPATH: /evil\n",
                "cannot override reserved or protected",
            ),
            (
                f"version: {models.FORMAT}\nname: pathenv\nenv:\n  PATH: /evil\n",
                "cannot override reserved or protected",
            ),
            (
                f"version: {models.FORMAT}\nname: badtopenvfrom\nenv_from: [GH_TOKEN]\n",
                "unknown property 'env_from'",
            ),
            (
                f"version: {models.FORMAT}\nname: badenvfrom\nloops:\n  l1:\n    run: [echo]\n    env_from: [LD_PRELOAD]\n",
                "cannot allowlist reserved or protected",
            ),
            (
                f"version: {models.FORMAT}\nname: badclients\nloops:\n  l1:\n    run: [echo]\n    ingest:\n      source: clients\n",
                "reserved",
            ),
            (
                f"version: {models.FORMAT}\nname: badprompts\nloops:\n  l1:\n    run: [echo]\n    ingest:\n      source: prompts\n",
                "reserved",
            ),
            (
                f"version: {models.FORMAT}\nname: badsearchexcl\nsearch:\n  exclude: [concepts]\n",
                "reserved",
            ),
            (f"version: {models.FORMAT}\nname: badconc\nconcurrency: 0\n", "violates minimum 1"),
            (f"version: {models.FORMAT}\nname: badconc2\nconcurrency: 64\n", "violates maximum 32"),
            (
                f"version: {models.FORMAT}\nname: unsatcron\nloops:\n  l1:\n    when: '0 0 31 2 *'\n    run: [echo]\n",
                "duration .*, @macro, or 5-field cron",
            ),
            (f'version: {models.FORMAT}\nname: ctlbr\nbrains:\n  sib: "/tmp/\\u0007bad"\n', "no control characters"),
            (f'version: {models.FORMAT}\nname: ctldesc\ndescription: "bad\\u0000desc"\n', "forbidden control"),
            (
                f"version: {models.FORMAT}\nname: dupsched\nloops:\n  l1:\n    when:\n      - 30m\n      - 1800s\n    run: [echo]\n",
                "duplicate schedule trigger",
            ),
            (
                f"version: {models.FORMAT}\nname: dupcronws\nloops:\n  l1:\n    when:\n      - '0 8 * * *'\n      - '0  8   * * *'\n    run: [echo]\n",
                "duplicate schedule trigger",
            ),
            (
                f"version: {models.FORMAT}\nname: badwhenarr\nloops:\n  l1:\n    when: []\n    run: [echo]\n",
                "violates minItems 1",
            ),
            (
                f"version: {models.FORMAT}\nname: badbfzero\nloops:\n  l1:\n    window:\n      backfill: 0s\n    run: [echo]\n",
                "failed schema rule|must be",
            ),
            (f"version: {models.FORMAT}\nname: emptynotes\nnotes: []\n", "violates minItems 1"),
            (f"version: {models.FORMAT}\nname: resnotes\nnotes: [memories]\n", "value is reserved"),
            (f"version: {models.FORMAT}\nname: dupnotes\nnotes: [wiki, wiki]\n", "unique"),
            (
                f"version: {models.FORMAT}\nname: badph\nloops:\n  l1:\n    run: [echo, '{{{{unknown}}}}']\n",
                "unknown placeholder",
            ),
            (
                f"version: {models.FORMAT}\nname: badphcmd\nloops:\n  l1:\n    run: ['{{{{brain}}}}/sensors/s.py']\n",
                "placeholders are not allowed in executable name",
            ),
        )
    ):
        bad_dir = tmp_path / f"bad-{idx}"
        bad_dir.mkdir(exist_ok=True)
        (bad_dir / "bf.yaml").write_text(bad_yaml)
        with pytest.raises(Error, match=err_match):
            config.load(Store(bad_dir))

    # Malformed `.env` key with an unclosed quote fails without echoing the unvalidated pre-`=` text.
    bad_env_dir = tmp_path / "bad-dotenv-key"
    bad_env_dir.mkdir()
    bad_lhs = "https://alice:" + "marker_pw" + "@example.test/?a"
    (bad_env_dir / ".env").write_text(f'{bad_lhs}="unclosed\n')
    (bad_env_dir / ".env").chmod(0o600)
    with pytest.raises(Error, match="invalid environment assignment") as env_exc:
        config.load_dotenv(Store(bad_env_dir))
    assert "marker_pw" not in str(env_exc.value)

    # Symlinked bf.yaml rejected by both `inside_brain()` and explicit `select(str(sym_brain))`.
    sym_brain = tmp_path / "sym-brain"
    sym_brain.mkdir()
    (sym_brain / "bf.yaml").symlink_to(b1 / "bf.yaml")
    monkeypatch.delenv("BF_BRAIN", raising=False)
    monkeypatch.chdir(sym_brain)
    assert config.inside_brain() is None
    with pytest.raises(Error, match="must be a regular file"):
        config.select(str(sym_brain))

    # `Store.iter_lines` & `Store.append_log` `O_APPEND` inode stability, bounded trimming, and `0700`/`0600` perms:
    log_store = Store(b1)
    log_store.append_log("stream", b'{"n":1}')
    ino_first = (b1 / "logs" / "stream.jsonl").stat().st_ino
    log_store.append_log("stream", b'{"n":2}')
    assert (b1 / "logs" / "stream.jsonl").stat().st_ino == ino_first
    assert [ln.decode() for ln in log_store.read("logs/stream.jsonl").splitlines()] == ['{"n":1}', '{"n":2}']
    log_store.append_log("sample", b'{"msg":"one"}', max_lines=2, max_bytes=80)
    ino_sample = (b1 / "logs" / "sample.jsonl").stat().st_ino
    log_store.append_log("sample", b'{"msg":"two"}', max_lines=2, max_bytes=80)
    assert (b1 / "logs" / "sample.jsonl").stat().st_ino == ino_sample
    log_store.append_log("sample", b'{"msg":"three"}', max_lines=2, max_bytes=80)
    assert oct((b1 / "logs").stat().st_mode & 0o777) == "0o700"
    assert oct((b1 / "logs" / "sample.jsonl").stat().st_mode & 0o777) == "0o600"
    assert [ln.decode() for ln in log_store.read("logs/sample.jsonl").splitlines()] == [
        '{"msg":"two"}',
        '{"msg":"three"}',
    ]
    log_store.write("logs/big.jsonl", b"".join(f'{{"n":{i}}}\n'.encode() for i in range(40)), mode=0o600)
    log_store.append_log("big", b'{"n":40}', max_lines=5, max_bytes=80)
    big_bytes = log_store.read("logs/big.jsonl")
    assert len(big_bytes) <= 80
    assert big_bytes.endswith(b'{"n":40}\n')
    exact_entry = b'{"msg":"012345678901234567890123"}\n'
    log_store.append_log("sample", exact_entry, max_lines=5, max_bytes=35)
    assert log_store.read("logs/sample.jsonl") == exact_entry

    from bf import storage as st_m

    for i in range(26):
        st_m._LOG_APPENDS.clear()  # noqa: SLF001 - simulate fresh CLI invocation per append
        log_store.append_log("multi", f'{{"n":{i}}}'.encode(), max_lines=20, max_bytes=4096)
    multi_lines = log_store.read("logs/multi.jsonl").splitlines()
    assert len(multi_lines) == 20
    assert multi_lines[-1] == b'{"n":25}'
    with pytest.raises(Error, match="exceeds 20 bytes"):
        log_store.append_log("sample", b"x" * 25, max_bytes=20)
    os.mkfifo(b1 / "logs" / "fifo.jsonl")
    with pytest.raises(Error, match="expected a regular file"):
        log_store.append_log("fifo", b'{"msg":"x"}')
    b1_docs = Store(b1)
    b1_docs.write("sub/a.jsonl", b'{"a":1}\n')
    assert list(b1_docs.iter_lines("sub/a.jsonl")) == [b'{"a":1}\n']
    with pytest.raises(Error, match="exceeds 2 bytes"):
        list(b1_docs.iter_lines("sub/a.jsonl", limit=2))
    assert tail_lines([b"a\n", b"b\n"], 0, 100) == []
    assert tail_lines([b"a\n", b"b\n"], 2, 0) == []
    long_jwt = "eyJ" + ("a" * 600)
    for raw_sec in (
        "SECRET_KEY=topsecret1",
        "AWS_SESSION_TOKEN=tok12345",
        "Authorization: Basic dXNlcjpwYXNz",
        f'access_token="{long_jwt}"',
    ):
        assert models.redact_secrets(raw_sec) != raw_sec
        assert "[REDACTED]" in models.redact_secrets(raw_sec)
        assert long_jwt not in models.redact_secrets(raw_sec)
    red_d = models.redact_value(
        {
            "OPENAI_API_KEY": "raw-val",
            "AWS_SECRET_ACCESS_KEY": "raw-2",
            "Authorization": "Basic dXNlcjpwYXNz",
            "GH_TOKEN": "tok-raw",
            "OAUTH_TOKEN": ["t1", "t2"],
            "title": "safe",
        }
    )
    assert red_d == {
        "OPENAI_API_KEY": "[REDACTED]",
        "AWS_SECRET_ACCESS_KEY": "[REDACTED]",
        "Authorization": "[REDACTED]",
        "GH_TOKEN": "[REDACTED]",
        "OAUTH_TOKEN": "[REDACTED]",
        "title": "safe",
    }
    assert models.redact_secrets("Error: basic validation failed") == "Error: basic validation failed"


def test_lazy_imports() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, bf.models, bf.storage, bf.cron, bf.refs, bf.config, bf.schemas, bf.records; "
                "bad = {'structlog', 'yaml', 'croniter', 'jsonschema', 'markdown', 'markdown_it', 'watchfiles'} & sys.modules.keys(); "
                "assert not bad, bad"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_cron_edge_cases_wrap_around_and_config_errors(tmp_path: Path) -> None:
    # Invalid cron expressions.
    for bad_expr in (
        "",
        "0 8 * *",
        "@bogus",
        "foo * * * *",
        "99 * * * *",
        "1,,2 * * * *",
        "*/0 * * * *",
        "*/abc * * * *",
        "1-2-3 * * * *",
        "5-2 * * * *",
        "0 0 * * 7-1",
        "0 0 * * 1-9",
        "0 0 31 2 *",
        "0 0 31 4 *",
    ):
        assert not cron.is_valid_cron(bad_expr), bad_expr

    # Step from start (`5/15`), named months/days, `5-7` / `mon-sun` / `fri-sun` / `sat-sun` / `7-7` dow ranges, and dom/dow OR-semantics.
    spec_step = cron.parse_cron("5/15 9-17 * jan-mar mon-fri")
    assert spec_step.minutes == frozenset({5, 20, 35, 50})
    assert spec_step.months == frozenset({1, 2, 3})
    assert spec_step.dows == frozenset({1, 2, 3, 4, 5})
    assert cron.parse_cron("0 0 * * 5-7").dows == frozenset({0, 5, 6})
    assert cron.parse_cron("0 0 * * mon-sun").dows == frozenset({0, 1, 2, 3, 4, 5, 6})
    assert cron.parse_cron("0 0 * * fri-sun").dows == frozenset({0, 5, 6})
    assert cron.parse_cron("0 0 * * sat-sun").dows == frozenset({0, 6})
    assert cron.parse_cron("0 0 * * 7-7").dows == frozenset({0})

    # DOM + DOW both non-wildcard -> OR semantics (15th OR Monday).
    t_or = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)  # Sat Nov 14 -> Sun Nov 15 matches DOM!
    assert cron.next_cron("0 0 15 * 1", t_or) == datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
    assert cron.next_cron("0 0 15 * 1", datetime(2026, 11, 15, 1, 0, tzinfo=UTC)) == datetime(
        2026, 11, 16, 0, 0, tzinfo=UTC
    )  # Mon Nov 16 matches DOW!
    # DOM only vs DOW only:
    assert cron.next_cron("0 0 15 * *", datetime(2026, 11, 16, 0, 0, tzinfo=UTC)) == datetime(
        2026, 12, 15, 0, 0, tzinfo=UTC
    )
    assert cron.prev_cron("0 0 15 * *", datetime(2026, 11, 14, 0, 0, tzinfo=UTC)) == datetime(
        2026, 10, 15, 0, 0, tzinfo=UTC
    )

    # Month, year, and leap-day wrap-around both forward (`next_cron`) and backward (`prev_cron`)!
    assert cron.next_cron("0 0 29 2 *", datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == datetime(
        2028, 2, 29, 0, 0, tzinfo=UTC
    )
    assert cron.prev_cron("0 0 29 2 *", datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == datetime(
        2024, 2, 29, 0, 0, tzinfo=UTC
    )
    assert cron.next_cron("30 23 31 12 *", datetime(2026, 12, 31, 23, 35, tzinfo=UTC)) == datetime(
        2027, 12, 31, 23, 30, tzinfo=UTC
    )
    assert cron.prev_cron("30 23 31 12 *", datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == datetime(
        2025, 12, 31, 23, 30, tzinfo=UTC
    )
    assert cron.next_cron("0 10 * 3,9 *", datetime(2026, 10, 1, 0, 0, tzinfo=UTC)) == datetime(
        2027, 3, 1, 10, 0, tzinfo=UTC
    )
    assert cron.prev_cron("0 10 * 3,9 *", datetime(2026, 2, 1, 0, 0, tzinfo=UTC)) == datetime(
        2025, 9, 30, 10, 0, tzinfo=UTC
    )
    assert cron.next_cron("0 23 * * *", datetime(2026, 11, 15, 23, 30, tzinfo=UTC)) == datetime(
        2026, 11, 16, 23, 0, tzinfo=UTC
    )
    assert cron.prev_cron("0 23 * * *", datetime(2026, 11, 16, 0, 30, tzinfo=UTC)) == datetime(
        2026, 11, 15, 23, 0, tzinfo=UTC
    )
    assert cron.next_cron("0 10 * * *", datetime(2026, 11, 15, 11, 0, tzinfo=UTC)) == datetime(
        2026, 11, 16, 10, 0, tzinfo=UTC
    )
    assert cron.prev_cron("0 10 * * *", datetime(2026, 11, 15, 9, 0, tzinfo=UTC)) == datetime(
        2026, 11, 14, 10, 0, tzinfo=UTC
    )

    # Multi-document YAML & excessive node count fail closed!
    with pytest.raises(Error, match="invalid YAML"):
        yaml_io.yaml_object(b"---\na: 1\n---\nb: 2\n")
    with pytest.raises(Error, match="structure exceeds"):
        yaml_io.yaml_object(b"items: [" + b",".join(b"1" for _ in range(20_005)) + b"]\n")

    # Config env_from / env blocked keys (incl. OPENSSL_*, SSLKEYLOGFILE, CURL_HOME, BF_*), conflicting ingest schemas, duplicate schedule triggers, and where control chars.
    cfg_root = tmp_path / "cfg-edges"
    cfg_root.mkdir()
    st = Store(cfg_root)
    for bad_yaml, match in (
        (
            f"version: {models.FORMAT}\nname: x\nloops:\n  a:\n    run: [echo]\n    env_from: [PATH]\n",
            "protected variable",
        ),
        (
            f"version: {models.FORMAT}\nname: x\nloops:\n  a:\n    run: [echo]\n    env_from: [LD_PRELOAD]\n",
            "protected variable",
        ),
        (
            f"version: {models.FORMAT}\nname: x\nloops:\n  a:\n    run: [echo]\n    env_from: [OPENSSL_CONF]\n",
            "protected variable",
        ),
        (
            f"version: {models.FORMAT}\nname: x\nloops:\n  a:\n    run: [echo]\n    env_from: [SSLKEYLOGFILE]\n",
            "protected variable",
        ),
        (
            f"version: {models.FORMAT}\nname: x\nloops:\n  a:\n    run: [echo]\n    env_from: [BF_CUSTOM]\n",
            "protected variable",
        ),
        (f"version: {models.FORMAT}\nname: x\nenv:\n  PYTHONSTARTUP: /tmp/x\n", "protected environment"),
        (f"version: {models.FORMAT}\nname: x\nenv:\n  CURL_HOME: /tmp/x\n", "protected environment"),
        (
            f"version: {models.FORMAT}\nname: x\nloops:\n  a:\n    ingest: {{source: shared, schema: s1}}\n    run: [echo]\n  b:\n    ingest: {{source: shared, schema: s2}}\n    run: [echo]\n",
            "conflicts with loops.a",
        ),
        (
            f"version: {models.FORMAT}\nname: x\nloops:\n  a:\n    when: [1h, 60m]\n    run: [echo]\n",
            "duplicate schedule",
        ),
        (
            f'version: {models.FORMAT}\nname: x\nloops:\n  a:\n    when:\n      event:\n        kind: a.b\n        where:\n          data.x: "bad\\x00val"\n    run: [echo]\n',
            "forbidden control",
        ),
        (
            f"version: {models.FORMAT}\nname: x\nloops:\n  a:\n    run: ['{{{{brain}}}}/sensors/x.py']\n",
            "executable name",
        ),
        (
            f"version: {models.FORMAT}\nname: x\nloops:\n  a:\n    run: [echo, '{{{{unknown}}}}']\n",
            "unknown placeholder",
        ),
    ):
        st.write("bf.yaml", bad_yaml.encode())
        with pytest.raises(Error, match=match):
            config.load(st)

    # `validate_loop_scripts` rejects symlinks at root, in `clients/`, `/./`, and leading symlink dirs before `..` without blocking `owner/repo`!
    outside_py = tmp_path / "outside.py"
    outside_py.write_text("print(1)\n")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (cfg_root / "clients").mkdir()
    (cfg_root / "clients" / "evil.py").symlink_to(outside_py)
    (cfg_root / "misc").mkdir()
    (cfg_root / "misc" / "sym.txt").symlink_to(outside_py)
    (cfg_root / "root_evil.py").symlink_to(outside_py)
    (cfg_root / "linkdir").symlink_to(outside_dir)
    with pytest.raises(Error, match="regular file inside brain"):
        config.validate_loop_scripts(st, ["python3", "clients/evil.py"], "a")
    with pytest.raises(Error, match=r"symlink misc/sym\.txt"):
        config.validate_loop_scripts(st, ["python3", "misc/sym.txt"], "a")
    with pytest.raises(Error, match=r"symlink misc/sym\.txt"):
        config.validate_loop_scripts(st, ["python3", "--cfg=misc/sym.txt"], "a")
    with pytest.raises(Error, match=r"symlink root_evil\.py"):
        config.validate_loop_scripts(st, ["python3", "root_evil.py"], "a")
    with pytest.raises(Error, match="normalized brain-relative path"):
        config.validate_loop_scripts(st, ["python3", "misc/./sym.txt"], "a")
    with pytest.raises(Error, match="normalized brain-relative path"):
        config.validate_loop_scripts(st, ["python3", "linkdir/a/../../outside.py"], "a")
    with pytest.raises(Error, match=r"symlink linkdir/a/\.\./\.\./\.\./outside\.py"):
        config.validate_loop_scripts(st, ["python3", "linkdir/a/../../../outside.py"], "a")
    config.validate_loop_scripts(st, ["gh", "issue", "list", "--repo", "owner/repo"], "a")

    # Malformed IPv6 URLs raise `Error` (never uncaught `ValueError`) in `refs.validate_url` and `refs.parse_link`!
    from bf import refs, storage

    with pytest.raises(Error, match="valid URL"):
        refs.validate_url("https://[bad")
    with pytest.raises(Error, match="invalid bf:// URI"):
        refs.parse_link("concepts/x.md", "bf://[bad/concepts/a.md", "x")

    # `cron.resolve_tz("UTC")` returns `datetime.UTC`; `sched_anchor` never starves newly added slower schedules!
    assert cron.resolve_tz("UTC") is UTC
    s_fast = cron.ScheduleSpec("every:1800", every=1800)
    s_slow = cron.ScheduleSpec("every:86400", every=86400)
    doc_multi = {
        "last_scheduled": "2026-11-15T10:00:00Z",
        "schedule_last": {"every:1800": "2026-11-15T10:00:00Z"},
    }
    assert cron.sched_anchor(doc_multi, s_fast) == "2026-11-15T10:00:00Z"
    assert cron.sched_anchor(doc_multi, s_slow) == ""
    assert (
        cron.backoff_remaining(
            {"name": "u", "consecutive_failures": 0, "incomplete_runs": 1, "last_end": "2026-11-15T10:00:00Z"},
            3600,
            datetime(2026, 11, 15, 10, 0, 5, tzinfo=UTC),
        )
        > 0
    )
    assert (
        cron.backoff_cap(
            config.LoopSpec(name="cron_only", schedules=(cron.ScheduleSpec("cron:0 8 * * *", cron="0 8 * * *"),))
        )
        == 3600
    )
    assert (
        cron.backoff_cap(config.LoopSpec(name="fast", schedules=(cron.ScheduleSpec("every:600", every=600),))) == 3600
    )
    assert (
        cron.backoff_cap(config.LoopSpec(name="hr", schedules=(cron.ScheduleSpec("every:3600", every=3600),))) == 7200
    )
    assert (
        cron.backoff_cap(config.LoopSpec(name="day", schedules=(cron.ScheduleSpec("every:86400", every=86400),)))
        == 21_600
    )
    assert storage.try_unlink_loop_lock(st, "not-a-loop-lock") is False

    # Extra cron hour rollovers, config helpers, and storage defensive edges.
    assert cron.next_cron("0 8,16 * * *", datetime(2026, 11, 15, 8, 30, tzinfo=UTC)) == datetime(
        2026, 11, 15, 16, 0, tzinfo=UTC
    )
    assert cron.prev_cron("0 8,16 * * *", datetime(2026, 11, 15, 15, 30, tzinfo=UTC)) == datetime(
        2026, 11, 15, 8, 0, tzinfo=UTC
    )
    assert cron.prev_cron("30 8,16 * * *", datetime(2026, 11, 15, 16, 10, tzinfo=UTC)) == datetime(
        2026, 11, 15, 8, 30, tzinfo=UTC
    )
    assert cron.prev_cron("30 8 * * *", datetime(2026, 11, 15, 8, 10, tzinfo=UTC)) == datetime(
        2026, 11, 14, 8, 30, tzinfo=UTC
    )
    assert cron.prev_cron("0 10 * 5 *", datetime(2026, 6, 1, 0, 0, tzinfo=UTC)) == datetime(
        2026, 5, 31, 10, 0, tzinfo=UTC
    )
    assert cron.prev_cron("0 10 31 * *", datetime(2026, 3, 1, 0, 0, tzinfo=UTC)) == datetime(
        2026, 1, 31, 10, 0, tzinfo=UTC
    )

    # Huge integer (>4300 digits) fails cleanly in YAML, plus helper edge cases.
    with pytest.raises(Error, match="invalid YAML"):
        yaml_io.yaml_object(b"n: " + (b"9" * 4500) + b"\n")
    with pytest.raises(Error, match="unknown IANA timezone"):
        config._check_tz("UTC\n", "tz")  # noqa: SLF001
    assert cron.parse_dt("not-a-timestamp") is None
    assert cron.next_schedule_delay(config.LoopSpec("u"), {}, datetime(2026, 11, 15, tzinfo=UTC)) is None
    assert (
        cron.next_schedule_delay(
            config.LoopSpec("u", schedules=(config.ScheduleSpec("every:60", every=60),)),
            {},
            datetime(2026, 11, 15, tzinfo=UTC),
        )
        == 0.5
    )
    assert config.normalize_brain_script("{{brain}}/sensors/a.py") == "sensors/a.py"
    assert config.normalize_brain_script("/abs/path.py") == ""
    assert config.normalize_brain_script("./sensors/a.py") == "sensors/a.py"
    assert config.normalize_brain_script("../escape.py") == ""
    config.validate_loop_scripts(st, (), "u")

    # Reserved ingest source name and non-file bf.yaml rejected.
    st.write("sensors/concepts.py", b"print('[]')\n")
    with pytest.raises(Error, match="is reserved"):
        config.resolve_loop(st, config.LoopSpec("concepts", ingest="concepts"), reserved=frozenset({"concepts"}))
    bf_dir = tmp_path / "bf-is-dir"
    (bf_dir / "bf.yaml").mkdir(parents=True)
    with pytest.raises(Error, match="must be a regular file"):
        config.select(str(bf_dir))

    # `config.load` LRU cache eviction past 64 distinct configs.
    for i in range(66):
        st.write("bf.yaml", f"version: {models.FORMAT}\nname: c{i}\n".encode())
        assert config.load(st).name == f"c{i}"

    # Storage defensive edges: delete_tree on file, append_log oversize & compaction, non-regular lock file.
    bf_st = private_dir(st)
    bf_st.write("not-a-dir.txt", b"x", mode=0o600)
    with pytest.raises(Error, match="non-directory"):
        bf_st.delete_tree("not-a-dir.txt")
    with pytest.raises(Error, match="exceeds 16 bytes"):
        st.append_log("oversize", b"x" * 32, max_bytes=16)
    for i in range(6):
        st.append_log("compact", f'{{"n":{i}}}\n'.encode(), max_lines=2, max_bytes=120)
    assert len(private_dir(st, "logs").read("compact.jsonl").splitlines()) <= 2
    fifo_lk = st.root / ".bf" / "fifo.lock"
    os.mkfifo(fifo_lk)
    with pytest.raises(Error, match="invalid writer lock"), writer(st, name="fifo.lock"):
        pass
    fifo_lk.unlink()

    # DST spring-forward monotonicity (`next_cron` & `prev_cron`), DOW step `hi=6`, sub-second cron boundary, and `@hourly` overdue!
    paris = ZoneInfo("Europe/Paris")
    ny = ZoneInfo("America/New_York")
    for utc_dt in (
        datetime(2026, 3, 29, 0, 58, tzinfo=UTC),
        datetime(2026, 3, 29, 0, 59, tzinfo=UTC),
    ):
        assert cron.next_cron("0 2 * * *", utc_dt.astimezone(paris)).astimezone(UTC) == datetime(
            2026, 3, 29, 1, 0, tzinfo=UTC
        )
    for utc_dt in (
        datetime(2026, 3, 29, 0, 58, tzinfo=UTC),
        datetime(2026, 3, 29, 0, 59, tzinfo=UTC),
        datetime(2026, 3, 29, 1, 5, tzinfo=UTC),
    ):
        assert cron.next_cron("30 2 * * *", utc_dt.astimezone(paris)).astimezone(UTC) == datetime(
            2026, 3, 29, 1, 30, tzinfo=UTC
        )
    assert cron.prev_cron("30 2 * * *", datetime(2026, 3, 29, 1, 35, tzinfo=UTC).astimezone(paris)).astimezone(
        UTC
    ) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    for utc_dt in (
        datetime(2026, 3, 8, 6, 58, tzinfo=UTC),
        datetime(2026, 3, 8, 6, 59, tzinfo=UTC),
        datetime(2026, 3, 8, 7, 5, tzinfo=UTC),
    ):
        assert cron.next_cron("30 2 * * *", utc_dt.astimezone(ny)).astimezone(UTC) == datetime(
            2026, 3, 8, 7, 30, tzinfo=UTC
        )
    assert 7 not in cron.parse_cron("0 0 * * 5-6,3/2").dows

    # Sub-second `:59.500Z` does NOT fire `0 8 * * *` early or double-fire!
    cron_lp = config.LoopSpec("c8", schedules=(config.ScheduleSpec("cron:0 8 * * *", cron="0 8 * * *"),))
    st_prev = {"schedule_last": {"cron:0 8 * * *": "2026-11-14T08:00:00Z"}}
    assert cron.due_schedules(cron_lp, st_prev, datetime(2026, 11, 15, 7, 59, 59, 500_000, tzinfo=UTC)) == ()
    assert len(cron.due_schedules(cron_lp, st_prev, datetime(2026, 11, 15, 8, 0, 0, 500_000, tzinfo=UTC))) == 1

    # Softened overdue grace: `@hourly` cron allows 2h (`7200s`) grace past first missed fire!
    hr_lp = config.LoopSpec("hr", schedules=(config.ScheduleSpec("cron:@hourly", cron="@hourly"),))
    assert (
        cron.is_schedule_overdue(
            hr_lp,
            {"last_success": "2026-11-15T06:00:00Z"},
            datetime(2026, 11, 15, 9, 5, tzinfo=UTC),
        )
        is True
    )
    assert (
        cron.is_schedule_overdue(
            hr_lp,
            {"last_success": "2026-11-15T06:00:00Z"},
            datetime(2026, 11, 15, 8, 5, tzinfo=UTC),
        )
        is False
    )

    # BF-05 & BF-11: `validate_loop_scripts` rejects `..` traversal, `--flag=VALUE` bad RHS, & `{{brain}}/` symlinks; extended hijack env vars blocked!
    st.write("sensors/legit.py", b"print('ok')\n")
    (st.root / "clients" / "acme").mkdir(parents=True, exist_ok=True)
    # `clients/` directory argument is NOT falsely rejected as a missing script!
    config.validate_loop_scripts(st, ["python3", "sensors/legit.py", "clients/acme", "--script=sensors/legit.py"], "u")
    for bad_argv in (
        ["./missing-cmd.sh"],
        ["python3", f"./../{st.root.name}/sensors/legit.py"],
        ["python3", "sensors/sub/../legit.py"],
        ["python3", "{{brain}}/sensors/missing.py"],
        ["python3", "--script=sensors/missing.py"],
    ):
        with pytest.raises(Error):
            config.validate_loop_scripts(st, bad_argv, "u")
    for bad_key in (
        "NODE_OPTIONS",
        "SHELLOPTS",
        "BASHOPTS",
        "GIT_SSH",
        "GIT_CONFIG_COUNT",
        "XDG_CONFIG_HOME",
        "NPM_CONFIG_PREFIX",
        "SSL_CERT_FILE",
        "HTTPS_PROXY",
        "GLIBC_TUNABLES",
    ):
        assert config.is_blocked_env_key(bad_key)

    # BF-06: linear-time secret redaction and HTML stripping (no O(N^2) ReDoS on pathological runs)!
    import time as _t

    t_r0 = _t.monotonic()
    assert models.redact_secrets("-" * 65_536) == "-" * 65_536
    assert _t.monotonic() - t_r0 < 0.5


def test_concurrency_pools_debounce_and_logs_lock_isolation(tmp_path: Path) -> None:
    b = tmp_path / "pools-brain"
    b.mkdir()
    (b / "bf.yaml").write_text(
        f"version: {models.FORMAT}\nname: pools\n"
        "concurrency:\n  agents: 2\n  interactive: 3\n"
        "loops:\n"
        "  sensor-a:\n    when: 1h\n    run: [echo, a]\n"
        "  agent-a:\n    pool: agents\n    when:\n      - event: records.ingested\n        debounce: 15s\n    run: [echo, ag]\n"
        "  dm:\n    pool: interactive\n    when:\n      - event:\n          kind: loop.message\n          source: chat\n        debounce: 5s\n    run: [echo, dm]\n"
    )
    cfg = config.load(Store(b))
    assert cfg.concurrency == 4
    assert cfg.effective_pools() == {"default": 4, "agents": 2, "interactive": 3}
    assert cfg.total_concurrency() == 9
    assert cfg.loops["sensor-a"].pool == "default"
    assert cfg.loops["agent-a"].pool == "agents"
    assert cfg.loops["agent-a"].events == (config.EventMatch(kind="records.ingested", debounce=15),)
    assert cfg.loops["dm"].events == (config.EventMatch(kind="loop.message", source="chat", debounce=5),)

    for idx, (bad_yaml, pat) in enumerate(
        (
            (
                f"version: {models.FORMAT}\nname: badpool\nconcurrency: 4\nloops:\n  a:\n    pool: agents\n    run: [echo]\n",
                "unknown concurrency pool 'agents'",
            ),
            (
                f"version: {models.FORMAT}\nname: dpool\nconcurrency:\n  agents: 2\nloops:\n  d:\n    when: daemon\n    pool: agents\n    run: [echo]\n",
                "conflicting properties",
            ),
            (
                f"version: {models.FORMAT}\nname: baddeb\nloops:\n  a:\n    when:\n      - event: x.y\n        debounce: 2h\n    run: [echo]\n",
                "must be between 1s and 3600s",
            ),
            (
                f"version: {models.FORMAT}\nname: badpmap\nconcurrency: {{}}\n",
                "violates minProperties 1",
            ),
        )
    ):
        d = tmp_path / f"pool-bad-{idx}"
        d.mkdir()
        (d / "bf.yaml").write_text(bad_yaml)
        with pytest.raises(Error, match=pat):
            config.load(Store(d))

    # `Store.append_log` uses `.bf/logs.lock` without contending on `.bf/write.lock`!
    log_store = Store(b)
    with writer(log_store, name="write.lock"):
        log_store.append_log("agent-a", b'{"severity":"INFO","message":"no contention"}\n')
    assert (b / ".bf" / "logs.lock").is_file()
    assert "no contention" in (b / "logs" / "agent-a.jsonl").read_text()

    # BF-010 / BF-SEC-01: Private store enforces 0700 on nested subdirs, 0600 on `iter_lines_with_offsets`, and default/strict 0600 on `write`.
    priv = private_dir(log_store, ".bf", "state")
    priv.write("nested/item.jsonl", b'{"k":1}\n')
    nested_file = b / ".bf" / "state" / "nested" / "item.jsonl"
    assert (nested_file.stat().st_mode & 0o777) == 0o600
    assert list(priv.iter_lines_with_offsets("nested/item.jsonl")) == [(0, b'{"k":1}\n')]
    with pytest.raises(Error, match="mode 600"):
        priv.write("nested/loose.jsonl", b"x", mode=0o644)
    nested_file.chmod(0o644)
    try:
        with pytest.raises(Error, match="mode 600"):
            list(priv.iter_lines_with_offsets("nested/item.jsonl"))
    finally:
        nested_file.chmod(0o600)
    nested_dir = b / ".bf" / "state" / "nested"
    nested_dir.chmod(0o755)
    try:
        with pytest.raises(Error, match="mode 700"):
            priv.read("nested/item.jsonl")
    finally:
        nested_dir.chmod(0o700)
