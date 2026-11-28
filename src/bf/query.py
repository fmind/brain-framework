"""Strictly read-only offline retrieval via offset-indexed SQLite FTS5 cache (`.bf/cache/index.sqlite3`) with JSONL fallback."""

from __future__ import annotations

import re
import sqlite3
import sys
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing, suppress
from typing import Any, Literal, overload
from urllib.parse import quote, urlsplit

from bf import config, engine, index, markdown, models, records, refs
from bf.models import MAX_NOTE, NOTES_FILE, Error, InputError, clean, decode, terminal
from bf.storage import Store, relative

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _as_input[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return fn(*args, **kwargs)
    except Error as error:
        raise InputError(str(error)) from error


def _fold_ascii(value: str) -> str:
    """Fold Latin combining diacritics (`0x0300..0x036F`) while preserving Hangul and Japanese dakuten/handakuten."""
    stripped = "".join(ch for ch in unicodedata.normalize("NFD", value) if not (0x0300 <= ord(ch) <= 0x036F))
    return unicodedata.normalize("NFC", stripped).lower()


def _field_matches(words: list[str], word_set: set[str], joined: str, kind: str, val: str) -> bool:
    if kind == "word":
        return val in word_set
    if kind == "prefix":
        return any(w.startswith(val) for w in words)
    return f" {val} " in f" {joined} "


def _extract_snippet(rec: dict[str, Any], query_text: str, width: int = 240) -> tuple[str, str]:
    terms = [t for t in _WORD_RE.findall(_fold_ascii(query_text)) if len(t) >= 2] if query_text else []

    def center(raw: str) -> str:
        flat = re.sub(r"\s+", " ", raw).strip()
        if len(flat) <= width:
            return flat
        if terms:
            folded = _fold_ascii(flat)
            pos = min((p for t in terms if (p := folded.find(t)) >= 0), default=-1)
            if pos >= 0:
                start = 0 if pos <= width // 3 else min(pos - width // 3, len(flat) - width)
                return flat[start : start + width]
        return flat[:width]

    if terms:
        for key, tf, af in (("headings", "title", "id"), ("tasks", "text", "section")):
            for item in rec.get(key) or ():
                if isinstance(item, dict):
                    txt = str(item.get(tf) or "")
                    ft = _fold_ascii(txt)
                    if any(t in ft for t in terms):
                        return center(txt), str(item.get(af) or "")
        if isinstance(txt := rec.get("text"), str) and txt:
            folded_txt = _fold_ascii(txt)
            pos = min((p for t in terms if (p := folded_txt.find(t)) >= 0), default=-1)
            if pos >= 0:
                anc, cur = "", 0
                for heading in rec.get("headings") or ():
                    if (
                        isinstance(heading, dict)
                        and (t0 := heading.get("title"))
                        and 0 <= (ix := txt.find(str(t0), cur)) <= pos
                    ):
                        anc, cur = str(heading.get("id") or ""), ix + len(str(t0))
                return center(txt), anc
    return center(str(rec.get("description") or rec.get("title") or rec.get("text") or rec.get("ref") or "")), ""


def _summary_row(rec: dict[str, Any], *, snippet: str = "", anchor: str = "", score: float = 0.0) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ref": rec.get("ref", ""),
        "source": rec.get("source", ""),
        "kind": rec.get("kind", "memory"),
        "title": rec.get("title", ""),
    }
    if rtype := rec.get("type"):
        row["type"] = rtype
    if st := rec.get("status"):
        row["status"] = st
    if upd := rec.get("updated") or rec.get("created"):
        row["updated"] = upd
    if score:
        row["score"] = score
    desc = str(rec.get("description") or "")
    if desc:
        row["description"] = desc
    if snippet and snippet != desc:
        row["snippet"] = snippet
    return {**row, "anchor": anchor} if anchor else row


def _resolve_scope(scope: str, kind: str, cfg: config.Config, mem_sources: list[str]) -> tuple[str, str, str]:
    """Validate `--scope` + `--kind`, returning `(effective_kind, source_filter, ref_prefix)` or raising `InputError`."""
    if not scope:
        return kind, "", ""
    cl = scope.strip().rstrip("/")
    if not cl:
        raise InputError("scope must be nonempty")
    _as_input(relative, cl)
    notes_set = frozenset(cfg.notes)
    mem_set = frozenset(mem_sources) | {lp.ingest for lp in cfg.loops.values() if lp.ingest}

    if cl == "memories":
        if kind == "note":
            raise InputError("scope 'memories' conflicts with --kind note")
        return "memory", "", ""
    if cl.startswith("memories/"):
        rest = cl.removeprefix("memories/")
        if "/" in rest:
            raise InputError(f"invalid scope {scope!r}")
        src = rest.removesuffix(".jsonl")
        if src == "_notes":
            if kind == "memory":
                raise InputError("scope 'memories/_notes.jsonl' conflicts with --kind memory")
            return "note", "", ""
        if src not in mem_set:
            raise InputError(f"unknown memory source {src!r} in --scope")
        if kind == "note":
            raise InputError(f"memory source {src!r} conflicts with --kind note")
        return "memory", src, ""

    head = cl.split("/", 1)[0]
    if head in notes_set:
        if kind == "memory":
            raise InputError(f"note scope {scope!r} conflicts with --kind memory")
        return "note", head, (cl if "/" in cl else "")

    if "/" not in cl and cl in mem_set:
        if kind == "note":
            raise InputError(f"memory source {cl!r} conflicts with --kind note")
        return "memory", cl, ""

    raise InputError(f"unknown scope {scope!r}; expected a note folder/path or declared memory source")


def _normalize_exclude(raw_items: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for raw in raw_items:
        val = raw.strip().rstrip("/")
        if not val:
            raise InputError("--exclude must be nonempty")
        if val.startswith("memories/"):
            val = val.removeprefix("memories/").removesuffix(".jsonl")
        if not models.NAME_RE.fullmatch(val):
            raise InputError(f"invalid --exclude source {raw!r}")
        out.add(val)
    return out


def _normalize_links_to(store: Store, cfg: config.Config, raw: str) -> tuple[str, ...]:
    target = _as_input(clean, raw.strip(), "--links-to")
    bare = refs.strip_local_uri(target, cfg.name)
    if not bare:
        raise InputError("--links-to target must be nonempty")
    cands: list[str] = list(refs.alias_variants(bare))
    if refs.is_allowed_note_path(bare, cfg.notes) and not bare.endswith(".md"):
        cands.extend(ext for ext in (f"{bare}.md", f"{bare}/index.md") if store.exists(ext))
    target_rec: dict[str, Any] | None = None
    note_cand = next((c for c in cands if refs.is_allowed_note_path(c, cfg.notes) and store.exists(c)), "")
    if note_cand:
        with suppress(Error, OSError):
            target_rec = markdown.compile_note(
                note_cand, store.read(note_cand, MAX_NOTE), brain_name=cfg.name, note_dirs=cfg.notes
            )
    elif (rec_parts := refs.parse_record_ref(bare, cfg.reserved_sources)) is not None:
        con = index.open_readonly(store)
        if con is not None:
            try:
                if index.is_source_fresh(con, store, rec_parts[0], records.memory_file(rec_parts[0])):
                    target_rec = index.lookup_ref(con, store, bare)
            except (sqlite3.Error, index.IndexStaleError, Error):
                pass
            finally:
                con.close()
        if target_rec is None and (hit := _find_record_scan(store, rec_parts[0], bare)) is not None:
            target_rec = hit[1]
    if target_rec is None and (al_hit := find_by_alias(store, bare)) is not None:
        target_rec = al_hit[1]
    if target_rec is not None:
        if isinstance(r := target_rec.get("ref"), str) and r:
            cands.append(r)
        if isinstance(ent := target_rec.get("entity"), str) and ent:
            cands.extend(refs.alias_variants(ent))
        for al in target_rec.get("aliases") or ():
            if isinstance(al, str) and al:
                cands.extend(refs.alias_variants(al))
    return tuple(dict.fromkeys(cands))


def _ref_matches(ref: str, ref_prefix: str) -> bool:
    if not ref_prefix:
        return True
    return (
        ref == ref_prefix
        or ref == f"{ref_prefix}.md"
        or ref == f"{ref_prefix}/index.md"
        or ref.startswith(f"{ref_prefix}/")
    )


def _sort_scan_matches(matches: list[tuple[float, str, str, dict[str, Any]]], *, has_clauses: bool) -> None:
    matches.sort(key=lambda x: x[2])
    matches.sort(key=lambda x: x[1], reverse=True)
    if has_clauses:
        matches.sort(key=lambda x: -x[0])
    else:
        matches.sort(key=lambda x: 0 if x[3].get("kind") == "note" else 1)


def _scan_fallback(
    store: Store,
    *,
    brain_name: str,
    query_text: str,
    kind: str,
    source: str,
    ref_prefix: str,
    rtype: str,
    status: str,
    tag: str,
    since_key: str,
    links_to: Sequence[str],
    exclude: set[str],
    mem_sources: list[str],
    limit: int | None,
) -> tuple[list[tuple[dict[str, Any], float]], bool]:
    """Read-only JSONL fallback when `.bf/cache/index.sqlite3` is absent or stale (bounded top-K memory, zero disk mutation)."""
    clauses = index.parse_query_clauses(query_text, fold=_fold_ascii) if query_text else []
    if query_text and not clauses:
        return [], False
    links_to_set = set(links_to)
    files: list[str] = []
    if kind in {"", "note"} and store.exists(NOTES_FILE):
        files.append(NOTES_FILE)
    if kind in {"", "memory"}:
        for src in mem_sources:
            if source and src != source:
                continue
            if src in exclude:
                continue
            files.append(records.memory_file(src))

    matches: list[tuple[float, str, str, dict[str, Any]]] = []
    scanned = 0
    for path in files:
        with suppress(FileNotFoundError), closing(records.iter_jsonl(store, path, check_duplicates=False)) as rows:
            for _, rec in rows:
                scanned += 1
                if scanned > models.MAX_TOTAL_FILES:
                    raise Error(f"memories exceeds the {models.MAX_TOTAL_FILES:,}-record total scan limit")
                rk = "note" if path == NOTES_FILE else "memory"
                rs = str(rec.get("source") or "")
                rr = str(rec.get("ref") or "")
                if kind and rk != kind:
                    continue
                if source and rs != source:
                    continue
                if rs in exclude:
                    continue
                if ref_prefix and not _ref_matches(rr, ref_prefix):
                    continue
                if rtype and str(rec.get("type") or "") != rtype:
                    continue
                if status and str(rec.get("status") or "") != status:
                    continue
                if tag and tag not in (rec.get("tags") or ()):
                    continue
                sort_ts = models.ts_key(str(rec.get("updated") or rec.get("created") or ""))
                if since_key and sort_ts < since_key:
                    continue
                if links_to_set:
                    lks = rec.get("links") or ()
                    if not any(
                        isinstance(lk, dict)
                        and isinstance(lk.get("to"), str)
                        and refs.strip_local_uri(str(lk["to"]), brain_name) in links_to_set
                        for lk in lks
                    ):
                        continue
                score = 0.0
                if clauses:
                    aliases_raw = [
                        *(str(a) for a in (rec.get("aliases") or ()) if isinstance(a, str)),
                        str(rec.get("entity") or ""),
                    ]
                    fields = (
                        (10.0, _WORD_RE.findall(_fold_ascii(str(rec.get("title") or "")))),
                        (5.0, _WORD_RE.findall(_fold_ascii(str(rec.get("description") or "")))),
                        (3.0, _WORD_RE.findall(_fold_ascii(" ".join(str(x) for x in (rec.get("tags") or ()))))),
                        (4.0, _WORD_RE.findall(_fold_ascii(" ".join(aliases_raw)))),
                        (2.0, _WORD_RE.findall(_fold_ascii(rr))),
                        (1.0, _WORD_RE.findall(_fold_ascii(str(rec.get("text") or "")))),
                    )
                    field_pre = [(w, toks, set(toks), " ".join(toks)) for w, toks in fields]
                    matched_all = True
                    for c_kind, c_val in clauses:
                        c_score = sum(
                            w
                            for w, toks, tset, joined in field_pre
                            if _field_matches(toks, tset, joined, c_kind, c_val)
                        )
                        if c_score == 0.0:
                            matched_all = False
                            break
                        score += c_score
                    if not matched_all:
                        continue
                    if rk == "note":
                        score *= index.NOTE_BOOST
                matches.append((score, sort_ts, rr, rec))
                if limit is not None and len(matches) > max(limit * 4, 200):
                    _sort_scan_matches(matches, has_clauses=bool(clauses))
                    del matches[limit + 1 :]

    _sort_scan_matches(matches, has_clauses=bool(clauses))

    trunc = limit is not None and len(matches) > limit
    picked = matches[:limit] if limit is not None else matches
    return [(rec, round(sc, 4)) for sc, _, _, rec in picked], trunc


def validate_query_args(
    query_text: str | None = None,
    *,
    kind: str = "",
    scope: str = "",
    rtype: str = "",
    status: str = "",
    tag: str = "",
    since: str = "",
    links_to: str = "",
    exclude: Iterable[str] = (),
    limit: int = 20,
    all_items: bool = False,
) -> tuple[str, str, set[str]]:
    """Validate brain-independent `bf query` arguments, returning `(clean_query, since_key, exclude_set)`."""
    if scope and not scope.strip():
        raise InputError("scope must be nonempty")
    for flag, val in (("--type", rtype), ("--status", status), ("--tag", tag), ("--links-to", links_to)):
        if val and not val.strip():
            raise InputError(f"{flag} must be nonempty")
    q_clean = _as_input(clean, query_text, "query") if query_text is not None else ""
    if q_clean:
        index.build_fts_query(q_clean)
    if not any((q_clean, kind, scope.strip(), rtype, status, tag, since, links_to)):
        raise InputError(
            "pass QUERY or at least one filter (--kind, --scope, --type, --status, --tag, --since, --links-to)"
        )
    if not all_items and (limit < 1 or limit > 5000):
        raise InputError("limit must be between 1 and 5000")
    if kind not in {"", "note", "memory"}:
        raise InputError("kind must be 'note' or 'memory'")
    since_key = ""
    if since:
        since_norm = _as_input(models.normalize_iso_date_or_time, since, "--since")
        since_key = models.ts_key(since_norm, start_of_day=True)
    excl_set = _normalize_exclude(exclude)
    return q_clean, since_key, excl_set


def query(
    store: Store,
    query_text: str | None = None,
    *,
    kind: str = "",
    scope: str = "",
    rtype: str = "",
    status: str = "",
    tag: str = "",
    since: str = "",
    links_to: str = "",
    exclude: Iterable[str] = (),
    all_sources: bool = False,
    limit: int = 20,
    all_items: bool = False,
) -> dict[str, Any]:
    """Read-only ranked BM25 FTS5 & structured query over notes and `memories/*.jsonl`."""
    q_clean, since_key, excl_set = validate_query_args(
        query_text,
        kind=kind,
        scope=scope,
        rtype=rtype,
        status=status,
        tag=tag,
        since=since,
        links_to=links_to,
        exclude=exclude,
        limit=limit,
        all_items=all_items,
    )

    cfg = config.load(store)
    mem_sources = records.valid_memory_sources(store, reserved=cfg.reserved_sources)
    eff_kind, source_filter, ref_prefix = _resolve_scope(scope, kind, cfg, mem_sources)
    default_excl: list[str] = []
    if cfg.search_exclude and not all_sources and not source_filter and eff_kind != "note":
        default_excl = sorted(set(cfg.search_exclude))
        excl_set.update(default_excl)
    links_to_cands = _normalize_links_to(store, cfg, links_to) if links_to else ()
    eff_limit = None if all_items else limit

    # Per-source freshness check so one stale memory source never invalidates `--kind note` or another fresh `--scope`.
    if eff_kind == "note":
        stale_idx = index.is_stale(store, (), include_notes=True, exact_sources=False)
    elif eff_kind == "memory" and source_filter:
        stale_idx = index.is_stale(store, (source_filter,), include_notes=False, exact_sources=False)
    elif eff_kind == "memory":
        stale_idx = index.is_stale(store, mem_sources, include_notes=False, exact_sources=True, ignore_sources=excl_set)
    else:
        stale_idx = index.is_stale(store, mem_sources, include_notes=True, exact_sources=True, ignore_sources=excl_set)

    con = None if stale_idx else index.open_readonly(store)
    hits: list[tuple[dict[str, Any], float]] = []
    trunc = False
    eng_name = "scan"
    if con is not None:
        try:
            hits, trunc = index.search_fts(
                con,
                store,
                query_text=q_clean,
                kind=eff_kind,
                source=source_filter,
                ref_prefix=ref_prefix,
                rtype=rtype.strip(),
                status=status.strip(),
                tag=tag.strip(),
                since_key=since_key,
                links_to=links_to_cands,
                exclude=excl_set,
                limit=eff_limit,
            )
            eng_name = "fts5"
        except (sqlite3.Error, index.IndexStaleError, Error):
            stale_idx = True
        finally:
            con.close()
    if eng_name != "fts5":
        hits, trunc = _scan_fallback(
            store,
            brain_name=cfg.name,
            query_text=q_clean,
            kind=eff_kind,
            source=source_filter,
            ref_prefix=ref_prefix,
            rtype=rtype.strip(),
            status=status.strip(),
            tag=tag.strip(),
            since_key=since_key,
            links_to=links_to_cands,
            exclude=excl_set,
            mem_sources=mem_sources,
            limit=eff_limit,
        )

    items = []
    for rec, score in hits:
        snip, anc = _extract_snippet(rec, q_clean) if q_clean else ("", "")
        items.append(_summary_row(rec, snippet=snip, anchor=anc, score=score))

    unbuilt: list[str] = []
    if eff_kind in {"", "note"}:
        all_unbuilt = engine.unbuilt_note_dirs(store, cfg.notes, fast=True)
        unbuilt = (
            [d for d in all_unbuilt if d == source_filter]
            if (source_filter and source_filter in cfg.notes)
            else all_unbuilt
        )

    return {
        "ok": True,
        "brain": cfg.name,
        "engine": eng_name,
        **({"query": q_clean} if q_clean else {}),
        **({"excluded_sources": default_excl} if default_excl else {}),
        "count": len(items),
        "truncated": trunc,
        "items": items,
        **({"unbuilt_notes": unbuilt} if unbuilt else {}),
        **({"index_stale": True} if stale_idx else {}),
    }


def _find_record_scan(store: Store, source: str, ref: str) -> tuple[str, dict[str, Any]] | None:
    rec_path = records.memory_file(source)
    if not store.exists(rec_path):
        return None
    needle = models.encode(ref).rstrip(b"\n")
    with closing(store.iter_lines(rec_path, models.MAX_FILE)) as lines:
        for idx_ln, raw_ln in enumerate(lines, 1):
            if needle not in raw_ln and b"\\u" not in raw_ln and b"\\/" not in raw_ln:
                continue
            if not (stripped := raw_ln.strip()):
                continue
            parsed = decode(stripped, f"{rec_path}:{idx_ln}", check_duplicates=False)
            if isinstance(parsed, dict) and parsed.get("ref") == ref:
                return rec_path, parsed
    return None


def find_by_alias(store: Store, identity: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve `identity` via `aliases` or `entity` (O(1) via SQLite index when fresh, else JSONL scan)."""
    reserved = models.RESERVED_SOURCES | frozenset(models.DEFAULT_NOTES)
    with suppress(Error):
        reserved = config.load(store).reserved_sources
    mem_sources = records.valid_memory_sources(store, reserved=reserved)
    if not index.is_stale(store, mem_sources):
        con = index.open_readonly(store)
        if con is not None:
            try:
                if hit := index.lookup_alias(con, store, identity):
                    hp = (
                        str(hit["ref"])
                        if hit.get("kind") == "note"
                        else records.memory_file(str(hit.get("source", "")))
                    )
                    return hp, hit
                return None
            except (sqlite3.Error, index.IndexStaleError, Error):
                pass
            finally:
                con.close()

    candidates = set(refs.alias_variants(identity))
    needles = [models.encode(c).rstrip(b"\n") for c in candidates]
    groups: list[list[str]] = []
    if store.exists(NOTES_FILE):
        groups.append([NOTES_FILE])
    groups.append([records.memory_file(s) for s in mem_sources])

    scanned = 0
    for group in groups:
        matched: list[tuple[str, dict[str, Any]]] = []
        for path in group:
            try:
                with closing(store.iter_lines(path, models.MAX_FILE)) as lines:
                    for raw_ln in lines:
                        if not raw_ln.strip():
                            continue
                        scanned += 1
                        if scanned > models.MAX_TOTAL_FILES:
                            raise Error(f"memories exceeds the {models.MAX_TOTAL_FILES:,}-record total scan limit")
                        if not any(n in raw_ln for n in needles) and b"\\u" not in raw_ln and b"\\/" not in raw_ln:
                            continue
                        with suppress(Error):
                            rec = decode(raw_ln.strip(), path, check_duplicates=False)
                            if isinstance(rec, dict):
                                ent = rec.get("entity")
                                al = rec.get("aliases")
                                if (isinstance(ent, str) and ent in candidates) or (
                                    isinstance(al, list) and any(isinstance(a, str) and a in candidates for a in al)
                                ):
                                    is_note_bucket = path == NOTES_FILE and rec.get("kind") == "note"
                                    safe_rec = rec if is_note_bucket else {**rec, "kind": "memory"}
                                    hp = str(safe_rec.get("ref") or path) if is_note_bucket else path
                                    matched.append((hp, safe_rec))
            except (Error, OSError) as err:
                if isinstance(err, Error) and "total scan limit" in str(err):
                    raise
        if matched:
            matched.sort(key=lambda pair: str(pair[1].get("ref") or ""))
            return matched[0]
    return None


def _cap_json_bytes(out: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    if len(terminal(out)) <= max_bytes:
        return out
    cand = {**out, "truncated": True}
    holder: dict[str, Any] | None = None
    if isinstance(cand.get("text"), str) and cand["text"]:
        holder = cand
    elif isinstance(rec := cand.get("record"), dict):
        holder = cand["record"] = dict(rec)
    if holder is not None and isinstance(holder.get("text"), str) and holder["text"]:
        val = str(holder["text"])
        lo, hi, best = 0, len(val), ""
        while lo <= hi:
            holder["text"] = val[: (mid := (lo + hi) // 2)]
            if len(terminal(cand)) <= max_bytes:
                best, lo = val[:mid], mid + 1
            else:
                hi = mid - 1
        holder["text"] = best
        if len(terminal(cand)) <= max_bytes:
            return cand
    # Shrink heavy optional fields inside `record` without ever silently dropping `record` itself.
    if isinstance(rec_d := cand.get("record"), dict):
        for drop_k in (
            "text",
            "links",
            "headings",
            "tasks",
            "attributes",
            "description",
            "aliases",
            "tags",
            "sha256",
        ):
            if len(terminal(cand)) <= max_bytes:
                return cand
            rec_d.pop(drop_k, None)
    else:
        for drop in ("text", "heading", "anchor"):
            if len(terminal(cand)) <= max_bytes:
                return cand
            cand.pop(drop, None)
    if len(terminal(cand)) > max_bytes:
        raise InputError(f"--max-bytes {max_bytes} is too small for JSON envelope")
    return cand


def _cap_raw_bytes(text: str, max_bytes: int, *, warn: bool = False) -> str:
    raw_b = text.encode("utf-8")
    if len(raw_b) <= max_bytes:
        return text
    if warn:
        sys.stderr.write(f"bf: warning: truncated output to {max_bytes} bytes\n")
    return raw_b[:max_bytes].decode("utf-8", errors="ignore")


def _read_note_file(
    store: Store,
    cfg: config.Config,
    to: str,
    *,
    anchor: str,
    fmt: str,
    max_bytes: int | None,
    warn_raw_trunc: bool,
) -> dict[str, Any] | str:
    if not store.exists(to) and not to.endswith(".md"):
        for cand in (f"{to}.md", f"{to}/index.md"):
            if refs.is_allowed_note_path(cand, cfg.notes) and store.exists(cand):
                to = cand
                break
    if not store.exists(to):
        raise Error(f"note {to} not found")
    raw_bytes = store.read(to, MAX_NOTE)
    text = raw_bytes.decode("utf-8").removeprefix("\ufeff")
    is_note = markdown.is_authored_note(store, to, cfg.notes)
    if anchor:
        if not is_note:
            raise Error(f"{to}: missing anchor #{anchor}")
        sec = markdown.compile_note_section(to, raw_bytes, anchor, brain_name=cfg.name, note_dirs=cfg.notes)
        if fmt == "raw":
            raw_sec = str(sec["text"])
            return _cap_raw_bytes(raw_sec, max_bytes, warn=warn_raw_trunc) if max_bytes is not None else raw_sec
        out = {"ok": True, "brain": cfg.name, "kind": "section", "ref": to, "path": to, **sec}
        return _cap_json_bytes(out, max_bytes) if max_bytes is not None else out
    if fmt == "raw":
        return _cap_raw_bytes(text, max_bytes, warn=warn_raw_trunc) if max_bytes is not None else text
    if not is_note:
        out = {"ok": True, "brain": cfg.name, "kind": "file", "ref": to, "path": to, "text": text}
        return _cap_json_bytes(out, max_bytes) if max_bytes is not None else out
    comp = markdown.compile_note(to, raw_bytes, brain_name=cfg.name, note_dirs=cfg.notes)
    out = {"ok": True, "brain": cfg.name, "kind": "note", "ref": to, "path": to, "record": comp}
    return _cap_json_bytes(out, max_bytes) if max_bytes is not None else out


@overload
def read_target(
    store: Store, raw_target: str, *, fmt: Literal["raw"], max_bytes: int | None = None, warn_raw_trunc: bool = False
) -> str: ...
@overload
def read_target(
    store: Store,
    raw_target: str,
    *,
    fmt: Literal["json"] = "json",
    max_bytes: int | None = None,
    warn_raw_trunc: bool = False,
) -> dict[str, Any]: ...
@overload
def read_target(
    store: Store, raw_target: str, *, fmt: str = "json", max_bytes: int | None = None, warn_raw_trunc: bool = False
) -> dict[str, Any] | str: ...
def validate_read_args(raw_target: str, *, fmt: str = "json", max_bytes: int | None = None) -> str:
    """Validate brain-independent `bf read` arguments and return the stripped, control-free target."""
    if fmt not in {"json", "raw"}:
        raise InputError("format must be 'json' or 'raw'")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise InputError("--max-bytes must be at least 1")
    if not (target := raw_target.strip()):
        raise InputError("target is required; use bf status for brain overview")
    target = _as_input(clean, target, "target")
    if target.startswith("#"):
        raise InputError("target cannot be a bare #anchor fragment; include a note path or alias")
    return target


def read_target(
    store: Store, raw_target: str, *, fmt: str = "json", max_bytes: int | None = None, warn_raw_trunc: bool = False
) -> dict[str, Any] | str:
    """Strictly read-only allowlisted resolver for notes, `#anchor` sections, `source:id` records, aliases, or `bf://` URIs."""
    target = validate_read_args(raw_target, fmt=fmt, max_bytes=max_bytes)

    cfg = config.load(store)

    def rec_out(hp: str, hr: dict[str, Any], default_ref: str, *, anchor: str = "") -> dict[str, Any] | str:
        rk, rr = str(hr.get("kind", "memory")), str(hr.get("ref", default_ref))
        if rk == "note" and refs.is_allowed_note_path(rr, cfg.notes) and store.exists(rr):
            return _read_note_file(
                store, cfg, rr, anchor=anchor, fmt=fmt, max_bytes=max_bytes, warn_raw_trunc=warn_raw_trunc
            )
        if anchor:
            raise InputError("JSON records do not support #fragments")
        if fmt == "raw":
            txt = str(hr.get("text") or "")
            return _cap_raw_bytes(txt, max_bytes, warn=warn_raw_trunc) if max_bytes is not None else txt
        env = {"ok": True, "brain": cfg.name, "kind": rk, "ref": rr, "path": hp, "record": hr}
        return _cap_json_bytes(env, max_bytes) if max_bytes is not None else env

    # Try plain non-path alias (including `<alias>#<anchor>`) when head is nonempty and has no `/`, `:`, `?`, or `.md`.
    al_head, has_al_hash, al_anchor = target.partition("#")
    if al_head and "/" not in al_head and ":" not in al_head and "?" not in al_head and not al_head.endswith(".md"):
        if has_al_hash and not al_anchor:
            raise InputError("invalid empty #anchor fragment")
        if alias_hit := find_by_alias(store, al_head):
            return rec_out(alias_hit[0], alias_hit[1], al_head, anchor=al_anchor)
        raise Error(f"target {target} not found")

    parsed = _as_input(refs.parse_link, "target", target, cfg.name, note_dirs=cfg.notes, relative_base=False)
    kind, to, anchor = str(parsed["kind"]), str(parsed["to"]), str(parsed["anchor"])
    if kind == "brain":
        host, _, rest = to.removeprefix("bf://").partition("/")
        uri = urlsplit(target.partition("#")[0]) if target.lower().startswith("bf://") else None
        raw_r = uri.path.lstrip("/") if uri else ""
        http_q = uri.query if uri and raw_r.partition(":")[2].lower().startswith(("https://", "http://")) else ""
        fwd = (
            (f"{raw_r}?{http_q}" if http_q else raw_r)
            if uri and refs.is_record_ref(raw_r, models.RESERVED_SOURCES)
            else quote(rest, safe="/:@")
        )
        sub = f"bf:///{fwd}" + (f"#{quote(anchor, safe='')}" if anchor else "")
        sib = config.resolve_brain(store, cfg, host)
        return read_target(sib, sub, fmt=fmt, max_bytes=max_bytes, warn_raw_trunc=warn_raw_trunc)

    if kind == "url":
        hit = find_by_alias(store, to)
        if hit is None:
            raise InputError("external URLs are not read offline; pass a note path, source:id, alias, or bf:// URI")
        return rec_out(hit[0], hit[1], to, anchor=anchor)

    if kind == "record":
        parsed_r = refs.parse_record_ref(to, cfg.reserved_sources)
        found: tuple[str, dict[str, Any]] | None = None
        if not anchor and parsed_r is not None:
            rec_path = records.memory_file(parsed_r[0])
            con = index.open_readonly(store)
            if con is not None:
                try:
                    if index.is_source_fresh(con, store, parsed_r[0], rec_path):
                        found = (rec_path, hit_r) if (hit_r := index.lookup_ref(con, store, to)) else ("", {})
                except (sqlite3.Error, index.IndexStaleError, Error):
                    found = None
                finally:
                    con.close()
            if found is None:
                found = _find_record_scan(store, parsed_r[0], to)
            elif not found[0]:
                found = None
        if found is not None:
            return rec_out(found[0], found[1], to)
        if (alias_hit := find_by_alias(store, to)) is not None:
            return rec_out(alias_hit[0], alias_hit[1], to, anchor=anchor)
        if anchor:
            raise InputError("JSON records do not support #fragments")
        raise Error(f"record {to} not found")

    if not refs.is_allowed_note_path(to, cfg.notes):
        raise InputError(f"{to}: only configured note directories are readable via bf read")

    return _read_note_file(store, cfg, to, anchor=anchor, fmt=fmt, max_bytes=max_bytes, warn_raw_trunc=warn_raw_trunc)
