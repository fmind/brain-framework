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

MANIFEST, SKILL, SKILLS = ".bf-skill.json", "bf", ("bf",)
BLOCKED = frozenset({"modified", "unmanaged", "newer"})
_HOSTS, _LEGACY = (".agents", ".claude"), frozenset({"bf-use", "bf-action", "bf-setup", "bf-maintain"})


def _packaged(skill: str = SKILL) -> dict[str, bytes]:
    result: dict[str, bytes] = {}

    def walk(node: Traversable, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda i: i.name):
            if not child.name.startswith(".") and child.name != "__pycache__":
                walk(child, f"{prefix}{child.name}/") if child.is_dir() else result.update(
                    {prefix + child.name: child.read_bytes()}
                )

    walk(files("bf") / "skills" / skill, "")
    return result


def _release(version: object) -> tuple[int, ...]:
    return (
        tuple(map(int, version.split(".")))
        if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version)
        else ()
    )


def _require_dir(path: Path, *, allow_symlink: bool = False) -> None:
    if (path.is_symlink() and not allow_symlink) or ((path.exists() or path.is_symlink()) and not path.is_dir()):
        raise Error(f"{path} is not a folder")


def _state(store: Store, skill: str, wanted: dict[str, str], prefix: str = "") -> tuple[str, list[str], dict[str, str]]:
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
    recorded = {str(k): str(v) for k, v in manifest["files"].items()}
    if recorded != wanted and _release(manifest.get("version")) > _release(__version__):
        return "newer", [], recorded
    edited: list[str] = []
    for name in sorted(recorded.keys() | wanted.keys()):
        try:
            found = digest(store.read(f"{base}/{name}"))
        except FileNotFoundError:
            if name in recorded and name in wanted:
                edited.append(name)
        except Error:
            edited.append(name)
        else:
            if found not in {recorded.get(name), wanted.get(name)}:
                edited.append(name)
    return (
        ("modified", edited, recorded) if edited else (("current" if recorded == wanted else "outdated"), [], recorded)
    )


def _install_skill(store: Store, skill: str, *, prefix: str = "", check: bool, force: bool) -> dict[str, object]:
    base = f"{prefix}{skill}"
    if force and not check and (store.root / base).is_symlink():
        raise Error(f"{base} is a link; remove it before installing a copy")
    packaged = _packaged(skill)
    wanted = {name: digest(data) for name, data in packaged.items()}
    can_read = store.root.is_dir() and (not prefix or (store.root / prefix.rstrip("/")).is_dir())
    status, edited, recorded = _state(store, skill, wanted, prefix) if can_read else ("missing", [], {})
    entry: dict[str, object] = {"name": skill, "status": status, **({"edited": edited} if edited else {})}
    if check or not (status in {"missing", "outdated"} or (force and status in BLOCKED)):
        return entry
    if status == "missing":
        store.write(f"{base}/{MANIFEST}", encode({"version": __version__, "files": {}}))
    touched: set[str] = set()
    for name, data in packaged.items():
        store.write(f"{base}/{name}", data, durable=False)
        if "/" in name:
            touched.add(f"{base}/{name.rsplit('/', 1)[0]}")
        with store.parent(f"{base}/{name}") as (parent, leaf):
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            try:
                os.fchmod(fd, 0o700 if name.startswith("scripts/") else 0o600)
            finally:
                os.close(fd)
    for name in sorted(set(recorded) - set(packaged)):
        with suppress(FileNotFoundError, Error):
            if digest(store.read(f"{base}/{name}")) == recorded[name]:
                store.delete(f"{base}/{name}", durable=False)
                if "/" in name:
                    touched.add(f"{base}/{name.rsplit('/', 1)[0]}")
    for folder in sorted(touched):
        with suppress(FileNotFoundError):
            store.sync(folder)
    store.write(f"{base}/{MANIFEST}", encode({"version": __version__, "files": wanted}))
    entry["status"] = "installed" if status == "missing" else "updated"
    return entry


def _discover_brain_skills(store: Store) -> list[str]:
    _require_dir(store.root / "skills")
    if not (store.root / "skills").exists():
        return [SKILL]
    discovered = {SKILL}
    for name in store.entries("skills"):
        if name.startswith(".") or name == "__pycache__":
            continue
        info = store.mode(f"skills/{name}")
        if stat.S_ISLNK(info):
            if name == SKILL:
                discovered.discard(SKILL)
                continue
            raise Error(f"skills/{name}: symlinks are not followed; replace the link with a directory")
        if stat.S_ISDIR(info):
            discovered.add(name)
    return sorted(discovered)


def _relink(host_dir: Path, entry: Path, name: str, target: Path) -> str:
    tmp = host_dir / f".{name}.{os.urandom(8).hex()}.tmp"
    try:
        tmp.symlink_to(target)
        tmp.replace(entry)
    finally:
        tmp.unlink(missing_ok=True)
    return "updated"


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
    _require_dir(host_parent, allow_symlink=allow_parent_symlink)
    host_dir, resolved = host_parent / "skills", skills_dir.resolve()
    if host_dir.exists() or host_dir.is_symlink():
        with suppress(OSError, RuntimeError):
            if host_dir.resolve() == resolved:
                return []
    _require_dir(host_dir)
    if not check:
        host_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    anchor = host_dir.resolve() if host_dir.exists() else host_dir.absolute()
    links: list[dict[str, str]] = []
    for name in names:
        entry, target = host_dir / name, Path(os.path.relpath(resolved / name, anchor))
        if entry.is_symlink():
            status = (
                "current"
                if entry.readlink() == target
                else ("outdated" if check else _relink(host_dir, entry, name, target))
            )
        elif entry.exists():
            status = "unmanaged"
        elif check:
            status = "missing"
        else:
            entry.symlink_to(target)
            status = "linked"
        links.append({"path": f"{host_label}/skills/{name}", "target": target.as_posix(), "status": status})
    if host_dir.is_dir() and not host_dir.is_symlink():
        wanted = set(names)
        for child in sorted(host_dir.iterdir(), key=lambda i: i.name):
            if child.is_symlink() and child.name not in wanted:
                with suppress(OSError, ValueError):
                    raw = child.readlink()
                    in_skills = Path(os.path.normpath(raw if raw.is_absolute() else anchor / raw)).parent == resolved
                    if child.name in _LEGACY or (prune_owned and in_skills):
                        if not check:
                            child.unlink()
                        links.append(
                            {"path": f"{host_label}/skills/{child.name}", "status": "stale" if check else "pruned"}
                        )
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
        _require_dir(user_home / ".agents")
        destination = user_home / ".agents" / "skills"
        _require_dir(destination)
        if not check:
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        results = (
            [_install_skill(Store(destination.resolve()), SKILL, check=check, force=force)]
            if destination.is_dir()
            else [{"name": SKILL, "status": "missing"}]
        )
        links: list[dict[str, str]] = []
        if (claude := user_home / ".claude").exists() or claude.is_symlink():
            target = (user_home.resolve() if user_home.exists() else user_home) / ".agents" / "skills"
            links = _sync_host_dir(
                claude, "~/.claude", target, (SKILL,), check=check, allow_parent_symlink=True, prune_owned=False
            )
    else:
        if store is None:
            raise Error("no brain selected; pass --brain, run inside a brain, or pass --global")
        destination = store.root / "skills"
        _require_dir(destination)
        results = [_install_skill(store, SKILL, prefix="skills/", check=check, force=force)]
        names = _discover_brain_skills(store)
        links = [i for h in _HOSTS for i in _sync_host_dir(store.root / h, h, destination, names, check=check)]

    blocked = any(e["status"] in BLOCKED for e in results) or any(i["status"] == "unmanaged" for i in links)
    behind = check and (any(e["status"] != "current" for e in results) or any(i["status"] != "current" for i in links))
    return {
        "ok": not blocked and not behind,
        "scope": "global" if global_ else "brain",
        "destination": str(destination.absolute()),
        "version": __version__,
        "skills": results,
        "links": links,
    }
