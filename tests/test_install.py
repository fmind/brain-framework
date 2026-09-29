"""`bf skills` installs the packaged skills and updates them only while a person has not edited them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from bf import __version__
from bf.cli import app
from bf.install import MANIFEST, SKILLS

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
