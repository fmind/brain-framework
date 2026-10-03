"""One Markdown projection for searchable notes, exact section reads and link checks."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterator, Sequence
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
    from markdown_it.token import Token

LEAD = 320
# Record titles share this bound: a note title is copied into every listing and search row.
MAX_TITLE = 4096
# An OKF claim footnote's definition, `[^id]:`, at the start of a line, after a list item's indentation or a quote.
# An id holds no `[`: each citation scan then stops at the next one, so a long run of `[^` parses in linear time.
_FOOTNOTE = re.compile(r"([ \t>]*)\[\^([^\[\]\s]+)\]:")
_CITATION = re.compile(r"\[\^([^\[\]\s]+)\]")
# A footnote's continuation line: indented by four spaces or a tab, which it loses in the parsed copy.
_CONTINUED = re.compile(r" {4}|\t")
_TASK = re.compile(r"\[([ xX])\]\s+(\S.*)")
# Emphasis markers open or close a word; inside one, `_` and `*` belong to identifiers and arithmetic (MAX_SIZE, 2*3).
_EMPHASIS = re.compile(r"(?<![\w\]])[*_`]{1,3}|[*_`]{1,3}(?![\w(])")
_CODE = re.compile(r"`+([^`]*)`+")


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
    attributes: dict[str, object]
    headings: list[Heading]
    # Each link as written, with the section it supports and its source line.
    contexts: list[tuple[str, str, int]]
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
    # A section's note title and enclosing headings, such as ("Plans", "Vega") above `### Budget`.
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
    # Each body link as written, with the section it supports and its source line; frontmatter `links` and OKF
    # `sources` support the whole note.
    contexts: list[tuple[str, str, int]] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    # OKF `sources` resources: whole-note `cites` claims unless a BF link names another relation.
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
    # OKF claim footnotes (`[^id]: [Source](ref)`) are not link reference definitions in CommonMark;
    # parse them as ordinary lines so their links are checked, on a copy with the same line numbers. A fixed
    # marker replaces the label, which could otherwise open a fence or an HTML block, such as [^```] or [^<pre>].
    copy: list[str] = []
    # The footnote each definition line starts, or an indented paragraph continues: its links support the section
    # that first cites it, not the section where the definition happens to sit, usually the last one. Labels match
    # case-insensitively, as on GitHub.
    definitions: dict[int, str] = {}
    label = ""
    for number, line in enumerate(source_lines[offset:], offset + 1):
        definition = _FOOTNOTE.match(line)
        if definition:
            definitions[number] = definition[2].casefold()
            copy.append(definition[1] + "^:" + line[definition.end() :])
            # Only a definition outside a block quote continues on indented lines.
            label = "" if ">" in definition[1] else definitions[number]
        elif label and (continued := _CONTINUED.match(line)):
            definitions[number] = label
            copy.append(line[continued.end() :])
        else:
            # A blank line may separate a footnote's paragraphs; any other unindented line ends it.
            label = label if not line.strip() else ""
            copy.append(line)
    tokens = _parser().parse("".join(copy))
    headings = _headings(path, tokens, offset, strict=okf(path))
    sections = iter(headings)
    # Each link with its section, line and footnote, then the section first citing each footnote.
    found: list[tuple[str, str, int, str]] = []
    cited: dict[str, str] = {}
    tasks: list[Task] = []
    fragment = ""
    quoted = 0
    for i, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quoted += 1
        elif token.type == "blockquote_close":
            quoted -= 1
        elif token.type == "heading_open" and token.map is not None:
            fragment = next(sections).slug
        if token.type != "inline" or token.map is None:
            continue
        # A task is an unquoted list paragraph starting with [ ] or [x]; code blocks never produce one.
        if (
            not quoted
            and i >= 2
            and (tokens[i - 1].type, tokens[i - 2].type) == ("paragraph_open", "list_item_open")
            and (task := _TASK.match(token.content.split("\n", 1)[0]))
        ):
            tasks.append(Task(task[1] != " ", _task(path, task[2])[:LEAD], fragment, token.map[0] + offset + 1))
        # Each line break moves to the next source line, so a link names its own line; a code span, image
        # description or HTML tag spanning lines can only make it an earlier line of the same paragraph. A footnote
        # holds the lines from its definition on.
        at = token.map[0] + offset + 1
        footnote = definitions.get(at, "")
        for child in token.children or []:
            if child.type in {"softbreak", "hardbreak"}:
                at += 1
                footnote = definitions.get(at, footnote)
            elif child.type == "text":
                # Only text cites: `[^1]` in a code span is an example.
                for citation in _CITATION.findall(child.content):
                    cited.setdefault(citation.casefold(), fragment)
            # An embedded image is a link too, so validation catches a missing asset; inline data names no target.
            elif child.type in {"link_open", "image"}:
                target = str(child.attrGet("href" if child.type == "link_open" else "src"))
                # Split local URL fragments before decoding: %23 is part of a filename.
                # BF parses each component before decoding; external queries keep their original meaning.
                if not (child.type == "image" and target.lower().startswith("data:")):
                    found.append((target, fragment, at, footnote))
    contexts = [(target, cited.get(footnote, origin), at) for target, origin, at, footnote in found]
    return Markdown(text, attributes, headings, contexts, tasks, offset)


def _title(path: str, inline: Token, *, strict: bool) -> tuple[str, str]:
    """A heading's words and its explicit anchor, written last in its source as ` {#id}`.

    The anchor is read from the source, so braces in a code span or escaped with a backslash stay words. Outside
    `strict` OKF notes, an anchor ending in .md stays words too.
    """
    title = "".join(
        " " if child.type in {"softbreak", "hardbreak"} else child.content
        for child in inline.children or []
        if child.type in {"text", "code_inline", "image", "softbreak", "hardbreak"}
    )
    # An anchor id holds no `{` or `#`, so only the last `{#` can start one: no regex backtracking.
    start = inline.content.rfind("{#")
    anchor = (
        re.fullmatch(r"\{#([A-Za-z0-9][A-Za-z0-9_.-]*)\}", inline.content[start:])
        if start > 0 and inline.content[start - 1].isspace()
        else None
    )
    if not anchor:
        return title, ""
    if anchor[1].endswith(".md"):
        # `note.md#part.md` would read as a file name, not a section.
        if not strict:
            return title, ""
        raise Error(f"{path}: explicit heading anchors cannot end in .md")
    return title.removesuffix(anchor[0]).rstrip(), anchor[1]


def _headings(path: str, tokens: Sequence[Token], offset: int, *, strict: bool) -> list[Heading]:
    """Headings with unique slugs: an explicit anchor, otherwise the title's slug or its next free `-N` suffix.

    Generated suffixes skip every explicit anchor, wherever it appears. In a `strict` OKF note, a title whose own
    slug another heading anchors explicitly is ambiguous and fails, like two equal anchors. Ordinary Markdown, such as
    a copied document, stays searchable: a repeated anchor yields to the first, and such a title takes a suffix.
    """
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
    # The last suffix each slug took, so repeated headings, as in a transcript, probe each suffix once.
    last: dict[str, int] = {}
    headings = []
    for level, start, end, title, anchor in titled:
        slug = anchor
        if not anchor:
            # A heading without word characters still needs an addressable, non-empty slug.
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
    """A note section's Markdown, from its bytes or from a parse the caller already holds."""
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


@contextmanager
def _at(path: str, where: str) -> Iterator[None]:
    """Name where the note writes a failing value, such as `line 12` or a frontmatter key, rather than quoting it."""
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
    # OKF lets a resource describe a population, such as "all pull requests of a repository": only a link-shaped
    # resource, a URL, identity or path, is cited and checked.
    return [
        resource
        for resource in (str(source["resource"]).strip() for source in sources)
        if not re.search(r"\s", resource)
        and (re.match(IDENTITY, resource) or "/" in resource or resource.endswith(".md"))
    ]


def _task(path: str, markdown: str) -> str:
    """A task's words with its links kept as `[label](ref)`: a relative target becomes the brain-relative ref that
    `bf read` opens, so the next step names what to read without another lookup.

    Only words and labels lose emphasis markers: a ref keeps every character, such as the `_` ending a document ID.
    """
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
    """A compact, readable lead: drop headings markers, rules, emphasis and link targets.

    Callers keep at most LEAD characters; a bounded prefix keeps the link pattern's cost independent of note size.
    """
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown[: 8 * LEAD])
    text = re.sub(r"^#+\s*|^ {0,3}(?:-[ \t]*){3,}$", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", _unmarked(text)).strip()


def _unmarked(text: str) -> str:
    """Text without emphasis markers; a code span keeps its words verbatim, such as `__init__.py`, without backticks."""
    return "".join(part if i % 2 else _EMPHASIS.sub("", part) for i, part in enumerate(_CODE.split(text)))


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


def note(path: str, data: bytes | Markdown) -> Note:
    if not addressable(path):
        # Every note has a BF address: links, backlinks and identities name it by that address.
        raise Error(f"{path}: path exceeds {MAX_ENCODED} characters once percent-encoded in a BF address; shorten it")
    markdown = data if isinstance(data, Markdown) else parse(path, data)
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
    # A section also ranks under its parents: `## Vega` then `### Budget` answers "Vega budget".
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
    enclosing: list[Heading] = []
    for i, heading in enumerate(sections):
        end = sections[i + 1].line if i + 1 < len(sections) else len(source_lines)
        text = "".join(source_lines[heading.end : end])
        while enclosing and enclosing[-1].level >= heading.level:
            enclosing.pop()
        parents = (title, *(h.title for h in enclosing))
        passages.append(Passage(heading.slug, " — ".join((*parents, heading.title)), heading.title, text, parents))
        enclosing.append(heading)
    lead = _plain(summary or introduction)
    if not lead and len(passages) > 1:
        lead = _plain(passages[1].text)
    # OKF provenance cites its resources in each authored note format.
    # Working inputs and outputs remain ordinary Markdown, even when their filename is ACTION.md.
    sources = _sources(path, markdown.attributes) if okf(path) else []
    # A link that cannot resolve makes the note invalid; the problem names its frontmatter key or line.
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
            # Ordinary Markdown, such as a document copied from another tool, stays searchable without a URL Python
            # cannot split, such as http://[host]/. Only BF writers write BF links: a malformed one fails any note.
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
        # Like an alias, a BF resource names the note; one with spaces describes a population, as OKF allows.
        if re.fullmatch(IDENTITY, knowledge.resource) and bf_links.parse(knowledge.resource):
            bf_links.identity(knowledge.resource)
    return Note(
        path=path,
        title=title,
        knowledge=knowledge,
        lead=lead[:LEAD],
        passages=passages,
        slugs={h.slug for h in markdown.headings},
        targets=targets,
        contexts=contexts,
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


def validate_okf(path: str, data: bytes | Markdown) -> None:
    """Check project, concept and action OKF v0.2 structure; retrieval remains tolerant. A caller holding the note's
    parse passes it instead of its bytes."""
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
