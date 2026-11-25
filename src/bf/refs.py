"""Leaf universal reference, link, tag, and URL parser/validator (breaks markdown <-> records coupling)."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from bf import models, storage
from bf.models import DEFAULT_NOTES, RESERVED_SOURCES, Error, clean, clean_env, invisible

META_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*\Z")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):(.*)\Z")
URL_SCHEMES = frozenset({"https", "http", "mailto", "urn"})
_FORBIDDEN = frozenset({"javascript", "data", "file", "vbscript", "tel"})
_CONTROL_OR_SPACE = re.compile(r"[\x00-\x1f\x7f-\x9f\s]")
_DEFAULT_RESERVED = RESERVED_SOURCES | frozenset(DEFAULT_NOTES)


def _clean_vis(val: str, label: str) -> str:
    if invisible(out := clean(val, label)):
        raise Error(f"{label} contains invisible format characters")
    return out


def _has_dot_segment(path: str) -> bool:
    return any(part.startswith(".") for part in path.split("/") if part)


def _bad_rel(path: str) -> bool:
    try:
        storage.relative(path)
    except Error:
        return True
    return False


def validate_url(url: str, label: str = "url") -> str:
    """Validate that `url` uses a lowercase allowed external scheme (`https://`, `http://`, `mailto:`, `urn:`) with host/body."""
    cleaned = _clean_vis(url, label)
    if _CONTROL_OR_SPACE.search(cleaned):
        raise Error(f"{label} must not contain whitespace or control characters")
    m = _SCHEME.match(cleaned)
    if not m or m[1] not in URL_SCHEMES:
        raise Error(f"{label} must use https://, http://, mailto:, or urn:")
    if m[1] in {"https", "http"}:
        try:
            netloc = urlsplit(cleaned).netloc
        except ValueError as err:
            raise Error(f"{label} must be a valid URL") from err
        if not m[2].startswith("//") or not netloc:
            raise Error(f"{label} must use https:// or http:// with a host")
    elif not m[2]:
        raise Error(f"{label} must be nonempty after {m[1]}:")
    return cleaned


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


def is_allowed_note_path(path: str, note_dirs: Iterable[str] = DEFAULT_NOTES) -> bool:
    """Allowlist check: path must be normalized, dot-segment-free, and inside configured `note_dirs`."""
    if not path or _bad_rel(path) or _has_dot_segment(path):
        return False
    return path.split("/", 1)[0] in frozenset(note_dirs)


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
    if not is_allowed_note_path(out, note_dirs):
        raise Error(f"{src}: link target must be inside configured note directories without dotfiles")
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
        if not META_KEY_RE.fullmatch(key) or key in meta:
            raise Error(f"{src}: link query keys must be unique identifiers")
        meta[key] = _clean_vis(val, f"{src}: link parameter {key}")
    return meta


def record_link_problem(
    lnk: Mapping[str, Any], *, note_dirs: Iterable[str] = DEFAULT_NOTES, reserved: frozenset[str] | None = None
) -> str:
    """Return a sanitized error string if normalized link object `lnk` violates schema/confinement rules, else `''`."""
    dirs = frozenset(note_dirs)
    res = (RESERVED_SOURCES | dirs) if reserved is None else reserved
    kind, target, anchor = str(lnk.get("kind", "")), str(lnk.get("to", "")), str(lnk.get("anchor", ""))
    sib_host, _, sib_path = target.removeprefix("bf://").partition("/")
    bad_ch = bool(_CONTROL_OR_SPACE.search(target))
    if kind == "url":
        try:
            validate_url(target, "url link")
        except Error as err:
            return str(err)
    if kind == "record" and not is_record_ref(target, res):
        return f"record link {target!r} must use <source>:<id>"
    if kind == "note" and (bad_ch or ":" in target or "?" in target or not is_allowed_note_path(target, dirs)):
        return f"note link {target!r} must target a configured note directory without dotfiles"
    if kind == "brain":
        bad_brain = bad_ch or not target.startswith("bf://") or not models.NAME_RE.fullmatch(sib_host) or not sib_path
        sib_rec = is_record_ref(sib_path, RESERVED_SOURCES)
        sib_note = (
            ":" not in sib_path
            and "?" not in sib_path
            and not _bad_rel(sib_path)
            and not _has_dot_segment(sib_path)
            and sib_path.split("/", 1)[0] not in RESERVED_SOURCES
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
            if not META_KEY_RE.fullmatch(key):
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
    if scheme in URL_SCHEMES:
        validate_url(target, f"{source_path}: URL link")
        return make_link(target, "url")
    if scheme in _FORBIDDEN:
        raise Error(f"{source_path}: unsupported link scheme {scheme}:")
    if scheme == "bf":
        if not target.lower().startswith("bf://") or re.search(r"%(?![0-9a-fA-F]{2})", target):
            raise Error(f"{source_path}: invalid bf:// URI")
        head, anchor = _split_target_parts(source_path, target, "invalid bf:// URI or empty # fragment")
        try:
            has_q, uri = "?" in head, urlsplit(head)
        except ValueError as err:
            raise Error(f"{source_path}: invalid bf:// URI") from err
        _unquote(uri.netloc, source_path)
        raw_rest, netloc = uri.path.lstrip("/"), uri.netloc
        is_local = netloc in {"", brain_name}
        if netloc in reserved or netloc in dirs:
            raise Error(f"{source_path}: use bf:///{netloc}/... instead of bf://{netloc}/...")
        if not is_local and not models.NAME_RE.fullmatch(netloc):
            raise Error(f"{source_path}: invalid cross-brain name {netloc!r} in bf:// URI")
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
            if _bad_rel(rest) or _has_dot_segment(rest):
                raise Error(f"{source_path}: invalid path in bf:// URI")
            if not is_local and rest.split("/", 1)[0] in RESERVED_SOURCES:
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


def dedupe_typed_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop bare duplicate links when an otherwise identical link with metadata exists."""
    typed = {(it["to"], it["kind"], it.get("anchor", ""), it.get("section", "")) for it in links if it.get("meta")}
    return [
        it
        for it in links
        if it.get("meta") or (it["to"], it["kind"], it.get("anchor", ""), it.get("section", "")) not in typed
    ]


def normalize_tags(raw_tags: object, label: str) -> list[str]:
    """Validate and deduplicate a list of string/numeric tags without spaces or slashes."""
    if not isinstance(raw_tags, list):
        raise Error(f"{label}: tags must be a list of strings")
    out, seen = [], set()
    for item in raw_tags:
        val = str(item) if type(item) in {int, float} else item
        if not isinstance(val, str) or not val.strip() or re.search(r"[/\\\s]", val) or invisible(val):
            raise Error(f"{label}: tags must be nonempty tokens without spaces, slashes, or invisible characters")
        clean(val, f"{label}: tag")
        if val not in seen:
            seen.add(val)
            out.append(val)
    return out
