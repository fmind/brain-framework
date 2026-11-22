"""Python-Markdown compiler turning Markdown notes into uniform JSON records (`kind: 'note'`)."""

from __future__ import annotations

import html
import posixpath
import re
import stat
import threading
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, unquote, urlsplit

from bf import models, storage
from bf.config import yaml_object
from bf.models import DEFAULT_NOTES, RESERVED_SOURCES, Error, clean, clean_env, clean_text, digest, encode, invisible
from bf.storage import LINKED, Store

if TYPE_CHECKING:
    import xml.etree.ElementTree as ET

_RESERVED_FM = frozenset(("id", "ref", "source", "kind", "text", "headings", "tasks", "sha256"))
_OMIT_OPT = frozenset(("tags", "aliases", "entity", "stale_after", "description", "links", "headings", "tasks"))
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
_ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?!#)")
_DEFAULT_RESERVED = RESERVED_SOURCES | frozenset(DEFAULT_NOTES)
_LOCAL = threading.local()


def _engine() -> tuple[Any, Any]:
    if (pair := getattr(_LOCAL, "engine", None)) is None:
        import markdown
        from markdown.extensions import Extension
        from markdown.treeprocessors import Treeprocessor

        class _Capture(Treeprocessor):
            root: Any = None

            def run(self, root: Any) -> Any:
                self.root = root
                return root

        cap = _Capture()

        class _Ext(Extension):
            def extendMarkdown(self, md: markdown.Markdown) -> None:  # noqa: N802
                md.treeprocessors.register(cap, "bf_capture", 1)

        md = markdown.Markdown(extensions=["extra", "toc", "sane_lists", _Ext()])
        pair = _LOCAL.engine = (md, cap)
    return pair


def note_role(path: str, note_dirs: Iterable[str] = DEFAULT_NOTES) -> tuple[str, bool] | None:
    """Return `(source_dir, is_bundle)` if `path` matches a Markdown note path under `note_dirs`."""
    if path.rsplit("/", 1)[-1].startswith(".#") or not path.endswith(".md"):
        return None
    parts = path.split("/")
    if len(parts) < 2 or parts[0] not in set(note_dirs):
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


def parse_record_ref(ref: str, reserved: frozenset[str] = _DEFAULT_RESERVED) -> tuple[str, str] | None:
    """Return `(source, id)` when `ref` is a valid `<source>:<id>` record reference, else `None`."""
    src, sep, rec_id = ref.partition(":")
    is_http = rec_id.lower().startswith(("https://", "http://"))
    if not sep or not rec_id or rec_id.startswith("//") or "#" in rec_id or ("?" in rec_id and not is_http):
        return None
    if not models.NAME_RE.fullmatch(src) or src in reserved:
        return None
    if any(ch.isspace() for ch in rec_id) or invisible(rec_id) or re.search(r"[\x00-\x1f\x7f-\x9f]", rec_id):
        return None
    return src, rec_id


def is_record_ref(ref: str, reserved: frozenset[str] = _DEFAULT_RESERVED) -> bool:
    return parse_record_ref(ref, reserved) is not None


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
            if _CONFLICT.match(stripped):
                raise Error(f"{path}: line {num}: unresolved merge conflict marker")
            if _BAD_ATTR.search(stripped):
                raise Error(f"{path}: line {num}: invalid attr_list syntax; separate #id and attributes with spaces")
    return frontmatter, "".join(lines[offset:])


def _clean_vis(val: str, label: str) -> str:
    if invisible(out := clean(val, label)):
        raise Error(f"{label} contains invisible format characters")
    return out


def _unstash(text: str, stash: list[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        return _HTML_TAG.sub("", stash[idx]) if 0 <= (idx := int(match[1])) < len(stash) else ""

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


def is_private_path(path: str) -> bool:
    parts = [p.lower() for p in path.split("/") if p]
    forbidden = {".bf", ".git", ".venv", ".env"}
    return (
        not parts
        or parts[0] == "logs"
        or any(p in forbidden or (p[:5] == ".env." and p != ".env.example") for p in parts)
    )


def _bad_rel(path: str) -> bool:
    try:
        storage.relative(path)
    except Error:
        return True
    return False


def _normalize_note_path(src: str, raw_path: str, note_dirs: frozenset[str], *, relative_base: bool) -> str:
    if not raw_path:
        if not relative_base:
            raise Error(f"{src}: fragment-only link requires a target note path")
        return src
    if raw_path.startswith("/") or "\\" in raw_path or "" in raw_path.split("/"):
        raise Error(f"{src}: invalid relative or brain-root link path")
    root_prefixes = tuple(f"{folder}/" for folder in note_dirs)
    if not relative_base or raw_path.startswith(root_prefixes):
        if any(part in {".", ".."} for part in raw_path.split("/")):
            raise Error(f"{src}: brain-root path must not contain . or .. segments")
        out = raw_path
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(src), raw_path))
        if resolved == ".." or resolved.startswith("../"):
            raise Error(f"{src}: link leaves the brain")
        out = resolved
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


def _split_target_parts(src: str, raw: str, msg: str) -> tuple[str, str]:
    head, has_hash, raw_frag = raw.partition("#")
    if re.search(r"\s", head) or (has_hash and not raw_frag):
        raise Error(f"{src}: {msg}")
    frag = clean(_unquote(raw_frag, src), "anchor") if raw_frag else ""
    return head, frag


def _parse_query(raw_query: str, src: str) -> dict[str, str]:
    if not raw_query:
        raise Error(f"{src}: invalid link query; use ?key=value or {{key=value}}")
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True, max_num_fields=32, errors="strict")
    except ValueError:
        raise Error(f"{src}: invalid link query; use ?key=value or {{key=value}}") from None
    meta: dict[str, str] = {}
    for key, val in pairs:
        if not _META_KEY.fullmatch(key) or key in meta:
            raise Error(f"{src}: link query keys must be unique identifiers")
        meta[key] = _clean_vis(val, f"{src}: link parameter {key}")
    return meta


def record_link_problem(
    lnk: Mapping[str, Any], *, note_dirs: Iterable[str] = DEFAULT_NOTES, reserved: frozenset[str] | None = None
) -> str:
    dirs = frozenset(note_dirs)
    res = (RESERVED_SOURCES | dirs) if reserved is None else reserved
    kind, target, anchor = str(lnk.get("kind", "")), str(lnk.get("to", "")), str(lnk.get("anchor", ""))
    sib_host, _, sib_path = target.removeprefix("bf://").partition("/")
    bad_ch = bool(re.search(r"[\x00-\x1f\x7f-\x9f\s]", target))
    if kind == "url" and (bad_ch or not ((m := _SCHEME.match(target)) and m[1].lower() in _URL_SCHEMES)):
        return f"url link {target!r} must use https://, http://, mailto:, or urn:"
    if kind == "record" and not is_record_ref(target, res):
        return f"record link {target!r} must use <source>:<id>"
    note_prefixes = tuple(f"{folder}/" for folder in dirs)
    bad_note = bad_ch or ":" in target or "?" in target or _bad_rel(target) or not target.startswith(note_prefixes)
    if kind == "note" and (bad_note or is_private_path(target)):
        return f"note link {target!r} must target a configured note directory"
    if kind == "brain":
        bad_brain = bad_ch or not target.startswith("bf://") or not sib_host or "\\" in sib_host or not sib_path
        sib_rec = is_record_ref(sib_path, RESERVED_SOURCES)
        sib_note = (
            ":" not in sib_path and "?" not in sib_path and not _bad_rel(sib_path) and not is_private_path(sib_path)
        )
        if bad_brain or not (sib_rec or sib_note):
            return f"cross-brain link {target!r} must use bf://<brain>/<ref>"
        if anchor and sib_rec:
            return "record links do not support #anchor fragments"
    if anchor and kind == "record":
        return "record links do not support #anchor fragments"
    return ""


def parse_link(
    source_path: str,
    raw_target: str,
    brain_name: str,
    *,
    note_dirs: Iterable[str] = DEFAULT_NOTES,
    section: str = "",
    attr_meta: Mapping[str, str] | None = None,
    relative_base: bool = True,
    require_uri: bool = False,
) -> dict[str, Any]:
    """Normalize a Markdown, frontmatter, or record link into canonical `{to, kind, anchor, meta, section}`."""
    dirs = frozenset(note_dirs)
    reserved = RESERVED_SOURCES | dirs
    clean_sec = _clean_vis(section, f"{source_path}: link section") if section else ""
    if len(target := _clean_vis(raw_target, f"{source_path}: link target").strip()) > 8192:
        raise Error(f"{source_path}: link exceeds 8192 characters")

    def make_link(to: str, kind: str, anchor: str = "", q_meta: Mapping[str, str] | None = None) -> dict[str, Any]:
        meta: dict[str, str] = dict(q_meta or {})
        for key, val in (attr_meta or {}).items():
            if not _META_KEY.fullmatch(key):
                raise Error(f"{source_path}: invalid link attribute {key!r}")
            cleaned = clean(val, f"{source_path}: link attribute {key}")
            if key in meta and meta[key] != cleaned:
                raise Error(f"{source_path}: conflicting link attribute {key!r}")
            meta[key] = cleaned
        return {"to": to, "kind": kind, "anchor": anchor, "meta": meta, "section": clean_sec}

    def canonical_rec(src_s: str, rid_s: str, err_lbl: str, res_set: frozenset[str]) -> str:
        ref = f"{src_s}:{rid_s}"
        if parse_record_ref(ref, res_set) is None:
            raise Error(f"{source_path}: invalid record reference {err_lbl!r}")
        return ref

    scheme_m = _SCHEME.match(target)
    scheme = scheme_m[1].lower() if scheme_m else ""
    if scheme in _URL_SCHEMES:
        return make_link(target, "url")
    if scheme in _FORBIDDEN:
        raise Error(f"{source_path}: unsupported link scheme {scheme}:")
    if scheme == "bf":
        if not target.lower().startswith("bf://") or re.search(r"%(?![0-9a-fA-F]{2})", target):
            raise Error(f"{source_path}: invalid bf:// URI")
        head, anchor = _split_target_parts(source_path, target, "invalid bf:// URI or empty # fragment")
        has_q, uri = "?" in head, urlsplit(head)
        _unquote(uri.netloc, source_path)
        raw_rest, netloc = uri.path.lstrip("/"), uri.netloc
        is_local = netloc in {"", brain_name}
        if netloc in reserved:
            raise Error(f"{source_path}: use bf:///{netloc}/... instead of bf://{netloc}/...")
        src_part, sep, raw_id = raw_rest.partition(":")
        q_meta: dict[str, str] = {}
        if is_rec := bool(sep and "/" not in src_part):
            is_http = raw_id.lower().startswith(("https://", "http://"))
            if is_http and has_q:
                rec_id = f"{raw_id}?{uri.query}" if uri.query else f"{raw_id}?"
            else:
                rec_id = raw_id
                if has_q:
                    q_meta = _parse_query(uri.query, source_path)
            canon = canonical_rec(src_part, rec_id, raw_rest, reserved if is_local else RESERVED_SOURCES)
            rest = canon
        else:
            if has_q:
                q_meta = _parse_query(uri.query, source_path)
            rest = _unquote(raw_rest, source_path)
            if not rest or re.search(r"\s", rest):
                raise Error(f"{source_path}: expected bf:///path or bf://brain/path")
            if _bad_rel(rest):
                raise Error(f"{source_path}: invalid path in bf:// URI")
            if is_private_path(rest):
                raise Error(f"{source_path}: private or runtime paths are not allowed in links")
            canon = ""
        if is_local:
            if is_rec:
                return make_link(canon, "record", anchor, q_meta)
            to = _normalize_note_path(source_path, rest, dirs, relative_base=False)
            return make_link(to, "note", anchor, q_meta)
        return make_link(f"bf://{netloc}/{canon if is_rec else rest}", "brain", anchor, q_meta)
    if scheme and scheme_m is not None:
        raw_src, rhs = scheme_m[1], scheme_m[2]
        if rhs.startswith("//"):
            raise Error(f"{source_path}: unsupported link scheme {scheme}:")
        if not models.NAME_RE.fullmatch(raw_src) or not rhs:
            raise Error(f"{source_path}: invalid record reference {target!r}")
        bad_r = f"invalid record reference {target!r}"
        rec_id, anchor = _split_target_parts(source_path, rhs, bad_r)
        q_meta = {}
        if not rec_id.lower().startswith(("https://", "http://")) and "?" in rec_id:
            rec_id, _, raw_q = rec_id.partition("?")
            q_meta = _parse_query(raw_q, source_path)
        if not rec_id:
            raise Error(f"{source_path}: record id must be nonempty")
        return make_link(canonical_rec(raw_src, rec_id, target, reserved), "record", anchor, q_meta)
    if require_uri:
        raise Error(f"{source_path}: links must use <source>:<id>, bf://, or https:// URLs")
    raw_lhs, anchor = _split_target_parts(source_path, target, "invalid Markdown link target or trailing #")
    raw_p, has_q, raw_q = raw_lhs.partition("?")
    q_meta = _parse_query(raw_q, source_path) if has_q else {}
    dec_p = _unquote(raw_p, source_path)
    if re.search(r"\s", dec_p):
        raise Error(f"{source_path}: invalid Markdown link target or trailing #")
    if not dec_p and not anchor:
        raise Error(f"{source_path}: empty link target")
    to = _normalize_note_path(source_path, dec_p, dirs, relative_base=relative_base)
    return make_link(to, "note", anchor, q_meta)


def add_link(seen: set[tuple[Any, ...]], out: list[dict[str, Any]], parsed: dict[str, Any]) -> None:
    """Append `parsed` link object to `out` when its normalized tuple key is unseen."""
    meta = parsed.get("meta") or {}
    key = (
        parsed["to"],
        parsed["kind"],
        parsed.get("anchor", ""),
        tuple(sorted(meta.items())),
        parsed.get("section", ""),
    )
    if key not in seen:
        seen.add(key)
        out.append(parsed)


def _extract_ast(
    path: str, body: str
) -> tuple[str, list[dict[str, Any]], list[tuple[str, dict[str, str], str]], list[dict[str, Any]], set[str]]:
    md, capture = _engine()
    md.reset()
    md.convert(body)
    if (root := capture.root) is None:
        return "", [], [], [], set()
    stash = [b if isinstance(b, str) else "".join(b.itertext()) for b in md.htmlStash.rawHtmlBlocks]
    headings: list[dict[str, Any]] = []
    raw_links: list[tuple[str, dict[str, str], str]] = []
    tasks: list[dict[str, Any]] = []
    cited_fn: dict[str, str] = {}
    heading_ids: set[str] = set()
    cur_sec = ""

    def walk(elem: ET.Element, *, in_quote: bool = False, fn_sec: str | None = None) -> None:
        nonlocal cur_sec
        tag, cls, active = elem.tag, _classes(elem.attrib), cur_sec if fn_sec is None else fn_sec
        if tag == "div" and "footnote" in cls:
            for ch in elem.iter("li"):
                sec = cited_fn.get(ch.attrib.get("id", "").removeprefix("fn:"), cur_sec)
                for sub in ch:
                    walk(sub, in_quote=in_quote, fn_sec=sec)
            return
        if tag in _HEADINGS and not in_quote and fn_sec is None:
            if not (hid := elem.attrib.get("id", "").strip()) or hid.endswith(".md"):
                raise Error(f"{path}: invalid heading anchor #{hid}")
            if hid in heading_ids:
                raise Error(f"{path}: duplicate heading anchor #{hid}")
            heading_ids.add(hid)
            title = re.sub(r"\s+", " ", _unstash("".join(elem.itertext()), stash)).strip()
            meta = _extract_meta(elem.attrib, path, drop=frozenset({"id"}))
            headings.append({"id": hid, "title": title, "level": int(tag[1]), "meta": meta})
            cur_sec = active = hid
        elif tag == "a":
            if "footnote-backref" in cls:
                return
            href = elem.attrib.get("href", "")
            if "footnote-ref" in cls:
                if href.startswith("#fn:"):
                    cited_fn.setdefault(href.removeprefix("#fn:"), active)
                return
            link_meta = _extract_meta(elem.attrib, path, drop=frozenset({"href", "id"}))
            raw_links.append((href, link_meta, active))
        elif (
            tag == "li"
            and not in_quote
            and fn_sec is None
            and (tm := _TASK.match(_own_inline_text(elem, stash)))
            and (t_body := tm[2].strip()[:512])
        ):
            sa = elem.attrib or (elem[0].attrib if len(elem) and elem[0].tag == "p" else {})
            t_meta = _extract_meta(sa, path, drop=frozenset({"id"}))
            tasks.append({"text": t_body, "done": tm[1] != " ", "section": active, "meta": t_meta})
        for child in elem:
            walk(child, in_quote=in_quote or tag == "blockquote", fn_sec=fn_sec)

    walk(root)
    plain = "\n".join(ln.strip() for ln in _unstash("".join(root.itertext()), stash).splitlines() if ln.strip())
    return plain, headings, raw_links, tasks, heading_ids


def anchor_ids(path: str, data: bytes) -> set[str]:
    """Extract all heading `#id` anchors from any UTF-8 Markdown file."""
    return set(_extract_ast(path, _split_frontmatter(path, data, parse_yaml=False)[1])[4])


def compiled_anchors(item: Mapping[str, Any]) -> set[str]:
    """Return every heading `#id` anchor recorded on a compiled note."""
    return {
        str(h["id"])
        for h in (item.get("headings") or ())
        if isinstance(h, Mapping) and isinstance(h.get("id"), str) and h["id"]
    }


def extract_section(text: str, anchor: str, compiled: dict[str, Any]) -> dict[str, Any]:
    """Extract the raw Markdown section for heading `#anchor` in a single line scan."""
    headings = [h for h in (compiled.get("headings") or ()) if isinstance(h, dict)]
    target_idx = next((idx for idx, h in enumerate(headings) if h.get("id") == anchor), None)
    if target_idx is None:
        raise Error(f"{compiled['ref']}: missing anchor #{anchor}")
    lines = text.splitlines()
    body_start = next((i + 1 for i in range(1, len(lines)) if lines[i] == "---"), 0) if lines[:1] == ["---"] else 0
    heading_lines: list[tuple[int, int]] = []
    idx, fence, prev_heading = body_start, "", False
    while idx < len(lines):
        ln, was_h, prev_heading = lines[idx], prev_heading, False
        fence, in_fence = _step_fence(fence, ln)
        if not in_fence:
            if m_atx := _ATX_RE.match(ln):
                heading_lines.append((idx, len(m_atx[1])))
                idx, prev_heading = idx + 1, True
                continue
            at_blk = (idx == body_start or was_h or not lines[idx - 1].strip()) and bool(ln.strip())
            can_set = at_blk and idx + 1 < len(lines) and not ln.startswith(("    ", "\t"))
            if (
                can_set
                and not ln.lstrip().startswith((">", "-", "*", "+", "|", ":"))
                and re.fullmatch(r"(?:=+|-+)[ ]*", lines[idx + 1])
            ):
                lvl = 1 if lines[idx + 1].startswith("=") else 2
                heading_lines.append((idx, lvl))
                idx, prev_heading = idx + 2, True
                continue
        idx += 1
    heading = headings[target_idx]
    if target_idx >= len(heading_lines):
        return {"anchor": anchor, "heading": heading, "text": ""}
    start_line, start_lvl = heading_lines[target_idx]
    end_line = next((ln for ln, lvl in heading_lines[target_idx + 1 :] if lvl <= start_lvl), len(lines))
    return {"anchor": anchor, "heading": heading, "text": "\n".join(lines[start_line:end_line]).strip()}


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


def compile_note(
    path: str, data: bytes, *, brain_name: str, note_dirs: Iterable[str] = DEFAULT_NOTES
) -> dict[str, Any]:
    """Compile a Markdown note into a compact unified JSON record (`kind: 'note'`)."""
    if (role := note_role(path, note_dirs)) is None:
        raise Error(f"{path}: not an authored note path")
    source_dir, is_bundle = role
    frontmatter, body = _split_frontmatter(path, data, parse_yaml=True)
    if forbidden := sorted(_RESERVED_FM & frontmatter.keys()):
        raise Error(f"{path}: frontmatter cannot override computed field {', '.join(forbidden)}")
    plain_text, headings, raw_links, tasks, _ = _extract_ast(path, body)
    fm = dict(frontmatter)
    if (fm_title := fm.pop("title", None)) is not None and not isinstance(fm_title, str):
        raise Error(f"{path}: title must be a string")
    stem = (PurePosixPath(path).parent.name if is_bundle else PurePosixPath(path).stem).replace("-", " ")
    h1_title = next((str(h["title"]) for h in headings if h["level"] == 1 and str(h["title"]).strip()), stem)
    title = clean((fm_title.strip() if isinstance(fm_title, str) else "") or h1_title, f"{path}: title")
    tags = normalize_tags(fm.pop("tags"), path) if "tags" in fm and fm["tags"] is not None else []
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
        parsed = parse_link(path, target, brain_name, note_dirs=note_dirs, section=sec, attr_meta=link_meta)
        add_link(seen, links, parsed)
    specs = (
        ("headings", headings, {"id", "title", "level"}),
        ("links", links, {"to", "kind"}),
        ("tasks", tasks, {"text", "done"}),
    )
    comp = {
        key: [{fk: fv for fk, fv in item.items() if fk in keep or fv not in ("", {}, [], None)} for item in seq]
        for key, seq, keep in specs
    }
    base = {"ref": path, "source": source_dir, "kind": "note", "title": title, "text": plain_text}
    merged = {**fm, **base, "tags": tags, **comp, "sha256": digest(data)}
    compiled = {k: v for k, v in merged.items() if not (k in _OMIT_OPT and v in ("", {}, [], None))}
    encode(compiled)
    return compiled
