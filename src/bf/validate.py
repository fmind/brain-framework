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
from bf.storage import UNNAMED, Store, relative, temporary, unnamed

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
            return stat.S_IFMT(store.mode(name))
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


def _script(argument: str) -> bool:
    """Whether a later command argument names a brain path in sensors/ or routines/, such as sensors/brief.py.

    Normalized and without placeholders, like the program itself; whitespace marks a shell command line instead.
    """
    parts = argument.split("/")
    return (
        parts[0] in {"sensors", "routines"}
        and len(parts) > 1
        and not any(part in {"", ".", ".."} for part in parts)
        and not re.search(r"[{}\\\s]", argument)
    )


def _programs(store: Store, config: Config) -> list[dict[str, str]]:
    """Each enabled program the brain holds exists; commands on PATH vary by machine.

    A script run directly is an executable regular file. Behind a command on PATH, such as an interpreter, only the
    first brain path among the arguments is checked: a script or a folder, such as a `uv run --project` project. Later
    ones may name files the program creates.
    """
    problems = []
    for kind, programs in (("sensors", config.sensors), ("routines", config.routines)):
        for name, program in sorted(programs.items()):
            # Program.command already requires a normalized path when the brain's own script runs directly.
            direct = program.command[0].startswith(("sensors/", "routines/"))
            path = program.command[0] if direct else next(filter(_script, program.command[1:]), "")
            if not program.enabled or not path:
                continue
            try:
                mode = store.mode(path)
                usable = (
                    stat.S_ISREG(mode) and bool(mode & stat.S_IXUSR)
                    if direct
                    else stat.S_ISREG(mode) or stat.S_ISDIR(mode)
                )
            except Error, OSError:
                usable = False
            if not usable:
                required = "an executable regular file" if direct else "a regular file or folder"
                problems.append(_problem("bf.yaml", f"{kind}.{name}.command: {path} is not {required}"))
    return problems


def validate(store: Store) -> dict[str, object]:
    """Report every problem instead of stopping at the first one."""
    with records.reading(store):
        return _validate(store)


def _validate(store: Store) -> dict[str, object]:
    check = _Validation(store)
    check.evidence()
    check.notes()
    check.note_links()
    check.identities()
    check.addresses()
    return check.reply()


class _Validation:
    """One pass over a brain: records, then notes, then what their links and identities name.

    Each phase adds to `problems` and to the state the later ones read: record ids, each identity's owners and the
    BF addresses files write.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self.config = load(store)
        self.problems = _programs(store, self.config)
        self.ids: dict[str, set[str]] = {}
        # Each identity's owners, note paths or record refs, with the file declaring each one.
        self.owners: dict[str, dict[str, str]] = {}
        # Each BF address a file writes as a link, with the files writing it.
        self.written: dict[str, set[str]] = {}
        self.spellings = _Spellings()
        self.count = 0
        # Linked and special files are never followed; the check reports those that would hold evidence.
        self.skipped: dict[str, tuple[int, int, int, int]] = {}
        # The record files of each source whose links hold text that is no identity: it is stored and searchable,
        # but links only to a note whose path it spells exactly.
        self.loose: dict[str, list[str]] = {}
        self.stored: set[str] = set()
        self.parsed: list[Note] = []
        # Notes that exist but cannot be parsed: their own problem is reported once, and links to them are not broken.
        self.invalid: set[str] = set()
        # Parsed notes whose identities or relations failed: their sections still resolve links, their names do not.
        self.unnamed: set[str] = set()
        # Typed note claims outside their relation's literal targets: the target's owner may declare a matching alias.
        self.targeted: list[tuple[str, links.Claim]] = []
        self.leftovers: list[str] = []
        self.slugs: dict[str, set[str]] = {}
        self.tagged: set[str] = set()
        self.unresolved: set[str] = set()
        self.kind = _kinds(store)

    def evidence(self) -> None:
        store, config = self.store, self.config
        evidence = [
            name
            for name in store.files("memories", skipped=self.skipped, split=True)
            # Check every file collection and search read as a record, even a hidden AppleDouble `._<sha>.json`,
            # and report other visible files; ignore other hidden files, such as .DS_Store, and the pending
            # transaction.
            if not name.startswith("memories/.pending/")
            and (records.stored(name) or not name.rsplit("/", 1)[1].startswith("."))
        ]
        self.stored = set(evidence)
        for name in evidence:
            try:
                source = records.source_of(name)
                record = records.load(store, name)
            except Error as error:
                self.problems.append(_problem(name, str(error)))
                continue
            except OSError:
                self.problems.append(_problem(name, "inaccessible file; check permissions"))
                continue
            # A readable record exists even when its fields no longer match the schema.
            self.count += 1
            ref = f"{source}:{record.id}"
            # The SHA-256 filename makes each id unique within its source.
            self.ids.setdefault(source, set()).add(record.id)
            # Edges follow the current schema; the check still names each stored value that schema now rejects.
            messages: list[str] = []
            ontology.validate(record, config, errors=messages)
            self.problems.extend(_problem(name, message) for message in messages)
            targets = [*record.links, *([record.url] if record.url else [])]
            if not all(re.fullmatch(IDENTITY, target) for target in targets):
                self.loose.setdefault(source, []).append(name)
            try:
                claims = ontology.record_claims(record, config, ref)
            except Error:
                # An alias that cannot name it or a malformed link, as reported above: retrieval drops the record.
                continue
            try:
                # A record's ref is one of its names: a note alias repeating it is ambiguous.
                identities = [links.target(alias) for alias in [ref, ontology.qualify(config, ref), *record.aliases]]
            except Error as error:
                # Only the ref of a source named bf, which reads as a malformed BF address.
                self.problems.append(_problem(name, str(error)))
                continue
            for identity in identities:
                self.owners.setdefault(identity, {})[ref] = name
            for claim in claims:
                if links.parse(claim.target):
                    self.written.setdefault(claim.target, set()).add(name)
            self.spellings.add({*map(links.target, record.aliases), *(claim.target for claim in claims)})

    def _unread(self, name: str) -> str:
        """Why the scan left a path out: its name, an unreadable folder, or a link or special file."""
        if unnamed(name):
            return UNNAMED
        try:
            folder = stat.S_ISDIR((self.store.root / name).lstat().st_mode)
        except PermissionError:
            # A folder above it cannot be searched, such as a memories/ of mode 600: the advice is the same.
            folder = True
        except OSError:
            folder = False
        if folder:
            return "unreadable folder; grant read and search permission or move it out of the brain"
        return "symlinks and special files are not read; replace it with a regular file"

    def notes(self) -> None:
        store, config = self.store, self.config
        # Editor locks beside authored files, such as Emacs `.#note.md` links, come and go with an open editor. A
        # write killed before its rename leaves a temporary file: a warning names it, and its folder is not a broken
        # action.
        found = {directory: store.files(directory, skipped=self.skipped) for directory in AUTHORED}
        self.leftovers = sorted(name for names in found.values() for name in names if temporary(name))
        listed = {
            directory: [name for name in names if not editor_lock(name) and not temporary(name)]
            for directory, names in found.items()
        }
        self.problems.extend(
            _problem(name, self._unread(name))
            for name in sorted(self.skipped)
            if not (name.split("/")[0] in AUTHORED and editor_lock(name))
        )
        self.problems.extend(_actions(listed["actions"]))
        for name in (n for directory in AUTHORED for n in listed[directory] if authored(n)):
            try:
                # One parse serves the projection and the OKF checks.
                markdown = parse(name, store.read(name, MAX_NOTE))
                parsed_note = note(name, markdown)
            except (Error, UnicodeError) as error:
                self.problems.append(_problem(name, str(error)))
                self.invalid.add(name)
                continue
            except OSError:
                self.problems.append(_problem(name, "inaccessible file; check permissions"))
                self.invalid.add(name)
                continue
            # Each later check reports on its own, so one problem never hides the note's others.
            self.parsed.append(parsed_note)
            messages: list[str] = []
            try:
                claims = ontology.note_claims(parsed_note, config, errors=messages)
            except Error as error:
                # Like retrieval, only a path or identity that cannot name the note drops its names; a wrong
                # relation, field value or link leaves the links to the note intact.
                self.problems.append(_problem(name, str(error)))
                self.unnamed.add(name)
            else:
                self.problems.extend(_problem(name, message) for message in messages)
                names = [*parsed_note.knowledge.names, parsed_note.knowledge.entity]
                self.spellings.add({*(links.target(v) for v in names if v), *(claim.target for claim in claims)})
                self.targeted.extend((name, claim) for claim in claims if ontology.outside(config, claim))
            if okf(name):
                try:
                    validate_okf(name, markdown)
                except Error as error:
                    self.problems.append(_problem(name, str(error)))
                # OKF ignores unknown keys: a relation named at the top level would silently assert nothing.
                self.problems.extend(
                    _problem(name, f"{key}: declared fields belong under fields:, such as fields: {{{key}: ...}}")
                    for key in sorted(markdown.attributes)
                    if key in config.ontology and key not in Knowledge.model_fields
                )
        self.slugs = {n.path: n.slugs for n in self.parsed}
        self.tagged = {tag for item in self.parsed for tag in item.knowledge.tags}

    def _exists(self, name: str) -> bool:
        return self.kind(name) in {stat.S_IFREG, stat.S_IFDIR}

    def _folder(self, path: str) -> bool:
        return path.split("/")[0] in AUTHORED and self.kind(path) == stat.S_IFDIR

    def _page(self, path: str) -> bool:
        """Whether `bf read` opens this same-brain path as a page with evidence, following pages.route."""
        parts = path.split("/")
        try:
            if path in {"", "tasks", "tags", "memories", *AUTHORED} or pages.period(path):
                return True
            if parts[0] == "tags":
                # A label no note declares opens an empty page: most likely a typo.
                return len(parts) == 2 and parts[1] in self.tagged
            if parts[0] == "memories":
                if len(parts) > 3 or (parts[1] not in self.ids and parts[1] not in self.config.sensors):
                    return False
                name = parts[2] if len(parts) == 3 else ""
                if re.fullmatch(r"[0-9a-f]{64}\.json", name):
                    return path in self.stored
                return name in {"", "undated"} or pages.period(name) is not None
        except Error:
            # An invalid period, such as 2026-13, fails to open.
            return False
        return self._folder(path)

    def note_links(self) -> None:
        config, ids, owners = self.config, self.ids, self.owners
        for item in self.parsed:
            named = item.path not in self.unnamed
            for alias in [
                ontology.qualify(config, item.path),
                *(item.knowledge.names if named else []),
                *([item.knowledge.entity] if item.knowledge.entity and named else []),
            ]:
                # A path holding a control character or backslash has no BF address: note_claims reported it.
                with suppress(Error):
                    owners.setdefault(links.target(alias), {})[item.path] = item.path
            self.problems.extend(_problem(item.path, message) for message in broken(item, self._exists, self.slugs))
            for target in item.targets:
                source = scheme(item.path, target)
                written_source, _, record_id = target.partition(":")
                if (source in ids or source in config.sensors) and written_source != source:
                    # A URL scheme ignores case, but a record ref names its source exactly: Mail:m1 never links
                    # mail:m1.
                    self.problems.append(
                        _problem(item.path, f"record refs are case-sensitive; write {source}:{record_id}, not {target}")
                    )
                elif (
                    (source in ids or source in config.sensors)
                    and record_id not in ids.get(source, set())
                    # A record's provider alias, such as calendar:primary/ID beside calendar:ID, names it too.
                    and target not in owners
                ):
                    self.problems.append(_problem(item.path, f"missing record {target}"))
                if links.parse(target):
                    self.written.setdefault(links.target(target), set()).add(item.path)
                elif "?rel=" in target and re.fullmatch(IDENTITY, target):
                    # Only a bf:// link carries a relation; another identity would silently become a different one.
                    self.problems.append(
                        _problem(
                            item.path, f"?rel= types bf:// links only; set the relation in fields instead: {target}"
                        )
                    )

    def identities(self) -> None:
        config, owners = self.config, self.owners
        # Every identity each note or record answers to: a typed link may name its owner by any of them.
        names: dict[str, set[str]] = {}
        for alias, claimed in owners.items():
            for owner in claimed:
                names.setdefault(owner, set()).add(alias)
        for file, claim in self.targeted:
            definition = config.ontology[claim.relation]
            if not any(definition.allows(alias) for owner in owners.get(claim.target, {}) for alias in names[owner]):
                # Search keeps such a claim: the note states it, and only its author can correct it.
                self.problems.append(
                    _problem(file, f"{claim.relation} link outside the declared targets: {claim.target}")
                )
        for alias, claimed in sorted(owners.items()):
            # Prefer naming an authored note: its alias is usually the claim to revisit.
            file = min(claimed.values(), key=lambda value: (not authored(value), value))
            if len(claimed) > 1:
                self.problems.append(_problem(file, f"ambiguous identity {alias}: " + ", ".join(sorted(claimed))))
            if (parsed := links.parse(alias)) and parsed.brain == config.name and self._folder(parsed.path):
                self.problems.append(
                    _problem(file, f"identity {alias} is also a folder page; rename the entity or folder")
                )

    def addresses(self) -> None:
        for value, files in sorted(self.written.items()):
            parsed = links.parse(value)
            if parsed is None or parsed.brain != self.config.name:
                self.unresolved.add(value)
                continue
            if self._page(parsed.path):
                # `bf read` opens a page whole: a fragment selects nothing on it.
                if parsed.fragment:
                    self.problems.extend(
                        _problem(file, f"page cannot select a section: {value}") for file in sorted(files)
                    )
                continue
            errors = set()
            for path in self.owners.get(links.address(parsed.brain, parsed.path), {}) or {parsed.path}:
                source, separator, record_id = path.partition(":")
                if path in self.invalid:
                    continue
                if path in self.slugs:
                    if parsed.fragment and parsed.fragment not in self.slugs[path]:
                        errors.add("missing BF section")
                elif separator and record_id in self.ids.get(source, set()):
                    if parsed.fragment:
                        errors.add("record cannot select a Markdown section")
                else:
                    errors.add("unresolved BF target")
            self.problems.extend(
                _problem(file, f"{error}: {value}") for file in sorted(files) for error in sorted(errors)
            )

    def reply(self) -> dict[str, object]:
        # Few sources and interrupted writes, each with one warning, come before the case variants a large brain can
        # hold many of.
        warnings: list[dict[str, object]] = [
            {
                "warning": "record links are not namespaced identities or URLs",
                "source": source,
                "records": len(files),
                "file": min(files),
            }
            for source, files in sorted(self.loose.items())
        ]
        warnings.extend(
            {"warning": "an interrupted write left this temporary file; delete it", "file": name}
            for name in self.leftovers
        )
        warnings.extend(self.spellings.warnings())
        # One problem per file and error: a link repeated across a note or a record never crowds out the others.
        problems = list({(problem["file"], problem["error"]): problem for problem in self.problems}.values())
        unresolved = self.unresolved
        return {
            "valid": not problems,
            "notes": len(self.parsed),
            "records": self.count,
            "problems": problems[:LIMIT],
            **({"problems_truncated": True} if len(problems) > LIMIT else {}),
            **({"warnings": warnings[:LIMIT]} if warnings else {}),
            **({"warnings_truncated": True} if len(warnings) > LIMIT else {}),
            **({"unresolved": sorted(unresolved)[:LIMIT]} if unresolved else {}),
            **({"unresolved_truncated": True} if len(unresolved) > LIMIT else {}),
        }
