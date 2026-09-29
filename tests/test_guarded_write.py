"""The guarded-write helper replaces a note only while it still has the digest an agent read."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bf import retrieve
from bf.storage import Store
from bf.validate import validate

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills/bf-learn/scripts/guarded-write.py"
NOTE = "projects/offline.md"


def write(brain: Store, digest: str, content: str, path: str = NOTE) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the bundled helper on a synthetic brain
        [sys.executable, str(HELPER), path, "--expect-sha256", digest],
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
    digest, mode = helper.current(path)
    # An editor saves while the helper writes its temporary file.
    answers = iter([(digest, mode), ("0" * 64, mode)])
    monkeypatch.setattr(helper, "current", lambda _path: next(answers))
    with pytest.raises(helper.ChangedError):
        helper.replace(path, digest, b"# Replacement\n")
    assert path.read_bytes() == original
    assert not leftovers(brain)
    # A directory sync failure after the rename is reported as written, never as unchanged or as success.
    monkeypatch.setattr(helper, "current", lambda _path: (digest, mode))
    monkeypatch.setattr(helper, "sync", lambda _directory: (_ for _ in ()).throw(OSError("sync")))
    monkeypatch.setattr(sys, "argv", ["guarded-write.py", str(path), "--expect-sha256", digest])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"# Replacement\n")))
    with pytest.raises(SystemExit) as exit_info:
        helper.main()
    assert exit_info.value.code == 1
    assert capsys.readouterr().err.startswith("Written, but its directory could not be synced")
    assert path.read_bytes() == b"# Replacement\n"
