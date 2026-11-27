"""CommonMark compiler (`markdown-it-py`) turning Markdown notes into uniform JSON records (`kind: 'note'`)."""

from __future__ import annotations

import html
import re
import stat
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from markdown_it import MarkdownIt

from bf import models, refs
from bf.models import (
    DEFAULT_NOTES,
    Error,
    clean,
    clean_text,
    digest,
    encode,
    invisible,
    normalize_iso_date_or_time,
    sanitize_record_strings,
    strip_unsafe_text,
)
from bf.storage import LINKED, Store
from bf.yaml_io import yaml_object

_RESERVED_FM = frozenset(("id", "ref", "source", "kind", "text", "headings", "tasks", "sha256", "ingested_at"))
_OMIT_OPT = frozenset(
    (
        "tags",
        "aliases",
        "entity",
        "stale_after",
        "description",
        "links",
        "headings",
        "tasks",
        "created",
        "updated",
        "url",
    )
)
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_CONFLICT_OPEN = re.compile(r"^<{7,}(?: |$)")
_CONFLICT_OTHER = re.compile(r"^(?:>{7,}|\|{7,})(?: |$)")
_BAD_ATTR = re.compile(r"\{#[A-Za-z0-9_.-]+\?[^}\r\n]+\}")
_TASK = re.compile(r"^\[([ xX])\]\s+(\S[\s\S]*)$")
_TASK_PREFIX = re.compile(r"^\[([ xX])\]\s+")
_ATTR_TAIL = re.compile(r"(?:^|\s)\{:?([^{}\r\n]++)\}\s*$")
_ATTR_HEAD = re.compile(r"^\{:?([^{}\r\n]++)\}")
_ATTR_TOK = re.compile(
    r"""(?P<id>#[A-Za-z0-9_.-]+)|(?P<cls>\.[A-Za-z0-9_.-]+)|(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)=(?:"(?P<dq>(?:[^"\\]|\\.)*)"|'(?P<sq>(?:[^'\\]|\\.)*)'|(?P<uq>[^\s"']+))|(?P<bad>\S+)"""
)
_HTML_TAG = re.compile(r"<[^<>]+>")
_FOOTNOTE_REF = re.compile(r"\[\^([^\s\]]+)\]")
_FOOTNOTE_DEF = re.compile(r"^ {0,3}\[\^([^\s\]]+)\]:[ \t]*(.*)$")
_SLUG_DROP = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_DASH = re.compile(r"[-\s]+")


@dataclass(frozen=True)
class _ParsedHeading:
    id: str
    title: str
    level: int
    meta: dict[str, str]
    start_line: int


@cache
def _md_parser() -> MarkdownIt:
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark").enable("table").enable("strikethrough")


def note_role(path: str, note_dirs: Iterable[str] = DEFAULT_NOTES) -> tuple[str, bool] | None:
    """Return `(source_dir, is_bundle)` if `path` matches an authored Markdown note under `note_dirs` (no dotfiles)."""
    parts = path.split("/")
    if not path.endswith(".md") or len(parts) < 2 or any(p.startswith(".") for p in parts):
        return None
    if parts[0] not in set(note_dirs):
        return None
    return parts[0], len(parts) >= 3 and parts[-1] == "index.md"


def is_authored_note(store: Store, path: str, note_dirs: Iterable[str] = DEFAULT_NOTES) -> bool:
    """Return `True` only if `path` is an authored note (not a sibling/attachment inside an `index.md` bundle)."""
    if note_role(path, note_dirs) is None:
        return False
    parts = path.split("/")
    for depth in range(2, len(parts)):
        anc_idx = "/".join((*parts[:depth], "index.md"))
        if anc_idx != path and store.exists(anc_idx):
            return False
    return True


def authored_notes(
    store: Store, note_dirs: Iterable[str] = DEFAULT_NOTES, *, skipped: list[str] | None = None
) -> list[str]:
    """Return sorted authored Markdown note paths across `note_dirs` (flat `.md` and `<slug>/index.md` bundles)."""
    dirs, paths, seen = tuple(note_dirs), [], [0]

    def scan_dir(directory: str) -> None:
        if directory.count("/") >= 64:
            raise Error(f"{directory}: exceeds the 64-level depth limit")
        sub_dirs, top_files = store.subdirs(directory, skipped=skipped)
        seen[0] += len(sub_dirs) + len(top_files)
        if seen[0] > models.MAX_FILES:
            raise Error(f"{directory}: exceeds the {models.MAX_FILES:,}-entry scan limit")
        flat_set = {rel for rel in top_files if note_role(rel, dirs) is not None}
        paths.extend(flat_set)
        for sub in sub_dirs:
            if sub.startswith("."):
                continue
            sub_path = f"{directory}/{sub}"
            idx_rel = f"{sub_path}/index.md"
            try:
                info = store.stat(idx_rel)
            except FileNotFoundError:
                scan_dir(sub_path)
                continue
            except PermissionError as err:
                if skipped is None:
                    raise Error(f"{idx_rel}: permission denied") from err
                skipped.append(f"{idx_rel}: permission denied")
                continue
            except Error as err:
                if skipped is None:
                    raise
                skipped.append(str(err))
                continue
            if stat.S_ISREG(info.st_mode):
                if (flat_rel := f"{sub_path}.md") in flat_set:
                    msg = f"{idx_rel}: collides with {flat_rel}"
                    if skipped is None:
                        raise Error(msg)
                    skipped.append(msg)
                else:
                    paths.append(idx_rel)
            else:
                msg = LINKED if stat.S_ISLNK(info.st_mode) else "expected a regular file"
                if skipped is None:
                    raise Error(f"{idx_rel}: {msg}")
                skipped.append(f"{idx_rel}: {msg}")

    for folder in dirs:
        scan_dir(folder)
    return sorted(paths)


def _step_fence(fence: str, line: str) -> tuple[str, bool]:
    if match := FENCE.fullmatch(line):
        if fence and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
            return "", True
        if not fence and not (match[1][0] == "`" and "`" in match[2]):
            return match[1], True
    return fence, bool(fence)


def _split_frontmatter(path: str, data: bytes, *, parse_yaml: bool) -> tuple[dict[str, object], str]:
    if len(data) > models.MAX_NOTE:
        raise Error(f"{path}: note exceeds {models.MAX_NOTE} bytes")
    try:
        text = data.decode("utf-8").removeprefix("\ufeff")
    except UnicodeError as error:
        raise Error(f"{path}: note is not UTF-8") from error
    if parse_yaml:
        clean_text(text, path)
    lines = [ln for ln in re.findall(r"[^\r\n]*(?:\r\n?|\n|$)", text) if ln]
    offset, frontmatter, fence = 0, {}, ""
    if lines and lines[0].rstrip("\r\n") == "---":
        if (end := next((i for i, ln in enumerate(lines[1:], 1) if ln.rstrip("\r\n") == "---"), None)) is None:
            if parse_yaml:
                raise Error(f"{path}: unclosed YAML frontmatter")
        else:
            offset = end + 1
            frontmatter = yaml_object("".join(lines[1:end]).encode(), path, line=2) if parse_yaml else {}
    for num, line in enumerate(lines[offset:], offset + 1):
        fence, in_fence = _step_fence(fence, stripped := line.rstrip("\r\n"))
        if not in_fence:
            if _CONFLICT_OPEN.match(stripped) or _CONFLICT_OTHER.match(stripped):
                raise Error(f"{path}: line {num}: unresolved merge conflict marker")
            if _BAD_ATTR.search(stripped):
                raise Error(f"{path}: line {num}: invalid attr_list syntax; separate #id and attributes with spaces")
    return frontmatter, "".join(lines[offset:])


def _slugify(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    cleaned = _SLUG_DROP.sub("", folded).strip()
    return _SLUG_DASH.sub("-", cleaned).strip("-") or "section"


def _parse_attr_block(raw_inner: str, path: str, *, strict: bool = True) -> tuple[str, dict[str, str]] | None:
    """Parse `attr_list` inner text (`#id .cls key=val key="v"`), returning `(explicit_id, meta)` or `None` if not attrs."""
    explicit_id = ""
    classes: list[str] = []
    meta: dict[str, str] = {}
    found_any = False
    for m in _ATTR_TOK.finditer(raw_inner):
        if m.group("bad") is not None:
            if strict and m.group("bad").startswith("#"):
                raise Error(f"{path}: invalid attr_list syntax; separate #id and attributes with spaces")
            return None
        found_any = True
        if raw_id := m.group("id"):
            explicit_id = raw_id[1:]
        elif raw_cls := m.group("cls"):
            classes.append(raw_cls[1:])
        elif key := m.group("key"):
            val = (
                m.group("dq")
                if m.group("dq") is not None
                else (m.group("sq") if m.group("sq") is not None else m.group("uq"))
            )
            val = re.sub(r'\\(["\'\\])', r"\1", val or "").strip()
            if val:
                meta[key] = clean(strip_unsafe_text(val), f"{path}: attribute {key}")
    if not found_any:
        return None
    if classes:
        meta["class"] = " ".join(classes)
    return explicit_id, meta


def _strip_html(raw: str) -> str:
    # Linear single-pass comment stripper avoiding quadratic regex backtracking on repeated unclosed `<!--`.
    if "<!--" in raw:
        chunks: list[str] = []
        pos = 0
        while (start := raw.find("<!--", pos)) != -1:
            chunks.append(raw[pos:start])
            end = raw.find("-->", start + 4)
            if end == -1:
                chunks.append(raw[start:])
                pos = len(raw)
                break
            pos = end + 3
        if pos < len(raw):
            chunks.append(raw[pos:])
        raw = "".join(chunks)
    return html.unescape(_HTML_TAG.sub("", raw))


def _render_inline(
    children: list[Any],
    path: str,
    active_sec: str,
    raw_links: list[tuple[str, dict[str, str], str]],
) -> tuple[str, str, dict[str, str]]:
    """Render CommonMark inline tokens to plain text, extract links + `{...}` link attrs, and strip trailing `{...}`."""
    parts: list[str] = []
    n = len(children)
    idx = 0
    link_stack: list[str] = []

    while idx < n:
        tok = children[idx]
        ttype = tok.type
        if ttype in {"text", "code_inline"}:
            parts.append(tok.content)
        elif ttype in {"softbreak", "hardbreak"}:
            parts.append(" ")
        elif ttype == "html_inline":
            parts.append(_strip_html(tok.content))
        elif ttype == "image":
            if tok.content:
                parts.append(tok.content)
        elif ttype == "link_open":
            href = str(tok.attrGet("href") or "")
            link_stack.append(href)
        elif ttype == "link_close":
            href = link_stack.pop() if link_stack else ""
            link_meta: dict[str, str] = {}
            if idx + 1 < n and children[idx + 1].type == "text":
                nxt_text = children[idx + 1].content
                if (m := _ATTR_HEAD.match(nxt_text)) and (
                    parsed_a := _parse_attr_block(m.group(1).strip(), path, strict=False)
                ) is not None:
                    _, link_meta = parsed_a
                    remainder = nxt_text[m.end() :]
                    if remainder:
                        parts.append(remainder)
                    idx += 1
            if href:
                raw_links.append((href, link_meta, active_sec))
        idx += 1

    text = re.sub(r"\s+", " ", strip_unsafe_text("".join(parts))).strip()
    explicit_id, tail_meta = "", {}
    # Avoid stripping attr syntax that came from code_inline by verifying last child was text.
    if (
        (m := _ATTR_TAIL.search(text))
        and children
        and children[-1].type == "text"
        and (parsed_t := _parse_attr_block(m.group(1).strip(), path, strict=True)) is not None
    ):
        explicit_id, tail_meta = parsed_t
        text = text[: m.start()].strip()
    return text, explicit_id, tail_meta


def _extract_footnotes(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Extract Markdown footnote definitions (`[^id]: ...`) outside fences while preserving exact line numbers."""
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    main_lines: list[str] = []
    footnotes: list[tuple[str, str]] = []
    fence = ""
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        fence, in_fence = _step_fence(fence, ln)
        if not in_fence and (m := _FOOTNOTE_DEF.match(ln)):
            label = m.group(1)
            fn_lines = [m.group(2)]
            main_lines.append("")
            i += 1
            while i < n:
                nxt = lines[i]
                if nxt.startswith("    "):
                    fn_lines.append(nxt[4:])
                    main_lines.append("")
                    i += 1
                elif nxt.startswith("\t"):
                    fn_lines.append(nxt[1:])
                    main_lines.append("")
                    i += 1
                elif not nxt.strip():
                    # Look ahead once in O(N) total: blank lines belong to footnote only if followed by indented text.
                    j = i + 1
                    while j < n and not lines[j].strip():
                        j += 1
                    if j < n and lines[j].startswith(("    ", "\t")):
                        blanks = j - i
                        fn_lines.extend([""] * blanks)
                        main_lines.extend([""] * blanks)
                        i = j
                    else:
                        break
                else:
                    break
            footnotes.append((label, "\n".join(fn_lines)))
        else:
            main_lines.append(ln)
            i += 1
    return "\n".join(main_lines), footnotes


def _parse_body(
    path: str, body: str
) -> tuple[str, list[_ParsedHeading], list[tuple[str, dict[str, str], str]], list[dict[str, Any]], set[str]]:
    """Single stateless CommonMark AST walk extracting plain text, exact-line headings, links, and tasks."""
    if not body.strip():
        return "", [], [], [], set()
    main_body, fn_defs = _extract_footnotes(body)
    tokens = _md_parser().parse(main_body)
    headings: list[_ParsedHeading] = []
    raw_links: list[tuple[str, dict[str, str], str]] = []
    tasks: list[dict[str, Any]] = []
    heading_ids: set[str] = set()
    plain_lines: list[str] = []
    cur_sec = ""
    footnote_secs: dict[str, str] = {}
    blockquote_depth = 0
    # Per active list_item: [first_inline_seen, task_dict_or_None] so loose-item `{: attr=val}` attaches to its task.
    list_item_stack: list[list[Any]] = []
    idx = 0
    total = len(tokens)

    while idx < total:
        tok = tokens[idx]
        ttype = tok.type
        if ttype == "blockquote_open":
            blockquote_depth += 1
        elif ttype == "blockquote_close":
            blockquote_depth = max(0, blockquote_depth - 1)
        elif ttype == "list_item_open":
            list_item_stack.append([False, None])
        elif ttype == "list_item_close":
            if list_item_stack:
                list_item_stack.pop()
        elif ttype == "heading_open":
            level = int(tok.tag[1])
            start_line = tok.map[0] if tok.map else 0
            inline_tok = tokens[idx + 1] if idx + 1 < total and tokens[idx + 1].type == "inline" else None
            children = list(inline_tok.children or ()) if inline_tok is not None else []
            heading_links: list[tuple[str, dict[str, str], str]] = []
            title, explicit_id, h_meta = _render_inline(children, path, "", heading_links)
            if blockquote_depth == 0:
                if explicit_id:
                    if explicit_id.endswith(".md") or invisible(explicit_id):
                        raise Error(f"{path}: invalid heading anchor #{explicit_id}")
                    if explicit_id in heading_ids:
                        raise Error(f"{path}: duplicate heading anchor #{explicit_id}")
                    hid = explicit_id
                else:
                    base_slug = _slugify(title)
                    hid = base_slug
                    suffix = 1
                    while hid in heading_ids:
                        hid = f"{base_slug}_{suffix}"
                        suffix += 1
                heading_ids.add(hid)
                headings.append(_ParsedHeading(hid, title, level, h_meta, start_line))
                cur_sec = hid
            for href, lmeta, _ in heading_links:
                raw_links.append((href, lmeta, cur_sec))
            if title:
                plain_lines.append(title)
            idx += 2  # Skip inline; loop advances past heading_close on next iteration or idx+1
        elif ttype == "inline":
            children = list(tok.children or ())
            raw_content = tok.content or ""
            for fn_ref in _FOOTNOTE_REF.finditer(raw_content):
                footnote_secs.setdefault(fn_ref.group(1), cur_sec)
            text, _, tail_meta = _render_inline(children, path, cur_sec, raw_links)
            has_plain_box = (
                bool(children) and children[0].type == "text" and bool(_TASK_PREFIX.match(children[0].content))
            )
            if (
                blockquote_depth == 0
                and list_item_stack
                and not list_item_stack[-1][0]
                and has_plain_box
                and (tm := _TASK.match(text))
                and (t_body := tm[2].strip()[:512])
            ):
                task_obj: dict[str, Any] = {"text": t_body, "done": tm[1] != " ", "section": cur_sec, "meta": tail_meta}
                list_item_stack[-1][0] = True
                list_item_stack[-1][1] = task_obj
                tasks.append(task_obj)
            elif list_item_stack and not list_item_stack[-1][0]:
                list_item_stack[-1][0] = True
            elif list_item_stack and list_item_stack[-1][1] is not None and not text and tail_meta:
                list_item_stack[-1][1]["meta"].update(tail_meta)
            if text:
                plain_lines.append(text)
        elif ttype in {"fence", "code_block"}:
            if stripped := strip_unsafe_text(tok.content).strip():
                plain_lines.extend(ln.strip() for ln in stripped.splitlines() if ln.strip())
        elif ttype == "html_block" and (stripped := strip_unsafe_text(_strip_html(tok.content)).strip()):
            plain_lines.extend(ln.strip() for ln in stripped.splitlines() if ln.strip())
        idx += 1

    # Walk extracted footnote definitions attributing links to their referring heading section.
    for fn_label, fn_body in fn_defs:
        if not fn_body.strip():
            continue
        fn_sec = footnote_secs.get(fn_label, cur_sec)
        for ft in _md_parser().parse(fn_body):
            if ft.type == "inline":
                ft_txt, _, _ = _render_inline(list(ft.children or ()), path, fn_sec, raw_links)
                if ft_txt:
                    plain_lines.append(ft_txt)
            elif (ft.type in {"fence", "code_block"} and (st := strip_unsafe_text(ft.content).strip())) or (
                ft.type == "html_block" and (st := strip_unsafe_text(_strip_html(ft.content)).strip())
            ):
                plain_lines.extend(ln.strip() for ln in st.splitlines() if ln.strip())

    plain = "\n".join(ln for ln in plain_lines if ln)
    return plain, headings, raw_links, tasks, heading_ids


def anchor_ids(path: str, data: bytes) -> set[str]:
    """Extract all heading `#id` anchors from any UTF-8 Markdown file."""
    _, body = _split_frontmatter(path, data, parse_yaml=False)
    return _parse_body(path, body)[4]


def compiled_anchors(item: dict[str, Any]) -> set[str]:
    """Return every heading `#id` anchor recorded on a compiled note."""
    return {
        str(h["id"])
        for h in (item.get("headings") or ())
        if isinstance(h, dict) and isinstance(h.get("id"), str) and h["id"]
    }


def extract_section(text: str, anchor: str, compiled: dict[str, Any]) -> dict[str, Any]:
    """Extract the exact raw Markdown section for heading `#anchor` using the single CommonMark AST line map."""
    ref = str(compiled.get("ref") or "<note>")
    _, body = _split_frontmatter(ref, text.encode("utf-8"), parse_yaml=False)
    _, parsed_headings, _, _, _ = _parse_body(ref, body)
    target_idx = next((i for i, h in enumerate(parsed_headings) if h.id == anchor), None)
    if target_idx is None:
        raise Error(f"{ref}: missing anchor #{anchor}")
    target = parsed_headings[target_idx]
    body_lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    end_line = next(
        (h.start_line for h in parsed_headings[target_idx + 1 :] if h.level <= target.level),
        len(body_lines),
    )
    heading_obj: dict[str, Any] = {"id": target.id, "title": target.title, "level": target.level}
    if target.meta:
        heading_obj["meta"] = target.meta
    return {
        "anchor": anchor,
        "heading": heading_obj,
        "text": "\n".join(body_lines[target.start_line : end_line]).strip(),
    }


def compile_note(
    path: str, data: bytes, *, brain_name: str, note_dirs: Iterable[str] = DEFAULT_NOTES
) -> dict[str, Any]:
    """Compile a Markdown note into a sanitized, schema-ready JSON record (`kind: 'note'`)."""
    if (role := note_role(path, note_dirs)) is None:
        raise Error(f"{path}: not an authored note path")
    source_dir, is_bundle = role
    frontmatter, body = _split_frontmatter(path, data, parse_yaml=True)
    frontmatter = sanitize_record_strings(frontmatter)
    if forbidden := sorted(_RESERVED_FM & frontmatter.keys()):
        raise Error(f"{path}: frontmatter cannot override computed field {', '.join(forbidden)}")
    plain_text, parsed_headings, raw_links, tasks, _ = _parse_body(path, body)
    fm = dict(frontmatter)
    if (fm_title := fm.pop("title", None)) is not None and not isinstance(fm_title, str):
        raise Error(f"{path}: title must be a string")
    stem = (PurePosixPath(path).parent.name if is_bundle else PurePosixPath(path).stem).replace("-", " ")
    h1_title = next((h.title for h in parsed_headings if h.level == 1 and h.title.strip()), stem)
    title = clean(
        strip_unsafe_text(fm_title.strip() if isinstance(fm_title, str) else "") or h1_title, f"{path}: title"
    )
    tags = refs.normalize_tags(fm.pop("tags"), path) if "tags" in fm and fm["tags"] is not None else []
    for dt_key in ("created", "updated"):
        if dt_key in fm and (dt_val := fm[dt_key]) is not None:
            if not isinstance(dt_val, str) or not dt_val:
                raise Error(f"{path}: {dt_key} must be an ISO date (YYYY-MM-DD) or timestamp with timezone")
            fm[dt_key] = normalize_iso_date_or_time(dt_val, f"{path}: {dt_key}")
    if "url" in fm and fm["url"] is not None:
        if not isinstance(fm["url"], str):
            raise Error(f"{path}: url must be a string")
        fm["url"] = refs.validate_url(fm["url"], f"{path}: url")
    if "stale_after" in fm and fm["stale_after"] is not None:
        fm["stale_after"] = models.validate_stale_after(fm["stale_after"], f"{path}: stale_after")

    fm_specs: list[tuple[str, dict[str, str], str]] = []
    bad_fm = f"{path}: frontmatter links must be a list of link targets or {{to, meta}} objects"
    if (fm_links := fm.pop("links", None)) is not None:
        if not isinstance(fm_links, list):
            raise Error(bad_fm)
        for item in fm_links:
            if isinstance(item, str):
                fm_specs.append((item, {}, ""))
            elif isinstance(item, dict) and set(item) <= {"to", "meta"} and isinstance(to_s := item.get("to"), str):
                meta = item.get("meta", {})
                if not isinstance(meta, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in meta.items()
                ):
                    raise Error(bad_fm)
                fm_specs.append((to_s, dict(meta), ""))
            else:
                raise Error(bad_fm)
    links: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for target, link_meta, sec in [*fm_specs, *raw_links]:
        parsed = refs.parse_link(path, target, brain_name, note_dirs=note_dirs, section=sec, attr_meta=link_meta)
        refs.add_link(seen, links, parsed)

    headings_dicts = [
        {"id": h.id, "title": h.title, "level": h.level, **({"meta": h.meta} if h.meta else {})}
        for h in parsed_headings
    ]
    specs = (
        ("headings", headings_dicts, {"id", "title", "level"}),
        ("links", links, {"to", "kind"}),
        ("tasks", tasks, {"text", "done"}),
    )
    comp = {
        key: [{fk: fv for fk, fv in item.items() if fk in keep or fv not in ("", {}, [], None)} for item in seq]
        for key, seq, keep in specs
    }
    base = {"ref": path, "source": source_dir, "kind": "note", "title": title, "text": plain_text}
    merged = {**fm, **base, "tags": tags, **comp, "sha256": digest(data)}
    sanitized = sanitize_record_strings(merged)
    compiled = {k: v for k, v in sanitized.items() if not (k in _OMIT_OPT and v in ("", {}, [], None))}
    encode(compiled)
    return compiled
