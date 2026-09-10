"""Private user-scope Agent Skill installation contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from fkf.errors import InvalidUsageError
from fkf.private_skills import PrivateSkillsRequest, install_private_skills


def _skill(base: Path, name: str) -> Path:
    directory = base / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return directory


def test_private_skills_from_multiple_bases_coexist_and_check_current(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    first = tmp_path / "first"
    second = tmp_path / "second"
    alpha = _skill(first, "alpha")
    beta = _skill(second, "beta")

    first_report = install_private_skills(first, PrivateSkillsRequest(home=home))
    second_report = install_private_skills(second, PrivateSkillsRequest(home=home))

    catalog = home / ".agents" / "skills"
    assert first_report.complete is True
    assert second_report.complete is True
    assert (catalog / "alpha").resolve(strict=True) == alpha.resolve(strict=True)
    assert (catalog / "beta").resolve(strict=True) == beta.resolve(strict=True)
    checked = install_private_skills(first, PrivateSkillsRequest(home=home, check=True))
    assert checked.complete is True
    assert checked.changes == ()


def test_private_skills_reject_shared_catalog_link(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agents = home / ".agents"
    agents.mkdir(parents=True, exist_ok=True)
    shared = tmp_path / "dot-skills"
    shared.mkdir()
    (agents / "skills").symlink_to(shared, target_is_directory=True)
    base = tmp_path / "base"
    _skill(base, "alpha")

    with pytest.raises(InvalidUsageError, match="must be a real directory"):
        install_private_skills(base, PrivateSkillsRequest(home=home))

    assert not (shared / "alpha").exists()


def test_private_skills_preflight_all_collisions_before_writing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    catalog = home / ".agents" / "skills"
    catalog.mkdir(parents=True)
    base = tmp_path / "base"
    _skill(base, "alpha")
    _skill(base, "private-taken")
    (catalog / "private-taken").mkdir()

    with pytest.raises(InvalidUsageError, match=r"private-taken.*already exists"):
        install_private_skills(base, PrivateSkillsRequest(home=home))

    assert not (catalog / "alpha").exists()


def test_private_skills_dry_run_and_check_do_not_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    base = tmp_path / "base"
    _skill(base, "alpha")

    preview = install_private_skills(base, PrivateSkillsRequest(home=home, dry_run=True))
    checked = install_private_skills(base, PrivateSkillsRequest(home=home, check=True))

    assert preview.mode == "dry-run"
    assert preview.complete is False
    assert [change.name for change in preview.changes] == ["alpha"]
    assert checked.mode == "check"
    assert checked.complete is False
    assert not (home / ".agents").exists()


def test_private_skills_reject_invalid_sources_and_options(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    base = tmp_path / "base"
    _skill(base, "_invalid")

    with pytest.raises(InvalidUsageError, match="lowercase hyphenated name"):
        install_private_skills(base, PrivateSkillsRequest(home=home))
    with pytest.raises(InvalidUsageError, match="cannot be combined"):
        install_private_skills(base, PrivateSkillsRequest(home=home, check=True, dry_run=True))


def test_private_skills_require_regular_skill_packages(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside\n", encoding="utf-8")
    (base / "skills").mkdir(parents=True)
    (base / "skills" / "private-linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InvalidUsageError, match=r"private-linked.*must be a real directory"):
        install_private_skills(base, PrivateSkillsRequest(home=home))
