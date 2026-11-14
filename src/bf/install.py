"""Install the packaged `bf` skill cleanly into a brain or user host directory."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Sequence
from contextlib import suppress
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from bf import __version__
from bf.models import Error, decode, digest, encode
from bf.storage import Store

# Installed files and their SHA-256 digests; a later install replaces only files still matching them.
MANIFEST = ".bf-skill.json"
SKILL = "bf"
SKILLS = (SKILL,)
BLOCKED = frozenset({"modified", "unmanaged", "newer"})
_HOSTS = (".agents", ".claude")
_LEGACY_SKILLS = frozenset({"bf-use", "bf-action", "bf-setup", "bf-maintain"})


def _packaged(skill: str = SKILL) -> dict[str, bytes]:
    """Return packaged skill files keyed by path relative to the skill directory."""
    result: dict[str, bytes] = {}

    def walk(node: Traversable, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda item: item.name):
            if child.name.startswith(".") or child.name == "__pycache__":
                continue
            if child.is_dir():
                walk(child, f"{prefix}{child.name}/")
            else:
                result[prefix + child.name] = child.read_bytes()

    walk(files("bf") / "skills" / skill, "")
    return result


def _release(version: object) -> tuple[int, ...]:
    """Parse an X.Y.Z release string, returning empty tuple for unparsable values."""
    if isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        return tuple(int(part) for part in version.split("."))
    return ()


def _state(
    store: Store, skill: str, wanted: dict[str, str], *, prefix: str = ""
) -> tuple[str, list[str], dict[str, str]]:
    """Inspect a skill folder's install status, edited relative paths, and recorded digests."""
    base = f"{prefix}{skill}"
    folder = store.root / base
    if not folder.is_symlink() and (not folder.exists() or (folder.is_dir() and not any(folder.iterdir()))):
        return "missing", [], {}
    try:
        manifest = decode(store.read(f"{base}/{MANIFEST}", 1 << 20))
    except FileNotFoundError, NotADirectoryError, Error:
        return "unmanaged", [], {}
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        return "unmanaged", [], {}
    recorded = {str(name): str(value) for name, value in manifest["files"].items()}
    if recorded != wanted and _release(manifest.get("version")) > _release(__version__):
        return "newer", [], recorded
    edited = []
    for name in sorted(recorded.keys() | wanted.keys()):
        try:
            found = digest(store.read(f"{base}/{name}"))
        except FileNotFoundError:
            if name in recorded and name in wanted:
                edited.append(name)
            continue
        except Error:
            edited.append(name)
            continue
        if found not in {recorded.get(name), wanted.get(name)}:
            edited.append(name)
    if edited:
        return "modified", edited, recorded
    return ("current" if recorded == wanted else "outdated"), [], recorded


def _unedited(store: Store, name: str, recorded: str) -> bool:
    """Return whether `name` still contains the exact bytes recorded at install time."""
    try:
        return digest(store.read(name)) == recorded
    except FileNotFoundError, Error:
        return False


def _install_skill(
    store: Store,
    skill: str,
    *,
    prefix: str = "",
    check: bool,
    force: bool,
) -> dict[str, object]:
    """Install or check one packaged skill inside `store` under `prefix`."""
    base = f"{prefix}{skill}"
    target = store.root / base
    if force and not check and target.is_symlink():
        raise Error(f"{base} is a link; remove it before installing a copy")
    packaged = _packaged(skill)
    wanted = {name: digest(data) for name, data in packaged.items()}
    can_read = store.root.is_dir() and (not prefix or (store.root / prefix.rstrip("/")).is_dir())
    status, edited, recorded = _state(store, skill, wanted, prefix=prefix) if can_read else ("missing", [], {})
    entry: dict[str, object] = {"name": skill, "status": status, **({"edited": edited} if edited else {})}
    if check or not (status in {"missing", "outdated"} or (force and status in BLOCKED)):
        return entry
    if status == "missing":
        # Write manifest first so an interrupted initial install later resumes as outdated.
        store.write(f"{base}/{MANIFEST}", encode({"version": __version__, "files": {}}))
    touched_dirs: set[str] = set()
    for name, data in packaged.items():
        store.write(f"{base}/{name}", data, durable=False)
        if "/" in name:
            touched_dirs.add(f"{base}/{name.rsplit('/', 1)[0]}")
        mode = 0o700 if name.startswith("scripts/") else 0o600
        with store.parent(f"{base}/{name}") as (parent, leaf):
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            try:
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
    for name in sorted(set(recorded) - set(packaged)):
        if _unedited(store, f"{base}/{name}", recorded[name]):
            store.delete(f"{base}/{name}", durable=False)
            if "/" in name:
                touched_dirs.add(f"{base}/{name.rsplit('/', 1)[0]}")
    for folder in sorted(touched_dirs):
        with suppress(FileNotFoundError):
            store.sync(folder)
    store.write(f"{base}/{MANIFEST}", encode({"version": __version__, "files": wanted}))
    entry["status"] = "installed" if status == "missing" else "updated"
    return entry


def _discover_brain_skills(store: Store) -> list[str]:
    """List every non-hidden regular skill directory in `<brain>/skills`, always including `bf`."""
    skills_path = store.root / "skills"
    if skills_path.is_symlink() or (skills_path.exists() and not skills_path.is_dir()):
        raise Error("skills is not a regular folder; symlinks are not followed")
    discovered = {SKILL}
    if not skills_path.exists():
        return sorted(discovered)
    for name in store.entries("skills"):
        if name.startswith(".") or name == "__pycache__":
            continue
        info = store.mode(f"skills/{name}")
        if stat.S_ISLNK(info):
            if name == SKILL:
                # Let _install_skill report a symlinked `bf` folder as unmanaged (and reject --force cleanly).
                discovered.discard(SKILL)
                continue
            raise Error(f"skills/{name}: symlinks are not followed; replace the link with a directory")
        if stat.S_ISDIR(info):
            discovered.add(name)
    return sorted(discovered)


def _points_into_skills(entry: Path, host_anchor: Path, resolved_skills: Path) -> bool:
    """Return whether symlink `entry` targets an entry directly inside `resolved_skills`."""
    try:
        raw_target = entry.readlink()
    except OSError:
        return False
    target_path = raw_target if raw_target.is_absolute() else host_anchor / raw_target
    try:
        resolved_target = Path(os.path.normpath(target_path))
    except OSError, ValueError:
        return False
    return resolved_target.parent == resolved_skills


def _sync_host_dir(
    host_parent: Path,
    host_label: str,
    skills_dir: Path,
    names: Sequence[str],
    *,
    check: bool,
    allow_parent_symlink: bool = False,
    prune_owned: bool = True,
) -> list[dict[str, str]]:
    """Create, update, or check relative symlinks under `<host_parent>/skills` pointing into `skills_dir`."""
    if not allow_parent_symlink and host_parent.is_symlink():
        raise Error(f"{host_parent} is not a folder")
    if (host_parent.exists() or host_parent.is_symlink()) and not host_parent.is_dir():
        raise Error(f"{host_parent} is not a folder")
    host_dir = host_parent / "skills"
    resolved_skills = skills_dir.resolve()
    if host_dir.exists() or host_dir.is_symlink():
        with suppress(OSError, RuntimeError):
            if host_dir.resolve() == resolved_skills:
                return []
    if (host_dir.exists() or host_dir.is_symlink()) and (host_dir.is_symlink() or not host_dir.is_dir()):
        raise Error(f"{host_dir} is not a folder")
    if not check:
        host_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    anchor = host_dir.resolve() if host_dir.exists() else host_dir.absolute()
    links: list[dict[str, str]] = []
    for name in names:
        entry = host_dir / name
        rel_entry = f"{host_label}/skills/{name}"
        target = Path(os.path.relpath(resolved_skills / name, anchor))
        if entry.is_symlink():
            if entry.readlink() == target:
                status = "current"
            elif check:
                status = "outdated"
            else:
                tmp = host_dir / f".{name}.{os.urandom(8).hex()}.tmp"
                try:
                    tmp.symlink_to(target)
                    tmp.replace(entry)
                finally:
                    tmp.unlink(missing_ok=True)
                status = "updated"
        elif entry.exists():
            status = "unmanaged"
        elif check:
            status = "missing"
        else:
            entry.symlink_to(target)
            status = "linked"
        links.append({"path": rel_entry, "target": target.as_posix(), "status": status})
    if host_dir.is_dir() and not host_dir.is_symlink():
        wanted = set(names)
        for child in sorted(host_dir.iterdir(), key=lambda item: item.name):
            if not child.is_symlink() or child.name in wanted:
                continue
            owned = child.name in _LEGACY_SKILLS or (
                prune_owned and _points_into_skills(child, anchor, resolved_skills)
            )
            if not owned:
                continue
            if check:
                links.append({"path": f"{host_label}/skills/{child.name}", "status": "stale"})
            else:
                child.unlink()
                links.append({"path": f"{host_label}/skills/{child.name}", "status": "pruned"})
    return links


def install(
    store: Store | None = None,
    *,
    global_: bool = False,
    check: bool = False,
    force: bool = False,
    home: Path | None = None,
) -> dict[str, object]:
    """Install or verify the packaged `bf` skill in a brain or user global skill directories."""
    if global_:
        user_home = (home if home is not None else Path.home()).expanduser()
        agents_root = user_home / ".agents"
        if agents_root.is_symlink() or (agents_root.exists() and not agents_root.is_dir()):
            raise Error(f"{agents_root} is not a folder")
        destination = agents_root / "skills"
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise Error(f"{destination} is not a folder")
        if not check:
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.is_dir():
            results = [_install_skill(Store(destination.resolve()), SKILL, check=check, force=force)]
        else:
            results = [{"name": SKILL, "status": "missing"}]
        links: list[dict[str, str]] = []
        claude_root = user_home / ".claude"
        if claude_root.exists() or claude_root.is_symlink():
            agents_target = (user_home.resolve() if user_home.exists() else user_home) / ".agents" / "skills"
            links.extend(
                _sync_host_dir(
                    claude_root,
                    "~/.claude",
                    agents_target,
                    (SKILL,),
                    check=check,
                    allow_parent_symlink=True,
                    prune_owned=False,
                )
            )
        target_label = str(destination.absolute())
    else:
        if store is None:
            raise Error("no brain selected; pass --brain, run inside a brain, or pass --global")
        destination = store.root / "skills"
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise Error(f"{destination} is not a folder")
        results = [_install_skill(store, SKILL, prefix="skills/", check=check, force=force)]
        skill_names = _discover_brain_skills(store)
        links = []
        for host_name in _HOSTS:
            links.extend(_sync_host_dir(store.root / host_name, host_name, destination, skill_names, check=check))
        target_label = str(destination.absolute())

    blocked = any(entry["status"] in BLOCKED for entry in results) or any(
        item["status"] == "unmanaged" for item in links
    )
    behind = check and (
        any(entry["status"] != "current" for entry in results) or any(item["status"] != "current" for item in links)
    )
    return {
        "ok": not blocked and not behind,
        "scope": "global" if global_ else "brain",
        "destination": target_label,
        "version": __version__,
        "skills": results,
        "links": links,
    }
