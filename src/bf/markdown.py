"""One Markdown projection for searchable notes, exact section reads and link checks."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from bf import links as bf_links
from bf.config import yaml_object
from bf.models import AUTHORED, IDENTITY, MAX_ENCODED, Error, Knowledge, addressable, explain

if TYPE_CHECKING:
    from markdown_it import MarkdownIt

LEAD = 320
# Record titles share this bound: a note title is copied into every listing and search row.
MAX_TITLE = 4096
_FOOTNOTE = re.compile(r"^\[\^([^\]\s]+)\]:", re.MULTILINE)
_TASK = re.compile(r"\[([ xX])\]\s+(\S.*)")


@dataclass(frozen=True)
class Heading:
    slug: str
    title: str
    level: int
    line: int
    # The first line after the heading; a setext heading spans two lines.
    end: int


@dataclass(frozen=True)
class Task:
    done: bool
    text: str
    fragment: str
    line: int


@dataclass
class Markdown:
    text: str
    body: str
    attributes: dict[str, object]
    headings: list[Heading]
    links: list[str]
    contexts: list[tuple[str, str]]
    # Task list items in document order, with their containing section and source line.
    tasks: list[Task] = field(default_factory=list)
    # The first body line after any frontmatter.
    offset: int = 0


@dataclass(frozen=True)
class Passage:
    fragment: str
    title: str
    heading: str
    text: str


@dataclass
class Note:
    path: str
    title: str
    knowledge: Knowledge
    lead: str
    passages: list[Passage]
    slugs: set[str] = field(default_factory=set)
    targets: list[str] = field(default_factory=list)
    contexts: list[tuple[str, str]] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    # OKF `sources` resources: whole-note `cites` claims unless a BF link names another role.
    sources: list[str] = field(default_factory=list)

    @property
    def links(self) -> list[str]:
        return sorted({reference(self.path, t) for t in self.targets})


def lines(text: str) -> list[str]:
    """Markdown recognizes CR/LF line endings, not Unicode paragraph separators."""
    return [line for line in re.findall(r"[^\r\n]*(?:\r\n?|\n|$)", text) if line]


def slugify(title: str) -> str:
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", title.lower()))


@cache
def _parser() -> MarkdownIt:
    """One CommonMark parser, imported on first use: a fresh cache answers searches and pages without it."""
    from markdown_it import MarkdownIt

    class Parser(MarkdownIt):
        def normalizeLink(self, url: str) -> str:  # noqa: N802 - markdown-it's hook
            # Keep destinations as written, like frontmatter links: BF parses its own addresses and resolve()
            # decodes relative paths once, so percent-encoding here would change identities and record refs.
            return url

    return Parser()


def parse(path: str, data: bytes) -> Markdown:
    try:
        # A byte order mark would hide the frontmatter; removing it keeps every line number.
        text = data.decode("utf-8").removeprefix("\ufeff")
    except UnicodeError as error:
        raise Error(f"{path}: note is not UTF-8") from error
    source_lines = lines(text)
    for number, value in enumerate(source_lines, 1):
        # Fail closed inside code blocks too: a real conflict can land there.
        if re.match(r"^(?:<{7,}|>{7,}|\|{7,})(?: |$)", value.rstrip("\r\n")):
            raise Error(
                f"{path}: line {number}: unresolved merge conflict marker; reconcile both revisions, "
                "or indent a literal example by one space"
            )
    offset = 0
    attributes: dict[str, object] = {}
    if source_lines and source_lines[0].rstrip("\r\n") == "---":
        end = next((i for i, line in enumerate(source_lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
        # Ordinary Markdown, such as a document copied from another tool, stays searchable without foreign
        # frontmatter it cannot parse: an unclosed block is text, and an invalid one is skipped whole.
        if end is None:
            if okf(path):
                raise Error(f"{path}: unclosed frontmatter")
        else:
            offset = end + 1
            try:
                attributes = yaml_object("".join(source_lines[1:end]).encode(), path, line=2)
            except Error:
                if okf(path):
                    raise
    body = "".join(source_lines[offset:])
    # OKF claim footnotes (`[^id]: [Source](ref)`) are not link reference definitions in CommonMark;
    # parse them as ordinary lines so their links are checked, on a copy with the same line numbers.
    tokens = _parser().parse(_FOOTNOTE.sub(r"\1:", body))
    headings: list[Heading] = []
    links: list[str] = []
    contexts: list[tuple[str, str]] = []
    tasks: list[Task] = []
    used: set[str] = set()
    explicit: set[str] = set()
    fragment = ""
    quoted = 0
    for i, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quoted += 1
        elif token.type == "blockquote_close":
            quoted -= 1
        if token.type == "heading_open" and token.map is not None:
            inline = tokens[i + 1]
            title = "".join(
                child.content for child in inline.children or [] if child.type in {"text", "code_inline", "image"}
            )
            # An anchor id holds no `{` or `#`, so only the last `{#` can start one: no regex backtracking.
            start = title.rfind("{#")
            anchor = (
                re.fullmatch(r"\{#([A-Za-z0-9][A-Za-z0-9_.-]*)\}", title[start:])
                if start > 0 and title[start - 1].isspace()
                else None
            )
            if anchor:
                title = title[:start].rstrip()
                if anchor[1].endswith(".md"):
                    # `note.md#part.md` would read as a file name, not a section.
                    raise Error(f"{path}: explicit heading anchors cannot end in .md")
            # A heading without word characters still needs an addressable, non-empty slug.
            stem = slug = anchor[1] if anchor else slugify(title) or "section"
            if slug in used and (anchor or slug in explicit):
                raise Error(f"{path}: duplicate explicit heading anchor")
            if anchor:
                explicit.add(slug)
            suffix = 0
            while slug in used:
                suffix += 1
                slug = f"{stem}-{suffix}"
            used.add(slug)
            headings.append(Heading(slug, title, int(token.tag[1:]), token.map[0] + offset, token.map[1] + offset))
            fragment = slug
        # A task is an unquoted list paragraph starting with [ ] or [x]; code blocks never produce one.
        if (
            token.type == "inline"
            and not quoted
            and token.map is not None
            and i >= 2
            and (tokens[i - 1].type, tokens[i - 2].type) == ("paragraph_open", "list_item_open")
            and (task := _TASK.match(token.content.split("\n", 1)[0]))
        ):
            tasks.append(Task(task[1] != " ", _plain(task[2])[:LEAD], fragment, token.map[0] + offset + 1))
        for child in token.children or []:
            # An embedded image is a link too, so validation catches a missing asset; inline data names no target.
            if child.type not in {"link_open", "image"}:
                continue
            target = str(child.attrGet("href" if child.type == "link_open" else "src"))
            if child.type == "image" and target.lower().startswith("data:"):
                continue
            # Split local URL fragments before decoding: %23 is part of a filename.
            # BF parses each component before decoding; external queries keep their original meaning.
            links.append(target)
            contexts.append((target, fragment))
    return Markdown(text, body, attributes, headings, links, contexts, tasks, offset)


def section(path: str, data: bytes, fragment: str) -> str:
    markdown = parse(path, data)
    heading = next((h for h in markdown.headings if h.slug == fragment), None)
    if heading is None:
        raise Error(f"{path}: heading #{fragment} does not exist")
    source_lines = lines(markdown.text)
    end = next(
        (h.line for h in markdown.headings if h.line > heading.line and h.level <= heading.level), len(source_lines)
    )
    return "".join(source_lines[heading.line : end])


def split_ref(ref: str) -> tuple[str, str]:
    """Split a readable ref, preserving hashes in Markdown filenames and directory names."""
    if authored(ref) or "#" not in ref:
        return ref, ""
    path, _, fragment = ref.rpartition("#")
    return path, fragment


def resolve(path: str, target: str) -> tuple[str, str]:
    """A relative link as a brain-relative path and a fragment, split at the link's first unescaped `#`.

    Splitting before decoding keeps a literal `%23` in a file name, such as `data%231.csv`, in the path.
    """
    name, _, fragment = target.partition("#")
    name, fragment = unquote(name), unquote(fragment)
    bundle = path.split("/")[0]
    if name.startswith("/") and bundle in {"projects", "concepts"}:
        # OKF bundle-relative links start at the bundle root: each of these folders is one bundle.
        # Actions and their attachments have no bundle root, so a leading `/` stays an absolute path there.
        joined = posixpath.normpath(bundle + name)
    else:
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), name)) if name else path
    return joined, fragment


def reference(path: str, target: str) -> str:
    """Resolve a relative note link to a brain-relative path; identities and URLs stay unchanged."""
    if scheme(path, target):
        return bf_links.target(target)
    name, fragment = resolve(path, target)
    return name + ("#" + fragment if fragment else "")


def scheme(path: str, target: str) -> str:
    """Malformed URLs are file diagnostics, never exceptions escaping retrieval."""
    try:
        return urlsplit(target).scheme
    except ValueError as error:
        raise Error(f"{path}: invalid link") from error


def _sources(path: str, attributes: dict[str, object]) -> list[str]:
    sources = attributes.get("sources", [])
    if not isinstance(sources, list) or any(
        not isinstance(source, dict) or not isinstance(source.get("resource"), str) or not source["resource"].strip()
        for source in sources
    ):
        raise Error(f"{path}: OKF sources require mappings with a nonempty resource string")
    return [str(source["resource"]) for source in sources]


def _plain(markdown: str) -> str:
    """A compact, readable lead: drop headings markers, rules, emphasis and link targets.

    Callers keep at most LEAD characters; a bounded prefix keeps the link pattern's cost independent of note size.
    """
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown[: 8 * LEAD])
    text = re.sub(r"^#+\s*|^ {0,3}(?:-[ \t]*){3,}$", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`]{1,3}", "", text)
    return re.sub(r"\s+", " ", text).strip()


# Display and lifecycle metadata, the only frontmatter ordinary Markdown contributes.
_ORDINARY = {"title", "type", "status", "updated", "summary", "description"}


def okf(path: str) -> bool:
    """Projects, concepts and each action's ACTION.md are OKF notes; other authored Markdown is ordinary."""
    return authored(path) and (path.startswith(("projects/", "concepts/")) or action_note(path))


def _knowledge(path: str, attributes: dict[str, object]) -> Knowledge:
    if not okf(path):
        # Ordinary Markdown, such as a document copied into an action's inputs, declares no identity, tag, link or
        # review date. Its other metadata applies only where valid, so foreign frontmatter stays searchable.
        attributes = {key: value for key, value in attributes.items() if key in _ORDINARY}
        try:
            return Knowledge.model_validate(attributes)
        except ValidationError as error:
            invalid = {e["loc"][0] for e in error.errors() if e["loc"]}
            attributes = {key: value for key, value in attributes.items() if key not in invalid}
    try:
        return Knowledge.model_validate(attributes)
    except ValidationError as error:
        raise Error(f"{path}: invalid frontmatter: " + explain(error)) from error


def note(path: str, data: bytes) -> Note:
    if not addressable(path):
        # Every note has a BF address: links, backlinks and identities name it by that address.
        raise Error(f"{path}: path exceeds {MAX_ENCODED} characters once percent-encoded in a BF address; shorten it")
    markdown = parse(path, data)
    knowledge = _knowledge(path, markdown.attributes)
    if not knowledge.type:
        knowledge.type = {"projects": "project", "actions": "action", "concepts": "concept"}[path.split("/")[0]]
    stem = PurePosixPath(path).stem.replace("-", " ")
    title = knowledge.title or next((h.title for h in markdown.headings if h.level == 1), stem)
    if not okf(path) and (len(title) > MAX_TITLE or not title.strip()):
        # Ordinary Markdown stays searchable: an overlong or blank title gives way to the file name.
        title = stem if stem.strip() else PurePosixPath(path).name
    if len(title) > MAX_TITLE:
        raise Error(f"{path}: title exceeds {MAX_TITLE} characters")
    if not title.strip():
        raise Error(f"{path}: title must be nonempty")
    # H2+ sections are separate passages, so a search can land on the answering section.
    # Headings rank through each passage's title; its text, and so its excerpt, starts below them.
    source_lines = lines(markdown.text)
    sections = [h for h in markdown.headings if h.level >= 2]
    titled = next((h for h in markdown.headings if h.level == 1 and h.title == title), None)
    hidden = range(titled.line, titled.end) if titled else range(0)
    opening = range(markdown.offset, sections[0].line if sections else len(source_lines))
    introduction = "".join(source_lines[n] for n in opening if n not in hidden)
    summary = knowledge.summary or knowledge.description
    passages = [
        Passage("", title, title, "\n\n".join(part.strip() for part in (summary, introduction) if part.strip()))
    ]
    for i, heading in enumerate(sections):
        end = sections[i + 1].line if i + 1 < len(sections) else len(source_lines)
        text = "".join(source_lines[heading.end : end])
        passages.append(Passage(heading.slug, f"{title} — {heading.title}", heading.title, text))
    lead = _plain(summary or introduction)
    if not lead and len(passages) > 1:
        lead = _plain(passages[1].text)
    # OKF provenance cites its resources in each authored note format.
    # Working inputs and outputs remain ordinary Markdown, even when their filename is ACTION.md.
    sources = _sources(path, markdown.attributes) if okf(path) else []
    targets = sorted({target for target in [*markdown.links, *knowledge.links, *sources] if target})
    for target in targets:
        reference(path, target)
    if knowledge.entity:
        bf_links.identity(knowledge.entity)
    for alias in knowledge.aliases:
        if bf_links.parse(alias):
            bf_links.identity(alias)
    return Note(
        path=path,
        title=title,
        knowledge=knowledge,
        lead=lead[:LEAD],
        passages=passages,
        slugs={h.slug for h in markdown.headings},
        targets=targets,
        contexts=[*markdown.contexts, *((target, "") for target in knowledge.links)],
        tasks=markdown.tasks,
        sources=sources,
    )


def broken(note: Note, exists: Callable[[str], bool], slugs: dict[str, set[str]]) -> list[str]:
    """This note's relative links that name no brain file or, for notes, no heading; the caller names the note."""
    problems = []
    for target in note.targets:
        if scheme(note.path, target) or target == "#":
            continue
        name, fragment = resolve(note.path, target)
        if name == ".." or name.startswith("../"):
            problems.append(f"link leaves the brain: {target}")
        elif name.startswith("/"):
            problems.append(f"absolute link; use a path relative to this file: {target}")
        elif not exists(name):
            problems.append(f"broken link: {target}")
        elif fragment and name in slugs and fragment not in slugs[name]:
            problems.append(f"missing heading: {target}")
    return problems


def validate_okf(path: str, data: bytes) -> None:
    """Check project, concept and action OKF v0.2 structure; retrieval remains tolerant."""
    attributes = parse(path, data).attributes
    filename = PurePosixPath(path).name
    if filename == "index.md":
        allowed = {"okf_version"} if path in {"projects/index.md", "concepts/index.md"} else set()
        if attributes.keys() - allowed:
            raise Error(f"{path}: OKF index frontmatter permits only the bundle-root okf_version")
        return
    if filename == "log.md":
        if attributes:
            raise Error(f"{path}: keep OKF update logs as dated Markdown, without concept metadata")
        for heading in parse(path, data).headings:
            if heading.level == 2:
                try:
                    if date.fromisoformat(heading.title).isoformat() != heading.title:
                        raise ValueError
                except ValueError:
                    raise Error(f"{path}: OKF log dates must use YYYY-MM-DD") from None
        return
    if not isinstance(attributes.get("type"), str) or not str(attributes["type"]).strip():
        raise Error(f"{path}: OKF documents require a nonempty type in YAML frontmatter")
    if attributes.get("status", "stable") not in {"draft", "stable", "deprecated"}:
        raise Error(f"{path}: OKF status must be draft, stable or deprecated")
    aliases = attributes.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(v, str) and re.fullmatch(IDENTITY, v) for v in aliases):
        raise Error(f"{path}: OKF aliases must be namespaced identities, such as person:email/alice@example.test")
    _sources(path, attributes)
    verified = attributes.get("verified", [])
    events = [verified] if isinstance(verified, dict) else verified
    if not isinstance(events, list) or any(
        not isinstance(e, dict) or not e.get("by") or not e.get("at") for e in events
    ):
        raise Error(f"{path}: OKF verification events require by and at")


def authored(path: str) -> bool:
    return path.split("/")[0] in AUTHORED and path.endswith(".md")


def editor_lock(path: str) -> bool:
    """An Emacs lock, `.#NAME`: a dangling link or small file kept beside an edited file, never a note or input."""
    return path.rsplit("/", 1)[-1].startswith(".#")


def action_note(path: str) -> bool:
    """Only folders directly below actions/ hold an ACTION.md; inputs/ and outputs/ may hold other notes."""
    parts = path.split("/")
    return len(parts) == 3 and parts[0] == "actions" and parts[2] == "ACTION.md"


def entry_note(path: str) -> bool:
    """OKF notes that hold knowledge or work, not bundle indexes or update logs: they rank, remind and queue tasks."""
    return okf(path) and path.rsplit("/", 1)[-1] not in {"index.md", "log.md"}
