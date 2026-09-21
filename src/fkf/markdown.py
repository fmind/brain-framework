"""One Markdown projection for searchable notes and exact section reads."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from pydantic import ValidationError

from fkf.config import yaml_object
from fkf.models import AUTHORED, Error, Item, Knowledge, NoteType, Passage, explain


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
    metadata: str
    attributes: dict[str, object]
    headings: list[Heading]
    links: list[str]


def lines(text: str) -> list[str]:
    """Markdown recognizes CR/LF line endings, not Unicode paragraph separators."""
    return [line for line in re.findall(r"[^\r\n]*(?:\r\n?|\n|$)", text) if line]


def parse(path: str, data: bytes) -> Markdown:
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise Error(f"{path}: note is not UTF-8") from error
    source_lines = lines(text)
    offset = 0
    attributes: dict[str, object] = {}
    metadata = ""
    if source_lines and source_lines[0].rstrip("\r\n") == "---":
        end = next((i for i, line in enumerate(source_lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
        if end is None:
            raise Error(f"{path}: unclosed frontmatter")
        metadata = "".join(source_lines[1:end])
        attributes = yaml_object(metadata.encode())
        offset = end + 1
    body = "".join(source_lines[offset:])
    tokens = MarkdownIt().parse(body)
    headings: list[Heading] = []
    links: list[str] = []
    used: set[str] = set()
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.map is not None:
            inline = tokens[i + 1]
            title = "".join(
                child.content for child in inline.children or [] if child.type in {"text", "code_inline", "image"}
            )
            stem = re.sub(r"[^\w\s-]", "", title.lower())
            stem = re.sub(r"\s", "-", stem)
            slug, suffix = stem, 0
            while slug in used:
                suffix += 1
                slug = f"{stem}-{suffix}"
            used.add(slug)
            headings.append(Heading(slug, title, int(token.tag[1:]), token.map[0] + offset))
        links.extend(str(child.attrGet("href")) for child in token.children or [] if child.type == "link_open")
    return Markdown(text, body, metadata, attributes, headings, links)


def section(path: str, data: bytes, fragment: str) -> str:
    markdown = parse(path, data)
    heading = next((h for h in markdown.headings if h.slug == fragment), None)
    if heading is None:
        raise Error("note heading does not exist")
    source_lines = lines(markdown.text)
    end = next(
        (h.line for h in markdown.headings if h.line > heading.line and h.level <= heading.level), len(source_lines)
    )
    return "".join(source_lines[heading.line : end])


def validate_wiki(path: str, data: bytes) -> None:
    """Check authored OKF v0.2 structure; retrieval remains a tolerant consumer."""
    markdown = parse(path, data)
    attributes = markdown.attributes
    filename = PurePosixPath(path).name
    if filename == "index.md":
        allowed = {"okf_version"} if path == "wiki/index.md" else set()
        if attributes.keys() - allowed:
            raise Error(f"{path}: OKF index frontmatter permits only the bundle-root okf_version")
    elif filename == "log.md":
        if attributes:
            raise Error(f"{path}: keep OKF update logs as dated Markdown, without concept metadata")
        for heading in markdown.headings:
            if heading.level == 2:
                try:
                    if date.fromisoformat(heading.title).isoformat() != heading.title:
                        raise ValueError
                except ValueError:
                    raise Error(f"{path}: OKF log dates must use YYYY-MM-DD") from None
    else:
        if not isinstance(attributes.get("type"), str) or not str(attributes["type"]).strip():
            raise Error(f"{path}: OKF concepts require a nonempty type in YAML frontmatter")
        knowledge = note(path, data).knowledge
        if attributes.get("status", "stable") not in {"draft", "stable", "deprecated"}:
            raise Error(f"{path}: OKF status must be draft, stable or deprecated")
        if any(not event.at for event in knowledge.verified):
            raise Error(f"{path}: OKF verification events require by and at")
        if any(isinstance(source, str) for source in knowledge.sources):
            raise Error(f"{path}: OKF sources require mappings with a resource")


def reference(path: str, target: str) -> str:
    if urlsplit(target).scheme:
        return target
    name, _, fragment = unquote(target).partition("#")
    if name.startswith("/") and path.startswith("wiki/"):
        joined = posixpath.normpath("wiki/" + name.lstrip("/"))
    else:
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), name)) if name else path
    if joined.split("/")[0] not in AUTHORED:
        return target
    return joined + ("#" + fragment if fragment else "")


def note(path: str, data: bytes) -> Item:
    markdown = parse(path, data)
    attributes = markdown.attributes
    try:
        knowledge = Knowledge.model_validate(attributes)
    except ValidationError as error:
        raise Error(f"{path}: invalid knowledge metadata: " + explain(error)) from error
    if not knowledge.type:
        defaults: dict[str, NoteType] = {"projects": "project", "tasks": "task", "wiki": "wiki"}
        knowledge.type = defaults[path.split("/")[0]]
    if path.startswith("wiki/") and PurePosixPath(path).name not in {"index.md", "log.md"} and not knowledge.status:
        knowledge.status = "stable"
    title = attributes.get(
        "title", next((h.title for h in markdown.headings if h.level == 1), PurePosixPath(path).stem)
    )
    if not isinstance(title, str) or not title.strip():
        raise Error(f"{path}: title must be a nonempty string")
    aliases, links = attributes.get("aliases", []), attributes.get("links", [])
    if not isinstance(aliases, list) or not all(isinstance(v, str) for v in aliases):
        raise Error(f"{path}: aliases must be strings")
    if not isinstance(links, list) or not all(isinstance(v, str) for v in links):
        raise Error(f"{path}: links must be strings")
    resource = attributes.get("resource", "")
    if not isinstance(resource, str):
        raise Error(f"{path}: resource must be a string")
    if resource:
        aliases = [*aliases, reference(path, resource)]
    summary = attributes.get("summary", attributes.get("description", ""))
    if not isinstance(summary, str):
        raise Error(f"{path}: summary must be a string")
    # H2+ sections are separate retrieval passages, while exact reads retain their parent document.
    # The authored heading 'History' explicitly marks superseded material, including descendants.
    headings = [heading for heading in markdown.headings if heading.level >= 2]
    source_lines = lines(markdown.text)
    introduction = "".join(source_lines[: headings[0].line]) if headings else markdown.text
    introduction = parse(path, introduction.encode()).body
    passages = [
        Passage(
            title=title,
            text=(summary + "\n\n" + introduction + "\n\n" + markdown.metadata).strip(),
            status=knowledge.status,
        )
    ]
    history_level = 0
    for i, heading in enumerate(headings):
        if history_level and heading.level <= history_level:
            history_level = 0
        if heading.title.casefold() == "history":
            history_level = heading.level
        end = headings[i + 1].line if i + 1 < len(headings) else len(source_lines)
        passages.append(
            Passage(
                fragment=heading.slug,
                title=title + " — " + heading.title,
                text="".join(source_lines[heading.line : end]),
                status="superseded" if history_level else knowledge.status,
            )
        )
    return Item(
        uri=path,
        kind="note",
        title=title,
        text="\n\n".join(p.text for p in passages if p.status not in {"superseded", "archived", "deprecated"})
        or markdown.body,
        path=path,
        aliases=sorted(set(aliases)),
        links=sorted(
            set(links)
            | {
                reference(path, t)
                for t in [
                    *markdown.links,
                    *(s if isinstance(s, str) else s.resource for s in knowledge.sources),
                    *knowledge.supersedes,
                ]
                if t
            }
        ),
        knowledge=knowledge,
        passages=passages,
    )
