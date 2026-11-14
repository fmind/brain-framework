"""Offline checks of the whole brain: notes, OKF structure, links and record files."""

from __future__ import annotations

import re
import stat
from collections.abc import Callable
from contextlib import suppress
from datetime import date
from functools import cache

from bf import links, ontology, records
from bf.config import load
from bf.markdown import (
    Note,
    authored,
    broken,
    editor_lock,
    entry_note,
    note,
    okf,
    parse,
    resolve,
    scheme,
    validate_okf,
)
from bf.markdown import lines as markdown_lines
from bf.models import (
    AUTHORED,
    IDENTITY,
    MAX_NOTE,
    MAX_PROMPT,
    Config,
    Error,
    Knowledge,
)
from bf.storage import Store, relative, temporary, unread

_ACTION = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[a-z0-9]+(?:-[a-z0-9]+)*")
LIMIT, VARIANTS = 200, 20


def _kinds(store: Store) -> Callable[[str], int]:
    """The file type of an exact-cased brain path; 0 when absent or differently cased."""

    @cache
    def entries(directory: str) -> frozenset[str]:
        return frozenset(p.name for p in store.root.iterdir()) if not directory else frozenset(store.entries(directory))

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
    """A note's relative links that name no brain file or heading, before it is saved."""
    kind = _kinds(store)
    slugs = {item.path: item.slugs}
    for target in item.targets:
        if scheme(item.path, target) or target == "#":
            continue
        name, fragment = resolve(item.path, target)
        entry = name + "/ACTION.md" if re.fullmatch(r"actions/[^/#]+", name) else name
        if fragment and authored(entry) and entry not in slugs and kind(entry) == stat.S_IFREG:
            with suppress(Error, OSError, UnicodeError):
                slugs[entry] = note(entry, store.read(entry, MAX_NOTE)).slugs
    return broken(item, lambda name: name == item.path or kind(name) in {stat.S_IFREG, stat.S_IFDIR}, slugs)


def note_policy_errors(
    config: Config, name: str, text: str, attributes: dict[str, object], parsed_note: Note, *, check_okf: bool = False
) -> list[str]:
    """Check note frontmatter, stale_after, forbidden patterns and misplaced relation keys against brain policy."""
    errors = (
        [
            f"{key}: relations belong under relations:, such as relations: {{{key}: ...}}"
            for key in sorted(attributes)
            if key in config.ontology and key not in Knowledge.model_fields
        ]
        if check_okf
        else []
    )
    rules = config.validation
    if entry_note(name):
        if rules.required_status is not None and attributes.get("status") not in rules.required_status:
            errors.append(f"frontmatter status must be explicitly set to one of: {', '.join(rules.required_status)}")
        if (
            parsed_note.knowledge.stale_after
            and rules.max_stale_days is not None
            and (date.fromisoformat(parsed_note.knowledge.stale_after[:10]) - date.today()).days > rules.max_stale_days
        ):
            errors.append(f"stale_after exceeds max_stale_days ({rules.max_stale_days} days)")
    for pattern in rules.forbid_patterns:
        compiled = re.compile(pattern)
        for line_number, line in enumerate((item.rstrip("\r\n") for item in markdown_lines(text)), 1):
            if compiled.search(line):
                errors.append(f"line {line_number}: matches forbidden pattern {pattern}")
                break
    return errors


def validate_action_markdown(
    store: Store, config: Config, path: str, raw: bytes, *, require_draft: bool = False
) -> None:
    """Validate an `actions/.../ACTION.md` note against OKF, ontology, brain policies and relative links."""
    if len(raw) > MAX_NOTE:
        raise Error("action note exceeds 4 MiB")
    md = parse(path, raw)
    action = note(path, md)
    ontology.note_claims(action, config, strict=True)
    validate_okf(path, md)
    if action.knowledge.type != "action":
        raise Error(f"{path}: action note requires type: action")
    if require_draft and md.attributes.get("status") != "draft":
        raise Error(f"{path}: proposed action requires status: draft")
    if errs := note_policy_errors(config, path, md.text, md.attributes, action, check_okf=True):
        raise Error(f"{path}: {errs[0]}")
    if links_bad := broken_links(store, action):
        first = links_bad[0] if len(links_bad[0]) <= 200 else links_bad[0][:199] + "…"
        raise Error(first + (f" and {len(links_bad) - 1} more" if len(links_bad) > 1 else ""))


def _problem(file: str, message: str) -> dict[str, str]:
    """One problem naming `file` once, whether or not `message` already did."""
    return {"file": file, "error": message.removeprefix(file + ":").lstrip()}


def _actions(files: list[str]) -> list[dict[str, str]]:
    """Each folder below actions/ must be YYYY-MM-DD_slug with an ACTION.md; loose files are allowed."""
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
    """Case variants of identities grouped by their lowercase form."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, int]] = {}

    def add(self, identities: set[str]) -> None:
        for identity in identities:
            if re.fullmatch(IDENTITY, identity):
                group = self.files.setdefault(identity.lower(), {})
                group[identity] = group.get(identity, 0) + 1

    def warnings(self) -> list[dict[str, object]]:
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
    parts = argument.split("/")
    return (
        parts[0] in {"sensors", "routines", "channels"}
        and len(parts) > 1
        and not any(part in {"", ".", ".."} for part in parts)
        and not re.search(r"[{}\\\s]", argument)
    )


def _check_cmd(
    store: Store,
    problems: list[dict[str, str]],
    label: str,
    cmd: list[str],
    expand_fn: Callable[[Store, str], list[str]],
) -> None:
    direct = cmd[0].startswith(("sensors/", "routines/", "channels/"))
    if path := (cmd[0] if direct else next(filter(_script, cmd[1:]), "")):
        try:
            mode = store.mode(path)
            usable = (
                stat.S_ISREG(mode) and bool(mode & stat.S_IXUSR)
                if direct
                else (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
            )
        except Error, OSError:
            usable = False
        if not usable:
            req = "an executable regular file" if direct else "a regular file or folder"
            problems.append(_problem("bf.yaml", f"{label}: {path} is not {req}"))
    count = 1
    for arg in cmd[1:]:
        if lines_match := re.fullmatch(r"\{\{lines:([^{}\\]+)\}\}", arg):
            try:
                count += len(expand_fn(store, lines_match[1]))
            except Error as error:
                problems.append(_problem("bf.yaml", f"{label}: {error}"))
        else:
            count += 1
    if count > 128:
        problems.append(_problem("bf.yaml", f"{label}: command arguments exceed 128 items after expanding line files"))


def _programs(store: Store, config: Config) -> list[dict[str, str]]:
    """Verify every enabled job's script, prompt, cwd and `{{lines:...}}` file."""
    from bf.jobs import expand_lines

    problems: list[dict[str, str]] = []
    if config.defaults.harness:
        _check_cmd(store, problems, "defaults.harness", config.defaults.harness, expand_lines)
    for name, job in sorted(config.jobs.items()):
        if not job.enabled:
            continue
        if job.prompt:
            try:
                readable = stat.S_ISREG(store.mode(job.prompt))
                size = store.lstat(job.prompt).st_size if readable else 0
            except Error, OSError:
                readable, size = False, 0
            if not readable:
                problems.append(_problem("bf.yaml", f"jobs.{name}.prompt: {job.prompt} is not a regular file"))
            elif size > MAX_PROMPT:
                problems.append(_problem("bf.yaml", f"jobs.{name}.prompt: {job.prompt} exceeds {MAX_PROMPT} bytes"))
        if job.cwd:
            try:
                is_dir = stat.S_ISDIR(store.mode(job.cwd))
            except Error, OSError:
                is_dir = False
            if not is_dir:
                problems.append(_problem("bf.yaml", f"jobs.{name}.cwd: {job.cwd} is not a directory"))
        for field, cmd in (("run", job.run), ("harness", job.harness), ("send", job.send), ("gate", job.gate)):
            if cmd:
                _check_cmd(store, problems, f"jobs.{name}.{field}", cmd, expand_lines)
    return problems


def validate(store: Store) -> dict[str, object]:
    """Report every problem instead of stopping at the first one."""
    with records.reading(store):
        check = _Validation(store)
        check.evidence()
        check.notes()
        check.note_links()
        check.identities()
        check.addresses()
        return check.reply()


class _Validation:
    """Whole-brain validation pass across records, notes, links, identities and BF addresses."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.config = load(store)
        self.problems = _programs(store, self.config)
        self.ids: dict[str, set[str]] = {}
        self.owners: dict[str, dict[str, str]] = {}
        self.written: dict[str, set[str]] = {}
        self.spellings = _Spellings()
        self.count = 0
        self.skipped: dict[str, tuple[int, int, int, int]] = {}
        self.loose: dict[str, list[str]] = {}
        self.stored: set[str] = set()
        self.parsed: list[Note] = []
        self.invalid: set[str] = set()
        self.unnamed: set[str] = set()
        self.targeted: list[tuple[str, links.Claim]] = []
        self.leftovers: list[str] = []
        self.stale_warnings: list[dict[str, object]] = []
        self.slugs: dict[str, set[str]] = {}
        self.tagged: set[str] = set()
        self.unresolved: set[str] = set()
        self.kind = _kinds(store)

    def _fail(self, file: str, error: object) -> None:
        self.problems.append(_problem(file, str(error)))

    def evidence(self) -> None:
        store, config = self.store, self.config
        listed = store.files("memories", skipped=self.skipped, split=True)
        # Keep stored records (including AppleDouble `._<sha>.json`) and visible files; ignore .DS_Store & .pending.
        evidence = [
            name
            for name in listed
            if not name.startswith("memories/.pending/")
            and (records.stored(name) or not name.rsplit("/", 1)[1].startswith("."))
        ]
        self.stored = set(evidence)
        for name in listed:
            parts = name.split("/")
            if len(parts) == 3 and parts[2] == records.FORGOTTEN:
                try:
                    records.forgotten(store, parts[1])
                except Error as error:
                    self._fail(name, error)
                except OSError:
                    self._fail(name, "inaccessible file; check permissions")
        for name in evidence:
            try:
                source = records.source_of(name)
                record = records.load(store, name)
            except Error as error:
                self._fail(name, error)
                continue
            except OSError:
                self._fail(name, "inaccessible file; check permissions")
                continue
            self.count += 1
            ref = f"{source}:{record.id}"
            self.ids.setdefault(source, set()).add(record.id)
            messages: list[str] = []
            ontology.validate(record, config, errors=messages)
            self.problems.extend(_problem(name, message) for message in messages)
            targets = [*record.links, *([record.url] if record.url else [])]
            if not all(re.fullmatch(IDENTITY, target) for target in targets):
                self.loose.setdefault(source, []).append(name)
            try:
                claims = ontology.record_claims(record, config, ref)
            except Error:
                continue
            for identity in [links.target(alias) for alias in [ref, ontology.qualify(config, ref), *record.aliases]]:
                self.owners.setdefault(identity, {})[ref] = name
            for claim in claims:
                if links.parse(claim.target):
                    self.written.setdefault(claim.target, set()).add(name)
            self.spellings.add({*map(links.target, record.aliases), *(claim.target for claim in claims)})

    def notes(self) -> None:
        store, config = self.store, self.config
        found = {directory: store.files(directory, skipped=self.skipped) for directory in AUTHORED}
        self.leftovers = sorted(name for names in found.values() for name in names if temporary(name))
        listed = {
            directory: [name for name in names if not editor_lock(name) and not temporary(name)]
            for directory, names in found.items()
        }
        self.problems.extend(
            _problem(name, unread(store, name))
            for name in sorted(self.skipped)
            if not (name.split("/")[0] in AUTHORED and editor_lock(name))
        )
        self.problems.extend(_actions(listed["actions"]))
        for name in (n for directory in AUTHORED for n in listed[directory] if authored(n)):
            try:
                markdown = parse(name, store.read(name, MAX_NOTE))
                parsed_note = note(name, markdown)
            except (Error, UnicodeError) as error:
                self._fail(name, error)
                self.invalid.add(name)
                continue
            except OSError:
                self._fail(name, "inaccessible file; check permissions")
                self.invalid.add(name)
                continue
            self.parsed.append(parsed_note)
            messages: list[str] = []
            try:
                claims = ontology.note_claims(parsed_note, config, errors=messages)
            except Error as error:
                self._fail(name, error)
                self.unnamed.add(name)
            else:
                self.problems.extend(_problem(name, message) for message in messages)
                names = [*parsed_note.knowledge.names, parsed_note.knowledge.entity]
                self.spellings.add({*(links.target(v) for v in names if v), *(claim.target for claim in claims)})
                self.targeted.extend((name, claim) for claim in claims if ontology.outside(config, claim))
            if is_okf := okf(name):
                try:
                    validate_okf(name, markdown)
                except Error as error:
                    self._fail(name, error)
            self._policy(name, markdown.text, markdown.attributes, parsed_note, check_okf=is_okf)
        if config.validation.index_note is not None:
            self._check_index_note(config.validation.index_note)
        self.slugs = {n.path: n.slugs for n in self.parsed}
        self.tagged = {tag for item in self.parsed for tag in item.knowledge.tags}

    def _policy(
        self, name: str, text: str, attributes: dict[str, object], parsed_note: Note, *, check_okf: bool = False
    ) -> None:
        rules = self.config.validation
        for msg in note_policy_errors(self.config, name, text, attributes, parsed_note, check_okf=check_okf):
            self._fail(name, msg)
        if (
            entry_note(name)
            and parsed_note.knowledge.stale_after
            and rules.warn_stale_days is not None
            and (date.fromisoformat(parsed_note.knowledge.stale_after[:10]) - date.today()).days
            <= rules.warn_stale_days
        ):
            self.stale_warnings.append(
                {
                    "warning": f"stale_after is within {rules.warn_stale_days} days",
                    "file": name,
                    "stale_after": parsed_note.knowledge.stale_after[:10],
                }
            )

    def _check_index_note(self, index_path: str) -> None:
        index_item = {item.path: item for item in self.parsed}.get(index_path)
        if index_item is None:
            if index_path not in self.invalid:
                self._fail("bf.yaml", f"validate.index_note: {index_path} does not exist")
            return
        bundle = index_path.rsplit("/", 1)[0]
        linked = {resolve(index_path, t)[0] for t in index_item.targets if not scheme(index_path, t) and t != "#"}
        for item in self.parsed:
            if item.path.startswith(bundle + "/") and entry_note(item.path) and item.path not in linked:
                self._fail(index_path, f"missing link to {item.path}")

    def _exists(self, name: str) -> bool:
        return self.kind(name) in {stat.S_IFREG, stat.S_IFDIR}

    def _folder(self, path: str) -> bool:
        return (
            not re.fullmatch(r"actions/[^/#]+", path)
            and path.split("/")[0] in AUTHORED
            and self.kind(path) == stat.S_IFDIR
        )

    def _canonical(self, target: str) -> str:
        if (
            (parsed := links.parse(target))
            and parsed.brain == self.config.name
            and re.fullmatch(r"actions/[^/#]+", parsed.path)
        ):
            return links.address(parsed.brain, f"{parsed.path}/ACTION.md")
        return target

    def _page(self, path: str) -> bool:
        from bf import pages

        parts = path.split("/")
        try:
            if path in {"", "review", "tasks", "tags", "memories", *AUTHORED} or pages.period(path):
                return True
            if parts[0] == "tags":
                return len(parts) == 2 and parts[1] in self.tagged
            if parts[0] == "memories":
                if len(parts) > 3 or not (parts[1] in self.ids or parts[1] in self.config.record_sources()):
                    return False
                name = parts[2] if len(parts) == 3 else ""
                if re.fullmatch(r"[0-9a-f]{64}\.json", name):
                    return path in self.stored
                return name in {"", "undated"} or pages.period(name) is not None
        except Error:
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
                with suppress(Error):
                    owners.setdefault(links.target(alias), {})[item.path] = item.path

        def present(name: str) -> bool:
            return (
                self._exists(name)
                or name in self.skipped
                or (name.endswith("/ACTION.md") and name.removesuffix("/ACTION.md") in self.skipped)
            )

        for item in self.parsed:
            for message in broken(item, present, self.slugs, self.unresolved):
                self._fail(item.path, message)
            for value in item.knowledge.relations.values():
                for target in value if isinstance(value, list) else [value]:
                    if links.parse(target):
                        self.written.setdefault(links.target(target), set()).add(item.path)
            for target in item.targets:
                source = scheme(item.path, target)
                written_source, _, record_id = target.partition(":")
                collected = source in ids or source in config.record_sources()
                if collected and written_source != source:
                    self._fail(item.path, f"record refs are case-sensitive; write {source}:{record_id}, not {target}")
                elif collected and record_id not in ids.get(source, set()) and target not in owners:
                    self._fail(item.path, f"missing record {target}")
                if links.parse(target):
                    self.written.setdefault(links.target(target), set()).add(item.path)
                elif "?rel=" in target and re.fullmatch(IDENTITY, target):
                    self._fail(
                        item.path, f"?rel= types bf:// links only; set the relation in relations instead: {target}"
                    )

    def identities(self) -> None:
        config, owners = self.config, self.owners
        names: dict[str, set[str]] = {}
        for alias, claimed in owners.items():
            for owner in claimed:
                names.setdefault(owner, set()).add(alias)
        for file, claim in self.targeted:
            definition = config.ontology[claim.relation]
            canonical = self._canonical(claim.target)
            if not any(
                definition.allows(alias)
                for owner in owners.get(canonical, {})
                for alias in {claim.target, *names[owner]}
            ):
                self._fail(file, f"{claim.relation} link outside the declared targets: {claim.target}")
        for alias, claimed in sorted(owners.items()):
            file = min(claimed.values(), key=lambda value: (not authored(value), value))
            if len(claimed) > 1:
                self._fail(file, f"ambiguous identity {alias}: " + ", ".join(sorted(claimed)))
            if (parsed := links.parse(alias)) and parsed.brain == config.name and self._folder(parsed.path):
                self._fail(file, f"identity {alias} is also a folder page; rename the entity or folder")

    def addresses(self) -> None:
        for value, files in sorted(self.written.items()):
            parsed = links.parse(value)
            if parsed is None or parsed.brain != self.config.name:
                self.unresolved.add(value)
                continue
            if self._page(parsed.path):
                if parsed.fragment:
                    for file in sorted(files):
                        self._fail(file, f"page cannot select a section: {value}")
                continue
            errors = set()
            resolved = f"{parsed.path}/ACTION.md" if re.fullmatch(r"actions/[^/#]+", parsed.path) else parsed.path
            action_target = bool(re.fullmatch(r"actions/[^/#]+/ACTION\.md", resolved))
            for path in self.owners.get(links.address(parsed.brain, resolved), {}) or {resolved}:
                source, separator, record_id = path.partition(":")
                if path in self.invalid or path in self.skipped:
                    continue
                if path in self.slugs:
                    if parsed.fragment and parsed.fragment not in self.slugs[path]:
                        errors.add("missing BF section")
                elif separator and record_id in self.ids.get(source, set()):
                    if parsed.fragment:
                        errors.add("record cannot select a Markdown section")
                elif (
                    action_target
                    and path == resolved
                    and resolved.removesuffix("/ACTION.md") not in self.skipped
                    and any(authored(file) for file in files)
                ):
                    self.unresolved.add(value)
                    for file in sorted(files):
                        if not authored(file):
                            self._fail(file, f"unresolved BF target: {value}")
                else:
                    errors.add("unresolved BF target")
            for file in sorted(files):
                for error in sorted(errors):
                    self._fail(file, f"{error}: {value}")

    def reply(self) -> dict[str, object]:
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
        warnings.extend([*self.stale_warnings, *self.spellings.warnings()])
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
