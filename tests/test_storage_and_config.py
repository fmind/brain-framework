"""Tests for models, confined O_NOFOLLOW storage, YAML 1.2 parsing, and brain selection."""

from __future__ import annotations

import fcntl
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

from bf import config, models
from bf.models import BusyError, Error, FormatError
from bf.storage import Store, expand, private_dir, relative, writer, xdg


def test_models_sanitization_json_and_dates() -> None:
    assert models.clean("hello") == "hello"
    for bad in ("", "   ", "line\nbreak", "bell\x07"):
        with pytest.raises(Error, match="nonempty and contain no control"):
            models.clean(bad)

    assert models.clean_text("line 1\n\tline 2\r\n") == "line 1\n\tline 2\r\n"
    with pytest.raises(Error, match="forbidden control"):
        models.clean_text("bad\x00byte")

    assert not models.invisible("plain-ascii")
    assert not models.invisible("éàü")
    assert models.invisible("bidir\u202eattack")
    assert "\\u001b" in models.printable("esc\x1b[31m")
    assert b"\\u202e" in models.terminal({"msg": "rtl\u202e"})
    assert models.terminal({"msg": "ok"}) == b'{"msg":"ok"}\n'

    with pytest.raises(Error, match="finite UTF-8"):
        models.encode({"bad": float("nan")})
    with pytest.raises(Error, match="duplicate key"):
        models.decode('{"a": 1, "a": 2}')
    with pytest.raises(Error, match="must be finite"):
        models.decode('{"a": NaN}')
    with pytest.raises(Error, match="invalid JSON"):
        models.decode("{broken")

    assert models.normalize_date("2026-11-15") == "2026-11-15"
    for bad_date in ("2026-2-3", "0001-01-01", "9999-12-31", "not-a-date"):
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

    assert models.parse_duration(30) == 30
    assert models.parse_duration(0, allow_zero=True) == 0
    assert models.parse_duration("0", allow_zero=True) == 0
    assert models.parse_duration("15m") == 900
    assert models.parse_duration("1h30m") == 5400
    assert models.parse_duration("2h") == 7200
    assert models.parse_duration("1d") == 86400
    assert models.parse_duration("1w") == 604800
    assert models.parse_duration_seconds("300ms") == 0.3
    assert models.clean_env("") == ""
    assert models.clean_env("ok-value") == "ok-value"
    with pytest.raises(Error, match="no control characters"):
        models.clean_env("bad\nline")
    for bad_dur in (True, 0, -5, float("nan"), float("inf"), "bad", "0s", "150ms", [], "999y"):
        with pytest.raises(Error):
            models.parse_duration(bad_dur)
    for bad_sec in ("999y", "9999999999999999999999999y"):
        with pytest.raises(Error):
            models.parse_duration_seconds(bad_sec)


def test_check_version_rejects_missing_bool_and_wrong_versions() -> None:
    models.check_version({"version": models.FORMAT}, "bf.yaml")
    with pytest.raises(FormatError, match="has no version"):
        models.check_version({}, "bf.yaml")
    with pytest.raises(FormatError, match="declares version 11"):
        models.check_version({"version": 11}, "bf.yaml")
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

    # Special FIFO rejected on read and recorded in files(skipped=...).
    fifo = root / "notes" / "pipe.fifo"
    os.mkfifo(fifo)
    with pytest.raises(Error, match="expected a regular file"):
        store.read("notes/pipe.fifo")
    (root / "notes" / ".write-0123456789abcdef0123456789abcdef").write_text("tmp")
    (root / "notes" / "bad\\slash.txt").write_text("backslash")

    with pytest.raises(Error):
        store.files("notes")
    skipped: list[str] = []
    listed = store.files("notes", skipped=skipped)
    assert listed == ["notes/a.txt"]
    assert len(skipped) == 3

    assert store.files("nonexistent", skipped=[]) == []
    skipped_top: list[str] = []
    assert store.files("linked-dir", skipped=skipped_top) == []
    assert skipped_top
    assert "symlinks" in skipped_top[0]
    (root / "plain-file").write_text("x")
    skipped_file: list[str] = []
    assert store.files("plain-file", skipped=skipped_file) == []
    assert skipped_file
    assert "expected a directory" in skipped_file[0]

    store.delete("notes/a.txt")
    assert not store.exists("notes/a.txt")

    # Scan limits (MAX_FILES and 64-level directory depth).
    store.write("many/f1.txt", b"1", durable=False)
    store.write("many/f2.txt", b"2", durable=False)
    monkeypatch.setattr("bf.storage.MAX_FILES", 1)
    with pytest.raises(Error, match="1-entry scan limit"):
        store.files("many")
    monkeypatch.setattr("bf.storage.MAX_FILES", 100_000)

    deep_rel = "/".join(f"d{i}" for i in range(66))
    (root / deep_rel).mkdir(parents=True)
    with pytest.raises(Error, match="64-level depth limit"):
        store.files("d0")

    # Private `.bf` state and writer lock contention.
    with pytest.raises(Error, match="invalid private state path"):
        private_dir(store, "../escape")
    bf_dir = private_dir(store)
    assert bf_dir.root == root / ".bf"
    (root / ".bf").chmod(0o755)
    with pytest.raises(Error, match="mode 700"):
        private_dir(store)
    (root / ".bf").chmod(0o700)

    (root / "bad-state").symlink_to(outside)
    with pytest.raises(Error, match="without symlinks"):
        private_dir(store, "bad-state")

    with writer(store, wait=0.1):
        lock_file = root / ".bf" / "locks" / "write.lock"
        assert lock_file.is_file()
    # Simulate another process holding LOCK_EX on write.lock.
    lock_fd = os.open(root / ".bf" / "locks" / "write.lock", os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BusyError, match="another writer is active"), writer(store, wait=0.06):
            pass
    finally:
        os.close(lock_fd)

    # expand & xdg helpers.
    assert expand(Path("~/sub")).is_absolute()
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/ignored")
    assert xdg("XDG_CONFIG_HOME", ".config").is_absolute()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "abs-xdg"))
    assert xdg("XDG_CONFIG_HOME", ".config") == tmp_path / "abs-xdg"
    monkeypatch.delenv("HOME")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "expanduser", lambda _self: (_ for _ in ()).throw(RuntimeError("no home")))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: (_ for _ in ()).throw(RuntimeError("no home"))))
    with pytest.raises(Error, match="set HOME"):
        expand(Path("~/x"))
    with pytest.raises(Error, match="check the user name"):
        expand(Path("~nosuchuser/x"))
    with pytest.raises(Error, match="cannot find the home directory"):
        xdg("XDG_STATE_HOME", ".local/state")


def test_yaml_object_and_brain_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = config.yaml_object(b"oct: 0o10\nhex: 0x10\ndec: 42\ndate: 2026-11-15\nempty:\n")
    assert parsed == {"oct": 8, "hex": 16, "dec": 42, "date": "2026-11-15", "empty": None}
    assert config.yaml_object(b"") == {}

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
            config.yaml_object(payload, "test.yaml")

    monkeypatch.setattr(yaml, "CSafeLoader", None)
    with pytest.raises(Error, match="libyaml"):
        config.yaml_object(b"a: 1\n")
    monkeypatch.undo()

    # Brain selection precedence: --brain -> BF_BRAIN -> nearest enclosing bf.yaml -> Error.
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
    monkeypatch.setenv("DEFAULT_BRAIN_PATH", str(b1))
    assert config.load(config.select()).name == "beta"  # enclosing bf.yaml beats DEFAULT_BRAIN_PATH!
    monkeypatch.chdir(tmp_path)
    assert config.inside_brain() is None
    assert config.load(config.select()).name == "alpha"  # outside brain -> DEFAULT_BRAIN_PATH
    monkeypatch.setenv("BF_BRAIN", str(b2))
    assert config.load(config.select()).name == "beta"  # BF_BRAIN beats DEFAULT_BRAIN_PATH
    assert config.load(config.select(str(b1))).name == "alpha"  # --brain beats BF_BRAIN

    # Cross-brain resolution (`resolve_brain`): self, `brains:` map, sibling `../<name>`, percent-encoded, and unknown.
    sib_path.mkdir()
    (sib_path / "bf.yaml").write_text(f"version: {models.FORMAT}\nname: sib\n")
    s1 = Store(b1)
    assert config.resolve_brain(s1, cfg1, "").root == b1.resolve()
    assert config.resolve_brain(s1, cfg1, "alpha").root == b1.resolve()
    assert config.resolve_brain(s1, cfg1, "sib").root == sib_path.resolve()
    assert config.resolve_brain(s1, cfg1, "b2").root == b2.resolve()
    assert config.resolve_brain(s1, cfg1, "b2").root == b2.resolve()
    assert config.resolve_brain(s1, cfg1, quote(str(b2), safe="")).root == b2.resolve()
    assert config.resolve_brain(s1, cfg1, quote("../b2", safe="")).root == b2.resolve()
    with pytest.raises(Error, match="unknown brain"):
        config.resolve_brain(s1, cfg1, "nonexistent-brain")

    # Jobs, channels, servers, humanized durations, env mapping, and .env loading in `bf.yaml`.
    rich_b = tmp_path / "rich"
    rich_b.mkdir()
    (rich_b / "bf.yaml").write_text(
        f"version: {models.FORMAT}\nname: rich\n"
        "defaults:\n  timezone: UTC\n  env:\n    GWS_DIR: '{{home}}/.config/gws'\n    EMPTY_VAR: ''\n"
        "jobs:\n"
        "  sync:\n"
        "    run: [echo, hi]\n"
        "    ingest:\n      source: sync-src\n      mode: snapshot\n      allow_removal: true\n"
        "    every: 10m\n"
        "    lookback: 2d\n"
        "    overlap: 5m\n"
        "    timeout: 2m\n"
        "    hook: pre-commit\n"
        "    env:\n      EXTRA_VAR: custom\n"
        "  digest:\n    run: [claude, -p, '{{input}}']\n    input: prompts/digest.md\n    at: '08:00'\n    channel: chat\n"
        "  short-ing:\n    run: [echo, '[]']\n    ingest: simple\n"
        "channels:\n  chat:\n    listen: [echo, listen]\n    send: [echo, send]\n    timeout: 45s\n"
        "servers:\n  mcp:\n    run: [bf, mcp, --transport, http, --port, '9012']\n"
        "watch:\n  interval: 1s\n  debounce: 250ms\n"
    )
    rich_store = Store(rich_b)
    rich_cfg = config.load(rich_store)
    assert rich_cfg.env == {"GWS_DIR": "{{home}}/.config/gws", "EMPTY_VAR": ""}
    assert rich_cfg.jobs["sync"].every == 600
    assert rich_cfg.jobs["sync"].lookback == 172_800
    assert rich_cfg.jobs["sync"].overlap == 300
    assert rich_cfg.jobs["sync"].timeout == 120
    assert (rich_cfg.jobs["sync"].ingest, rich_cfg.jobs["sync"].mode, rich_cfg.jobs["sync"].allow_removal) == (
        "sync-src",
        "snapshot",
        True,
    )
    assert rich_cfg.jobs["sync"].hooks == ("pre-commit",)
    assert rich_cfg.jobs["sync"].env == {"GWS_DIR": "{{home}}/.config/gws", "EMPTY_VAR": "", "EXTRA_VAR": "custom"}
    assert rich_cfg.jobs["digest"].input == "prompts/digest.md"
    assert rich_cfg.jobs["digest"].channel == "chat"
    assert (rich_cfg.jobs["short-ing"].ingest, rich_cfg.jobs["short-ing"].mode) == ("simple", "upsert")
    assert rich_cfg.channels["chat"].timeout == 45
    assert rich_cfg.channels["chat"].env == {"GWS_DIR": "{{home}}/.config/gws", "EMPTY_VAR": ""}
    assert rich_cfg.servers["mcp"].run == ("bf", "mcp", "--transport", "http", "--port", "9012")
    assert rich_cfg.servers["mcp"].emit == "none"
    assert (rich_cfg.watch.interval, rich_cfg.watch.debounce_ms) == (1.0, 250)
    sec_b = tmp_path / "sec-watch"
    sec_b.mkdir()
    (sec_b / "bf.yaml").write_text(f"version: {models.FORMAT}\nname: sec\nwatch:\n  debounce: 1\n")
    assert config.load(Store(sec_b)).watch.debounce_ms == 1000

    # Optional gitignored `.env` loading + edge cases:
    assert config.load_dotenv(rich_store) == {}
    rich_store.write(
        ".env",
        b"# local overrides\n\nexport API_TOKEN = 'lit\"quote' # single-quote comment\n"
        b'QUOTED="a\\"b\\\\c" # double-quote comment\n'
        b'SLASH="trailing\\\\"\n'
        b"PLAIN=val # inline comment\nEMPTY=\n",
    )
    assert config.load_dotenv(rich_store) == {
        "API_TOKEN": 'lit"quote',
        "QUOTED": 'a"b\\c',
        "SLASH": "trailing\\",
        "PLAIN": "val",
        "EMPTY": "",
    }
    for bad_env_bytes, env_err in (
        (b"\xff\xfe", "not valid UTF-8"),
        (b"NO_EQUALS\n", "invalid environment assignment"),
        (b"1BAD=val\n", "invalid environment assignment"),
        (b"BF_BRAIN=/evil\n", "cannot override reserved"),
        (b'UNCLOSED="abc\n', "unclosed quote"),
        (b'ESCAPED_END="abc\\"\n', "unclosed quote"),
        (b'MID_QUOTE="a" "b"\n', "unclosed quote"),
        (b"SINGLE_MID='a' 'b'\n", "unclosed quote"),
    ):
        rich_store.write(".env", bad_env_bytes)
        with pytest.raises(Error, match=env_err):
            config.load_dotenv(rich_store)

    for idx, (bad_yaml, err_match) in enumerate(
        (
            (f"version: {models.FORMAT}\nname: badtz\ndefaults:\n  timezone: Not/AZone\n", "unknown IANA timezone"),
            (f"version: {models.FORMAT}\nname: badpost\ndefaults:\n  post: chat\n", "unknown property 'post'"),
            (f"version: {models.FORMAT}\nname: noexec\njobs:\n  j1:\n    every: 5m\n", "failed schema rule oneOf"),
            (
                f"version: {models.FORMAT}\nname: bininp\njobs:\n  j1:\n    builtin: build\n    input: p.md\n",
                "builtin job cannot specify input",
            ),
            (
                f"version: {models.FORMAT}\nname: resing\njobs:\n  j1:\n    run: [echo]\n    ingest: concepts\n",
                "reserved",
            ),
            (
                f"version: {models.FORMAT}\nname: ingemit\njobs:\n  j1:\n    run: [echo]\n    ingest: x\n    emit: action\n",
                "cannot combine ingest and emit",
            ),
            (
                f"version: {models.FORMAT}\nname: badch\nchannels:\n  c1:\n    timeout: 10s\n",
                "failed schema rule anyOf",
            ),
            (f"version: {models.FORMAT}\nname: nosrv\nservers:\n  s1:\n    enabled: true\n", "missing required 'run'"),
            (
                f"version: {models.FORMAT}\nname: dupunit\njobs:\n  u1:\n    run: [echo]\nservers:\n  u1:\n    run: [echo]\n",
                "declared in multiple sections",
            ),
            (
                f"version: {models.FORMAT}\nname: nosend\njobs:\n  j1:\n    run: [echo]\n    channel: c1\nchannels:\n  c1:\n    listen: [echo]\n",
                "has no send command",
            ),
            (
                f"version: {models.FORMAT}\nname: resenv\ndefaults:\n  env:\n    BF_BRAIN: /x\n",
                "cannot override reserved",
            ),
            (f"version: {models.FORMAT}\nname: badiv\nwatch:\n  interval: 100ms\n", "between 500ms and 1h"),
            (f"version: {models.FORMAT}\nname: baddeb\nwatch:\n  debounce: 10ms\n", "between 50ms and 60s"),
            (f"version: {models.FORMAT}\nname: badenvlist\ndefaults:\n  env: [A]\n", "expected type object"),
            (f"version: {models.FORMAT}\nname: badenvk\ndefaults:\n  env:\n    1BAD: v\n", "does not match pattern"),
            (f"version: {models.FORMAT}\nname: badenvv\ndefaults:\n  env:\n    OK_K: 123\n", "expected type string"),
        )
    ):
        bad_dir = tmp_path / f"bad-{idx}"
        bad_dir.mkdir(exist_ok=True)
        (bad_dir / "bf.yaml").write_text(bad_yaml)
        with pytest.raises(Error, match=err_match):
            config.load(Store(bad_dir))

    # Symlinked bf.yaml rejected by inside_brain().
    sym_brain = tmp_path / "sym-brain"
    sym_brain.mkdir()
    (sym_brain / "bf.yaml").symlink_to(b1 / "bf.yaml")
    monkeypatch.delenv("BF_BRAIN")
    monkeypatch.delenv("DEFAULT_BRAIN_PATH")
    monkeypatch.chdir(sym_brain)
    with pytest.raises(Error, match="not a regular file owned by you"):
        config.select()
