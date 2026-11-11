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
    assert (destination / "bf-use/scripts/evidence.py").stat().st_mode & 0o100
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
    # Even --force keeps an edited file the new version dropped, as it keeps every file BF does not ship, and still
    # lists it under `edited`.
    recorded = json.loads(manifest.read_text())
    (destination / "bf-setup/retired.md").write_text("old, then edited\n")
    recorded["files"]["retired.md"] = hashlib.sha256(b"old\n").hexdigest()
    manifest.write_text(json.dumps(recorded))
    assert run(str(destination), code=1)["skills"][SKILLS.index("bf-setup")] == {
        "name": "bf-setup",
        "status": "modified",
        "edited": ["retired.md"],
    }
    assert run(str(destination), "--force")["skills"][SKILLS.index("bf-setup")] == {
        "name": "bf-setup",
        "status": "updated",
        "edited": ["retired.md"],
    }
    assert (destination / "bf-setup/retired.md").read_text() == "old, then edited\n"
    (destination / "bf-setup/retired.md").unlink()
    # A person's edit is kept and reported; --force replaces it and still lists it.
    skill = destination / "bf-use/SKILL.md"
    skill.write_text(skill.read_text() + "\nLocal rule.\n")
    edited = run(str(destination), code=1)
    assert edited["skills"][0] == {"name": "bf-use", "status": "modified", "edited": ["SKILL.md"]}
    assert skill.read_text().endswith("Local rule.\n")
    forced = run(str(destination), "--force")
    assert forced["skills"][0] == {"name": "bf-use", "status": "updated", "edited": ["SKILL.md"]}
    assert not skill.read_text().endswith("Local rule.\n")
    # A deleted file leaves the skill incomplete: it is reported like an edit, and --force restores it.
    (destination / "bf-use/scripts/evidence.py").unlink()
    deleted = run(str(destination), "--check", code=1)
    assert deleted["skills"][0] == {"name": "bf-use", "status": "modified", "edited": ["scripts/evidence.py"]}
    restored = run(str(destination), "--force")
    assert restored["skills"][0] == {"name": "bf-use", "status": "updated", "edited": ["scripts/evidence.py"]}
    assert (destination / "bf-use/scripts/evidence.py").is_file()
    # A file a person added where a newer version ships one is kept; the unchanged copy updates silently.
    manifest = destination / "bf-use" / MANIFEST
    recorded = json.loads(manifest.read_text())
    for name in ("SKILL.md", "scripts/evidence.py"):
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
    assert statuses(reply) == {
        "bf-use": "unmanaged",
        "bf-setup": "unmanaged",
        "bf-maintain": "installed",
        "bf-action": "installed",
    }
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
    helper = destination / "bf-use/scripts/evidence.py"
    helper.unlink()
    helper.symlink_to(tmp_path / "elsewhere.py")
    edited = run(str(destination), code=1)
    assert edited["skills"][0] == {"name": "bf-use", "status": "modified", "edited": ["scripts/evidence.py"]}
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
    assert statuses(reply) == {"bf-use": "outdated", **dict.fromkeys(SKILLS[1:], "missing")}
    assert statuses(run(str(destination))) == {"bf-use": "updated", **dict.fromkeys(SKILLS[1:], "installed")}
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
    helper = folder / "scripts/evidence.py"
    helper.write_text("Local change.\n")
    edited = run(str(destination), code=1)
    assert edited["skills"][0] == {"name": "bf-use", "status": "modified", "edited": ["scripts/evidence.py"]}
    helper.write_bytes(b"old scripts/evidence.py\n")
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
    assert reply["skills"][SKILLS.index("bf-setup")] == {"name": "bf-setup", "status": status}
    if status == "newer":
        assert (folder / "SKILL.md").read_text() == "Newer procedure.\n"
        assert (folder / "future.md").is_file()
        assert statuses(run(str(destination), "--force"))["bf-setup"] == "updated"
    # An older or unparsable version's copy updates like any other; either way, only this version's files remain.
    assert not (folder / "future.md").exists()
    assert json.loads((folder / MANIFEST).read_text())["version"] == __version__


def test_a_relative_destination_is_reported_as_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert run("skills")["destination"] == str(tmp_path / "skills")


def test_skills_link_manages_relative_host_symlinks_inside_a_brain(tmp_path: Path) -> None:
    outside = CliRunner().invoke(app, ["skills", str(tmp_path / "outside"), "--link"])
    assert outside.exit_code == 1
    assert "--link requires DIR to be inside a brain directory holding bf.yaml" in str(outside.exception)

    brain_root = tmp_path / "brain"
    brain_root.mkdir()
    Store(brain_root).write("bf.yaml", b"version: 8\nname: fixture\n")
    destination = brain_root / "skills"

    # Pre-seed a stale bf-* link, an outdated bf-* link, and an unrelated user file in host directories.
    agents_dir = brain_root / ".agents/skills"
    claude_dir = brain_root / ".claude/skills"
    agents_dir.mkdir(parents=True)
    claude_dir.mkdir(parents=True)
    (agents_dir / "bf-retired").symlink_to("../../skills/bf-retired")
    (agents_dir / "custom.md").write_text("keep me\n")
    (claude_dir / "bf-use").symlink_to("../wrong-target")

    preview = run(str(destination), "--link", "--check", code=1)
    preview_links = {entry["path"]: entry["status"] for entry in preview["links"]}
    assert preview_links[".agents/skills/bf-retired"] == "stale"
    assert preview_links[".agents/skills/bf-use"] == "missing"
    assert preview_links[".claude/skills/bf-use"] == "outdated"

    installed = run(str(destination), "--link")
    link_map = {entry["path"]: entry for entry in installed["links"]}
    assert link_map[".agents/skills/bf-retired"]["status"] == "pruned"
    assert not (agents_dir / "bf-retired").exists()
    assert not (agents_dir / "bf-retired").is_symlink()
    assert (agents_dir / "custom.md").read_text() == "keep me\n"
    assert link_map[".claude/skills/bf-use"]["status"] == "updated"
    for host in (".agents", ".claude"):
        for skill in SKILLS:
            entry = brain_root / host / "skills" / skill
            assert entry.is_symlink()
            assert entry.readlink() == Path("../../skills") / skill
            assert (entry / "SKILL.md").is_file()

    checked = run(str(destination), "--link", "--check")
    assert all(entry["status"] == "current" for entry in checked["links"])

    # An unmanaged non-symlink bf-* folder in a host directory fails closed without deleting it.
    (agents_dir / "bf-action").unlink()
    (agents_dir / "bf-action").mkdir()
    (agents_dir / "bf-action/local.md").write_text("mine\n")
    blocked = run(str(destination), "--link", code=1)
    blocked_map = {entry["path"]: entry["status"] for entry in blocked["links"]}
    assert blocked_map[".agents/skills/bf-action"] == "unmanaged"
    assert (agents_dir / "bf-action/local.md").read_text() == "mine\n"

    # Installing into .agents/skills directly creates real folders there and relative symlinks in .claude/skills.
    brain2 = tmp_path / "brain2"
    brain2.mkdir()
    Store(brain2).write("bf.yaml", b"version: 8\nname: second\n")
    direct_reply = run(str(brain2 / ".agents/skills"), "--link")
    assert all(entry["path"].startswith(".claude/skills/") for entry in direct_reply["links"])
    assert (brain2 / ".claude/skills/bf-use").readlink() == Path("../../.agents/skills/bf-use")


def test_skills_link_accepts_whole_directory_host_symlinks_to_destination(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    brain.mkdir()
    Store(brain).write("bf.yaml", b"version: 8\nname: whole-dir\n")
    (brain / ".agents").mkdir()
    (brain / ".claude").mkdir()
    (brain / ".agents/skills").symlink_to("../skills")
    (brain / ".claude/skills").symlink_to("../.agents/skills")

    # Preview before skills/ exists recognizes both whole-directory links as already pointing at destination.
    preview = run(str(brain / "skills"), "--check", "--link", code=1)
    assert preview["links"] == []
    assert all(entry["status"] == "missing" for entry in preview["skills"])

    # Installing into skills/ keeps custom skills and both whole-directory host symlinks intact.
    (brain / "skills/custom-skill").mkdir(parents=True)
    (brain / "skills/custom-skill/SKILL.md").write_text("---\nname: custom-skill\n---\n")
    installed = run(str(brain / "skills"), "--link")
    assert installed["ok"] is True
    assert installed["links"] == []
    assert (brain / ".agents/skills").is_symlink()
    assert (brain / ".agents/skills").readlink() == Path("../skills")
    assert (brain / ".claude/skills").is_symlink()
    assert (brain / ".claude/skills").readlink() == Path("../.agents/skills")
    assert (brain / ".claude/skills/custom-skill/SKILL.md").is_file()
    for skill in SKILLS:
        assert (brain / ".agents/skills" / skill / "SKILL.md").is_file()
        assert (brain / ".claude/skills" / skill / "SKILL.md").is_file()

    checked = run(str(brain / "skills"), "--check", "--link")
    assert checked["ok"] is True
    assert checked["links"] == []

    # Host symlinks escaping the brain, pointing elsewhere, or looping still fail closed.
    outside = tmp_path / "outside"
    outside.mkdir()
    other = brain / "other-skills"
    other.mkdir()
    for bad_target in (outside, Path("../other-skills"), Path("skills")):
        (brain / ".agents/skills").unlink()
        (brain / ".agents/skills").symlink_to(bad_target)
        rejected = CliRunner().invoke(app, ["skills", str(brain / "skills"), "--link"])
        assert rejected.exit_code == 1
        assert "is not a folder" in str(rejected.exception)
        rejected_check = CliRunner().invoke(app, ["skills", str(brain / "skills"), "--check", "--link"])
        assert rejected_check.exit_code == 1
        assert "is not a folder" in str(rejected_check.exception)
