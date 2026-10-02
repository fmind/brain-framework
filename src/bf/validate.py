"""Offline checks of the whole brain: notes, OKF structure, links and record files."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from contextlib import suppress
from datetime import date
from functools import cache

from bf import links, ontology, pages, records
from bf.config import load
from bf.markdown import Note, authored, broken, editor_lock, note, okf, parse, resolve, scheme, validate_okf
from bf.models import AUTHORED, IDENTITY, MAX_NOTE, Config, Error, Knowledge
from bf.storage import UNNAMED, Store, relative, unnamed

_ACTION = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[a-z0-9]+(?:-[a-z0-9]+)*")
# A reply lists at most this many problems, warnings and unresolved targets; a `*_truncated` flag marks the rest.
LIMIT = 200
# A warning lists at most this many spellings of one identity.
VARIANTS = 20


def _kinds(store: Store) -> Callable[[str], int]:
    """The file type of an exactly named brain path; 0 when it is absent or named with another case."""

    @cache
    def entries(directory: str) -> frozenset[str]:
        """Exact names: on a case-insensitive volume, stat would also accept a differently cased link."""
        if not directory:
            return frozenset(path.name for path in store.root.iterdir())
        with store.parent(directory) as (parent, leaf):
            descriptor = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            with os.scandir(descriptor) as found:
                return frozenset(entry.name for entry in found)
        finally:
            os.close(descriptor)

    def kind(name: str) -> int:
        try:
            parts = relative(name)
            if any(part not in entries("/".join(parts[:i])) for i, part in enumerate(parts)):
                return 0
            with store.parent(name) as (parent, leaf):
                return stat.S_IFMT(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode)
        except Error, OSError:
            return 0

    return kind


def broken_links(store: Store, item: Note) -> list[str]:
    """A note's relative links that name no brain file or heading, as `validate` reports them, before it is saved.

    The note may not exist yet, as a routine's action: its own path and headings answer links to itself.
    """
    kind = _kinds(store)
    slugs = {item.path: item.slugs}
    for target in item.targets:
        if scheme(item.path, target) or target == "#":
            continue
        name, fragment = resolve(item.path, target)
        if fragment and authored(name) and name not in slugs and kind(name) == stat.S_IFREG:
            # An unreadable or invalid target is its own file's problem, not a broken link.
            with suppress(Error, OSError, UnicodeError):
                slugs[name] = note(name, store.read(name, MAX_NOTE)).slugs
    return broken(item, lambda name: name == item.path or kind(name) in {stat.S_IFREG, stat.S_IFDIR}, slugs)


def _problem(file: str, message: str) -> dict[str, str]:
    """One problem names its brain-relative file or folder once, whether or not the message already did."""
    return {"file": file, "error": message.removeprefix(file + ":").lstrip()}


def _actions(files: list[str]) -> list[dict[str, str]]:
    """Each folder below actions/ is one dated action with its ACTION.md; loose files are allowed."""
    problems = []
    for folder in sorted({name.split("/")[1] for name in files if name.count("/") >= 2}):
        try:
            if not _ACTION.fullmatch(folder):
                raise ValueError
            date.fromisoformat(folder[:10])
        except ValueError:
            problems.append(
                _problem(f"actions/{folder}", "name action folders YYYY-MM-DD_slug (lowercase slug, hyphens)")
            )
            continue
        if f"actions/{folder}/ACTION.md" not in files:
            problems.append(_problem(f"actions/{folder}", "missing ACTION.md"))
    return problems


class _Spellings:
    """How many files name each identity, grouped by its lowercase form: one subject spelled several ways."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, int]] = {}

    def add(self, identities: set[str]) -> None:
        """Count one file's identities once each; URLs and BF addresses are identities too."""
        for identity in identities:
            if re.fullmatch(IDENTITY, identity):
                group = self.files.setdefault(identity.lower(), {})
                group[identity] = group.get(identity, 0) + 1

    def warnings(self) -> list[dict[str, object]]:
        """Case variants within one scheme, most referenced first; identities are case-sensitive, so each spelling
        names a different subject until the evidence agrees on one."""
        groups = sorted(
            (group for group in self.files.values() if len(group) > 1),
            key=lambda group: (-sum(group.values()), min(group)),
        )
        return [
            {
                "warning": "identities differ only by letter case",
                "identities": [
                    {"identity": identity, "files": files}
                    for identity, files in sorted(group.items(), key=lambda item: (-item[1], item[0]))[:VARIANTS]
                ],
            }
            for group in groups
        ]


def _programs(store: Store, config: Config) -> list[dict[str, str]]:
    """Each enabled program the brain holds is a regular, executable file; commands on PATH vary by machine."""
    problems = []
    for kind, programs in (("sensors", config.sensors), ("routines", config.routines)):
        for name, program in sorted(programs.items()):
            executable = program.command[0]
            if not program.enabled or not executable.startswith(("sensors/", "routines/")):
                continue
            try:
                with store.parent(executable) as (parent, leaf):
                    mode = os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode
                usable = stat.S_ISREG(mode) and mode & stat.S_IXUSR
            except Error, OSError:
                usable = False
            if not usable:
                problems.append(
                    _problem("bf.yaml", f"{kind}.{name}.command: {executable} is not an executable regular file")
                )
    return problems


def validate(store: Store) -> dict[str, object]:
    """Report every problem instead of stopping at the first one."""
    with records.reading(store):
        return _validate(store)


def _validate(store: Store) -> dict[str, object]:
    config = load(store)
    problems = _programs(store, config)
    ids: dict[str, set[str]] = {}
    # Each identity's owners, note paths or record refs, with the file declaring each one.
    owners: dict[str, dict[str, str]] = {}
    # Each BF address a file writes as a link, with the files writing it.
    written: dict[str, set[str]] = {}
    spellings = _Spellings()
    count = 0
    # Linked and special files are never followed; the check reports those that would hold evidence.
    skipped: dict[str, tuple[int, int, int, int]] = {}
    evidence = [
        name
        for name in store.files("memories", skipped=skipped, split=True)
        # Check every file collection and search read as a record, even a hidden AppleDouble `._<sha>.json`, and
        # report other visible files; ignore other hidden files, such as .DS_Store, and the pending transaction.
        if not name.startswith("memories/.pending/")
        and (records.stored(name) or not name.rsplit("/", 1)[1].startswith("."))
    ]
    for name in evidence:
        try:
            source = records.source_of(name)
            record = records.load(store, name)
            # A readable record exists even when its fields no longer match the schema.
            count += 1
            ref = f"{source}:{record.id}"
            # The SHA-256 filename makes each id unique within its source.
            ids.setdefault(source, set()).add(record.id)
            # A record's ref is one of its names: a note alias repeating it is ambiguous.
            for alias in [ref, ontology.qualify(config, ref), *record.aliases]:
                owners.setdefault(links.target(alias), {})[ref] = name
            claims = ontology.record_claims(record, config, ref, strict=True)
            for claim in claims:
                if links.parse(claim.target):
                    written.setdefault(claim.target, set()).add(name)
            spellings.add({*map(links.target, record.aliases), *(claim.target for claim in claims)})
            # Edges follow the current schema; the check still names each stored value that schema now rejects.
            ontology.validate(record, config)
        except Error as error:
            problems.append(_problem(name, str(error)))
        except OSError:
            problems.append(_problem(name, "inaccessible file; check permissions"))
    # Editor locks beside authored files, such as Emacs `.#note.md` links, come and go with an open editor.
    listed = {
        directory: [name for name in store.files(directory, skipped=skipped) if not editor_lock(name)]
        for directory in AUTHORED
    }

    def unread(name: str) -> str:
        """Why the scan left a path out: its name, an unreadable folder, or a link or special file."""
        if unnamed(name):
            return UNNAMED
        try:
            folder = stat.S_ISDIR((store.root / name).lstat().st_mode)
        except OSError:
            folder = False
        if folder:
            return "unreadable folder; grant read and search permission or move it out of the brain"
        return "symlinks and special files are not read; replace it with a regular file"

    problems.extend(
        _problem(name, unread(name))
        for name in sorted(skipped)
        if not (name.split("/")[0] in AUTHORED and editor_lock(name))
    )
    problems.extend(_actions(listed["actions"]))
    notes: list[Note] = []
    # Notes that exist but cannot be parsed: their own problem is reported once, and links to them are not broken.
    invalid: set[str] = set()
    # Parsed notes whose identities or relations failed: their sections still resolve links, their names do not.
    unnamed_notes: set[str] = set()
    # Typed note claims outside their relation's literal targets: the target's owner may declare a matching alias.
    targeted: list[tuple[str, links.Claim]] = []
    for name in (n for directory in AUTHORED for n in listed[directory] if authored(n)):
        try:
            data = store.read(name, MAX_NOTE)
            parsed_note = note(name, data)
        except (Error, UnicodeError) as error:
            problems.append(_problem(name, str(error)))
            invalid.add(name)
            continue
        except OSError:
            problems.append(_problem(name, "inaccessible file; check permissions"))
            invalid.add(name)
            continue
        # Each later check reports on its own, so one problem never hides the note's others.
        notes.append(parsed_note)
        try:
            claims = ontology.note_claims(parsed_note, config, strict=True)
        except Error as error:
            problems.append(_problem(name, str(error)))
            unnamed_notes.add(name)
            claims = []
        else:
            names = [*parsed_note.knowledge.names, parsed_note.knowledge.entity]
            spellings.add({*(links.target(v) for v in names if v), *(claim.target for claim in claims)})
            targeted.extend((name, claim) for claim in claims if ontology.outside(config, claim))
        if okf(name):
            try:
                validate_okf(name, data)
            except Error as error:
                problems.append(_problem(name, str(error)))
            # OKF ignores unknown keys: a relation named at the top level would silently assert nothing.
            problems.extend(
                _problem(name, f"{key}: declared fields belong under fields:, such as fields: {{{key}: ...}}")
                for key in sorted(parse(name, data).attributes)
                if key in config.ontology and key not in Knowledge.model_fields
            )
    slugs = {n.path: n.slugs for n in notes}

    kind = _kinds(store)

    def exists(name: str) -> bool:
        return kind(name) in {stat.S_IFREG, stat.S_IFDIR}

    def folder(path: str) -> bool:
        return path.split("/")[0] in AUTHORED and kind(path) == stat.S_IFDIR

    stored = set(evidence)
    tagged = {tag for item in notes for tag in item.knowledge.tags}

    def page(path: str) -> bool:
        """Whether `bf read` opens this same-brain path as a page with evidence, following pages.page's routing."""
        parts = path.split("/")
        try:
            if path in {"", "tasks", "tags", "memories", *AUTHORED} or pages.period(path):
                return True
            if parts[0] == "tags":
                # A label no note declares opens an empty page: most likely a typo.
                return len(parts) == 2 and parts[1] in tagged
            if parts[0] == "memories":
                if len(parts) > 3 or (parts[1] not in ids and parts[1] not in config.sensors):
                    return False
                name = parts[2] if len(parts) == 3 else ""
                if re.fullmatch(r"[0-9a-f]{64}\.json", name):
                    return path in stored
                return name in {"", "undated"} or pages.period(name) is not None
        except Error:
            # An invalid period, such as 2026-13, fails to open.
            return False
        return folder(path)

    for item in notes:
        named = item.path not in unnamed_notes
        for alias in [
            ontology.qualify(config, item.path),
            *(item.knowledge.names if named else []),
            *([item.knowledge.entity] if item.knowledge.entity and named else []),
        ]:
            owners.setdefault(links.target(alias), {})[item.path] = item.path
        problems.extend(_problem(item.path, message) for message in broken(item, exists, slugs))
        for target in item.targets:
            source = scheme(item.path, target)
            if (
                (source in ids or source in config.sensors)
                and target.partition(":")[2] not in ids.get(source, set())
                # A record's provider alias, such as calendar:primary/ID beside calendar:ID, names it too.
                and target not in owners
            ):
                problems.append(_problem(item.path, f"missing record {target}"))
            if links.parse(target):
                written.setdefault(links.target(target), set()).add(item.path)
            elif "?rel=" in target and re.fullmatch(IDENTITY, target):
                # Only a bf:// link carries a relation; another identity would silently become a different one.
                problems.append(
                    _problem(item.path, f"?rel= types bf:// links only; set the relation in fields instead: {target}")
                )
    # Every identity each note or record answers to: a typed link may name its owner by any of them.
    names: dict[str, set[str]] = {}
    for alias, claimed in owners.items():
        for owner in claimed:
            names.setdefault(owner, set()).add(alias)
    for file, claim in targeted:
        definition = config.ontology[claim.relation]
        if not any(definition.allows(alias) for owner in owners.get(claim.target, {}) for alias in names[owner]):
            # Search keeps such a claim: the note states it, and only its author can correct it.
            problems.append(_problem(file, f"{claim.relation} link outside the declared targets: {claim.target}"))
    for alias, claimed in sorted(owners.items()):
        # Prefer naming an authored note: its alias is usually the claim to revisit.
        file = min(claimed.values(), key=lambda value: (not authored(value), value))
        if len(claimed) > 1:
            problems.append(_problem(file, f"ambiguous identity {alias}: " + ", ".join(sorted(claimed))))
        if (parsed := links.parse(alias)) and parsed.brain == config.name and folder(parsed.path):
            problems.append(_problem(file, f"identity {alias} is also a folder page; rename the entity or folder"))
    unresolved: set[str] = set()
    for value, files in sorted(written.items()):
        parsed = links.parse(value)
        if parsed is None or parsed.brain != config.name:
            unresolved.add(value)
            continue
        if not parsed.fragment and page(parsed.path):
            continue
        errors = set()
        for path in owners.get(links.address(parsed.brain, parsed.path), {}) or {parsed.path}:
            source, separator, record_id = path.partition(":")
            if path in invalid:
                continue
            if path in slugs:
                if parsed.fragment and parsed.fragment not in slugs[path]:
                    errors.add("missing BF section")
            elif separator and record_id in ids.get(source, set()):
                if parsed.fragment:
                    errors.add("record cannot select a Markdown section")
            else:
                errors.add("unresolved BF target")
        problems.extend(_problem(file, f"{error}: {value}") for file in sorted(files) for error in sorted(errors))
    warnings = spellings.warnings()
    return {
        "valid": not problems,
        "notes": len(notes),
        "records": count,
        "problems": problems[:LIMIT],
        **({"problems_truncated": True} if len(problems) > LIMIT else {}),
        **({"warnings": warnings[:LIMIT]} if warnings else {}),
        **({"warnings_truncated": True} if len(warnings) > LIMIT else {}),
        **({"unresolved": sorted(unresolved)[:LIMIT]} if unresolved else {}),
        **({"unresolved_truncated": True} if len(unresolved) > LIMIT else {}),
    }
