"""The guarded-write helper replaces a note only while it still has the digest an agent read."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bf import retrieve
from bf.storage import Store
from bf.validate import validate

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src/bf/skills/bf-use/scripts/guarded-write.py"
NOTE = "projects/offline.md"


def write(
    brain: Store, digest: str, content: str = "", path: str = NOTE, *options: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the bundled helper on a synthetic brain
        [sys.executable, str(HELPER), path, "--expect-sha256", digest, *options],
        cwd=brain.root,
        input=content,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def leftovers(brain: Store) -> list[Path]:
    return sorted(brain.root.rglob(".guarded-write-*"))


def test_write_replaces_the_note_that_was_read(brain: Store) -> None:
    (brain.root / NOTE).chmod(0o640)
    read = retrieve.read([brain], NOTE)
    edited = str(read["text"]).replace("# ", "# Revised ", 1)
    # Digests are case-insensitive hexadecimal, as the agent may copy them.
    result = write(brain, str(read["sha256"]).upper(), edited)
    assert result.returncode == 0, result.stderr
    reply = json.loads(result.stdout)
    assert reply == {"written": NOTE, "sha256": retrieve.read([brain], NOTE)["sha256"]}
    assert (brain.root / NOTE).read_text() == edited
    assert (brain.root / NOTE).stat().st_mode & 0o777 == 0o640
    assert not leftovers(brain)
    assert validate(brain)["valid"]
    # The returned digest guards the next edit; the old one no longer matches.
    assert write(brain, reply["sha256"], edited + "\nMore.\n").returncode == 0
    assert write(brain, str(read["sha256"]), "stale edit\n").returncode == 1


def test_write_refuses_a_note_changed_since_it_was_read(brain: Store) -> None:
    digest = str(retrieve.read([brain], NOTE)["sha256"])
    concurrent = (brain.root / NOTE).read_text() + "\nAnother session's line.\n"
    (brain.root / NOTE).write_text(concurrent)
    result = write(brain, digest, "# Replacement\n")
    assert result.returncode == 1
    assert not result.stdout
    assert "changed since it was read" in result.stderr
    assert f"expected {digest}" in result.stderr
    assert "Another session" not in result.stderr
    assert (brain.root / NOTE).read_text() == concurrent
    assert not leftovers(brain)


def test_write_rejects_invalid_arguments_and_input(brain: Store, tmp_path: Path) -> None:
    original = (brain.root / NOTE).read_bytes()
    digest = str(retrieve.read([brain], NOTE)["sha256"])
    for bad in ("", "abc", "g" * 64, digest + "0"):
        assert write(brain, bad, "# New\n").returncode == 2
    assert write(brain, digest, "").returncode == 1
    (brain.root / "projects/link.md").symlink_to(brain.root / NOTE)
    (brain.root / "projects/folder").mkdir()
    for path in ("projects/link.md", "projects/folder", "projects/missing.md", str(tmp_path / "absent/x.md")):
        result = write(brain, digest, "# New\n", path)
        assert result.returncode == 1
        assert "Not written" in result.stderr
    assert (brain.root / NOTE).read_bytes() == original
    assert not leftovers(brain)


def test_write_follows_no_symlink_in_the_path(brain: Store, tmp_path: Path) -> None:
    # A linked folder could redirect the write outside the brain between the read and the rename.
    original = (brain.root / NOTE).read_bytes()
    digest = str(retrieve.read([brain], NOTE)["sha256"])
    (brain.root / "linked").symlink_to(brain.root / "projects", target_is_directory=True)
    linked = tmp_path / "linked-brain"
    linked.symlink_to(brain.root, target_is_directory=True)
    for path in ("linked/offline.md", str(linked / NOTE)):
        result = write(brain, digest, "# New\n", path)
        assert result.returncode == 1
        assert "on a path without symbolic links" in result.stderr
    assert (brain.root / NOTE).read_bytes() == original
    assert not leftovers(brain)
    # Inside a linked brain folder, the working directory is already resolved: a relative path works.
    result = subprocess.run(  # noqa: S603 - the bundled helper on a synthetic brain
        [sys.executable, str(HELPER), NOTE, "--expect-sha256", digest],
        cwd=linked,
        input="# New\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (brain.root / NOTE).read_text() == "# New\n"


def test_write_checks_again_before_the_rename(
    brain: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location("guarded_write", HELPER)
    assert spec is not None
    assert spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    path = brain.root / NOTE
    original = path.read_bytes()
    directory = helper.folder(path)
    try:
        data, mode = helper.snapshot(directory, path.name)
        digest = helper.digest(data)
        # An editor saves while the helper writes its temporary file.
        answers = iter([(data, mode), (data + b"Another save.\n", mode)])
        monkeypatch.setattr(helper, "snapshot", lambda *_: next(answers))
        with pytest.raises(helper.ChangedError):
            helper.replace(directory, path.name, digest, helper.whole(b"# Replacement\n"))
    finally:
        os.close(directory)
    assert path.read_bytes() == original
    assert not leftovers(brain)
    # A directory sync failure after the rename is reported as written, never as unchanged or as success.
    monkeypatch.setattr(helper, "snapshot", lambda *_: (data, mode))
    monkeypatch.setattr(helper, "sync", lambda _directory: (_ for _ in ()).throw(OSError("sync")))
    monkeypatch.setattr(sys, "argv", ["guarded-write.py", str(path), "--expect-sha256", digest])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"# Replacement\n")))
    with pytest.raises(SystemExit) as exit_info:
        helper.main()
    assert exit_info.value.code == 1
    assert capsys.readouterr().err.startswith("Written, but its directory could not be synced")
    assert path.read_bytes() == b"# Replacement\n"


def test_replace_edits_one_exact_passage_of_the_note_that_was_read(brain: Store) -> None:
    (brain.root / NOTE).chmod(0o640)
    read = retrieve.read([brain], NOTE)
    text = str(read["text"])
    passage = text.rstrip("\n").splitlines()[-1]
    assert passage
    assert text.count(passage) == 1
    # Replacement mode never reads stdin: an agent sends only the passage and its replacement.
    result = write(brain, str(read["sha256"]), "IGNORED STDIN", NOTE, "--old", passage, "--new", "Revised line.")
    assert result.returncode == 0, result.stderr
    reply = json.loads(result.stdout)
    expected = text.replace(passage, "Revised line.")
    assert (brain.root / NOTE).read_text() == expected
    assert reply == {"written": NOTE, "sha256": retrieve.read([brain], NOTE)["sha256"]}
    assert (brain.root / NOTE).stat().st_mode & 0o777 == 0o640
    assert validate(brain)["valid"]
    # Multi-line passages can come from files; an empty replacement deletes the passage.
    old, new = brain.root.parent / "old.txt", brain.root.parent / "new.txt"
    old.write_text("\nRevised line.")
    new.write_text("")
    result = write(brain, reply["sha256"], "", NOTE, "--old-file", str(old), "--new-file", str(new))
    assert result.returncode == 0, result.stderr
    assert (brain.root / NOTE).read_text() == expected.replace("\nRevised line.", "")
    assert not leftovers(brain)


def test_replace_refuses_a_missing_repeated_or_changed_passage(brain: Store) -> None:
    brain.write(NOTE, b"# Offline\n\nKeep one.\n\nKeep one.\n\nPRIVATE unique line.\n")
    original = (brain.root / NOTE).read_bytes()
    digest = str(retrieve.read([brain], NOTE)["sha256"])
    for old, message in (("absent passage", "does not occur in the file"), ("Keep one.", "occurs 2 times")):
        result = write(brain, digest, "", NOTE, "--old", old, "--new", "x")
        assert result.returncode == 1
        assert message in result.stderr
        assert "PRIVATE" not in result.stderr
        assert not result.stdout
    assert (brain.root / NOTE).read_bytes() == original
    # The digest guards a replacement too: another session's edit wins.
    (brain.root / NOTE).write_bytes(original + b"Another session.\n")
    result = write(brain, digest, "", NOTE, "--old", "PRIVATE unique line.", "--new", "x")
    assert result.returncode == 1
    assert "changed since it was read" in result.stderr
    assert (brain.root / NOTE).read_bytes() == original + b"Another session.\n"
    # A replacement never empties a file.
    brain.write("projects/tiny.md", b"x")
    tiny = str(retrieve.read([brain], "projects/tiny.md")["sha256"])
    assert write(brain, tiny, "", "projects/tiny.md", "--old", "x", "--new", "").returncode == 1
    assert (brain.root / "projects/tiny.md").read_bytes() == b"x"
    assert not leftovers(brain)


@pytest.mark.parametrize(
    "options",
    [
        ("--old", "a"),
        ("--new", "b"),
        ("--old", "", "--new", "b"),
        ("--old", "a", "--old-file", "x", "--new", "b"),
        ("--old", "a", "--new", "b", "--new-file", "x"),
    ],
    ids=["old-only", "new-only", "empty-old", "two-olds", "two-news"],
)
def test_replace_rejects_incomplete_or_conflicting_options(brain: Store, options: tuple[str, ...]) -> None:
    original = (brain.root / NOTE).read_bytes()
    digest = str(retrieve.read([brain], NOTE)["sha256"])
    assert write(brain, digest, "# New\n", NOTE, *options).returncode == 2
    assert (brain.root / NOTE).read_bytes() == original


def test_replace_reports_an_unreadable_passage_file(brain: Store, tmp_path: Path) -> None:
    original = (brain.root / NOTE).read_bytes()
    digest = str(retrieve.read([brain], NOTE)["sha256"])
    result = write(brain, digest, "", NOTE, "--old-file", str(tmp_path / "absent"), "--new", "x")
    assert result.returncode == 1
    assert "--old-file is not a readable file" in result.stderr
    assert (brain.root / NOTE).read_bytes() == original
