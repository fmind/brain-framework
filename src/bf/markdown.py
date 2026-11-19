"""Python-Markdown compiler turning OKF Markdown + attr_list into unified JSON records."""

from __future__ import annotations

import html
import posixpath
import re
import threading
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

from bf import models
from bf.config import yaml_object
from bf.models import Error, clean, clean_env, clean_text, digest, encode, invisible

_RESERVED_FM = frozenset({"id", "ref", "source", "kind", "text", "headings", "elements", "attrs", "tasks", "sha256"})
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_CONFLICT = re.compile(r"^(?:<{7,}|>{7,}|\|{7,}|={7,})(?: |$)")
_BAD_ATTR = re.compile(r"\{#[A-Za-z0-9_.-]+\?[^}\r\n]+\}")
_TASK = re.compile(r"^\[([ xX])\]\s+(\S[\s\S]*)$")
_META_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):(.*)\Z")
_URL_SCHEMES = frozenset({"https", "http", "mailto", "urn"})
_FORBIDDEN_SCHEMES = frozenset({"javascript", "data", "file", "vbscript", "tel"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_STASH_BLOCK = re.compile(r"\x02wzxhzdk:(\d+)\x03")
_STASH_ANY = re.compile(r"\x02[^\x03]*\x03")
_HTML_TAG = re.compile(r"<[^>]+>")
_BLOCK_ATTR = re.compile(r"(?:\n[ ]*\{\:?[ ]*|[ ]+\{:[ ]*)([^\}\n ][^\}\n]*)\}[ ]*$")
_LOCAL = threading.local()


class _Capture(Treeprocessor):
    root: ET.Element | None = None

    def run(self, root: ET.Element) -> ET.Element:
        self.root = root
        return root


class _CaptureExtension(Extension):
    def __init__(self) -> None:
        super().__init__()
        self.capture = _Capture()

    def extendMarkdown(self, md: markdown.Markdown) -> None:  # noqa: N802
        md.treeprocessors.register(self.capture, "bf_capture", 1)


def _engine() -> tuple[markdown.Markdown, _Capture]:
    if (pair := getattr(_LOCAL, "engine", None)) is None:
        ext = _CaptureExtension()
        md = markdown.Markdown(extensions=["extra", "toc", "admonition", "sane_lists", ext])
        md.treeprocessors["attr_list"].BLOCK_RE = _BLOCK_ATTR  # ty: ignore[missing-attribute]
        pair = (md, ext.capture)
        _LOCAL.engine = pair
    return pair


def editor_lock(path: str) -> bool:
    return path.rsplit("/", 1)[-1].startswith(".#")


def okf_role(path: str) -> tuple[str, str] | None:
    if editor_lock(path) or not path.endswith(".md"):
        return None
    parts = path.split("/")
    if parts[0] in {"projects", "concepts"} and len(parts) >= 2:
        return parts[0], parts[0][:-1]
    return ("actions", "action") if len(parts) == 3 and parts[0] == "actions" and parts[2] == "ACTION.md" else None


def compiled_note_path(path: str) -> str:
    if (role := okf_role(path)) is None:
        raise Error(f"{path}: not an OKF note path")
    slug = path.split("/")[1]
    return f"memories/actions/{slug}.json" if role[1] == "action" else f"memories/{path.removesuffix('.md')}.json"


def _check_body_lines(path: str, source_lines: list[str], offset: int) -> None:
    fence = ""
    for number, line in enumerate(source_lines[offset:], offset + 1):
        stripped = line.rstrip("\r\n")
        if fence:
            closing = FENCE.fullmatch(stripped)
            if closing and closing[1][0] == fence[0] and len(closing[1]) >= len(fence) and not closing[2].strip():
                fence = ""
            continue
        if (opening := FENCE.fullmatch(stripped)) and not (opening[1][0] == "`" and "`" in opening[2]):
            fence = opening[1]
        elif _CONFLICT.match(stripped):
            raise Error(f"{path}: line {number}: unresolved merge conflict marker")
        elif _BAD_ATTR.search(stripped):
            raise Error(f"{path}: line {number}: invalid attr_list syntax; separate #id and attributes with spaces")


def _split_frontmatter(path: str, data: bytes, *, parse_yaml: bool) -> tuple[dict[str, object], str]:
    if len(data) > models.MAX_NOTE:
        raise Error(f"{path}: note exceeds {models.MAX_NOTE} bytes")
    try:
        text = data.decode("utf-8").removeprefix("\ufeff")
    except UnicodeError as error:
        raise Error(f"{path}: note is not UTF-8") from error
    if parse_yaml:
        clean_text(text, path)
    source_lines = [line for line in re.findall(r"[^\r\n]*(?:\r\n?|\n|$)", text) if line]
    offset = 0
    frontmatter: dict[str, object] = {}
    if source_lines and source_lines[0].rstrip("\r\n") == "---":
        end = next((i for i, ln in enumerate(source_lines[1:], 1) if ln.rstrip("\r\n") == "---"), None)
        if end is None and parse_yaml:
            raise Error(f"{path}: unclosed YAML frontmatter")
        if end is not None:
            offset = end + 1
            if parse_yaml:
                frontmatter = yaml_object("".join(source_lines[1:end]).encode("utf-8"), path, line=2)
    _check_body_lines(path, source_lines, offset)
    return frontmatter, "".join(source_lines[offset:])


def _unstash(text: str, stash: list[str]) -> str:
    def repl(m: re.Match[str]) -> str:
        return _HTML_TAG.sub("", stash[idx]) if 0 <= (idx := int(m[1])) < len(stash) else ""

    return html.unescape(_STASH_ANY.sub("", _STASH_BLOCK.sub(repl, text)))


def _classes(attrib: Mapping[str, Any]) -> list[str]:
    raw = attrib.get("class", "")
    return [str(x) for x in raw if str(x)] if isinstance(raw, (list, tuple)) else (str(raw).split() if raw else [])


def _extract_meta(attrib: Mapping[str, Any], path: str, *, drop: frozenset[str] = frozenset()) -> dict[str, str]:
    meta: dict[str, str] = {}
    for key, raw_val in attrib.items():
        if key in drop:
            continue
        if not _META_KEY.fullmatch(key):
            raise Error(f"{path}: invalid attribute name {key!r}")
        val = " ".join(str(x) for x in raw_val if str(x)) if isinstance(raw_val, (list, tuple)) else str(raw_val)
        if val.strip():
            meta[key] = clean(val.strip(), f"{path}: attribute {key}")
    return meta


def _own_inline_text(elem: ET.Element, stash: list[str]) -> str:
    parts = [*([elem.text] if elem.text else [])]
    for child in elem:
        if child.tag not in {"ul", "ol", "dl", "table", "blockquote", "div"}:
            parts.append("".join(child.itertext()))
        if child.tail:
            parts.append(child.tail)
    return re.sub(r"\s+", " ", _unstash("".join(parts), stash)).strip()


def _parse_query(query: str, path: str) -> dict[str, str]:
    if not query:
        return {}
    try:
        pairs = parse_qsl(query, keep_blank_values=False, strict_parsing=True, max_num_fields=32, errors="strict")
    except ValueError:
        raise Error(f"{path}: invalid link query; use ?key=value or {{key=value}}") from None
    meta: dict[str, str] = {}
    for key, val in pairs:
        if not _META_KEY.fullmatch(key) or key in meta:
            raise Error(f"{path}: link query keys must be unique identifiers")
        meta[key] = clean(val, f"{path}: link parameter {key}")
    return meta


def _normalize_target_ext(resolved: str) -> str:
    if re.fullmatch(r"actions/[A-Za-z0-9][A-Za-z0-9_-]*", resolved):
        return f"{resolved}/ACTION.md"
    if resolved.startswith(("projects/", "concepts/")) and "." not in resolved.rsplit("/", 1)[-1]:
        return f"{resolved}.md"
    return resolved


def is_private_path(path: str) -> bool:
    parts = [p.lower() for p in path.split("/") if p]
    if not parts or parts[0] == "logs":
        return True
    return any(p in {".bf", ".git", ".venv", ".env"} or (p.startswith(".env.") and p != ".env.example") for p in parts)


def _normalize_note_path(source_path: str, raw_path: str, *, relative_base: bool) -> str:
    if not raw_path:
        if not relative_base:
            raise Error(f"{source_path}: fragment-only link requires a target note path")
        return source_path
    if raw_path.startswith("/") or "\\" in raw_path or "" in raw_path.split("/"):
        raise Error(f"{source_path}: invalid relative or brain-root link path")
    if not relative_base or raw_path.startswith(("projects/", "concepts/", "actions/", "memories/")):
        if any(part in {".", ".."} for part in raw_path.split("/")):
            raise Error(f"{source_path}: brain-root path must not contain . or .. segments")
        out = _normalize_target_ext(raw_path)
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), raw_path))
        if resolved == ".." or resolved.startswith("../"):
            raise Error(f"{source_path}: link leaves the brain")
        out = _normalize_target_ext(resolved)
    if is_private_path(out):
        raise Error(f"{source_path}: private or runtime paths are not allowed in links")
    return out


def _unquote(val: str, src: str) -> str:
    try:
        dec = unquote(val, errors="strict")
    except UnicodeError as error:
        raise Error(f"{src}: link contains invalid UTF-8 percent-encoding") from error
    if dec:
        clean_env(dec, f"{src}: link")
        if invisible(dec):
            raise Error(f"{src}: link contains invisible format characters")
    return dec


def _split_target_parts(source_path: str, raw: str, err_msg: str) -> tuple[str, dict[str, str], str]:
    before_hash, has_hash, raw_frag = raw.partition("#")
    if re.search(r"\s", before_hash) or (has_hash and not raw_frag):
        raise Error(f"{source_path}: {err_msg}")
    lhs, _, raw_query = before_hash.partition("?")
    dec_lhs = _unquote(lhs, source_path)
    if re.search(r"\s", dec_lhs):
        raise Error(f"{source_path}: {err_msg}")
    frag = clean(_unquote(raw_frag, source_path), "anchor") if raw_frag else ""
    return dec_lhs, _parse_query(raw_query, source_path), frag


def is_memory_record_path(path: str) -> bool:
    p = path.split("/")
    ok = len(p) == 3 and p[0] == "memories" and bool(models.NAME_RE.fullmatch(p[1])) and p[2].endswith(".json")
    safe = not _bad_rel(path) and not re.search(r"[\x00-\x1f\x7f-\x9f\s]", path)
    return ok and safe and p[1] not in models.RESERVED_SOURCES and len(p[2]) > 5 and p[2][0] != "." and ":" not in path


def _bad_rel(path: str) -> bool:
    return "\\" in path or "\x00" in path or any(p in {"", ".", ".."} for p in path.split("/"))


def record_link_problem(lnk: Mapping[str, Any]) -> str:
    k, t, a = str(lnk.get("kind", "")), str(lnk.get("to", "")), str(lnk.get("anchor", ""))
    sib_h, _, sib_p = t.removeprefix("bf://").partition("/")
    bad_ch = bool(re.search(r"[\x00-\x1f\x7f-\x9f\s]", t))
    if k == "url" and (bad_ch or not ((m := _SCHEME.match(t)) and m[1].lower() in _URL_SCHEMES)):
        return f"url link {t!r} must use https://, http://, mailto:, or urn:"
    if k == "record" and not is_memory_record_path(t):
        return f"record link {t!r} must use memories/<source>/<sha256>.json"
    bad_n = bad_ch or ":" in t or _bad_rel(t) or not t.startswith(("projects/", "concepts/", "actions/"))
    if k == "note" and (bad_n or is_private_path(t)):
        return f"note link {t!r} must target projects/, concepts/, or actions/"
    bad_b = bad_ch or not t.startswith("bf://") or not sib_h or "\\" in sib_h or not sib_p or ":" in sib_p
    if k == "brain" and (bad_b or _bad_rel(sib_p) or is_private_path(sib_p)):
        return f"cross-brain link {t!r} must use bf://<brain>/<path>"
    return "JSON record links do not support #anchor fragments" if a and (k == "record" or t.endswith(".json")) else ""


def parse_link(source_path: str, raw_target: str, brain_name: str, **opts: Any) -> dict[str, Any]:
    """Normalize a Markdown, frontmatter, or record link into `{to, kind, anchor, meta, section}`."""
    section, attr_meta = str(opts.get("section", "")), opts.get("attr_meta")
    if section:
        section = clean(section, f"{source_path}: link section")
        if invisible(section):
            raise Error(f"{source_path}: link section contains invisible format characters")
    relative_base, allow_record_ref = bool(opts.get("relative_base", True)), bool(opts.get("allow_record_ref"))
    require_uri, prose = bool(opts.get("require_uri")), bool(opts.get("prose"))
    target = clean(raw_target, f"{source_path}: link target").strip()
    if len(target) > 8192:
        raise Error(f"{source_path}: link exceeds 8192 characters")
    if invisible(target):
        raise Error(f"{source_path}: link contains invisible format characters")

    def mk(to: str, kind: str, anchor: str = "", q_meta: dict[str, str] | None = None) -> dict[str, Any]:
        merged = dict(q_meta or {})
        for key, val in (attr_meta or {}).items():
            if not _META_KEY.fullmatch(key):
                raise Error(f"{source_path}: invalid link attribute {key!r}")
            cleaned = clean(val, f"{source_path}: link attribute {key}")
            if key in merged and merged[key] != cleaned:
                raise Error(f"{source_path}: conflicting link metadata for {key!r}")
            merged[key] = cleaned
        return {"to": to, "kind": kind, "anchor": anchor, "meta": merged, "section": section}

    scheme_match = _SCHEME.match(target)
    scheme = scheme_match[1].lower() if scheme_match else ""
    if scheme in _URL_SCHEMES:
        return mk(target, "url")
    if scheme in _FORBIDDEN_SCHEMES:
        raise Error(f"{source_path}: unsupported link scheme {scheme}:")
    if scheme == "bf":
        if not target.lower().startswith("bf://") or re.search(r"%(?![0-9a-fA-F]{2})", target):
            raise Error(f"{source_path}: invalid bf:// URI")
        head, has_hash, raw_frag = target.partition("#")
        if re.search(r"\s", head) or (has_hash and not raw_frag):
            raise Error(f"{source_path}: invalid bf:// URI or empty # fragment")
        uri = urlsplit(head)
        q_meta = _parse_query(uri.query, source_path)
        anchor = clean(_unquote(raw_frag, source_path), "anchor") if raw_frag else ""
        _unquote(uri.netloc, source_path)
        rest, netloc = _unquote(uri.path.lstrip("/"), source_path), uri.netloc
        if netloc in models.RESERVED_SOURCES:
            raise Error(f"{source_path}: use bf:///{netloc}/... instead of bf://{netloc}/...")
        if not rest or re.search(r"\s", rest):
            raise Error(f"{source_path}: expected bf:///path or bf://brain/path")
        src_part, sep, rec_id = rest.partition(":")
        is_record = bool(sep and models.NAME_RE.fullmatch(src_part) and rec_id and "/" not in src_part)
        if sep and not allow_record_ref:
            msg = "legacy record reference in bf:// URI; use bf:///memories/<source>/<sha256>.json"
            raise Error(f"{source_path}: {msg}")
        if not is_record:
            if _bad_rel(rest):
                raise Error(f"{source_path}: invalid path in bf:// URI")
            if is_private_path(rest):
                raise Error(f"{source_path}: private or runtime paths are not allowed in links")
        if netloc in {"", brain_name}:
            if prose:
                raise Error(f"{source_path}: use a relative path in Markdown prose instead of {target!r}")
            to = rest if is_record else _normalize_note_path(source_path, rest, relative_base=False)
            return mk(to, "record" if (is_record or is_memory_record_path(to)) else "note", anchor, q_meta)
        return mk(f"bf://{netloc}/{rest if is_record else _normalize_target_ext(rest)}", "brain", anchor, q_meta)

    if scheme and scheme_match is not None:
        if not allow_record_ref:
            raise Error(f"{source_path}: unsupported link scheme {scheme}:; use a relative path, bf://, or https://")
        if not models.NAME_RE.fullmatch(scheme_match[1]) or not (rhs := scheme_match[2]):
            raise Error(f"{source_path}: invalid record reference {target!r}")
        if rhs.startswith(("https://", "http://")):
            if re.search(r"\s", rhs):
                raise Error(f"{source_path}: invalid record reference {target!r}")
            return mk(f"{scheme}:{rhs}", "record")
        rec_id, q_meta, anchor = _split_target_parts(source_path, rhs, f"invalid record reference {target!r}")
        if not rec_id:
            raise Error(f"{source_path}: record id must be nonempty")
        return mk(f"{scheme}:{rec_id}", "record", anchor, q_meta)

    if require_uri:
        raise Error(f"{source_path}: links must use bf:// or https:// URLs")
    dec_p, q_meta, anchor = _split_target_parts(source_path, target, "invalid Markdown link target or trailing #")
    if not dec_p and not anchor:
        raise Error(f"{source_path}: empty link target")
    to = _normalize_note_path(source_path, dec_p, relative_base=relative_base)
    return mk(to, "record" if is_memory_record_path(to) else "note", anchor, q_meta)


def add_link(seen: set[tuple[Any, ...]], out: list[dict[str, Any]], parsed: dict[str, Any]) -> None:
    """Append `parsed` link object to `out` when its normalized tuple key is unseen."""
    key = (parsed["to"], parsed["kind"], parsed["anchor"], tuple(sorted(parsed["meta"].items())), parsed["section"])
    if key not in seen:
        seen.add(key)
        out.append(parsed)


def _extract_ast(path: str, body: str):
    md, capture = _engine()
    md.reset()
    md.convert(body)
    if (root := capture.root) is None:
        return "", [], [], [], {}, []
    stash = [b if isinstance(b, str) else "".join(b.itertext()) for b in md.htmlStash.rawHtmlBlocks]
    headings: list[dict[str, Any]] = []
    attrs: dict[str, dict[str, str]] = {}
    raw_links: list[tuple[str, dict[str, str], str]] = []
    elements, tasks, cited_footnotes, consumed_p, current_section = [], [], {}, set(), ""

    def register_id(item_id: str, meta: dict[str, str]) -> None:
        if item_id in attrs:
            raise Error(f"{path}: duplicate anchor #{item_id}")
        attrs[item_id] = dict(meta)

    def add_elem(elem: ET.Element, tag_name: str, eid: str, sec: str, meta: dict[str, str]) -> None:
        txt = re.sub(r"\s+", " ", _unstash("".join(elem.itertext()), stash)).strip()[:16384]
        elements.append({"tag": tag_name, "id": eid, "text": txt, "section": sec, "meta": meta})

    def walk(elem: ET.Element, *, in_quote: bool = False, fn_section: str | None = None) -> None:
        nonlocal current_section
        tag, cls = elem.tag, _classes(elem.attrib)
        active = current_section if fn_section is None else fn_section
        if tag == "div" and "footnote" in cls:
            for child in elem.iter("li"):
                sec = cited_footnotes.get(child.attrib.get("id", "").removeprefix("fn:"), current_section)
                for sub in child:
                    walk(sub, in_quote=in_quote, fn_section=sec)
            return
        if tag in _HEADINGS and fn_section is None:
            if not (hid := elem.attrib.get("id", "").strip()) or hid.endswith(".md"):
                raise Error(f"{path}: invalid heading anchor #{hid}")
            htitle = re.sub(r"\s+", " ", _unstash("".join(elem.itertext()), stash)).strip()
            meta = _extract_meta(elem.attrib, path, drop=frozenset({"id"}))
            register_id(hid, meta)
            headings.append({"id": hid, "title": htitle, "level": int(tag[1]), "meta": meta})
            current_section = active = hid
        elif tag == "a":
            if "footnote-backref" in cls:
                return
            href = elem.attrib.get("href", "")
            if "footnote-ref" in cls:
                if href.startswith("#fn:"):
                    cited_footnotes.setdefault(href.removeprefix("#fn:"), active)
                return
            l_meta = _extract_meta(elem.attrib, path, drop=frozenset({"href", "id"}))
            if elem.attrib.get("id"):
                register_id(clean(elem.attrib["id"], f"{path}: link id"), l_meta)
            raw_links.append((href, l_meta, active))
        else:
            elem_id = clean(elem.attrib["id"], f"{path}: element id") if elem.attrib.get("id") else ""
            is_task_li = tag == "li" and not in_quote and fn_section is None
            tm = _TASK.match(_own_inline_text(elem, stash)) if is_task_li else None
            handled_task = bool(tm and tm[2].strip())
            if tm and (task_body := tm[2].strip()[:512]):
                src_attrib = elem.attrib
                if not src_attrib and len(elem) and elem[0].tag == "p" and elem[0].attrib:
                    src_attrib = elem[0].attrib
                    consumed_p.add(id(elem[0]))
                    elem_id = clean(src_attrib["id"], f"{path}: element id") if src_attrib.get("id") else ""
                t_meta = _extract_meta(src_attrib, path, drop=frozenset({"id"}))
                if elem_id:
                    register_id(elem_id, t_meta)
                t_item: dict[str, Any] = {"text": task_body, "done": tm[1] != " ", "section": active, "meta": t_meta}
                tasks.append({"id": elem_id, **t_item} if elem_id else t_item)
            if tag == "div" and "admonition" in cls:
                extra_cls = [c for c in cls if c != "admonition"]
                meta = _extract_meta(elem.attrib, path, drop=frozenset({"id", "class"}))
                meta["type"] = extra_cls[0] if extra_cls else "note"
                if len(extra_cls) > 1:
                    meta["class"] = " ".join(extra_cls[1:])
                if elem_id:
                    register_id(elem_id, meta)
                add_elem(elem, "admonition", elem_id, active, meta)
            elif not handled_task and id(elem) not in consumed_p and elem is not root and elem.attrib:
                is_lang = (
                    tag == "code" and set(elem.attrib) == {"class"} and len(cls) == 1 and cls[0][:9] == "language-"
                )
                skip_fn = (tag == "sup" and elem_id.startswith("fnref")) or (tag == "p" and cls == ["admonition-title"])
                if not (skip_fn or is_lang or (tag in {"th", "td"} and set(elem.attrib) == {"style"})):
                    meta = _extract_meta(elem.attrib, path, drop=frozenset({"id"}))
                    if elem_id:
                        register_id(elem_id, meta)
                    if elem_id or meta:
                        add_elem(elem, tag, elem_id, active, meta)
        for child in elem:
            walk(child, in_quote=in_quote or tag == "blockquote", fn_section=fn_section)

    walk(root)
    plain_text = "\n".join(ln.strip() for ln in _unstash("".join(root.itertext()), stash).splitlines() if ln.strip())
    return plain_text, headings, raw_links, elements, attrs, tasks


def anchor_ids(path: str, data: bytes) -> set[str]:
    """Extract all `#id` anchors (headings and keyed `attr_list` elements) from any UTF-8 Markdown file."""
    _, body = _split_frontmatter(path, data, parse_yaml=False)
    return set(_extract_ast(path, body)[4])


def extract_section(text: str, anchor: str, compiled: dict[str, Any]) -> dict[str, Any]:
    """Extract a `#anchor` section, block element, task, or link metadata from `text` and `compiled`."""
    for key, out_key in (("elements", "element"), ("tasks", "task")):
        for item in compiled.get(key, []):
            if isinstance(item, dict) and item.get("id") == anchor:
                return {"anchor": anchor, out_key: item}
    headings = [h for h in compiled.get("headings", []) if isinstance(h, dict)]
    heading = next((h for h in headings if h.get("id") == anchor), None)
    if heading is None:
        if isinstance(attrs := compiled.get("attrs"), dict) and anchor in attrs:
            return {"anchor": anchor, "meta": attrs[anchor]}
        raise Error(f"{compiled['ref']}: missing anchor #{anchor}")
    lines = text.splitlines()
    fm_end = next((i + 1 for i in range(1, len(lines)) if lines[i] == "---"), 0)
    body_start = fm_end if lines and lines[0] == "---" else 0
    idx, fence, prev_heading, spans, ref_defs = body_start, "", False, [], []
    while idx < len(lines):
        ln, was_heading, prev_heading = lines[idx], prev_heading, False
        if m_f := FENCE.fullmatch(ln):
            if fence and m_f[1][0] == fence[0] and len(m_f[1]) >= len(fence) and not m_f[2].strip():
                fence = ""
            elif not fence and not (m_f[1][0] == "`" and "`" in m_f[2]):
                fence = m_f[1]
            idx += 1
            continue
        if not fence:
            if re.match(r"^ {0,3}\[[^\]]+\]:", ln):
                ref_defs.append(ln)
            is_atx = bool(re.match(r"^ {0,3}#{1,6}(?:[^#]|$)", ln))
            at_block = idx == body_start or was_heading or not lines[idx - 1].strip()
            is_para = not is_atx and at_block and bool(ln.strip()) and not ln.startswith(("    ", "\t"))
            is_para = is_para and not ln.lstrip().startswith((">", "-", "*", "+", "|", ":"))
            is_setext = is_para and idx + 1 < len(lines) and bool(re.fullmatch(r"(?:=+|-+)[ ]*", lines[idx + 1]))
            if is_atx or is_setext:
                end_h = idx + (2 if is_setext else 1)
                spans.append((idx, "\n".join(lines[idx:end_h])))
                idx, prev_heading = end_h, True
                continue
        idx += 1
    md, capture = _engine()
    md.reset()
    doc_parts = [f"bfspan:{k}\n\n{span}" for k, (_, span) in enumerate(spans)]
    md.convert("\n\n".join([*doc_parts, "\n".join(ref_defs)] if ref_defs else doc_parts))
    known_ids, parsed_spans, cur_k = {str(h.get("id", "")) for h in headings}, [], -1
    for elem in capture.root if capture.root is not None else ():
        if elem.tag == "p" and (elem.text or "").startswith("bfspan:"):
            cur_k = int((elem.text or "")[7:])
        elif elem.tag in _HEADINGS and 0 <= cur_k < len(spans) and (hid := elem.attrib.get("id", "")) in known_ids:
            parsed_spans.append((spans[cur_k][0], hid, int(elem.tag[1])))
    if (hit := next(((p, s, lv) for p, (s, h, lv) in enumerate(parsed_spans) if h == anchor), None)) is None:
        return {"anchor": anchor, "heading": heading, "text": ""}
    end_line = next((s for s, _, lvl in parsed_spans[hit[0] + 1 :] if lvl <= hit[2]), len(lines))
    return {"anchor": anchor, "heading": heading, "text": "\n".join(lines[hit[1] : end_line]).strip()}


def normalize_tags(raw_tags: object, label: str) -> list[str]:
    """Validate and deduplicate a list of string/numeric tags without spaces or slashes."""
    if not isinstance(raw_tags, list):
        raise Error(f"{label}: tags must be a list of strings")
    out: list[str] = []
    seen_tags: set[str] = set()
    for item in raw_tags:
        val = str(item) if type(item) in {int, float} else item
        if not isinstance(val, str) or not val.strip() or re.search(r"[/\\\s]", val):
            raise Error(f"{label}: tags must be nonempty tokens without spaces or slashes")
        clean(val, f"{label}: tag")
        if val not in seen_tags:
            seen_tags.add(val)
            out.append(val)
    return out


def compile_note(path: str, data: bytes, *, brain_name: str) -> dict[str, Any]:
    """Compile an OKF Markdown note into a unified JSON record."""
    if (role := okf_role(path)) is None:
        raise Error(f"{path}: not an OKF note path")
    source, kind = role
    frontmatter, body = _split_frontmatter(path, data, parse_yaml=True)
    if forbidden := sorted(_RESERVED_FM & frontmatter.keys()):
        raise Error(f"{path}: frontmatter cannot override computed field {', '.join(forbidden)}")
    if "relations" in frontmatter:
        raise Error(f"{path}: frontmatter 'relations' was removed; use 'links' with ?rel=... query parameters")

    plain_text, headings, raw_links, elements, attrs, tasks = _extract_ast(path, body)
    fm = dict(frontmatter)
    fm_title = fm.pop("title", None)
    if fm_title is not None and not isinstance(fm_title, str):
        raise Error(f"{path}: title must be a string")
    stem = PurePosixPath(path.removesuffix("/ACTION.md") if kind == "action" else path).stem.replace("-", " ")
    h1_title = next((str(h["title"]) for h in headings if h["level"] == 1 and str(h["title"]).strip()), stem)
    title = clean((fm_title.strip() if isinstance(fm_title, str) else "") or h1_title, f"{path}: title")
    tags = normalize_tags(fm.pop("tags"), path) if "tags" in fm and fm["tags"] is not None else []

    fm_link_specs: list[tuple[str, dict[str, str], str, bool]] = []
    bad_fm_lk = f"{path}: frontmatter links must be a list of URL strings or {{to, meta}} objects"
    if (fm_links := fm.pop("links", None)) is not None:
        if not isinstance(fm_links, list):
            raise Error(bad_fm_lk)
        for item in fm_links:
            if isinstance(item, str):
                fm_link_specs.append((item, {}, "", True))
            elif isinstance(item, dict) and set(item) <= {"to", "meta"} and isinstance(to_s := item.get("to"), str):
                m = item.get("meta", {})
                if not isinstance(m, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in m.items()):
                    raise Error(bad_fm_lk)
                fm_link_specs.append((to_s, dict(m), "", True))
            else:
                raise Error(bad_fm_lk)
    if isinstance(sources_val := fm.get("sources"), list):
        cites = (s.get("resource") for s in sources_val if isinstance(s, dict))
        fm_link_specs.extend((r.strip(), {"rel": "cites"}, "", True) for r in cites if isinstance(r, str) and r.strip())

    links: list[dict[str, Any]] = []
    seen_links: set[tuple[Any, ...]] = set()
    for target, link_meta, sec, is_fm in [*fm_link_specs, *((t, m, s, False) for t, m, s in raw_links)]:
        kw: dict[str, Any] = {"section": sec, "attr_meta": link_meta, "relative_base": not is_fm}
        parsed = parse_link(path, target, brain_name, **kw, require_uri=is_fm, prose=not is_fm)
        add_link(seen_links, links, parsed)

    base = {"id": path, "ref": path, "source": source, "kind": kind, "title": title, "text": plain_text}
    extra = {"tags": tags, "links": links, "headings": headings, "elements": elements, "attrs": attrs, "tasks": tasks}
    compiled: dict[str, Any] = {**fm, **base, **extra, "sha256": digest(data)}
    encode(compiled)
    return compiled
