"""Offline checks of the whole base: notes, OKF wiki structure, links and record partitions."""

from __future__ import annotations

import os
import stat

from fkf import records
from fkf.config import load
from fkf.markdown import Note, authored, broken, note, scheme, split_ref, validate_wiki
from fkf.models import AUTHORED, Error
from fkf.storage import Store, relative


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
    notes: list[Note] = []
    for name in (n for directory in AUTHORED for n in store.files(directory) if authored(n)):
        try:
            data = store.read(name, 4 << 20)
            notes.append(note(name, data))
            if name.startswith("wiki/"):
                validate_wiki(name, data)
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
            if (source in ids or source in config.sources) and target.partition(":")[2] not in ids.get(source, set()):
                problems.append(f"{item.path}: missing record {target}")
    for alias, owners in sorted(aliases.items()):
        if len(owners) > 1:
            problems.append(f"ambiguous identity {alias}: " + ", ".join(sorted(owners)))
    return {"valid": not problems, "notes": len(notes), "records": count, "problems": problems[:200]}
