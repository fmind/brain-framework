"""Offline checks of the whole base: notes, OKF wiki structure, links and record partitions."""

from __future__ import annotations

from urllib.parse import urlsplit

from fkf import records
from fkf.config import load
from fkf.markdown import Note, authored, broken, note, validate_wiki
from fkf.models import AUTHORED, Error
from fkf.storage import Store, relative


def validate(store: Store) -> dict[str, object]:
    """Report every problem instead of stopping at the first one."""
    load(store)
    problems: list[str] = []
    ids: dict[str, set[str]] = {}
    count = 0
    for name in records.partitions(store):
        source, key = records.source_of(name), name.rsplit("/", 1)[1].removesuffix(".jsonl")
        seen = ids.setdefault(source, set())
        try:
            for record in records.load(store, name):
                count += 1
                if record.id in seen:
                    problems.append(f"{name}: duplicate id {record.id!r} in source {source}")
                seen.add(record.id)
                if key not in {records.SNAPSHOT, records.partition(record)}:
                    problems.append(f"{name}: record {record.id!r} belongs in {records.partition(record)}.jsonl")
        except Error as error:
            problems.append(str(error))
    notes: list[Note] = []
    for name in (n for directory in AUTHORED for n in store.files(directory) if authored(n)):
        try:
            data = store.read(name, 4 << 20)
            notes.append(note(name, data))
            if name.startswith("wiki/"):
                validate_wiki(name, data)
        except (Error, UnicodeError) as error:
            problems.append(str(error))
    slugs = {n.path: n.slugs for n in notes}

    def exists(name: str) -> bool:
        try:
            relative(name)
        except Error:
            return False
        return (store.root / name).exists()

    for item in notes:
        problems.extend(broken(item, {t.partition("#")[0] for t in item.links if exists(t.partition("#")[0])}, slugs))
        for target in item.targets:
            scheme = urlsplit(target).scheme
            if scheme in ids and target.partition(":")[2] not in ids[scheme]:
                problems.append(f"{item.path}: missing record {target}")
    return {"valid": not problems, "notes": len(notes), "records": count, "problems": problems[:200]}
