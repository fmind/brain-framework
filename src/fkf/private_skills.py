"""Conflict-safe installation of base-owned private global Agent Skills."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fkf.errors import InvalidUsageError, OperationalError
from fkf.process import Cancellation, check_cancel

PRIVATE_SKILLS_DIR: Final = "skills"
_MAX_SKILL_NAME: Final = 64
_SKILL_NAME_PATTERN: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class PrivateSkillsRequest:
    """One explicit user-scope installation or inspection."""

    home: Path | str = ""
    dry_run: bool = False
    check: bool = False


@dataclass(frozen=True, slots=True)
class PrivateSkillChange:
    """One missing user-scope link."""

    name: str
    action: str
    source: Path
    target: Path


@dataclass(frozen=True, slots=True)
class PrivateSkillsReport:
    """Complete result for one base's private skill catalog."""

    base: Path
    mode: str
    catalog: Path
    complete: bool
    discovered: int
    changes: tuple[PrivateSkillChange, ...]


def _absolute(value: Path | str, label: str) -> Path:
    rendered = os.fspath(value)
    if not rendered or not Path(rendered).is_absolute():
        raise InvalidUsageError(f"{label} must be an absolute path")
    if any(character in rendered for character in "\x00\r\n"):
        raise InvalidUsageError(f"{label} may not contain NUL or newlines")
    return Path(os.path.normpath(rendered))


def _private_skills(base: Path, cancel: Cancellation | None) -> tuple[tuple[str, Path], ...]:
    catalog = base / PRIVATE_SKILLS_DIR
    try:
        catalog_info = catalog.lstat()
    except OSError as error:
        raise InvalidUsageError(f"inspect private skill catalog {catalog}: {error}; run `fkf init {base}`") from error
    if stat.S_ISLNK(catalog_info.st_mode) or not stat.S_ISDIR(catalog_info.st_mode):
        raise InvalidUsageError(f"private skill catalog {catalog} must be a real directory")

    skills: list[tuple[str, Path]] = []
    try:
        entries = sorted(catalog.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise InvalidUsageError(f"read private skill catalog {catalog}: {error}") from error
    for entry in entries:
        check_cancel(cancel)
        try:
            info = entry.lstat()
        except OSError as error:
            raise InvalidUsageError(f"inspect private skill entry {entry}: {error}") from error
        if stat.S_ISREG(info.st_mode):
            continue
        if (
            len(entry.name) > _MAX_SKILL_NAME
            or _SKILL_NAME_PATTERN.fullmatch(entry.name) is None
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise InvalidUsageError(
                f"private skill {entry.name!r} must be a real directory with a lowercase hyphenated name "
                f"of at most {_MAX_SKILL_NAME} characters"
            )
        skill_file = entry / "SKILL.md"
        try:
            skill_info = skill_file.lstat()
        except OSError as error:
            raise InvalidUsageError(f"private skill {entry.name!r} requires a regular SKILL.md: {error}") from error
        if stat.S_ISLNK(skill_info.st_mode) or not stat.S_ISREG(skill_info.st_mode):
            raise InvalidUsageError(f"private skill {entry.name!r} requires a regular SKILL.md")
        skills.append((entry.name, entry))
    return tuple(skills)


def _preflight_catalog(home: Path, needed: bool) -> Path:
    try:
        home_info = home.lstat()
    except OSError as error:
        raise InvalidUsageError(f"inspect private skill home {home}: {error}") from error
    if stat.S_ISLNK(home_info.st_mode) or not stat.S_ISDIR(home_info.st_mode):
        raise InvalidUsageError(f"private skill home {home} must be a real directory")
    target = home / ".agents" / "skills"
    agents = target.parent
    try:
        agents_info = agents.lstat()
    except FileNotFoundError:
        agents_info = None
    except OSError as error:
        raise InvalidUsageError(f"inspect private skill parent {agents}: {error}") from error
    if agents_info is not None and (stat.S_ISLNK(agents_info.st_mode) or not stat.S_ISDIR(agents_info.st_mode)):
        raise InvalidUsageError(f"private skill parent {agents} must be a real directory")
    try:
        target_info = target.lstat()
    except FileNotFoundError:
        if not needed:
            return target
    except OSError as error:
        raise InvalidUsageError(f"inspect global skill catalog {target}: {error}") from error
    else:
        if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISDIR(target_info.st_mode):
            raise InvalidUsageError(
                f"global skill catalog {target} must be a real directory; "
                "replace the catalog symlink with a directory of individual skill links before installing"
            )
    return target


def _changes(skills: tuple[tuple[str, Path], ...], catalog: Path) -> tuple[PrivateSkillChange, ...]:
    changes: list[PrivateSkillChange] = []
    for name, source in skills:
        target = catalog / name
        try:
            info = target.lstat()
        except FileNotFoundError:
            changes.append(PrivateSkillChange(name, "link", source, target))
            continue
        except OSError as error:
            raise InvalidUsageError(f"inspect global private skill {target}: {error}") from error
        if stat.S_ISLNK(info.st_mode):
            try:
                if target.resolve(strict=True) == source.resolve(strict=True):
                    continue
            except OSError:
                pass
        raise InvalidUsageError(
            f"private skill {name!r} target {target} already exists and is not the link owned by {source}"
        )
    return tuple(changes)


def install_private_skills(
    base_root: Path | str,
    request: PrivateSkillsRequest | None = None,
    *,
    cancel: Cancellation | None = None,
) -> PrivateSkillsReport:
    """Preflight and optionally link one base's private skills into the user catalog."""
    request = request or PrivateSkillsRequest()
    if request.check and request.dry_run:
        raise InvalidUsageError("private skill --check and --dry-run cannot be combined")
    check_cancel(cancel)
    base = _absolute(base_root, "private skill base path")
    home = _absolute(request.home or Path.home(), "private skill home path")
    skills = _private_skills(base, cancel)
    catalog = _preflight_catalog(home, bool(skills))
    changes = _changes(skills, catalog)
    mode = "check" if request.check else "dry-run" if request.dry_run else "install"
    complete = not changes
    if changes and not request.check and not request.dry_run:
        check_cancel(cancel)
        if _changes(skills, catalog) != changes:
            raise OperationalError("global private skill catalog changed after preflight")
        try:
            catalog.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            catalog.mkdir(mode=0o700, exist_ok=True)
            for change in changes:
                check_cancel(cancel)
                change.target.symlink_to(change.source, target_is_directory=True)
        except OSError as error:
            raise OperationalError(f"install private skills from {base}: {error}") from error
        complete = True
    return PrivateSkillsReport(base, mode, catalog, complete, len(skills), changes)


__all__ = [
    "PRIVATE_SKILLS_DIR",
    "PrivateSkillChange",
    "PrivateSkillsReport",
    "PrivateSkillsRequest",
    "install_private_skills",
]
