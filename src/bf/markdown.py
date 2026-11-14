"""One Markdown projection for searchable notes, exact section reads and link checks."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, suppress
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
    from markdown_it.rules_inline import StateInline
    from markdown_it.token import Token

LEAD, MAX_TITLE = 320, 4096
# No `[` in footnote ID keeps citation scanning linear.
_FOOTNOTE = re.compile(r"([ \t>]*)\[\^([^\[\]\s]+)\]:")
_CITATION = re.compile(r"\[\^([^\[\]\s]+)\]")
_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_TASK = re.compile(r"\[([ xX])\]\s+(\S.*)")
_EMPHASIS = re.compile(r"(?<![\w\]])[*_`]{1,3}|[*_`]{1,3}(?![\w(])")
_CODE = re.compile(r"`+([^`]*)`+")


@dataclass(frozen=True)
class Heading:
    slug: str
    title: str
    level: int
    line: int
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
    attributes: dict[str, object]
    headings: list[Heading]
    contexts: list[tuple[str, str, int]]
    tasks: list[Task] = field(default_factory=list)
    offset: int = 0


@dataclass(frozen=True)
class Passage:
    fragment: str
    title: str
    heading: str
    text: str
    parents: tuple[str, ...] = ()


@dataclass
class Note:
    path: str
    title: str
    knowledge: Knowledge
    lead: str
    passages: list[Passage]
    slugs: set[str] = field(default_factory=set)
    targets: list[str] = field(default_factory=list)
    contexts: list[tuple[str, str, int]] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
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
    from markdown_it import MarkdownIt

    class Parser(MarkdownIt):
        def normalizeLink(self, url: str) -> str:  # noqa: N802 - markdown-it's hook
            return url

        def validateLink(self, url: str) -> bool:  # noqa: ARG002,N802 - markdown-it's hook
            # BF renders no HTML; allow file/data/etc. record-scheme links.
            return True

    parser = Parser()
    parser.inline.ruler.before("text", "bf_pending", _pending)
    return parser


def _pending(state: StateInline, silent: bool) -> bool:
    # Flush long dense inline runs to avoid quadratic string concatenation in markdown-it.
    if not silent and len(state.pending) >= 1024 and state.src[state.pos] != "\n":
        state.pushPending()
    return False


def parse(path: str, data: bytes) -> Markdown:
    try:
        text = data.decode("utf-8").removeprefix("\ufeff")
    except UnicodeError as error:
        raise Error(f"{path}: note is not UTF-8") from error
    source_lines = lines(text)
    for number, value in enumerate(source_lines, 1):
        if re.match(r"^(?:<{7,}|>{7,}|\|{7,})(?: |$)", value.rstrip("\r\n")):
            raise Error(
                f"{path}: line {number}: unresolved merge conflict marker; reconcile both revisions, "
                "or indent a literal example by one space"
            )
    offset = 0
    attributes: dict[str, object] = {}
    if source_lines and source_lines[0].rstrip("\r\n") == "---":
        end = next((i for i, line in enumerate(source_lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
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
    # Rewrite OKF claim footnote definitions into ordinary text lines so inline links stay indexed and validated.
    copy: list[str] = []
    definitions: dict[int, str] = {}
    label = fence = ""
    indent, under = 4, None
    for number, line in enumerate(source_lines[offset:], offset + 1):
        if fence:
            closing = _FENCE.fullmatch(line.rstrip("\r\n"))
            if closing and closing[1][0] == fence[0] and len(closing[1]) >= len(fence) and not closing[2].strip():
                fence = ""
            copy.append(line)
            continue
        lead = line[: len(line) - len(line.lstrip(" \t"))]
        definition = _FOOTNOTE.match(line)
        if definition:
            definitions[number] = definition[2].casefold()
            copy.append(definition[1] + "^:" + line[definition.end() :])
            columns = len(definition[1].expandtabs(4))
            if not (label and columns >= indent):
                indent = columns + 4 if columns >= 4 else 4
            label = "" if ">" in definition[1] else definitions[number]
            quote = definition[1][: definition[1].rfind(">") + 1]
            coded = len(definition[1][len(quote) :].expandtabs(4)) > 4 if quote else indent != 4
            under = None if coded else quote
        elif label and line.strip() and len(lead.expandtabs(4)) >= indent:
            definitions[number] = label
            copy.append(" " * (len(lead.expandtabs(4)) - 4) + line[len(lead) :])
            under = "" if indent == 4 else None
        elif (
            under is not None
            and line.startswith(under)
            and _UNDERLINE.fullmatch((rest := line[len(under) :]).removeprefix(" " if under else "").rstrip("\r\n"))
        ):
            # Neutralize setext underlines below rewritten footnote blocks so they never create phantom headings.
            if "---" in rest:
                copy.append(under + rest.replace("-", "*"))
                label, under = "", None
            else:
                copy.append(under + "\\" + rest.lstrip(" "))
        else:
            under = None
            label = label if not line.strip() else ""
            if (opening := _FENCE.fullmatch(line.rstrip("\r\n"))) and not (opening[1][0] == "`" and "`" in opening[2]):
                fence = opening[1]
            copy.append(line)
    tokens = _parser().parse("".join(copy))
    headings = _headings(path, tokens, offset, strict=okf(path))
    sections = iter(headings)
    found: list[tuple[str, str, int, str]] = []
    cited: dict[str, str] = {}
    tasks: list[Task] = []
    fragment, quoted = "", 0
    for i, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quoted += 1
        elif token.type == "blockquote_close":
            quoted -= 1
        elif token.type == "heading_open" and token.map is not None:
            fragment = next(sections).slug
        if token.type != "inline" or token.map is None:
            continue
        if (
            not quoted
            and i >= 2
            and (tokens[i - 1].type, tokens[i - 2].type) == ("paragraph_open", "list_item_open")
            and (task := _TASK.match(token.content.split("\n", 1)[0]))
        ):
            tasks.append(Task(task[1] != " ", _task(path, task[2])[:LEAD], fragment, token.map[0] + offset + 1))
        at = token.map[0] + offset + 1
        footnote = definitions.get(at, "")
        for child in token.children or []:
            if child.type in {"softbreak", "hardbreak"}:
                at += 1
                footnote = definitions.get(at, footnote)
            elif child.type == "text":
                for citation in _CITATION.findall(child.content):
                    cited.setdefault(citation.casefold(), fragment)
            elif child.type in {"link_open", "image"}:
                target = str(child.attrGet("href" if child.type == "link_open" else "src"))
                if not (child.type == "image" and target.lower().startswith("data:")):
                    found.append((target, fragment, at, footnote))
    contexts = [(target, cited.get(footnote, origin), at) for target, origin, at, footnote in found]
    return Markdown(text, attributes, headings, contexts, tasks, offset)


def _title(path: str, inline: Token, *, strict: bool) -> tuple[str, str]:
    """Heading text and optional trailing `{#id}` anchor read from source."""
    title = "".join(
        " " if child.type in {"softbreak", "hardbreak"} else child.content
        for child in inline.children or []
        if child.type in {"text", "code_inline", "image", "softbreak", "hardbreak"}
    )
    start = inline.content.rfind("{#")
    anchor = (
        re.fullmatch(r"\{#([A-Za-z0-9][A-Za-z0-9_.-]*)\}", inline.content[start:])
        if start > 0 and inline.content[start - 1].isspace()
        else None
    )
    if not anchor:
        return title, ""
    if anchor[1].endswith(".md"):
        if not strict:
            return title, ""
        raise Error(f"{path}: explicit heading anchors cannot end in .md")
    return title.removesuffix(anchor[0]).rstrip(), anchor[1]


def _headings(path: str, tokens: Sequence[Token], offset: int, *, strict: bool) -> list[Heading]:
    """Build headings with unique slugs across explicit anchors and generated `-N` suffixes."""
    titled = [
        (int(token.tag[1:]), token.map[0] + offset, token.map[1] + offset, *_title(path, tokens[i + 1], strict=strict))
        for i, token in enumerate(tokens)
        if token.type == "heading_open" and token.map is not None
    ]
    explicit = [anchor for *_, anchor in titled if anchor]
    reserved = set(explicit)
    if len(reserved) < len(explicit):
        if strict:
            raise Error(f"{path}: duplicate explicit heading anchor")
        kept: set[str] = set()
        for number, (level, start, end, title, anchor) in enumerate(titled):
            if anchor in kept:
                titled[number] = (level, start, end, title, "")
            kept.add(anchor)
    used: set[str] = set()
    last: dict[str, int] = {}
    headings = []
    for level, start, end, title, anchor in titled:
        slug = anchor
        if not anchor:
            stem = slug = slugify(title) or "section"
            if strict and stem in reserved:
                raise Error(f"{path}: duplicate explicit heading anchor")
            suffix = last.get(stem, 0)
            while slug in used or slug in reserved:
                suffix += 1
                slug = f"{stem}-{suffix}"
            last[stem] = suffix
        used.add(slug)
        headings.append(Heading(slug, title, level, start, end))
    return headings


def section(path: str, data: bytes | Markdown, fragment: str) -> str:
    """A note section's Markdown, from its bytes or an existing `Markdown` parse."""
    markdown = data if isinstance(data, Markdown) else parse(path, data)
    heading = next((h for h in markdown.headings if h.slug == fragment), None)
    if heading is None:
        slugs = [f"#{h.slug}" for h in markdown.headings]
        known = ", ".join(slugs[:20]) + (", …" if len(slugs) > 20 else "") if slugs else "none"
        raise Error(f"{path}: heading #{fragment} does not exist; its sections: {known}")
    source_lines = lines(markdown.text)
    end = next(
        (h.line for h in markdown.headings if h.line > heading.line and h.level <= heading.level), len(source_lines)
    )
    return "".join(source_lines[heading.line : end])


def split_ref(ref: str) -> tuple[str, str]:
    """Split a readable ref while preserving literal hashes in filenames."""
    if authored(ref) or "#" not in ref:
        return ref, ""
    path, _, fragment = ref.rpartition("#")
    return path, fragment


def resolve(path: str, target: str) -> tuple[str, str]:
    """Resolve a relative link into a brain-relative path and decoded fragment, splitting before decoding."""
    name, _, fragment = target.partition("#")
    name, fragment = unquote(name), unquote(fragment)
    bundle = path.split("/")[0]
    if name.startswith("/") and bundle in {"projects", "concepts"}:
        joined = posixpath.normpath(bundle + name)
    else:
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), name)) if name else path
    return joined, fragment


def reference(path: str, target: str) -> str:
    """Resolve a relative link to a brain-relative path while canonicalizing identities and URLs."""
    if scheme(path, target):
        return bf_links.target(target)
    name, fragment = resolve(path, target)
    return name + ("#" + fragment if fragment else "")


def scheme(path: str, target: str) -> str:
    try:
        return urlsplit(target).scheme
    except ValueError as error:
        raise Error(f"{path}: invalid link") from error


@contextmanager
def _at(path: str, where: str) -> Generator[None]:
    try:
        yield
    except Error as error:
        raise Error(f"{path}: {where}: {str(error).removeprefix(f'{path}: ')}") from error


def _sources(path: str, attributes: dict[str, object]) -> list[str]:
    sources = attributes.get("sources", [])
    if not isinstance(sources, list) or any(
        not isinstance(source, dict) or not isinstance(source.get("resource"), str) or not source["resource"].strip()
        for source in sources
    ):
        raise Error(f"{path}: OKF sources require mappings with a nonempty resource string")
    if any(len(str(source["resource"])) > 8192 for source in sources):
        raise Error(f"{path}: invalid frontmatter: sources: reference exceeds 8192 characters")
    return [
        resource
        for resource in (str(source["resource"]).strip() for source in sources)
        if not re.search(r"\s", resource)
        and (re.match(IDENTITY, resource) or "/" in resource or resource.endswith(".md"))
    ]


def _task(path: str, markdown: str) -> str:
    text = markdown[: 8 * LEAD]
    words: list[str] = []
    end = 0
    for link in re.finditer(r"\[([^\]]*)\]\(([^)\s]+)\)", text):
        label = _unmarked(link[1])
        with suppress(Error):
            label = f"[{label}]({reference(path, link[2])})"
        words += [_unmarked(text[end : link.start()]), label]
        end = link.end()
    return re.sub(r"\s+", " ", "".join([*words, _unmarked(text[end:])])).strip()


def _plain(markdown: str) -> str:
    prefix = markdown[: 8 * LEAD]
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", prefix)
    text = re.sub(r"^#+\s*|^ {0,3}(?:-[ \t]*){3,}$", "", text, flags=re.MULTILINE)
    plain = re.sub(r"\s+", " ", _unmarked(text)).strip()
    return plain + "…" if plain and len(prefix) < len(markdown) else plain


def preview(text: str, size: int = LEAD) -> str:
    """Trim one-line text to `size` chars on a near word boundary with trailing ellipsis."""
    if len(text) <= size:
        return text
    cut = text[: size - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space >= size // 2 else cut).rstrip() + "…"


def _unmarked(text: str) -> str:
    return "".join(part if i % 2 else _EMPHASIS.sub("", part) for i, part in enumerate(_CODE.split(text)))


_ORDINARY = {"title", "type", "status", "updated", "summary", "description"}


def okf(path: str) -> bool:
    """Projects, concepts and each action's ACTION.md are OKF notes; other authored Markdown is ordinary."""
    return authored(path) and (path.startswith(("projects/", "concepts/")) or action_note(path))


def _knowledge(path: str, attributes: dict[str, object]) -> Knowledge:
    if not okf(path):
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


def note(path: str, data: bytes | Markdown) -> Note:
    if not addressable(path):
        raise Error(f"{path}: path exceeds {MAX_ENCODED} characters once percent-encoded in a BF address; shorten it")
    markdown = data if isinstance(data, Markdown) else parse(path, data)
    knowledge = _knowledge(path, markdown.attributes)
    if not knowledge.type:
        knowledge.type = {"projects": "project", "actions": "action", "concepts": "concept"}[path.split("/")[0]]
    stem = PurePosixPath(path).stem.replace("-", " ")
    title = knowledge.title or next((h.title for h in markdown.headings if h.level == 1), stem)
    if not okf(path) and (len(title) > MAX_TITLE or not title.strip()):
        title = stem if stem.strip() else PurePosixPath(path).name
    if len(title) > MAX_TITLE:
        raise Error(f"{path}: title exceeds {MAX_TITLE} characters")
    if not title.strip():
        raise Error(f"{path}: title must be nonempty")
    source_lines = lines(markdown.text)
    titled = next((h for h in markdown.headings if h.level == 1 and h.title == title), None)
    first = next((n for n, h in enumerate(markdown.headings) if h.level >= 2), len(markdown.headings))
    sections = markdown.headings[first:]
    hidden = range(titled.line, titled.end) if titled else range(0)
    opening = range(markdown.offset, sections[0].line if sections else len(source_lines))
    introduction = "".join(source_lines[n] for n in opening if n not in hidden)
    summary = knowledge.summary or knowledge.description
    passages = [
        Passage("", title, title, "\n\n".join(part.strip() for part in (summary, introduction) if part.strip()))
    ]
    enclosing: list[Heading] = []
    for i, heading in enumerate(sections):
        end = sections[i + 1].line if i + 1 < len(sections) else len(source_lines)
        text = "".join(source_lines[heading.end : end])
        while enclosing and enclosing[-1].level >= heading.level:
            enclosing.pop()
        parents = (title, *(h.title for h in enclosing))
        passages.append(Passage(heading.slug, " — ".join((*parents, heading.title)), heading.title, text, parents))
        enclosing.append(heading)
    lead = _plain(summary or introduction) or (_plain(passages[1].text) if len(passages) > 1 else "")
    sources = _sources(path, markdown.attributes) if okf(path) else []
    for key, values in (("links", knowledge.links), ("sources", sources)):
        for target in values:
            with _at(path, key):
                reference(path, target)
    unsplit: set[str] = set()
    for target, _, line in markdown.contexts:
        try:
            with _at(path, f"line {line}"):
                reference(path, target)
        except Error:
            if okf(path) or target.lower().startswith("bf:"):
                raise
            unsplit.add(target)
    contexts = [context for context in markdown.contexts if context[0] not in unsplit]
    targets = sorted({target for target in [*(t for t, *_ in contexts), *knowledge.links, *sources] if target})
    if knowledge.entity:
        with _at(path, "entity"):
            bf_links.identity(knowledge.entity)
    with _at(path, "aliases"):
        for alias in knowledge.aliases:
            if bf_links.parse(alias):
                bf_links.identity(alias)
    with _at(path, "resource"):
        if re.fullmatch(IDENTITY, knowledge.resource) and bf_links.parse(knowledge.resource):
            bf_links.identity(knowledge.resource)
    return Note(
        path=path,
        title=title,
        knowledge=knowledge,
        lead=preview(lead),
        passages=passages,
        slugs={h.slug for h in markdown.headings},
        targets=targets,
        contexts=contexts,
        tasks=markdown.tasks,
        sources=sources,
    )


def broken(
    note: Note, exists: Callable[[str], bool], slugs: dict[str, set[str]], unresolved: set[str] | None = None
) -> list[str]:
    """This note's relative links that name no brain file or heading."""
    problems = []
    for target in note.targets:
        if scheme(note.path, target) or target == "#":
            continue
        name, fragment = resolve(note.path, target)
        entry = name + "/ACTION.md" if re.fullmatch(r"actions/[^/#]+", name) else name
        if name == ".." or name.startswith("../"):
            problems.append(f"link leaves the brain: {target}")
        elif name.startswith("/"):
            problems.append(f"absolute link; use a path relative to this file: {target}")
        elif re.fullmatch(r"actions/[^/#]+(?:/ACTION\.md)?", name) and (not exists(name) or not exists(entry)):
            if unresolved is not None:
                unresolved.add(reference(note.path, target))
        elif not exists(name):
            problems.append(f"broken link: {target}")
        elif fragment and entry in slugs and fragment not in slugs[entry]:
            problems.append(f"missing heading: {target}")
    return problems


def validate_okf(path: str, data: bytes | Markdown) -> None:
    """Check project, concept and action OKF v0.2 structure."""
    markdown = data if isinstance(data, Markdown) else parse(path, data)
    attributes = markdown.attributes
    filename = PurePosixPath(path).name
    if filename == "index.md":
        allowed = {"okf_version"} if path in {"projects/index.md", "concepts/index.md"} else set()
        if attributes.keys() - allowed:
            raise Error(f"{path}: OKF index frontmatter permits only the bundle-root okf_version")
        return
    if filename == "log.md":
        if attributes:
            raise Error(f"{path}: keep OKF update logs as dated Markdown, without concept metadata")
        for heading in markdown.headings:
            if heading.level == 2:
                try:
                    if date.fromisoformat(heading.title).isoformat() != heading.title:
                        raise ValueError
                except ValueError:
                    raise Error(f"{path}: OKF log dates must use YYYY-MM-DD") from None
        return
    if not isinstance(attributes.get("type"), str) or not str(attributes["type"]).strip():
        raise Error(f"{path}: OKF documents require a nonempty type in YAML frontmatter")
    status = attributes.get("status", "stable")
    if not isinstance(status, str) or status not in {"draft", "stable", "deprecated"}:
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
    """Emacs `.#NAME` lock files beside edited notes."""
    return path.rsplit("/", 1)[-1].startswith(".#")


def action_note(path: str) -> bool:
    """Only direct action subfolders hold `ACTION.md`; inputs/ and outputs/ may hold other notes."""
    parts = path.split("/")
    return len(parts) == 3 and parts[0] == "actions" and parts[2] == "ACTION.md"


def entry_note(path: str) -> bool:
    """OKF knowledge/work entries excluding bundle indexes and update logs."""
    return okf(path) and path.rsplit("/", 1)[-1] not in {"index.md", "log.md"}
