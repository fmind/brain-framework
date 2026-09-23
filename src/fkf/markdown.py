"""One Markdown projection for searchable notes, exact section reads and link checks."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from pydantic import ValidationError

from fkf.config import yaml_object
from fkf.models import AUTHORED, Error, Knowledge, explain

LEAD = 320
_FOOTNOTE = re.compile(r"^\[\^([^\]\s]+)\]:", re.MULTILINE)


@dataclass(frozen=True)
class Heading:
    slug: str
    title: str
    level: int
    line: int


@dataclass
class Markdown:
    text: str
    body: str
    attributes: dict[str, object]
    headings: list[Heading]
    links: list[str]


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
    used: set[str] = set()
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.map is not None:
            inline = tokens[i + 1]
            title = "".join(
                child.content for child in inline.children or [] if child.type in {"text", "code_inline", "image"}
            )
            stem = slug = slugify(title)
            suffix = 0
            while slug in used:
                suffix += 1
                slug = f"{stem}-{suffix}"
            used.add(slug)
            headings.append(Heading(slug, title, int(token.tag[1:]), token.map[0] + offset))
        # markdown-it percent-encodes hrefs; keep the literal target so identities match exactly.
        links.extend(unquote(str(child.attrGet("href"))) for child in token.children or [] if child.type == "link_open")
    return Markdown(text, body, attributes, headings, links)


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


def reference(path: str, target: str) -> str:
    """Resolve a relative note link to a base-relative path; identities and URLs stay unchanged."""
    if urlsplit(target).scheme:
        return target
    name, _, fragment = unquote(target).partition("#")
    if name.startswith("/") and path.startswith("wiki/"):
        joined = posixpath.normpath("wiki/" + name.lstrip("/"))
    else:
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), name)) if name else path
    return joined + ("#" + fragment if fragment else "")


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
        knowledge.type = {"projects": "project", "tasks": "task", "wiki": "wiki"}[path.split("/")[0]]
    title = knowledge.title or next(
        (h.title for h in markdown.headings if h.level == 1), PurePosixPath(path).stem.replace("-", " ")
    )
    if not title.strip():
        raise Error(f"{path}: title must be nonempty")
    # H2+ sections are separate passages, so a search can land on the answering section.
    source_lines = lines(markdown.text)
    sections = [h for h in markdown.headings if h.level >= 2]
    introduction = parse(path, "".join(source_lines[: sections[0].line] if sections else source_lines).encode()).body
    summary = knowledge.summary or knowledge.description
    passages = [
        Passage("", title, title, "\n\n".join(part.strip() for part in (summary, introduction) if part.strip()))
    ]
    for i, heading in enumerate(sections):
        end = sections[i + 1].line if i + 1 < len(sections) else len(source_lines)
        text = "".join(source_lines[heading.line : end])
        passages.append(Passage(heading.slug, f"{title} — {heading.title}", heading.title, text))
    lead = _plain(summary or introduction.replace(f"# {title}", "", 1))
    if not lead and len(passages) > 1:
        lead = _plain(passages[1].text)
    return Note(
        path=path,
        title=title,
        knowledge=knowledge,
        lead=lead[:LEAD],
        passages=passages,
        slugs={h.slug for h in markdown.headings},
        targets=sorted({t for t in [*markdown.links, *knowledge.links] if t}),
    )


def broken(note: Note, exists: set[str], slugs: dict[str, set[str]]) -> list[str]:
    """Relative links must name an existing base file and, for notes, an existing heading."""
    problems = []
    for target in note.targets:
        if urlsplit(target).scheme or target == "#":
            continue
        name, _, fragment = reference(note.path, target).partition("#")
        if name == ".." or name.startswith("../"):
            problems.append(f"{note.path}: link leaves the base: {target}")
        elif name not in exists:
            problems.append(f"{note.path}: broken link: {target}")
        elif fragment and name in slugs and fragment not in slugs[name]:
            problems.append(f"{note.path}: missing heading: {target}")
    return problems


def validate_wiki(path: str, data: bytes) -> None:
    """Check authored OKF v0.2 structure; retrieval remains a tolerant consumer."""
    attributes = parse(path, data).attributes
    filename = PurePosixPath(path).name
    if filename == "index.md":
        allowed = {"okf_version"} if path == "wiki/index.md" else set()
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
    sources = attributes.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(s, dict) or not s.get("resource") for s in sources):
        raise Error(f"{path}: OKF sources require mappings with a resource")
    verified = attributes.get("verified", [])
    events = [verified] if isinstance(verified, dict) else verified
    if not isinstance(events, list) or any(
        not isinstance(e, dict) or not e.get("by") or not e.get("at") for e in events
    ):
        raise Error(f"{path}: OKF verification events require by and at")


def authored(path: str) -> bool:
    return path.split("/")[0] in AUTHORED and path.endswith(".md")
