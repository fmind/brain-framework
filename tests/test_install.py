"""`bf skill install` manages the packaged `bf` skill and host symlinks safely."""

from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bf import __version__
from bf.cli import app
from bf.install import MANIFEST, SKILL, SKILLS, install
from bf.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, code: int = 0) -> dict:
    result = CliRunner().invoke(app, ["skill", "install", *args])
    assert result.exit_code == code, result.output
    return json.loads(result.stdout)


def make_brain(root: Path, name: str = "fixture") -> Store:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root)
    store.write("bf.yaml", f"version: 11\nname: {name}\n".encode())
    return store


def older(folder: Path) -> None:
    """Turn an installed skill into an unedited install of an older version."""
    recorded = {}
    for path in sorted(item for item in folder.rglob("*") if item.is_file() and item.name != MANIFEST):
        name = path.relative_to(folder).as_posix()
        path.write_bytes(data := f"old {name}\n".encode())
        recorded[name] = hashlib.sha256(data).hexdigest()
    (folder / MANIFEST).write_text(json.dumps({"version": "16.0.0", "files": recorded}))


def test_skill_install_update_and_never_replace_edits(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    make_brain(brain)
    preview = run("--brain", str(brain), "--check", code=1)
    assert (preview["ok"], preview["scope"]) == (False, "brain")
    assert preview["skills"] == [{"name": SKILL, "status": "missing"}]

    reply = run("--brain", str(brain))
    assert (reply["ok"], reply["scope"], reply["version"]) == (True, "brain", __version__)
    assert reply["destination"] == str((brain / "skills").resolve())
    assert reply["skills"] == [{"name": SKILL, "status": "installed"}]
    assert [entry["path"] for entry in reply["links"]] == [".agents/skills/bf", ".claude/skills/bf"]
    assert all(entry["status"] == "linked" for entry in reply["links"])

    packaged = ROOT / "src/bf/skills/bf"
    for path in packaged.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            relative = path.relative_to(packaged)
            assert (brain / "skills/bf" / relative).read_bytes() == path.read_bytes()
    assert json.loads((brain / "skills/bf" / MANIFEST).read_text())["version"] == __version__
    checked = run("--brain", str(brain), "--check")
    assert checked["ok"] is True
    assert checked["skills"] == [{"name": SKILL, "status": "current"}]
    assert all(entry["status"] == "current" for entry in checked["links"])

    # An unedited retired file tracked in the manifest is removed on update.
    manifest = brain / "skills/bf" / MANIFEST
    recorded = json.loads(manifest.read_text())
    (brain / "skills/bf/retired.md").write_text("old\n")
    recorded["files"]["retired.md"] = hashlib.sha256(b"old\n").hexdigest()
    manifest.write_text(json.dumps(recorded))
    assert run("--brain", str(brain), "--check", code=1)["skills"][0]["status"] == "outdated"
    assert run("--brain", str(brain))["skills"][0]["status"] == "updated"
    assert not (brain / "skills/bf/retired.md").exists()

    # An edited retired file is reported under `edited` and retained even with --force.
    recorded = json.loads(manifest.read_text())
    (brain / "skills/bf/retired.md").write_text("old, then edited\n")
    recorded["files"]["retired.md"] = hashlib.sha256(b"old\n").hexdigest()
    manifest.write_text(json.dumps(recorded))
    assert run("--brain", str(brain), code=1)["skills"][0] == {
        "name": SKILL,
        "status": "modified",
        "edited": ["retired.md"],
    }
    assert run("--brain", str(brain), "--force")["skills"][0] == {
        "name": SKILL,
        "status": "updated",
        "edited": ["retired.md"],
    }
    assert (brain / "skills/bf/retired.md").read_text() == "old, then edited\n"
    (brain / "skills/bf/retired.md").unlink()

    # A person's edit to SKILL.md is kept and reported; --force restores the packaged copy.
    skill_md = brain / "skills/bf/SKILL.md"
    skill_md.write_text(skill_md.read_text() + "\nLocal rule.\n")
    edited = run("--brain", str(brain), code=1)
    assert edited["skills"][0] == {"name": SKILL, "status": "modified", "edited": ["SKILL.md"]}
    assert skill_md.read_text().endswith("Local rule.\n")
    forced = run("--brain", str(brain), "--force")
    assert forced["skills"][0] == {"name": SKILL, "status": "updated", "edited": ["SKILL.md"]}
    assert not skill_md.read_text().endswith("Local rule.\n")

    # A deleted file leaves the skill modified; --force restores it.
    skill_md.unlink()
    deleted = run("--brain", str(brain), "--check", code=1)
    assert deleted["skills"][0] == {"name": SKILL, "status": "modified", "edited": ["SKILL.md"]}
    restored = run("--brain", str(brain), "--force")
    assert restored["skills"][0] == {"name": SKILL, "status": "updated", "edited": ["SKILL.md"]}
    assert skill_md.is_file()

    # Untracked SKILL.md differs from packaged bytes and blocks until replaced or matching.
    recorded = json.loads(manifest.read_text())
    del recorded["files"]["SKILL.md"]
    manifest.write_text(json.dumps(recorded))
    skill_md.write_text("My own skill.\n")
    added = run("--brain", str(brain), code=1)
    assert added["skills"][0] == {"name": SKILL, "status": "modified", "edited": ["SKILL.md"]}
    assert skill_md.read_text() == "My own skill.\n"
    skill_md.write_bytes((packaged / "SKILL.md").read_bytes())
    assert run("--brain", str(brain))["skills"][0]["status"] == "updated"


def test_skill_install_leaves_foreign_and_linked_folders_alone(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    make_brain(brain)
    (brain / "skills/bf").mkdir(parents=True)
    (brain / "skills/bf/SKILL.md").write_text("# Mine\n")
    reply = run("--brain", str(brain), code=1)
    assert reply["skills"] == [{"name": SKILL, "status": "unmanaged"}]
    assert (brain / "skills/bf/SKILL.md").read_text() == "# Mine\n"

    # A symlinked skills/bf directory is treated as unmanaged and never overwritten even with --force.
    (brain / "skills/bf/SKILL.md").unlink()
    (brain / "skills/bf").rmdir()
    elsewhere = tmp_path / "mirror"
    elsewhere.mkdir()
    (brain / "skills/bf").symlink_to(elsewhere)
    linked = run("--brain", str(brain), code=1)
    assert linked["skills"] == [{"name": SKILL, "status": "unmanaged"}]
    assert not any(elsewhere.iterdir())
    forced = CliRunner().invoke(app, ["skill", "install", "--brain", str(brain), "--force"])
    assert forced.exit_code == 1
    assert "skills/bf is a link" in str(forced.exception)


def test_a_link_inside_a_skill_is_listed_and_stops_a_forced_update(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    store = make_brain(brain)
    run("--brain", str(brain))
    older(brain / "skills/bf")
    skill_md = brain / "skills/bf/SKILL.md"
    skill_md.unlink()
    skill_md.symlink_to(tmp_path / "elsewhere.md")
    assert store.root == brain
    edited = run("--brain", str(brain), code=1)
    assert edited["skills"][0] == {"name": SKILL, "status": "modified", "edited": ["SKILL.md"]}
    forced = CliRunner().invoke(app, ["skill", "install", "--brain", str(brain), "--force"])
    assert forced.exit_code == 1
    assert "refusing to replace a non-regular file" in str(forced.exception)
    assert not (tmp_path / "elsewhere.md").exists()
    assert run("--brain", str(brain), "--check", code=1)["skills"][0] == edited["skills"][0]


def interrupt(monkeypatch: pytest.MonkeyPatch, writes: int) -> None:
    """Allow `writes` Store.write calls, then fail once with ENOSPC."""
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
    brain = tmp_path / "brain"
    store = make_brain(brain)
    # An empty skills/bf directory still installs cleanly.
    (brain / "skills/bf").mkdir(parents=True)
    assert run("--brain", str(brain), "--check", code=1)["skills"] == [{"name": SKILL, "status": "missing"}]
    # Interrupt after the initial manifest write so partial install resumes as outdated.
    interrupt(monkeypatch, 1)
    with pytest.raises(OSError, match="No space"):
        install(store)
    assert run("--brain", str(brain), "--check", code=1)["skills"] == [{"name": SKILL, "status": "outdated"}]
    assert run("--brain", str(brain))["skills"] == [{"name": SKILL, "status": "updated"}]
    assert run("--brain", str(brain), "--check")["skills"] == [{"name": SKILL, "status": "current"}]


@pytest.mark.parametrize(
    ("version", "status"),
    [
        ("999.0.0", "newer"),
        (__version__.rpartition(".")[0] + ".999", "newer"),
        (__version__, "updated"),
        ("9.99.99", "updated"),
        ("999.0", "updated"),
        (999, "updated"),
    ],
)
def test_an_older_bf_leaves_a_newer_install_alone(tmp_path: Path, version: object, status: str) -> None:
    brain = tmp_path / "brain"
    make_brain(brain)
    run("--brain", str(brain))
    folder = brain / "skills/bf"
    recorded = json.loads((folder / MANIFEST).read_text())
    recorded["version"] = version
    (folder / MANIFEST).write_text(json.dumps(recorded))
    assert run("--brain", str(brain), "--check")["skills"] == [{"name": SKILL, "status": "current"}]

    (folder / "SKILL.md").write_text("Newer procedure.\n")
    (folder / "future.md").write_text("Newer reference.\n")
    for name, data in (("SKILL.md", b"Newer procedure.\n"), ("future.md", b"Newer reference.\n")):
        recorded["files"][name] = hashlib.sha256(data).hexdigest()
    (folder / MANIFEST).write_text(json.dumps(recorded))
    reply = run("--brain", str(brain), code=1 if status == "newer" else 0)
    assert reply["skills"] == [{"name": SKILL, "status": status}]
    if status == "newer":
        assert (folder / "SKILL.md").read_text() == "Newer procedure.\n"
        assert (folder / "future.md").is_file()
        assert run("--brain", str(brain), "--force")["skills"] == [{"name": SKILL, "status": "updated"}]
    assert not (folder / "future.md").exists()
    assert json.loads((folder / MANIFEST).read_text())["version"] == __version__


def test_skill_install_discovers_custom_skills_prunes_legacy_and_rejects_symlinks(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    make_brain(brain)
    (brain / "skills/custom-skill").mkdir(parents=True)
    (brain / "skills/custom-skill/SKILL.md").write_text("---\nname: custom-skill\ndescription: Custom.\n---\n")

    agents_dir = brain / ".agents/skills"
    claude_dir = brain / ".claude/skills"
    agents_dir.mkdir(parents=True)
    claude_dir.mkdir(parents=True)
    # Stale legacy bf-* links, stale previously linked brain-skill links, and unrelated host links/files.
    (agents_dir / "bf-use").symlink_to("../../skills/bf-use")
    (agents_dir / "removed-skill").symlink_to("../../skills/removed-skill")
    (agents_dir / "external-skill").symlink_to("../../external/tool")
    (agents_dir / "custom.md").write_text("keep me\n")
    (claude_dir / "bf").symlink_to("../wrong-target")

    preview = run("--brain", str(brain), "--check", code=1)
    preview_links = {entry["path"]: entry["status"] for entry in preview["links"]}
    assert preview_links[".agents/skills/bf-use"] == "stale"
    assert preview_links[".agents/skills/removed-skill"] == "stale"
    assert preview_links[".agents/skills/bf"] == "missing"
    assert preview_links[".agents/skills/custom-skill"] == "missing"
    assert preview_links[".claude/skills/bf"] == "outdated"

    installed = run("--brain", str(brain))
    link_map = {entry["path"]: entry for entry in installed["links"]}
    assert link_map[".agents/skills/bf-use"]["status"] == "pruned"
    assert link_map[".agents/skills/removed-skill"]["status"] == "pruned"
    assert not (agents_dir / "bf-use").is_symlink()
    assert not (agents_dir / "removed-skill").is_symlink()
    assert (agents_dir / "external-skill").is_symlink()
    assert (agents_dir / "custom.md").read_text() == "keep me\n"
    assert link_map[".claude/skills/bf"]["status"] == "updated"
    for host in (".agents", ".claude"):
        for skill in (*SKILLS, "custom-skill"):
            entry = brain / host / "skills" / skill
            assert entry.is_symlink()
            assert entry.readlink() == Path("../../skills") / skill
            assert (entry / "SKILL.md").is_file()

    checked = run("--brain", str(brain), "--check")
    assert checked["ok"] is True
    assert all(entry["status"] == "current" for entry in checked["links"])

    # An unmanaged non-symlink folder in a host directory fails closed without deleting it.
    (agents_dir / "bf").unlink()
    (agents_dir / "bf").mkdir()
    (agents_dir / "bf/local.md").write_text("mine\n")
    blocked = run("--brain", str(brain), code=1)
    blocked_map = {entry["path"]: entry["status"] for entry in blocked["links"]}
    assert blocked_map[".agents/skills/bf"] == "unmanaged"
    assert (agents_dir / "bf/local.md").read_text() == "mine\n"

    # A symlinked directory inside skills/ is rejected cleanly.
    (brain / "skills/linked-skill").symlink_to(tmp_path)
    rejected = CliRunner().invoke(app, ["skill", "install", "--brain", str(brain)])
    assert rejected.exit_code == 1
    assert "skills/linked-skill: symlinks are not followed" in str(rejected.exception)


def test_skill_install_accepts_whole_directory_host_symlinks(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    make_brain(brain, "whole-dir")
    (brain / ".agents").mkdir()
    (brain / ".claude").mkdir()
    (brain / ".agents/skills").symlink_to("../skills")
    (brain / ".claude/skills").symlink_to("../.agents/skills")

    preview = run("--brain", str(brain), "--check", code=1)
    assert preview["links"] == []
    assert preview["skills"] == [{"name": SKILL, "status": "missing"}]

    (brain / "skills/custom-skill").mkdir(parents=True)
    (brain / "skills/custom-skill/SKILL.md").write_text("---\nname: custom-skill\n---\n")
    installed = run("--brain", str(brain))
    assert (installed["ok"], installed["links"]) == (True, [])
    assert (brain / ".agents/skills").readlink() == Path("../skills")
    assert (brain / ".claude/skills").readlink() == Path("../.agents/skills")
    assert (brain / ".claude/skills/custom-skill/SKILL.md").is_file()
    assert (brain / ".agents/skills/bf/SKILL.md").is_file()
    assert (brain / ".claude/skills/bf/SKILL.md").is_file()
    assert run("--brain", str(brain), "--check")["ok"] is True

    outside = tmp_path / "outside"
    outside.mkdir()
    (brain / "other-skills").mkdir()
    for bad_target in (outside, Path("../other-skills"), Path("skills")):
        (brain / ".agents/skills").unlink()
        (brain / ".agents/skills").symlink_to(bad_target)
        for flags in ([], ["--check"]):
            rejected = CliRunner().invoke(app, ["skill", "install", "--brain", str(brain), *flags])
            assert rejected.exit_code == 1
            assert "is not a folder" in str(rejected.exception)


def test_skill_install_global_scope_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "user-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    preview = run("--global", "--check", code=1)
    assert (preview["ok"], preview["scope"], preview["links"]) == (False, "global", [])
    assert preview["destination"] == str((home / ".agents/skills").resolve())
    assert preview["skills"] == [{"name": SKILL, "status": "missing"}]
    assert not (home / ".agents").exists()

    installed = run("--global")
    assert (installed["ok"], installed["scope"], installed["links"]) == (True, "global", [])
    assert (home / ".agents/skills/bf/SKILL.md").is_file()
    assert not (home / ".claude").exists()
    assert run("--global", "--check")["ok"] is True

    # When ~/.claude exists, --global also links ~/.claude/skills/bf -> ../../.agents/skills/bf.
    (home / ".claude").mkdir()
    with_claude = run("--global")
    assert with_claude["links"] == [
        {"path": "~/.claude/skills/bf", "target": "../../.agents/skills/bf", "status": "linked"}
    ]
    assert (home / ".claude/skills/bf").readlink() == Path("../../.agents/skills/bf")
    assert run("--global", "--check")["ok"] is True

    # ~/.agents as a symlink fails closed; combining --brain and --global exits 2.
    (home / ".agents").rename(tmp_path / "moved-agents")
    (home / ".agents").symlink_to(tmp_path / "moved-agents")
    symlinked_agents = CliRunner().invoke(app, ["skill", "install", "--global"])
    assert symlinked_agents.exit_code == 1
    assert "is not a folder" in str(symlinked_agents.exception)

    monkeypatch.chdir(tmp_path)
    missing_brain = CliRunner().invoke(app, ["skill", "install"])
    assert missing_brain.exit_code == 1
    conflict = CliRunner().invoke(app, ["skill", "install", "--brain", str(tmp_path), "--global"])
    assert conflict.exit_code == 2


def test_skill_install_handles_nested_packaged_files_and_missing_retired_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bf.install as install_mod

    pkg = tmp_path / "pkg"
    skill_src = pkg / "skills/bf"
    (skill_src / "scripts").mkdir(parents=True)
    (skill_src / "references").mkdir(parents=True)
    (skill_src / "__pycache__").mkdir(parents=True)
    (skill_src / "SKILL.md").write_text("---\nname: bf\n---\n# v1\n")
    (skill_src / ".DS_Store").write_bytes(b"ignored")
    (skill_src / "__pycache__/cache.pyc").write_bytes(b"ignored")
    (skill_src / "scripts/run.sh").write_text("#!/bin/sh\nexit 0\n")
    (skill_src / "references/guide.md").write_text("# Guide\n")
    monkeypatch.setattr(install_mod, "files", lambda _pkg: pkg)

    brain = tmp_path / "brain"
    store = make_brain(brain)
    first = install(store)
    assert first["ok"] is True
    assert first["skills"] == [{"name": SKILL, "status": "installed"}]
    assert ((brain / "skills/bf/scripts/run.sh").stat().st_mode & 0o777) == 0o700
    assert ((brain / "skills/bf/SKILL.md").stat().st_mode & 0o777) == 0o600
    assert not (brain / "skills/bf/.DS_Store").exists()
    assert not (brain / "skills/bf/__pycache__").exists()

    # Delete a tracked nested file, drop both subdirs from packaged copy, and update:
    # retired missing file hits `_unedited` FileNotFoundError, and retired nested file syncs its parent dir.
    (brain / "skills/bf/references/guide.md").unlink()
    (skill_src / "scripts/run.sh").unlink()
    (skill_src / "scripts").rmdir()
    (skill_src / "references/guide.md").unlink()
    (skill_src / "references").rmdir()
    (skill_src / "SKILL.md").write_text("---\nname: bf\n---\n# v2\n")

    second = install(store)
    assert second["ok"] is True
    assert second["skills"] == [{"name": SKILL, "status": "updated"}]
    assert not (brain / "skills/bf/scripts/run.sh").exists()
    assert (brain / "skills/bf/SKILL.md").read_text().endswith("# v2\n")


def test_skill_install_rejects_malformed_manifests_non_dir_targets_and_ignores_skill_files(tmp_path: Path) -> None:
    from bf.models import Error

    with pytest.raises(Error, match="no brain selected"):
        install(None)

    brain = tmp_path / "brain"
    store = make_brain(brain)
    (brain / "skills/bf").mkdir(parents=True)
    for bad_manifest in (b"[]", b'{"version": "22.0.0"}'):
        (brain / "skills/bf" / MANIFEST).write_bytes(bad_manifest)
        assert install(store)["skills"] == [{"name": SKILL, "status": "unmanaged"}]
    (brain / "skills/bf" / MANIFEST).unlink()
    (brain / "skills/bf").rmdir()

    # Non-directory entries and hidden folders inside <brain>/skills are ignored cleanly.
    (brain / "skills/.hidden").mkdir(parents=True)
    (brain / "skills/__pycache__").mkdir(parents=True)
    (brain / "skills/README.md").write_text("notes\n")
    installed = install(store)
    assert installed["ok"] is True
    links = installed["links"]
    assert isinstance(links, list)
    assert [entry["path"] for entry in links] == [".agents/skills/bf", ".claude/skills/bf"]

    # Symlinked or regular-file host directories fail closed.
    agents_parent = brain / ".agents"
    (agents_parent / "skills/bf").unlink()
    (agents_parent / "skills").rmdir()
    agents_parent.rmdir()
    agents_parent.symlink_to(tmp_path)
    with pytest.raises(Error, match="is not a folder"):
        install(store)
    agents_parent.unlink()
    agents_parent.write_text("not a dir\n")
    with pytest.raises(Error, match="is not a folder"):
        install(store)
    agents_parent.unlink()

    # Non-directory <brain>/skills fails cleanly.
    bad_brain = make_brain(tmp_path / "bad-brain")
    (bad_brain.root / "skills").write_text("not a dir\n")
    with pytest.raises(Error, match="is not a folder"):
        install(bad_brain)

    # Global --global fails closed when ~/.agents/skills is a regular file or symlink.
    bad_home = tmp_path / "bad-home"
    (bad_home / ".agents").mkdir(parents=True)
    (bad_home / ".agents/skills").write_text("not a dir\n")
    with pytest.raises(Error, match="is not a folder"):
        install(global_=True, home=bad_home)
