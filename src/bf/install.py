"""Install the agent skills this package ships into a host's skills directory, never over a person's edits."""

from __future__ import annotations

import os
import re
from contextlib import suppress
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from bf import __version__
from bf.models import Error, decode, digest, encode
from bf.storage import Store, expand

# The installed files and their digests: a later install replaces only files that still match them.
MANIFEST = ".bf-skill.json"
SKILLS = ("bf-use", "bf-action", "bf-setup", "bf-maintain")
# States that leave a folder as it is: a person's edits, files this command did not install or a newer version's.
BLOCKED = frozenset({"modified", "unmanaged", "newer"})


def _packaged(skill: str) -> dict[str, bytes]:
    """A packaged skill's files by path relative to its folder; caches and hidden files are not part of it."""
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
    """An X.Y.Z version's numbers; anything else is empty, older than every release."""
    if isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        return tuple(int(part) for part in version.split("."))
    return ()


def _state(store: Store, skill: str, wanted: dict[str, str]) -> tuple[str, list[str], dict[str, str]]:
    """A folder's status, its edited files and the digests its manifest recorded."""
    folder = store.root / skill
    # An empty folder holds nothing to keep, such as one a first install left when a full disk failed its first write.
    if not folder.is_symlink() and (not folder.exists() or (folder.is_dir() and not any(folder.iterdir()))):
        return "missing", [], {}
    try:
        manifest = decode(store.read(f"{skill}/{MANIFEST}", 1 << 20))
    except FileNotFoundError, NotADirectoryError, Error:
        # No manifest, or a linked folder: someone else manages it.
        return "unmanaged", [], {}
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        return "unmanaged", [], {}
    recorded = {str(name): str(value) for name, value in manifest["files"].items()}
    # Two bf versions can share a skills directory, such as a global install and a brain's pinned runtime: never
    # replace a newer version's different copy. A newer version that recorded these same files changes nothing.
    if recorded != wanted and _release(manifest.get("version")) > _release(__version__):
        return "newer", [], recorded
    edited = []
    for name in sorted(recorded.keys() | wanted.keys()):
        try:
            found = digest(store.read(f"{skill}/{name}"))
        except FileNotFoundError:
            # A deleted file the packaged skill still needs leaves it incomplete; `--force` restores it.
            if name in recorded and name in wanted:
                edited.append(name)
            continue
        except Error:
            edited.append(name)
            continue
        # Bytes BF installed or ships, such as those an interrupted run already wrote, are not a person's edit;
        # a file someone added where a newer version ships one is theirs until `--force` replaces it.
        if found not in {recorded.get(name), wanted.get(name)}:
            edited.append(name)
    if edited:
        return "modified", edited, recorded
    return ("current" if recorded == wanted else "outdated"), [], recorded


def _unedited(store: Store, name: str, recorded: str) -> bool:
    """Whether a file still holds the bytes an install recorded."""
    try:
        return digest(store.read(name)) == recorded
    except FileNotFoundError, Error:
        return False


def _brain_root(destination: Path) -> Path:
    """The enclosing brain root holding a regular `bf.yaml` above `destination`."""
    resolved = destination.resolve()
    for candidate in resolved.parents:
        config = candidate / "bf.yaml"
        if not config.is_symlink() and config.is_file():
            return candidate
    raise Error("--link requires DIR to be inside a brain directory holding bf.yaml")


def _link_hosts(brain_root: Path, resolved: Path, *, check: bool) -> list[dict[str, str]]:
    """Create, update or check relative `bf-*` symlinks in `<brain>/.agents/skills` and `<brain>/.claude/skills`."""
    links: list[dict[str, str]] = []
    for host_name in (".agents", ".claude"):
        host_parent = brain_root / host_name
        if (host_parent.exists() or host_parent.is_symlink()) and (
            host_parent.is_symlink() or not host_parent.is_dir()
        ):
            raise Error(f"{host_parent} is not a folder")
        host_dir = host_parent / "skills"
        try:
            same_target = host_dir.resolve() == resolved
        except OSError, RuntimeError:
            same_target = False
        if same_target:
            # `destination` itself or a whole-folder host symlink (e.g. `.agents/skills -> ../skills`) already exposes it.
            continue
        if (host_dir.exists() or host_dir.is_symlink()) and (host_dir.is_symlink() or not host_dir.is_dir()):
            raise Error(f"{host_dir} is not a folder")
        if not check:
            host_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        anchor = host_dir.resolve() if host_dir.exists() else host_dir
        for skill in SKILLS:
            entry = host_dir / skill
            rel_entry = f"{host_name}/skills/{skill}"
            target = Path(os.path.relpath(resolved / skill, anchor))
            if entry.is_symlink():
                if entry.readlink() == target:
                    status = "current"
                elif check:
                    status = "outdated"
                else:
                    tmp = host_dir / f".{skill}.{os.urandom(8).hex()}.tmp"
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
            for child in sorted(host_dir.iterdir(), key=lambda item: item.name):
                if child.is_symlink() and child.name.startswith("bf-") and child.name not in SKILLS:
                    if check:
                        links.append({"path": f"{host_name}/skills/{child.name}", "status": "stale"})
                    else:
                        child.unlink()
                        links.append({"path": f"{host_name}/skills/{child.name}", "status": "pruned"})
    return links


def skills(
    destination: Path,
    *,
    check: bool = False,
    force: bool = False,
    link: bool = False,
) -> dict[str, object]:
    """Install or update each packaged skill in `destination`, such as ~/.agents/skills.

    A folder this command installed is updated while its files still match its manifest; edited or foreign folders,
    and a different copy a newer bf installed, stay as they are unless `force` replaces them. `check` reports the
    same states without writing. Files a newer version no longer ships are removed only when unedited. With `link`,
    relative `bf-*` symlinks in `<brain>/.agents/skills` and `<brain>/.claude/skills` point to `destination`.
    """
    root = expand(destination)
    if (root.exists() or root.is_symlink()) and not root.is_dir():
        raise Error(f"{root} is not a folder")
    brain_root = _brain_root(root) if link else None
    if not check:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = root.resolve()
    store = Store(resolved) if root.is_dir() else None
    if force and not check and (linked := [skill for skill in SKILLS if (root / skill).is_symlink()]):
        # Forcing never writes through a link: replacing it is the person's decision.
        raise Error(f"{linked[0]} is a link; remove it before installing a copy")
    results = []
    for skill in SKILLS:
        packaged = _packaged(skill)
        wanted = {name: digest(data) for name, data in packaged.items()}
        status, edited, recorded = _state(store, skill, wanted) if store else ("missing", [], {})
        entry: dict[str, object] = {"name": skill, "status": status, **({"edited": edited} if edited else {})}
        if store and not check and (status in {"missing", "outdated"} or (force and status in BLOCKED)):
            if status == "missing":
                # A manifest first: an interrupted first install then reads as outdated, and the next run finishes it.
                store.write(f"{skill}/{MANIFEST}", encode({"version": __version__, "files": {}}))
            touched_dirs: set[str] = set()
            for name, data in packaged.items():
                store.write(f"{skill}/{name}", data, durable=False)
                if "/" in name:
                    touched_dirs.add(f"{skill}/{name.rsplit('/', 1)[0]}")
                # Helpers run directly: `scripts/NAME.py` is executable, like the packaged copy.
                (store.root / skill / name).chmod(0o700 if name.startswith("scripts/") else 0o600)
            for name in sorted(set(recorded) - set(packaged)):
                # Even `--force` keeps an edited file this version no longer ships: BF does not ship it.
                if _unedited(store, f"{skill}/{name}", recorded[name]):
                    store.delete(f"{skill}/{name}", durable=False)
                    if "/" in name:
                        touched_dirs.add(f"{skill}/{name.rsplit('/', 1)[0]}")
            for folder in sorted(touched_dirs):
                with suppress(FileNotFoundError):
                    store.sync(folder)
            store.write(f"{skill}/{MANIFEST}", encode({"version": __version__, "files": wanted}))
            entry["status"] = "installed" if status == "missing" else "updated"
        results.append(entry)
    links = _link_hosts(brain_root, resolved, check=check) if brain_root is not None else []
    blocked = any(entry["status"] in BLOCKED for entry in results) or any(
        item["status"] == "unmanaged" for item in links
    )
    behind = check and (
        any(entry["status"] != "current" for entry in results) or any(item["status"] != "current" for item in links)
    )
    return {
        "ok": not blocked and not behind,
        "destination": str(root.absolute()),
        "version": __version__,
        "skills": results,
        **({"links": links} if link else {}),
    }
