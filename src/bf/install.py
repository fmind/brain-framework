"""Install the agent skills this package ships into a host's skills directory, never over a person's edits."""

from __future__ import annotations

import re
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from bf import __version__
from bf.models import Error, decode, digest, encode
from bf.storage import Store, expand

# The installed files and their digests: a later install replaces only files that still match them.
MANIFEST = ".bf-skill.json"
SKILLS = ("bf-use", "bf-setup", "bf-maintain")
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


def skills(destination: Path, *, check: bool = False, force: bool = False) -> dict[str, object]:
    """Install or update each packaged skill in `destination`, such as ~/.agents/skills.

    A folder this command installed is updated while its files still match its manifest; edited or foreign folders,
    and a different copy a newer bf installed, stay as they are unless `force` replaces them. `check` reports the
    same states without writing. Files a newer version no longer ships are removed only when unedited.
    """
    root = expand(destination)
    if not check:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(root.resolve()) if root.is_dir() else None
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
            for name, data in packaged.items():
                store.write(f"{skill}/{name}", data)
                # Helpers run directly: `scripts/NAME.py` is executable, like the packaged copy.
                (store.root / skill / name).chmod(0o700 if name.startswith("scripts/") else 0o600)
            for name in sorted(set(recorded) - set(packaged)):
                # Even `--force` keeps an edited file this version no longer ships: BF does not ship it.
                if _unedited(store, f"{skill}/{name}", recorded[name]):
                    store.delete(f"{skill}/{name}")
            store.write(f"{skill}/{MANIFEST}", encode({"version": __version__, "files": wanted}))
            entry["status"] = "installed" if status == "missing" else "updated"
        results.append(entry)
    blocked = any(entry["status"] in BLOCKED for entry in results)
    behind = check and any(entry["status"] != "current" for entry in results)
    return {"ok": not blocked and not behind, "destination": str(root), "version": __version__, "skills": results}
