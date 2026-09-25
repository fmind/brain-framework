"""One Markdown projection for searchable notes, exact section reads and link checks."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from pydantic import ValidationError

from bf import links as bf_links
from bf.config import yaml_object
from bf.models import AUTHORED, Error, Knowledge, explain

LEAD = 320
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


@dataclass
class Markdown:
    text: str
    body: str
    attributes: dict[str, object]
    headings: list[Heading]
    links: list[str]
    contexts: list[tuple[str, str]]
    # Task list items in document order: (done, text).
    tasks: list[tuple[bool, str]] = field(default_factory=list)


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
    tasks: list[tuple[bool, str]] = field(default_factory=list)

    @property
    def links(self) -> list[str]:
        return sorted({reference(self.path, t) for t in self.targets})


def lines(text: str) -> list[str]:
    """Markdown recognizes CR/LF line endings, not Unicode paragraph separators."""
    return [line for line in re.findall(r"[^\r\n]*(?:\r\n?|\n|$)", text) if line]


def slugify(title: str) -> str:
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", title.lower()))


def parse(path: str, data: bytes) -> Markdown:
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise Error(f"{path}: note is not UTF-8") from error
    source_lines = lines(text)
    offset = 0
    attributes: dict[str, object] = {}
    if source_lines and source_lines[0].rstrip("\r\n") == "---":
        end = next((i for i, line in enumerate(source_lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
        if end is None:
            raise Error(f"{path}: unclosed frontmatter")
        try:
            attributes = yaml_object("".join(source_lines[1:end]).encode())
        except Error as error:
            raise Error(f"{path}: {error}") from error
        offset = end + 1
    body = "".join(source_lines[offset:])
    # OKF claim footnotes (`[^id]: [Source](ref)`) are not link reference definitions in CommonMark;
    # parse them as ordinary lines so their links are checked, on a copy with the same line numbers.
    tokens = MarkdownIt().parse(_FOOTNOTE.sub(r"\1:", body))
    headings: list[Heading] = []
    links: list[str] = []
    contexts: list[tuple[str, str]] = []
    tasks: list[tuple[bool, str]] = []
    used: set[str] = set()
    explicit: set[str] = set()
    fragment = ""
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.map is not None:
            inline = tokens[i + 1]
            title = "".join(
                child.content for child in inline.children or [] if child.type in {"text", "code_inline", "image"}
            )
            anchor = re.search(r"\s+\{#([A-Za-z0-9][A-Za-z0-9_.-]*)\}$", title)
            if anchor:
                title = title[: anchor.start()]
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
        # A task is a list item whose paragraph starts with [ ] or [x]; code blocks never produce one.
        if (
            token.type == "inline"
            and i >= 2
            and (tokens[i - 1].type, tokens[i - 2].type) == ("paragraph_open", "list_item_open")
            and (task := _TASK.match(token.content.split("\n", 1)[0]))
        ):
            tasks.append((task[1] != " ", _plain(task[2])[:LEAD]))
        for child in token.children or []:
            if child.type == "link_open":
                target = str(child.attrGet("href"))
                # Split local URL fragments before decoding: %23 is part of a filename.
                # BF parses each component before decoding; external queries keep their original meaning.
                links.append(target)
                contexts.append((target, fragment))
    return Markdown(text, body, attributes, headings, links, contexts, tasks)


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
    if name.startswith("/") and path.startswith("concepts/"):
        joined = posixpath.normpath("concepts/" + name.lstrip("/"))
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
    """A compact, readable lead: drop headings markers, emphasis and link targets."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`]{1,3}", "", text)
    return re.sub(r"\s+", " ", text).strip()


def note(path: str, data: bytes) -> Note:
    markdown = parse(path, data)
    try:
        knowledge = Knowledge.model_validate(markdown.attributes)
    except ValidationError as error:
        raise Error(f"{path}: invalid frontmatter: " + explain(error)) from error
    if not knowledge.type:
        knowledge.type = {"projects": "project", "actions": "action", "concepts": "concept"}[path.split("/")[0]]
    title = knowledge.title or next(
        (h.title for h in markdown.headings if h.level == 1), PurePosixPath(path).stem.replace("-", " ")
    )
    if not title.strip():
        raise Error(f"{path}: title must be nonempty")
    # H2+ sections are separate passages, so a search can land on the answering section.
    # Headings rank through each passage's title; its text, and so its excerpt, starts below them.
    source_lines = lines(markdown.text)
    sections = [h for h in markdown.headings if h.level >= 2]
    titled = next((h for h in markdown.headings if h.level == 1 and h.title == title), None)
    hidden = range(titled.line, titled.end) if titled else range(0)
    opening = [line for n, line in enumerate(source_lines[: sections[0].line if sections else None]) if n not in hidden]
    introduction = parse(path, "".join(opening).encode()).body
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
    targets = sorted(
        {
            target
            for target in [
                *markdown.links,
                *knowledge.links,
                *(_sources(path, markdown.attributes) if path.startswith("concepts/") else []),
            ]
            if target
        }
    )
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
    )


def broken(note: Note, exists: Callable[[str], bool], slugs: dict[str, set[str]]) -> list[str]:
    """Relative links must name an existing brain file and, for notes, an existing heading."""
    problems = []
    for target in note.targets:
        if scheme(note.path, target) or target == "#":
            continue
        name, fragment = resolve(note.path, target)
        if name == ".." or name.startswith("../"):
            problems.append(f"{note.path}: link leaves the brain: {target}")
        elif not exists(name):
            problems.append(f"{note.path}: broken link: {target}")
        elif fragment and name in slugs and fragment not in slugs[name]:
            problems.append(f"{note.path}: missing heading: {target}")
    return problems


def validate_concept(path: str, data: bytes) -> None:
    """Check authored OKF v0.2 structure; retrieval remains a tolerant consumer."""
    attributes = parse(path, data).attributes
    filename = PurePosixPath(path).name
    if filename == "index.md":
        allowed = {"okf_version"} if path == "concepts/index.md" else set()
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
        raise Error(f"{path}: OKF concepts require a nonempty type in YAML frontmatter")
    if attributes.get("status", "stable") not in {"draft", "stable", "deprecated"}:
        raise Error(f"{path}: OKF status must be draft, stable or deprecated")
    _sources(path, attributes)
    verified = attributes.get("verified", [])
    events = [verified] if isinstance(verified, dict) else verified
    if not isinstance(events, list) or any(
        not isinstance(e, dict) or not e.get("by") or not e.get("at") for e in events
    ):
        raise Error(f"{path}: OKF verification events require by and at")


def authored(path: str) -> bool:
    return path.split("/")[0] in AUTHORED and path.endswith(".md")
