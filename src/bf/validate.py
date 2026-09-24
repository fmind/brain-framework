"""Offline checks of the whole brain: notes, OKF concept structure, links and record partitions."""

from __future__ import annotations

import os
import re
import stat
from datetime import date

from bf import records
from bf.config import load
from bf.markdown import Note, authored, broken, note, scheme, split_ref, validate_concept
from bf.models import AUTHORED, Error
from bf.storage import Store, relative

_ACTION = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[a-z0-9]+(?:-[a-z0-9]+)*")


def _actions(files: list[str]) -> list[str]:
    """Each folder below actions/ is one dated action with its ACTION.md; loose files are allowed."""
    problems = []
    for folder in sorted({name.split("/")[1] for name in files if name.count("/") >= 2}):
        try:
            if not _ACTION.fullmatch(folder):
                raise ValueError
            date.fromisoformat(folder[:10])
        except ValueError:
            problems.append(f"actions/{folder}: name action folders YYYY-MM-DD_slug (lowercase slug, hyphens)")
            continue
        if f"actions/{folder}/ACTION.md" not in files:
            problems.append(f"actions/{folder}: missing ACTION.md")
    return problems


def validate(store: Store) -> dict[str, object]:
    """Report every problem instead of stopping at the first one."""
    with records.reading(store):
        return _validate(store)


def _validate(store: Store) -> dict[str, object]:
    config = load(store)
    problems: list[str] = []
    ids: dict[str, set[str]] = {}
    aliases: dict[str, set[str]] = {}
    count = 0
    for name in records.partitions(store):
        try:
            source, key = records.source_of(name), name.rsplit("/", 1)[1].removesuffix(".jsonl")
            seen = ids.setdefault(source, set())
            for record in records.load(store, name):
                count += 1
                if record.id in seen:
                    problems.append(f"{name}: duplicate id {record.id!r} in source {source}")
                seen.add(record.id)
                for alias in record.aliases:
                    aliases.setdefault(alias, set()).add(f"{source}:{record.id}")
                if key not in {records.SNAPSHOT, records.partition(record)}:
                    problems.append(f"{name}: record {record.id!r} belongs in {records.partition(record)}.jsonl")
        except Error as error:
            problems.append(str(error))
        except OSError:
            problems.append(f"{name}: inaccessible file; check permissions")
    listed = {directory: store.files(directory) for directory in AUTHORED}
    problems.extend(_actions(listed["actions"]))
    notes: list[Note] = []
    for name in (n for directory in AUTHORED for n in listed[directory] if authored(n)):
        try:
            data = store.read(name, 4 << 20)
            notes.append(note(name, data))
            if name.startswith("concepts/"):
                validate_concept(name, data)
        except (Error, UnicodeError) as error:
            problems.append(str(error))
        except OSError:
            problems.append(f"{name}: inaccessible file; check permissions")
    slugs = {n.path: n.slugs for n in notes}

    def exists(name: str) -> bool:
        try:
            relative(name)
            with store.parent(name) as (parent, leaf):
                mode = os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode
            return stat.S_ISREG(mode) or stat.S_ISDIR(mode)
        except Error, OSError:
            return False

    for item in notes:
        for alias in item.knowledge.aliases:
            aliases.setdefault(alias, set()).add(item.path)
        problems.extend(broken(item, {split_ref(t)[0] for t in item.links if exists(split_ref(t)[0])}, slugs))
        for target in item.targets:
            source = scheme(item.path, target)
            if (source in ids or source in config.sensors) and target.partition(":")[2] not in ids.get(source, set()):
                problems.append(f"{item.path}: missing record {target}")
    for alias, owners in sorted(aliases.items()):
        if len(owners) > 1:
            problems.append(f"ambiguous identity {alias}: " + ", ".join(sorted(owners)))
    return {"valid": not problems, "notes": len(notes), "records": count, "problems": problems[:200]}
