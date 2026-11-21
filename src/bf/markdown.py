"""Python-Markdown compiler turning OKF Markdown + attr_list into unified JSON records."""

from __future__ import annotations

import html
import posixpath
import re
import threading
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, unquote, urlsplit

from bf import models
from bf.config import yaml_object
from bf.models import Error, InputError, clean, clean_env, clean_text, digest, encode, invisible

if TYPE_CHECKING:
    import xml.etree.ElementTree as ET

_RESERVED_FM = frozenset(("id", "ref", "source", "kind", "text", "headings", "elements", "anchors", "tasks", "sha256"))
_OMIT_OPT = frozenset(
    (
        "tags",
        "aliases",
        "entity",
        "stale_after",
        "summary",
        "sources",
        "links",
        "headings",
        "elements",
        "anchors",
        "tasks",
    )
)
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_CONFLICT = re.compile(r"^(?:<{7,}|>{7,}|\|{7,}|={7,})(?: |$)")
_BAD_ATTR = re.compile(r"\{#[A-Za-z0-9_.-]+\?[^}\r\n]+\}")
_TASK = re.compile(r"^\[([ xX])\]\s+(\S[\s\S]*)$")
_META_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):(.*)\Z")
_URL_SCHEMES = frozenset({"https", "http", "mailto", "urn"})
_FORBIDDEN = frozenset({"javascript", "data", "file", "vbscript", "tel"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_STASH_BLOCK = re.compile(r"\x02wzxhzdk:(\d+)\x03")
_STASH_ANY = re.compile(r"\x02[^\x03]*\x03")
_HTML_TAG = re.compile(r"<[^>]+>")
_BLOCK_ATTR = re.compile(r"(?:\n[ ]*\{\:?[ ]*|[ ]+\{:[ ]*)([^\}\n ][^\}\n]*)\}[ ]*$")
_DIGITS = re.compile(r"^[0-9]+$")
_LOCAL = threading.local()


def _engine() -> tuple[Any, Any]:
    if (pair := getattr(_LOCAL, "engine", None)) is None:
        import markdown
        from markdown.extensions import Extension
        from markdown.treeprocessors import Treeprocessor

        class _Cap(Treeprocessor):
            root: Any = None

            def run(self, root: Any) -> Any:
                self.root = root
                return root

        cap = _Cap()

        class _Ext(Extension):
            def extendMarkdown(self, md: markdown.Markdown) -> None:  # noqa: N802
                md.treeprocessors.register(cap, "bf_capture", 1)

        md = markdown.Markdown(extensions=["extra", "toc", "admonition", "sane_lists", _Ext()])
        md.treeprocessors["attr_list"].BLOCK_RE = _BLOCK_ATTR  # ty: ignore[missing-attribute]
        pair = _LOCAL.engine = (md, cap)
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


def _step_fence(fence: str, line: str) -> tuple[str, bool]:
    if m := FENCE.fullmatch(line):
        if fence and m[1][0] == fence[0] and len(m[1]) >= len(fence) and not m[2].strip():
            return "", True
        if not fence and not (m[1][0] == "`" and "`" in m[2]):
            return m[1], True
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
    offset, fm, fence = 0, {}, ""
    if lines and lines[0].rstrip("\r\n") == "---":
        if (end := next((i for i, ln in enumerate(lines[1:], 1) if ln.rstrip("\r\n") == "---"), None)) is None:
            if parse_yaml:
                raise Error(f"{path}: unclosed YAML frontmatter")
        else:
            offset = end + 1
            fm = yaml_object("".join(lines[1:end]).encode(), path, line=2) if parse_yaml else {}
    for num, line in enumerate(lines[offset:], offset + 1):
        fence, in_fence = _step_fence(fence, stripped := line.rstrip("\r\n"))
        if not in_fence:
            if _CONFLICT.match(stripped):
                raise Error(f"{path}: line {num}: unresolved merge conflict marker")
            if _BAD_ATTR.search(stripped):
                raise Error(f"{path}: line {num}: invalid attr_list syntax; separate #id and attributes with spaces")
    return fm, "".join(lines[offset:])


def _clean_vis(val: str, label: str) -> str:
    if invisible(out := clean(val, label)):
        raise Error(f"{label} contains invisible format characters")
    return out


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
    for ch in elem:
        if ch.tag not in {"ul", "ol", "dl", "table", "blockquote", "div"}:
            parts.append("".join(ch.itertext()))
        if ch.tail:
            parts.append(ch.tail)
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
    bad = {".bf", ".git", ".venv", ".env"}
    return not parts or parts[0] == "logs" or any(p in bad or (p[:5] == ".env." and p != ".env.example") for p in parts)


def _normalize_note_path(src: str, raw_path: str, *, relative_base: bool) -> str:
    if not raw_path:
        if not relative_base:
            raise Error(f"{src}: fragment-only link requires a target note path")
        return src
    if raw_path.startswith("/") or "\\" in raw_path or "" in raw_path.split("/"):
        raise Error(f"{src}: invalid relative or brain-root link path")
    if not relative_base or raw_path.startswith(("projects/", "concepts/", "actions/", "memories/")):
        if any(part in {".", ".."} for part in raw_path.split("/")):
            raise Error(f"{src}: brain-root path must not contain . or .. segments")
        out = _normalize_target_ext(raw_path)
    else:
        res = posixpath.normpath(posixpath.join(posixpath.dirname(src), raw_path))
        if res == ".." or res.startswith("../"):
            raise Error(f"{src}: link leaves the brain")
        out = _normalize_target_ext(res)
    if is_private_path(out):
        raise Error(f"{src}: private or runtime paths are not allowed in links")
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


def _split_target_parts(
    src: str, raw: str, msg: str, *, with_query: bool = True, unquote_lhs: bool = True
) -> tuple[str, dict[str, str], str]:
    head, has_hash, raw_frag = raw.partition("#")
    if re.search(r"\s", head) or (has_hash and not raw_frag):
        raise Error(f"{src}: {msg}")
    frag = clean(_unquote(raw_frag, src), "anchor") if raw_frag else ""
    if not with_query:
        return head, {}, frag
    lhs, _, raw_query = head.partition("?")
    out_lhs = _unquote(lhs, src) if unquote_lhs else lhs
    if re.search(r"\s", out_lhs):
        raise Error(f"{src}: {msg}")
    return out_lhs, _parse_query(raw_query, src), frag


def _bad_rel(path: str) -> bool:
    return "\\" in path or "\x00" in path or any(p in {"", ".", ".."} for p in path.split("/"))


def is_memory_record_path(path: str) -> bool:
    p = path.split("/")
    ok = len(p) == 3 and p[0] == "memories" and bool(models.NAME_RE.fullmatch(p[1])) and p[2].endswith(".json")
    safe = not _bad_rel(path) and not re.search(r"[\x00-\x1f\x7f-\x9f\s]", path)
    return ok and safe and p[1] not in models.RESERVED_SOURCES and len(p[2]) > 5 and p[2][0] != "." and ":" not in path


def record_path(ref: str) -> str | None:
    """Return `memories/<source>/<sha256(id)>.json` when `ref` is a valid `<source>:<id>` reference."""
    src, sep, rid = ref.partition(":")
    bad = not sep or not rid or not models.NAME_RE.fullmatch(src) or src in models.RESERVED_SOURCES
    inv = bad or any(c.isspace() for c in rid) or invisible(rid)
    return None if inv else f"memories/{src}/{digest(rid.encode())}.json"


def is_record_ref(ref: str) -> bool:
    """Return whether `ref` is a valid `<source>:<id>` record reference."""
    return record_path(ref) is not None


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
    bad_b = bad_ch or not t.startswith("bf://") or not sib_h or "\\" in sib_h or not sib_p
    sib_ok = ":" not in sib_p and not _bad_rel(sib_p) and not is_private_path(sib_p)
    if k == "brain" and (bad_b or not sib_ok):
        return f"cross-brain link {t!r} must use bf://<brain>/<path>"
    if a and (k == "record" or t.endswith(".json")):
        return "JSON record links do not support #anchor fragments"
    return ""


def parse_link(
    source_path: str,
    raw_target: str,
    brain_name: str,
    *,
    section: str = "",
    attr_meta: Mapping[str, str] | None = None,
    link_id: str = "",
    relative_base: bool = True,
    require_uri: bool = False,
) -> dict[str, Any]:
    """Normalize a Markdown, frontmatter, or record link into canonical `{id?, to, kind, anchor, meta, section}`."""
    clean_sec = _clean_vis(section, f"{source_path}: link section") if section else ""
    if len(target := _clean_vis(raw_target, f"{source_path}: link target").strip()) > 8192:
        raise Error(f"{source_path}: link exceeds 8192 characters")

    def mk(to: str, kind: str, anchor: str = "", q_meta: dict[str, str] | None = None) -> dict[str, Any]:
        merged = dict(q_meta or {})
        for key, val in (attr_meta or {}).items():
            if not _META_KEY.fullmatch(key):
                raise Error(f"{source_path}: invalid link attribute {key!r}")
            cleaned = clean(val, f"{source_path}: link attribute {key}")
            if key in merged and merged[key] != cleaned:
                raise Error(f"{source_path}: conflicting link metadata for {key!r}")
            merged[key] = cleaned
        base: dict[str, Any] = {"to": to, "kind": kind, "anchor": anchor, "meta": merged, "section": clean_sec}
        return {"id": link_id, **base} if link_id else base

    def canonical_rec(src_s: str, rid_s: str, err_lbl: str) -> str:
        if not models.NAME_RE.fullmatch(src_s) or not rid_s:
            raise Error(f"{source_path}: invalid record reference {err_lbl!r}")
        if src_s in models.RESERVED_SOURCES:
            raise Error(f"{source_path}: record link {target!r} must use memories/<source>/<sha256>.json")
        if (rec_p := record_path(f"{src_s}:{rid_s}")) is None:
            raise Error(f"{source_path}: invalid record reference {err_lbl!r}")
        return rec_p

    scheme_m = _SCHEME.match(target)
    scheme = scheme_m[1].lower() if scheme_m else ""
    if scheme in _URL_SCHEMES:
        return mk(target, "url")
    if scheme in _FORBIDDEN:
        raise Error(f"{source_path}: unsupported link scheme {scheme}:")
    if scheme == "bf":
        if not target.lower().startswith("bf://") or re.search(r"%(?![0-9a-fA-F]{2})", target):
            raise Error(f"{source_path}: invalid bf:// URI")
        head, _, anchor = _split_target_parts(
            source_path, target, "invalid bf:// URI or empty # fragment", with_query=False
        )
        uri = urlsplit(head)
        _unquote(uri.netloc, source_path)
        raw_rest, netloc = uri.path.lstrip("/"), uri.netloc
        if netloc in models.RESERVED_SOURCES:
            raise Error(f"{source_path}: use bf:///{netloc}/... instead of bf://{netloc}/...")
        src_part, sep, raw_id = raw_rest.partition(":")
        if is_rec := bool(sep and "/" not in src_part):
            is_http = raw_id.lower().startswith(("https://", "http://"))
            rec_id = f"{raw_id}?{uri.query}" if is_http and uri.query else raw_id
            q_meta = {} if is_http else _parse_query(uri.query, source_path)
            canon = canonical_rec(src_part, rec_id, raw_rest)
            rest = raw_rest
        else:
            q_meta, rest = _parse_query(uri.query, source_path), _unquote(raw_rest, source_path)
            if not rest or re.search(r"\s", rest):
                raise Error(f"{source_path}: expected bf:///path or bf://brain/path")
            if _bad_rel(rest):
                raise Error(f"{source_path}: invalid path in bf:// URI")
            if is_private_path(rest):
                raise Error(f"{source_path}: private or runtime paths are not allowed in links")
            canon = ""
        if netloc in {"", brain_name}:
            to = canon if is_rec else _normalize_note_path(source_path, rest, relative_base=False)
            return mk(to, "record" if (is_rec or is_memory_record_path(to)) else "note", anchor, q_meta)
        return mk(f"bf://{netloc}/{canon if is_rec else _normalize_target_ext(rest)}", "brain", anchor, q_meta)
    if scheme and scheme_m is not None:
        raw_src, rhs = scheme_m[1], scheme_m[2]
        if not models.NAME_RE.fullmatch(raw_src) or not rhs:
            raise Error(f"{source_path}: invalid record reference {target!r}")
        bad_r = f"invalid record reference {target!r}"
        if rhs.lower().startswith(("https://", "http://")):
            head, _, anchor = _split_target_parts(source_path, rhs, bad_r, with_query=False)
            return mk(canonical_rec(raw_src, head, target), "record", anchor)
        rec_id, q_meta, anchor = _split_target_parts(source_path, rhs, bad_r, unquote_lhs=False)
        if not rec_id:
            raise Error(f"{source_path}: record id must be nonempty")
        return mk(canonical_rec(raw_src, rec_id, target), "record", anchor, q_meta)
    if require_uri:
        raise Error(f"{source_path}: links must use bf:// or https:// URLs")
    dec_p, q_meta, anchor = _split_target_parts(source_path, target, "invalid Markdown link target or trailing #")
    if not dec_p and not anchor:
        raise Error(f"{source_path}: empty link target")
    to = _normalize_note_path(source_path, dec_p, relative_base=relative_base)
    return mk(to, "record" if is_memory_record_path(to) else "note", anchor, q_meta)


def add_link(seen: set[tuple[Any, ...]], out: list[dict[str, Any]], parsed: dict[str, Any]) -> None:
    """Append `parsed` link object to `out` when its normalized tuple key is unseen."""
    m = parsed.get("meta") or {}
    key = (
        parsed.get("id", ""),
        parsed["to"],
        parsed["kind"],
        parsed.get("anchor", ""),
        tuple(sorted(m.items())),
        parsed.get("section", ""),
    )
    if key not in seen:
        seen.add(key)
        out.append(parsed)


def _extract_ast(path: str, body: str):
    md, capture = _engine()
    md.reset()
    md.convert(body)
    if (root := capture.root) is None:
        return "", [], [], [], {}, [], set()
    stash = [b if isinstance(b, str) else "".join(b.itertext()) for b in md.htmlStash.rawHtmlBlocks]
    headings, anchors, raw_links, elements, tasks, cited_fn, consumed_p, cur_sec = [], {}, [], [], [], {}, set(), ""
    all_ids: set[str] = set()

    def reg(item_id: str, meta: dict[str, str]) -> str:
        if item_id in all_ids:
            raise Error(f"{path}: duplicate anchor #{item_id}")
        all_ids.add(item_id)
        if meta:
            anchors[item_id] = dict(meta)
        return item_id

    def add_el(elem: ET.Element, tag_name: str, eid: str, sec: str, meta: dict[str, str]) -> None:
        txt = re.sub(r"\s+", " ", _unstash("".join(elem.itertext()), stash)).strip()[:16384]
        elements.append({"tag": tag_name, "id": eid, "text": txt, "section": sec, "meta": meta})

    def walk(elem: ET.Element, *, in_quote: bool = False, fn_sec: str | None = None) -> None:
        nonlocal cur_sec
        tag, cls, active = elem.tag, _classes(elem.attrib), cur_sec if fn_sec is None else fn_sec
        if tag == "div" and "footnote" in cls:
            for ch in elem.iter("li"):
                sec = cited_fn.get(ch.attrib.get("id", "").removeprefix("fn:"), cur_sec)
                for sub in ch:
                    walk(sub, in_quote=in_quote, fn_sec=sec)
            return
        if tag in _HEADINGS and fn_sec is None:
            if not (hid := elem.attrib.get("id", "").strip()) or hid.endswith(".md"):
                raise Error(f"{path}: invalid heading anchor #{hid}")
            htitle = re.sub(r"\s+", " ", _unstash("".join(elem.itertext()), stash)).strip()
            meta = _extract_meta(elem.attrib, path, drop=frozenset({"id"}))
            headings.append({"id": reg(hid, meta), "title": htitle, "level": int(tag[1]), "meta": meta})
            cur_sec = active = hid
        elif tag == "a":
            if "footnote-backref" in cls:
                return
            href = elem.attrib.get("href", "")
            if "footnote-ref" in cls:
                if href.startswith("#fn:"):
                    cited_fn.setdefault(href.removeprefix("#fn:"), active)
                return
            l_meta = _extract_meta(elem.attrib, path, drop=frozenset({"href", "id"}))
            lid = reg(clean(elem.attrib["id"], f"{path}: link id"), l_meta) if elem.attrib.get("id") else ""
            raw_links.append((href, l_meta, active, lid))
        else:
            eid = clean(elem.attrib["id"], f"{path}: element id") if elem.attrib.get("id") else ""
            tm = _TASK.match(_own_inline_text(elem, stash)) if tag == "li" and not in_quote and fn_sec is None else None
            handled = bool(tm and tm[2].strip())
            if tm and (t_body := tm[2].strip()[:512]):
                sa = elem.attrib
                if not sa and len(elem) and elem[0].tag == "p" and elem[0].attrib:
                    sa, _ = elem[0].attrib, consumed_p.add(id(elem[0]))
                    eid = clean(sa["id"], f"{path}: element id") if sa.get("id") else ""
                t_meta = _extract_meta(sa, path, drop=frozenset({"id"}))
                t_item: dict[str, Any] = {"text": t_body, "done": tm[1] != " ", "section": active, "meta": t_meta}
                tasks.append({"id": reg(eid, t_meta), **t_item} if eid else t_item)
            if tag == "div" and "admonition" in cls:
                ex = [c for c in cls if c != "admonition"]
                meta = _extract_meta(elem.attrib, path, drop=frozenset({"id", "class"}))
                meta.update(type=ex[0] if ex else "note", **({"class": " ".join(ex[1:])} if len(ex) > 1 else {}))
                add_el(elem, "admonition", reg(eid, meta) if eid else "", active, meta)
            elif not handled and id(elem) not in consumed_p and elem is not root and elem.attrib:
                is_lang = (
                    tag == "code" and set(elem.attrib) == {"class"} and len(cls) == 1 and cls[0][:9] == "language-"
                )
                skip = (tag == "sup" and eid.startswith("fnref")) or (tag == "p" and cls == ["admonition-title"])
                if not (skip or is_lang or (tag in {"th", "td"} and set(elem.attrib) == {"style"})):
                    meta = _extract_meta(elem.attrib, path, drop=frozenset({"id"}))
                    if eid or meta:
                        add_el(elem, tag, reg(eid, meta) if eid else "", active, meta)
        for child in elem:
            walk(child, in_quote=in_quote or tag == "blockquote", fn_sec=fn_sec)

    walk(root)
    plain = "\n".join(ln.strip() for ln in _unstash("".join(root.itertext()), stash).splitlines() if ln.strip())
    return plain, headings, raw_links, elements, anchors, tasks, all_ids


def anchor_ids(path: str, data: bytes) -> set[str]:
    """Extract all `#id` anchors (headings and keyed `attr_list` elements) from any UTF-8 Markdown file."""
    return set(_extract_ast(path, _split_frontmatter(path, data, parse_yaml=False)[1])[6])


def compiled_anchors(item: Mapping[str, Any]) -> set[str]:
    """Return every `#id` anchor recorded across `headings`, `elements`, `tasks`, `links`, and `anchors`."""
    found: set[str] = set()
    for field in ("headings", "elements", "tasks", "links"):
        for entry in item.get(field) or ():
            if isinstance(entry, Mapping) and isinstance(eid := entry.get("id"), str) and eid:
                found.add(eid)
    if isinstance(meta_map := item.get("anchors"), Mapping):
        found.update(k for k in meta_map if isinstance(k, str) and k)
    return found


def extract_section(text: str, anchor: str, compiled: dict[str, Any]) -> dict[str, Any]:
    """Extract a `#anchor` section, block element, task, or link metadata from `text` and `compiled`."""
    for key, out_key in (("elements", "element"), ("tasks", "task")):
        for it in compiled.get(key) or ():
            if isinstance(it, dict) and it.get("id") == anchor:
                return {"anchor": anchor, out_key: it}
    headings = [h for h in (compiled.get("headings") or ()) if isinstance(h, dict)]
    if (heading := next((h for h in headings if h.get("id") == anchor), None)) is None:
        if isinstance(anchors := compiled.get("anchors"), dict) and anchor in anchors:
            return {"anchor": anchor, "meta": anchors[anchor]}
        for it in compiled.get("links") or ():
            if isinstance(it, dict) and it.get("id") == anchor:
                return {"anchor": anchor, "meta": dict(it.get("meta") or {})}
        raise Error(f"{compiled['ref']}: missing anchor #{anchor}")
    lines = text.splitlines()
    b0 = next((i + 1 for i in range(1, len(lines)) if lines[i] == "---"), 0) if lines[:1] == ["---"] else 0
    idx, fence, prev_h, spans, ref_defs = b0, "", False, [], []
    while idx < len(lines):
        ln, was_h, prev_h = lines[idx], prev_h, False
        fence, in_fence = _step_fence(fence, ln)
        if not in_fence:
            if re.match(r"^ {0,3}\[[^\]]+\]:", ln):
                ref_defs.append(ln)
            is_atx = bool(re.match(r"^ {0,3}#{1,6}(?:[^#]|$)", ln))
            at_blk = (idx == b0 or was_h or not lines[idx - 1].strip()) and bool(ln.strip())
            can_set = not is_atx and at_blk and idx + 1 < len(lines) and not ln.startswith(("    ", "\t"))
            is_set = (
                can_set
                and not ln.lstrip().startswith((">", "-", "*", "+", "|", ":"))
                and bool(re.fullmatch(r"(?:=+|-+)[ ]*", lines[idx + 1]))
            )
            if is_atx or is_set:
                end_h = idx + (2 if is_set else 1)
                spans.append((idx, "\n".join(lines[idx:end_h])))
                idx, prev_h = end_h, True
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
    out, seen = [], set()
    for item in raw_tags:
        val = str(item) if type(item) in {int, float} else item
        if not isinstance(val, str) or not val.strip() or re.search(r"[/\\\s]", val):
            raise Error(f"{label}: tags must be nonempty tokens without spaces or slashes")
        clean(val, f"{label}: tag")
        if val not in seen:
            seen.add(val)
            out.append(val)
    return out


def parse_line_spec(spec: str) -> tuple[int, int | None]:
    """Parse `START`, `START:`, or `START:COUNT` (1-based) into `(start, count)`."""
    start_s, sep, count_s = spec.strip().partition(":")
    if not _DIGITS.fullmatch(start_s) or ":" in count_s or (count_s and not _DIGITS.fullmatch(count_s)):
        raise InputError("lines spec must be START, START:, or START:COUNT")
    start, count = int(start_s), (int(count_s) if count_s else None) if sep else 1
    if start < 1 or (count is not None and count < 0):
        raise InputError("lines spec requires start >= 1 and count >= 0")
    return start, count


def slice_lines(text: str, start: int = 1, count: int | None = None) -> dict[str, Any]:
    """Return a 1-based line slice `{total_lines, start, count, text}` from `text`."""
    if start < 1 or (count is not None and count < 0):
        raise InputError("lines requires start >= 1 and count >= 0")
    lines = text.splitlines()
    sliced = lines[start - 1 : (None if count is None else start - 1 + count)]
    return {"total_lines": len(lines), "start": start, "count": len(sliced), "text": "\n".join(sliced)}


def note_outline(compiled: Mapping[str, Any]) -> dict[str, Any]:
    """Return compact outline metadata for a compiled note without `text` or `elements`."""
    ext = {k: v for k in ("status", "updated") if (v := compiled.get(k))}
    open_n = sum(1 for t in compiled.get("tasks") or () if isinstance(t, Mapping) and not t.get("done"))
    base = {"kind": compiled["kind"], "ref": compiled["ref"], "title": compiled["title"], **ext}
    return {
        **base,
        "headings": list(compiled.get("headings") or ()),
        "tasks_open": open_n,
        "links_count": len(compiled.get("links") or ()),
    }


def compile_note(path: str, data: bytes, *, brain_name: str) -> dict[str, Any]:
    """Compile an OKF Markdown note into a compact unified JSON record."""
    if (role := okf_role(path)) is None:
        raise Error(f"{path}: not an OKF note path")
    source, kind = role
    frontmatter, body = _split_frontmatter(path, data, parse_yaml=True)
    if forbidden := sorted(_RESERVED_FM & frontmatter.keys()):
        raise Error(f"{path}: frontmatter cannot override computed field {', '.join(forbidden)}")
    plain_text, headings, raw_links, elements, anchors, tasks, _ = _extract_ast(path, body)
    fm = dict(frontmatter)
    if (fm_title := fm.pop("title", None)) is not None and not isinstance(fm_title, str):
        raise Error(f"{path}: title must be a string")
    stem = PurePosixPath(path.removesuffix("/ACTION.md") if kind == "action" else path).stem.replace("-", " ")
    h1_title = next((str(h["title"]) for h in headings if h["level"] == 1 and str(h["title"]).strip()), stem)
    title = clean((fm_title.strip() if isinstance(fm_title, str) else "") or h1_title, f"{path}: title")
    tags = normalize_tags(fm.pop("tags"), path) if "tags" in fm and fm["tags"] is not None else []
    fm_specs: list[tuple[str, dict[str, str], str, str]] = []
    bad_fm = f"{path}: frontmatter links must be a list of link targets or {{to, meta}} objects"
    if (fm_links := fm.pop("links", None)) is not None:
        if not isinstance(fm_links, list):
            raise Error(bad_fm)
        for it in fm_links:
            if isinstance(it, str):
                fm_specs.append((it, {}, "", ""))
            elif isinstance(it, dict) and set(it) <= {"to", "meta"} and isinstance(to_s := it.get("to"), str):
                m = it.get("meta", {})
                if not isinstance(m, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in m.items()):
                    raise Error(bad_fm)
                fm_specs.append((to_s, dict(m), "", ""))
            else:
                raise Error(bad_fm)
    if isinstance(sources_val := fm.get("sources"), list):
        cites = (s.get("resource") for s in sources_val if isinstance(s, dict))
        fm_specs.extend((r.strip(), {"rel": "cites"}, "", "") for r in cites if isinstance(r, str) and r.strip())
    links, seen = [], set()
    for target, link_meta, sec, lid in [*fm_specs, *raw_links]:
        add_link(seen, links, parse_link(path, target, brain_name, section=sec, attr_meta=link_meta, link_id=lid))
    specs = (
        ("headings", headings, {"id", "title", "level"}),
        ("links", links, {"to", "kind"}),
        ("elements", elements, {"tag"}),
        ("tasks", tasks, {"text", "done"}),
    )
    comp = {
        k: [{fk: fv for fk, fv in it.items() if fk in keep or fv not in ("", {}, [], None)} for it in seq]
        for k, seq, keep in specs
    }
    base = {"ref": path, "source": source, "kind": kind, "title": title, "text": plain_text}
    merged = {**fm, **base, "tags": tags, "anchors": anchors, **comp, "sha256": digest(data)}
    compiled = {k: v for k, v in merged.items() if not (k in _OMIT_OPT and v in ("", {}, [], None))}
    encode(compiled)
    return compiled
