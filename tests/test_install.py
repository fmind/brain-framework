"""`bf skills` installs the packaged skills and updates them only while a person has not edited them."""

from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bf import __version__
from bf.cli import app
from bf.install import MANIFEST, SKILLS, skills
from bf.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, code: int = 0) -> dict:
    result = CliRunner().invoke(app, ["skills", *args])
    assert result.exit_code == code, result.output
    return json.loads(result.stdout)


def statuses(reply: dict) -> dict[str, str]:
    return {entry["name"]: entry["status"] for entry in reply["skills"]}


def test_skills_install_update_and_never_replace_edits(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    assert statuses(run(str(destination), "--check", code=1)) == dict.fromkeys(SKILLS, "missing")
    reply = run(str(destination))
    assert (reply["version"], statuses(reply)) == (__version__, dict.fromkeys(SKILLS, "installed"))
    # The installed copy matches the packaged skill, with executable helpers and a manifest of digests.
    packaged = ROOT / "src/bf/skills/bf-use"
    for path in packaged.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            relative = path.relative_to(packaged)
            assert (destination / "bf-use" / relative).read_bytes() == path.read_bytes()
    assert (destination / "bf-use/scripts/new-action.py").stat().st_mode & 0o100
    assert json.loads((destination / "bf-use" / MANIFEST).read_text())["version"] == __version__
    assert statuses(run(str(destination), "--check")) == dict.fromkeys(SKILLS, "current")
    # An older install whose files are unedited updates, and a file the new version dropped goes away.
    manifest = destination / "bf-setup" / MANIFEST
    recorded = json.loads(manifest.read_text())
    (destination / "bf-setup/retired.md").write_text("old\n")
    recorded["files"]["retired.md"] = hashlib.sha256(b"old\n").hexdigest()
    manifest.write_text(json.dumps(recorded))
    assert statuses(run(str(destination), "--check", code=1))["bf-setup"] == "outdated"
    assert statuses(run(str(destination)))["bf-setup"] == "updated"
    assert not (destination / "bf-setup/retired.md").exists()
    # Even --force keeps an edited file the new version dropped, as it keeps every file BF does not ship.
    recorded = json.loads(manifest.read_text())
    (destination / "bf-setup/retired.md").write_text("old, then edited\n")
    recorded["files"]["retired.md"] = hashlib.sha256(b"old\n").hexdigest()
    manifest.write_text(json.dumps(recorded))
    assert run(str(destination), code=1)["skills"][1] == {
        "name": "bf-setup",
        "status": "modified",
        "edited": ["retired.md"],
    }
    assert statuses(run(str(destination), "--force"))["bf-setup"] == "updated"
    assert (destination / "bf-setup/retired.md").read_text() == "old, then edited\n"
    (destination / "bf-setup/retired.md").unlink()
    # A person's edit is kept and reported; --force replaces it.
    skill = destination / "bf-use/SKILL.md"
    skill.write_text(skill.read_text() + "\nLocal rule.\n")
    edited = run(str(destination), code=1)
    assert edited["skills"][0] == {"name": "bf-use", "status": "modified", "edited": ["SKILL.md"]}
    assert skill.read_text().endswith("Local rule.\n")
    assert statuses(run(str(destination), "--force"))["bf-use"] == "updated"
    assert not skill.read_text().endswith("Local rule.\n")
    # A deleted file leaves the skill incomplete: it is reported like an edit, and --force restores it.
    (destination / "bf-use/scripts/new-action.py").unlink()
    deleted = run(str(destination), "--check", code=1)
    assert deleted["skills"][0] == {"name": "bf-use", "status": "modified", "edited": ["scripts/new-action.py"]}
    assert statuses(run(str(destination), "--force"))["bf-use"] == "updated"
    assert (destination / "bf-use/scripts/new-action.py").is_file()
    # A file a person added where a newer version ships one is kept; the unchanged copy updates silently.
    manifest = destination / "bf-use" / MANIFEST
    recorded = json.loads(manifest.read_text())
    for name in ("SKILL.md", "scripts/new-action.py"):
        del recorded["files"][name]
    manifest.write_text(json.dumps(recorded))
    skill.write_text("My own skill.\n")
    added = run(str(destination), code=1)
    assert added["skills"][0] == {"name": "bf-use", "status": "modified", "edited": ["SKILL.md"]}
    assert skill.read_text() == "My own skill.\n"
    skill.write_bytes((packaged / "SKILL.md").read_bytes())
    assert statuses(run(str(destination)))["bf-use"] == "updated"


def test_skills_leave_foreign_and_linked_folders_alone(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    (destination / "bf-use").mkdir(parents=True)
    (destination / "bf-use/SKILL.md").write_text("# Mine\n")
    elsewhere = tmp_path / "mirror"
    elsewhere.mkdir()
    (destination / "bf-setup").symlink_to(elsewhere)
    reply = run(str(destination), code=1)
    assert statuses(reply) == {"bf-use": "unmanaged", "bf-setup": "unmanaged", "bf-maintain": "installed"}
    assert (destination / "bf-use/SKILL.md").read_text() == "# Mine\n"
    assert not any(elsewhere.iterdir())
    # Forcing never writes through a link: replacing it is the person's decision.
    result = CliRunner().invoke(app, ["skills", str(destination), "--force"])
    assert result.exit_code == 1
    assert "bf-setup is a link" in str(result.exception)


def older(folder: Path) -> None:
    """Turn an installed skill into an unedited install of an older version: other bytes, recorded in its manifest."""
    recorded = {}
    for path in sorted(item for item in folder.rglob("*") if item.is_file() and item.name != MANIFEST):
        name = path.relative_to(folder).as_posix()
        path.write_bytes(data := f"old {name}\n".encode())
        recorded[name] = hashlib.sha256(data).hexdigest()
    (folder / MANIFEST).write_text(json.dumps({"version": "16.0.0", "files": recorded}))


def test_a_link_inside_a_skill_is_listed_and_stops_a_forced_update(tmp_path: Path) -> None:
    destination = tmp_path / "skills"
    run(str(destination))
    older(destination / "bf-use")
    helper = destination / "bf-use/scripts/new-action.py"
    helper.unlink()
    helper.symlink_to(tmp_path / "elsewhere.py")
    edited = run(str(destination), code=1)
    assert edited["skills"][0] == {"name": "bf-use", "status": "modified", "edited": ["scripts/new-action.py"]}
    # Forcing never writes through it: the run stops after replacing the files before it, and the next one still
    # lists only the link, never the packaged bytes already written.
    result = CliRunner().invoke(app, ["skills", str(destination), "--force"])
    assert result.exit_code == 1
    assert "refusing to replace a non-regular file" in str(result.exception)
    assert not (tmp_path / "elsewhere.py").exists()
    assert (destination / "bf-use/SKILL.md").read_bytes() == (ROOT / "src/bf/skills/bf-use/SKILL.md").read_bytes()
    assert run(str(destination), "--check", code=1)["skills"][0] == edited["skills"][0]


def interrupt(monkeypatch: pytest.MonkeyPatch, writes: int) -> None:
    """The next `writes` file writes succeed, then a full disk stops the run once."""
    write = Store.write
    remaining = [writes]

    def limited(store: Store, name: str, data: bytes, **options: bool) -> None:
        remaining[0] -= 1
        if remaining[0] < 0:
            monkeypatch.setattr(Store, "write", write)
            raise OSError(errno.ENOSPC, "No space left on device")
        write(store, name, data, **options)

    monkeypatch.setattr(Store, "write", limited)


def test_an_interrupted_install_or_update_finishes_on_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "skills"
    # A full disk can fail the first write after creating the folder: an empty folder installs like a missing one.
    (destination / "bf-use").mkdir(parents=True)
    assert statuses(run(str(destination), "--check", code=1)) == dict.fromkeys(SKILLS, "missing")
    # The first install stops after its manifest and one file: BF's partial copy is outdated, not unmanaged.
    interrupt(monkeypatch, 2)
    with pytest.raises(OSError, match="No space"):
        skills(destination)
    reply = run(str(destination), "--check", code=1)
    assert reply["skills"][0] == {"name": "bf-use", "status": "outdated"}
    assert statuses(reply) == {"bf-use": "outdated", "bf-setup": "missing", "bf-maintain": "missing"}
    assert statuses(run(str(destination))) == {"bf-use": "updated", "bf-setup": "installed", "bf-maintain": "installed"}
    assert statuses(run(str(destination), "--check")) == dict.fromkeys(SKILLS, "current")
    # An unedited older install whose update stops after one file: that file already holds the packaged bytes.
    folder = destination / "bf-use"
    older(folder)
    interrupt(monkeypatch, 1)
    with pytest.raises(OSError, match="No space"):
        skills(destination)
    assert (folder / "SKILL.md").read_bytes() == (ROOT / "src/bf/skills/bf-use/SKILL.md").read_bytes()
    assert run(str(destination), "--check", code=1)["skills"][0] == {"name": "bf-use", "status": "outdated"}
    # Only a person's edit is listed, never the bytes BF already wrote beside it.
    helper = folder / "scripts/new-action.py"
    helper.write_text("Local change.\n")
    edited = run(str(destination), code=1)
    assert edited["skills"][0] == {"name": "bf-use", "status": "modified", "edited": ["scripts/new-action.py"]}
    helper.write_bytes(b"old scripts/new-action.py\n")
    assert statuses(run(str(destination)))["bf-use"] == "updated"
    assert statuses(run(str(destination), "--check")) == dict.fromkeys(SKILLS, "current")


@pytest.mark.parametrize(
    ("version", "status"),
    [
        ("999.0.0", "newer"),
        (__version__.rpartition(".")[0] + ".999", "newer"),  # A later patch of this minor release.
        (__version__, "updated"),
        ("9.99.99", "updated"),  # Numbers, not text: 9 is older than this release's major.
        ("999.0", "updated"),  # Not X.Y.Z: the digests decide.
        (999, "updated"),
    ],
)
def test_an_older_bf_leaves_a_newer_install_alone(tmp_path: Path, version: object, status: str) -> None:
    destination = tmp_path / "skills"
    run(str(destination))
    folder = destination / "bf-setup"
    recorded = json.loads((folder / MANIFEST).read_text())
    recorded["version"] = version
    (folder / MANIFEST).write_text(json.dumps(recorded))
    # Whichever version recorded them, this version's own files are current: no run would change them.
    assert statuses(run(str(destination), "--check"))["bf-setup"] == "current"
    # Two versions can share a skills directory: a newer one's different copy stays until --force.
    (folder / "SKILL.md").write_text("Newer procedure.\n")
    (folder / "future.md").write_text("Newer reference.\n")
    for name, data in (("SKILL.md", b"Newer procedure.\n"), ("future.md", b"Newer reference.\n")):
        recorded["files"][name] = hashlib.sha256(data).hexdigest()
    (folder / MANIFEST).write_text(json.dumps(recorded))
    reply = run(str(destination), code=1 if status == "newer" else 0)
    assert reply["skills"][1] == {"name": "bf-setup", "status": status}
    if status == "newer":
        assert (folder / "SKILL.md").read_text() == "Newer procedure.\n"
        assert (folder / "future.md").is_file()
        assert statuses(run(str(destination), "--force"))["bf-setup"] == "updated"
    # An older or unparsable version's copy updates like any other; either way, only this version's files remain.
    assert not (folder / "future.md").exists()
    assert json.loads((folder / MANIFEST).read_text())["version"] == __version__
