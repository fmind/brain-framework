"""Offline retrieval, deterministic evaluation, and combined brain checks via `rg` and `jq`."""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Any, TypedDict, Unpack, cast
from urllib.parse import quote, urlsplit

from bf import models
from bf.models import AUTHORED, MAX_BATCH, MAX_NOTE, Error, InputError, clean, decode, encode, terminal
from bf.proc import drain_tail as drain_tail
from bf.proc import resolve_tool as resolve_tool
from bf.proc import safe_env as safe_env
from bf.proc import stop_proc as stop_proc
from bf.proc import stream_proc
from bf.storage import Store, relative

if TYPE_CHECKING:
    from datetime import datetime

JQ_PRELUDE = (
    'def modulemeta: error("disabled"); def modulemeta($m): error("disabled"); '
    "def get_jq_origin: null; def get_prog_origin: null; def get_search_list: []; def env: {}; {} as $ENV |"
)
_NS_A = ("ref", "source", "kind", "title", "status", "updated", "text", "sha256", "id")
_NOTE_STRUCT = frozenset((*_NS_A, "headings", "links", "elements", "tasks", "anchors"))
_HEAD_RE = re.compile(r"(?m)^ {0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+\{[^}]*\})?[ \t]*$")


def _decode_obj(store: Store, path: str, *, check_duplicates: bool = True) -> dict[str, Any]:
    if not isinstance(parsed := decode(store.read(path), path, check_duplicates=check_duplicates), dict):
        raise Error(f"{path}: expected a JSON object")
    return cast("dict[str, Any]", parsed)


def _as_input[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return fn(*args, **kwargs)
    except Error as error:
        raise InputError(str(error)) from error


def _mem_rel(store: Store, raw_path: str) -> str | None:
    with suppress(ValueError):
        rel = Path(raw_path).resolve().relative_to(store.root).as_posix()
        return rel if rel.startswith("memories/") and rel.endswith(".json") else None
    return None


def _target_scope(store: Store, scope: str, kind: str) -> tuple[list[str], str]:
    from bf import markdown as md

    def ok(s: str) -> bool:
        return (kind != "note" or s in AUTHORED) and (kind != "memory" or s not in AUTHORED)

    if stripped := scope.strip():
        _as_input(relative, cl := stripped.rstrip("/"))
        if md.okf_role(cl) is not None:
            return ([cp] if ok(cl.split("/")[0]) and store.exists(cp := md.compiled_note_path(cl)) else []), ""
        if cl.endswith(".json") and cl.startswith("memories/"):
            return ([cl] if ok(cl.split("/")[1]) and store.exists(cl) else []), ""
        _as_input(relative, sub := cl if cl == "memories" or cl.startswith("memories/") else f"memories/{cl}")
        if sub != "memories":
            if not ok(sub_s := sub.split("/")[1]):
                return [], ""
            if store.is_dir(sub):
                return [sub], ""
            b = f"memories/{sub_s}"
            hits = [p for p in store.files(b) if p.endswith(".json") and p.startswith(sub)] if store.is_dir(b) else []
            return hits, ""
    if not store.is_dir("memories"):
        return [], ""
    if kind == "note":
        return [f"memories/{d}" for d in sorted(AUTHORED) if store.is_dir(f"memories/{d}")], ""
    return ["memories"], ("memory" if kind == "memory" else "")


def _extract_snippet(rec: dict[str, Any], pattern: str, data: dict[str, Any], width: int = 240) -> tuple[str, str]:
    s0 = subs[0] if isinstance(subs := data.get("submatches"), list) and subs and isinstance(subs[0], dict) else {}
    mt = s0["match"].get("text") if isinstance(s0.get("match"), dict) else None
    rx: re.Pattern[str] | None = None
    with suppress(re.error):
        rx = re.compile(re.escape(mt) if isinstance(mt, str) and mt else pattern, re.IGNORECASE)

    def center(raw: str, pat: re.Pattern[str] | None = rx) -> str:
        if len(flat := re.sub(r"\s+", " ", raw).strip()) <= width:
            return flat
        pos = m.start() if pat and (m := pat.search(flat)) else 0
        i0 = 0 if pos <= width // 3 else min(pos - width // 3, len(flat) - width)
        return flat[i0 : i0 + width]

    if rx is not None:
        for key, tf, af in (("headings", "title", "id"), ("elements", "text", "section"), ("tasks", "text", "section")):
            for it in rec.get(key) or ():
                if isinstance(it, dict) and rx.search(t := str(it.get(tf) or "")):
                    return center(t), str(it.get(af) or it.get("id") or "")
        if isinstance(txt := rec.get("text"), str) and (m := rx.search(txt)):
            anc, cur = "", 0
            for h in rec.get("headings") or ():
                if isinstance(h, dict) and (t0 := h.get("title")) and 0 <= (ix := txt.find(str(t0), cur)) <= m.start():
                    anc, cur = str(h.get("id") or ""), ix + len(str(t0))
            if not anc and (hms := _HEAD_RE.findall(txt[: m.start()])):
                anc = re.sub(r"[^a-z0-9]+", "-", hms[-1].lower()).strip("-")
            return center(txt), anc
        pri = ("title", "summary", "description")
        st: list[tuple[str, Any]] = [(k, rec[k]) for k in pri if k in rec]
        st.extend((k, v) for k, v in rec.items() if k not in {"sha256", "kind", "source", *pri})
        while st:
            fk, val = st.pop(0)
            if isinstance(val, str) and rx.search(val):
                ttl = str(rec.get("title") or "")
                snip = f"{ttl} ({val})" if fk in {"id", "ref"} and ttl else val
                return center(snip, None if fk in {"id", "ref"} else rx), ""
            if isinstance(val, (list, dict)):
                st.extend(((fk, x) for x in val) if isinstance(val, list) else val.items())
    return center(str(rec.get("title") or rec.get("text") or rec.get("summary") or rec.get("ref") or "")), ""


def _rg_scan(
    store: Store, pattern: str, targets: list[str], mem_f: str, limit: int | None, *, with_rows: bool = True
) -> tuple[list[str], list[dict[str, Any]], bool]:
    rg_bin = resolve_tool("rg")

    def fail(rc: int, err_b: bytes) -> None:
        if rc not in {0, 1} or (rc == 1 and err_b.strip()):
            et = err_b.decode("utf-8", "replace").strip()
            msg = f"rg failed: {et.splitlines()[-1] if et else 'invalid regex pattern'}"
            raise InputError(msg) if rc == 2 else Error(msg)

    if not targets:
        try:
            kw: dict[str, Any] = {"input": b"", "capture_output": True, "cwd": "/", "env": safe_env(rg_bin)}
            chk = subprocess.run([rg_bin, "--no-config", "-e", pattern], **kw, timeout=5, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as e:
            raise Error("rg timed out after 5s") from e
        fail(chk.returncode, chk.stderr)
        return [], [], False
    ex = ["--glob", "!projects/**", "--glob", "!concepts/**", "--glob", "!actions/**"] if mem_f == "memory" else []
    gl = ["--glob", "*.json", *ex] if any(store.is_dir(t) for t in targets) else []
    flags = ["--json", "--no-config", "--no-ignore", "--no-follow", "--max-count", "1", "-i"]
    cmd = [rg_bin, *flags, *gl, "--max-filesize", "64M", "-e", pattern, *(str(store.root / t) for t in targets)]
    mdata: dict[str, dict[str, Any]] = {}

    def read_rg(stdout: IO[bytes]) -> bool:
        while ln := stdout.readline(MAX_BATCH + 1):
            if len(ln) > MAX_BATCH:
                raise Error(f"rg output line exceeds {MAX_BATCH} bytes")
            if not (s := ln.strip()) or not isinstance(ev := decode(s, "rg output", check_duplicates=False), dict):
                continue
            if ev.get("type") != "match" or not isinstance(d := ev.get("data"), dict):
                continue
            rp = d["path"].get("text") if isinstance(d.get("path"), dict) else None
            if isinstance(rp, str) and (rel := _mem_rel(store, rp)) and rel not in mdata:
                if mem_f == "memory" and rel.split("/")[1] in AUTHORED:
                    continue
                mdata[rel] = d
                if limit is not None and len(mdata) > limit:
                    return False
        return True

    rc, err_bytes, timed_out = stream_proc(cmd, rg_bin, read_rg)
    if not (trunc := limit is not None and len(mdata) > limit):
        if timed_out:
            raise Error("rg timed out after 30s")
        fail(rc, err_bytes)
    kept = list(mdata)[:limit]
    if not with_rows:
        return kept, [], trunc
    rows = []
    for p in kept:
        snip, anc = _extract_snippet(r := _decode_obj(store, p, check_duplicates=False), pattern, mdata[p])
        row = {"ref": r.get("ref", p), "source": r.get("source", ""), "kind": r.get("kind", "memory")}
        row |= {"title": r.get("title", ""), "path": p, "snippet": snip, **({"anchor": anc} if anc else {})}
        rows.append(row)
    return kept, rows, trunc


def _run_jq_stream(blobs: Any, expr: str, limit: int) -> tuple[list[Any], bool]:
    jq_bin, feeder_err, out = resolve_tool("jq"), [], []

    def feed_jq(stdin: IO[bytes], stop: threading.Event) -> None:
        try:
            for data in blobs:
                if stop.is_set():
                    break
                stdin.write(data if data.endswith(b"\n") else data + b"\n")
            stdin.flush()
        except (Error, OSError) as error:
            if not isinstance(error, BrokenPipeError) and not stop.is_set():
                feeder_err.append(error)
        finally:
            with suppress(OSError):
                stdin.close()

    def read_jq(stdout: IO[bytes]) -> bool:
        while raw_line := stdout.readline(MAX_BATCH + 1):
            if len(raw_line) > MAX_BATCH:
                raise Error(f"jq output line exceeds {MAX_BATCH} bytes")
            if stripped := raw_line.strip():
                out.append(decode(stripped, "jq output", check_duplicates=False))
                if len(out) > limit:
                    return False
        return True

    cmd = [jq_bin, "-L", "/dev/null", "-c", "--", f"{JQ_PRELUDE}\n{expr}"]
    rc, err_bytes, timed_out = stream_proc(cmd, jq_bin, read_jq, feed_jq)
    if not (trunc := len(out) > limit):
        if timed_out:
            raise Error("jq timed out after 30s")
        if feeder_err:
            raise feeder_err[0]
        if rc != 0:
            err = err_bytes.decode("utf-8", "replace").strip()
            msg = f"jq failed: {err.splitlines()[-1] if err else 'jq filter failed'}"
            raise InputError(msg) if rc == 3 or (rc == 5 and "parse error:" not in err) else Error(msg)
    return out[:limit], trunc


def _iter_jq_blobs(store: Store, targets: list[str], mem_f: str) -> Any:
    skip_dirs = set(AUTHORED) if mem_f == "memory" else set()
    total = 0
    for target in targets:
        if target.endswith(".json"):
            total += 1
            yield store.read(target)
        elif target == "memories":
            subdirs, top_files = store.subdirs("memories")
            for tf in top_files:
                if tf.endswith(".json"):
                    total += 1
                    yield store.read(tf)
            for src in subdirs:
                if src in skip_dirs:
                    continue
                for _, data in store.iter_read_files(f"memories/{src}", suffix=".json"):
                    total += 1
                    if total > models.MAX_TOTAL_FILES:
                        raise Error(f"memories exceeds the {models.MAX_TOTAL_FILES:,}-entry total scan limit")
                    yield data
        else:
            for _, data in store.iter_read_files(target, suffix=".json", skip_dirs=skip_dirs):
                total += 1
                if total > models.MAX_TOTAL_FILES:
                    raise Error(f"memories exceeds the {models.MAX_TOTAL_FILES:,}-entry total scan limit")
                yield data


def query(
    store: Store,
    pattern: str | None = None,
    *,
    jq_expr: str | None = None,
    scope: str = "",
    kind: str = "",
    fields: Sequence[str] | str | None = None,
    limit: int = 20,
    read_only: bool = False,
) -> dict[str, Any]:
    """Query compiled notes and sensor memories via `rg`, `jq`, or combined `rg` + `jq`."""
    from bf import config as cfg_m
    from bf import engine as eng

    if scope and not scope.strip():
        raise InputError("scope must be nonempty")
    if pattern is None and jq_expr is None and not (kind or scope.strip()):
        raise InputError("pass PATTERN, --jq EXPR, or a filter (--kind, --scope)")
    for lbl, val in (("query", pattern), ("jq filter", jq_expr)):
        if val is not None:
            _as_input(clean, val, lbl)
    if limit < 1 or limit > 5000:
        raise InputError("limit must be between 1 and 5000")
    if kind not in {"", "note", "memory"}:
        raise InputError("kind must be 'note' or 'memory'")
    wanted, cfg = eng.parse_fields(fields), cfg_m.load(store)
    if not read_only and kind in {"", "note"}:
        with suppress(Error, OSError):
            eng.sync_notes(store)
    targets, mem_f = _target_scope(store, scope, kind)
    extra: dict[str, Any] = {}
    expr = jq_expr if jq_expr is not None else "."
    if pattern is not None and jq_expr is None:
        eng_n, q_s = "rg", pattern
        _, items, trunc = _rg_scan(store, pattern, targets, mem_f, limit)
    elif pattern is not None:
        eng_n, q_s, extra = "rg+jq", pattern, {"jq": expr}
        hits = _rg_scan(store, pattern, targets, mem_f, None, with_rows=False)[0]
        items, trunc = _run_jq_stream((store.read(p) for p in hits), expr, limit)
    else:
        eng_n, q_s = "jq", expr
        items, trunc = _run_jq_stream(_iter_jq_blobs(store, targets, mem_f), expr, limit)
    if wanted is not None:
        items = [{k: x[k] for k in wanted if k in x} if isinstance(x, dict) else x for x in items]
    hdr = {"ok": True, "brain": cfg.name, "engine": eng_n, "query": q_s, **extra}
    return {**hdr, "count": len(items), "truncated": trunc, "items": items}


class _QueryOpts(TypedDict, total=False):
    scope: str
    kind: str
    fields: Sequence[str] | str | None
    limit: int
    read_only: bool


def search_rg(store: Store, pattern: str, **opts: Unpack[_QueryOpts]) -> dict[str, Any]:
    """Run lexical search over pure JSON memories via `rg`."""
    return query(store, pattern, **opts)


def query_jq(store: Store, expr: str, **opts: Unpack[_QueryOpts]) -> dict[str, Any]:
    """Filter pure JSON memory records via isolated `jq -c`."""
    return query(store, None, jq_expr=expr, **opts)


def find_by_alias(store: Store, identity: str, *, read_only: bool = False) -> tuple[str, dict[str, Any]] | None:
    """Locate a stored JSON memory whose `entity` or `aliases` equals `identity`, preferring authored notes."""
    from bf import engine as eng

    idx = eng.load_index(store, read_only=read_only)
    hits = [
        e for e in idx if e.get("entity") == identity or (isinstance(al := e.get("aliases"), list) and identity in al)
    ]
    hits.sort(key=lambda e: (str(e.get("source", "")) not in AUTHORED, str(e.get("path", ""))))
    for hit in hits:
        mp = str(hit.get("path", ""))
        if not (mp.startswith("memories/") and mp.endswith(".json")) or any(p.startswith(".") for p in mp.split("/")):
            continue
        with suppress(Error):
            return mp, _decode_obj(store, mp, check_duplicates=False)
    return None


def _raw_record_alias(raw_target: str, brain_name: str, md: Any) -> str:
    bare = raw_target.partition("#")[0].partition("?")[0]
    if bare.lower().startswith("bf://"):
        uri = urlsplit(bare)
        if uri.netloc not in {"", brain_name}:
            return ""
        bare = uri.path.lstrip("/")
    return bare if md.is_record_ref(bare) else ""


def _cap_json_bytes(out: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    if len(terminal(out)) <= max_bytes:
        return out
    cand = {**out, "truncated": True}
    if isinstance(val := cand.get("text"), str) and val:
        lo, hi, best = 0, len(val), ""
        while lo <= hi:
            cand["text"] = val[: (mid := (lo + hi) // 2)]
            if len(terminal(cand)) <= max_bytes:
                best, lo = val[:mid], mid + 1
            else:
                hi = mid - 1
        cand["text"] = best
        if len(terminal(cand)) <= max_bytes:
            return cand
    bulky = ("elements", "headings", "links", "tasks", "meta", "record", "text")
    keep_last = ("updated", "status", "title", "kind", "brain", "ref")
    extra = [k for k in cand if k not in {*bulky, *keep_last, "truncated"}]
    for drop in (*bulky, *extra, *keep_last):
        if len(terminal(cand)) <= max_bytes:
            break
        cand.pop(drop, None)
    if len(terminal(cand)) > max_bytes:
        raise InputError(f"max-bytes {max_bytes} too small")
    return cand


def read_target(
    store: Store,
    raw_target: str,
    *,
    declared_brains_only: bool = False,
    outline: bool = False,
    lines: str | None = None,
    fields: Sequence[str] | str | None = None,
    max_bytes: int | None = None,
    raw: bool = False,
    full: bool = False,
    read_only: bool = False,
) -> dict[str, Any]:
    """Read an OKF note, `note#anchor` section, `source:id` record, JSON memory, or `bf://` URI."""
    from bf import config as cfg_m
    from bf import engine as eng
    from bf import markdown as md

    wanted, line_spec = eng.parse_fields(fields), (md.parse_line_spec(lines) if lines is not None else None)
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise InputError("max_bytes must be at least 1")
    if sum((bool(outline), line_spec is not None, bool(raw), bool(full))) > 1:
        raise InputError("--outline, --lines, --raw, and --full are mutually exclusive")
    if not (target := raw_target.strip()):
        raise InputError("target is required; use bf status for brain overview")
    cfg = cfg_m.load(store)
    p_lk = _as_input(md.parse_link, "<read>", target, cfg.name, relative_base=False)
    kind, to, anchor = str(p_lk["kind"]), str(p_lk["to"]), str(p_lk["anchor"])
    if kind == "brain":
        host, _, rest = to.removeprefix("bf://").partition("/")
        uri = urlsplit(target.partition("#")[0]) if target.lower().startswith("bf://") else None
        raw_r = uri.path.lstrip("/") if uri else ""
        http_q = uri.query if uri and raw_r.partition(":")[2].lower().startswith(("https://", "http://")) else ""
        fwd = (f"{raw_r}?{http_q}" if http_q else raw_r) if uri and md.is_record_ref(raw_r) else quote(rest, safe="/:@")
        sub = f"bf:///{fwd}" + (f"#{quote(anchor, safe='')}" if anchor else "")
        sib = cfg_m.resolve_brain(store, cfg, host, declared_only=declared_brains_only)
        r_a: dict[str, Any] = {"declared_brains_only": declared_brains_only, "outline": outline, "lines": lines}
        r_b: dict[str, Any] = {"fields": fields, "max_bytes": max_bytes, "raw": raw, "full": full}
        return read_target(sib, sub, **r_a, **r_b, read_only=read_only)

    def rec_out(hp: str, hr: dict[str, Any], dr: str) -> dict[str, Any]:
        if outline or line_spec is not None or raw:
            raise InputError("--outline, --lines, and --raw apply only to Markdown notes")
        hdr = {"brain": cfg.name, "kind": str(hr.get("kind", "record")), "ref": str(hr.get("ref", dr))}
        return {**hdr, "path": hp, "record": hr}

    if kind == "url":
        hit = find_by_alias(store, to, read_only=read_only) or (
            find_by_alias(store, to[4:], read_only=read_only) if to[:4] == "urn:" else None
        )
        if hit is None:
            raise InputError("external URLs are not read offline; pass a note path, source:id, or bf:// URI")
        out = rec_out(hit[0], hit[1], to)
    elif anchor and (kind == "record" or to.endswith(".json")):
        raise InputError("JSON records do not support #fragments; pipe bf read into jq")
    elif any(part.startswith(".") for part in to.split("/")) or to.split("/")[0].lower() == "logs":
        raise InputError(f"{to}: private or runtime paths are not readable via bf read")
    elif (is_mem := to.startswith("memories/") and to.endswith(".json")) or kind == "record":
        if not read_only and is_mem and to.split("/")[1] in AUTHORED:
            with suppress(Error, OSError):
                eng.sync_notes(store)
        alias_key = _raw_record_alias(target, cfg.name, md)
        hit = (
            (to, _decode_obj(store, to))
            if store.exists(to)
            else (find_by_alias(store, alias_key, read_only=read_only) if alias_key else None)
        )
        if hit is None:
            raise Error(f"record {alias_key or to} not found")
        out = rec_out(hit[0], hit[1], alias_key or to)
    else:
        if not store.exists(to):
            raise Error(f"note {to} not found")
        text = (raw_bytes := store.read(to, MAX_NOTE)).decode("utf-8").removeprefix("\ufeff")
        is_okf = md.okf_role(to) is not None
        comp, disk_b = eng.load_or_compile_note(store, to, raw_bytes, brain_name=cfg.name) if is_okf else ({}, b"")
        if not read_only and is_okf and disk_b is None:
            with suppress(Error, OSError):
                eng.sync_notes(store)
        base_n = {"brain": cfg.name, "kind": comp["kind"] if is_okf else "file", "ref": to}
        if anchor:
            if not is_okf:
                raise Error(f"{to}: missing anchor #{anchor}")
            if outline or line_spec is not None or raw or full:
                raise InputError("--outline, --lines, --raw, and --full cannot be combined with #anchor")
            sec = md.extract_section(text, anchor, comp)
            if "heading" in sec and "text" in sec:
                hd = sec["heading"]
                sec = {"anchor": sec["anchor"], "heading": hd, "title": hd["title"], "text": sec["text"]}
            out = {"brain": cfg.name, "kind": "section", "ref": to, **sec}
        elif (outline or full) and not is_okf:
            raise InputError("--outline and --full apply only to OKF notes")
        elif outline:
            out = {"brain": cfg.name, **md.note_outline(comp)}
        elif line_spec is not None:
            out = {**base_n, **md.slice_lines(text, *line_spec)}
        elif raw or not is_okf:
            out = {**base_n, "text": text}
        elif full:
            out = {**base_n, "path": md.compiled_note_path(to), "record": comp}
        else:
            meta = {k: v for k, v in comp.items() if k not in _NOTE_STRUCT}
            u_d = {"status": str(comp.get("status") or ""), "updated": str(comp.get("updated") or ""), "meta": meta}
            b_d = {k: list(comp.get(k) or ()) for k in ("headings", "tasks", "links")}
            out = {**base_n, "title": comp["title"], **u_d, **b_d, "text": comp["text"]}
    proj = {k: out[k] for k in wanted if k in out} if wanted is not None else out
    return _cap_json_bytes(proj, max_bytes) if max_bytes is not None else proj


def load_eval_doc(store: Store, path: str, val: Any, problems: list[str]) -> dict[str, Any] | None:
    """Read, version-check, and schema-validate one `evals/*.{yaml,yml,json}` suite."""
    from bf import config as cfg_m
    from bf import schemas as sch_m

    try:
        raw_bytes = store.read(path, 1 << 20)
        doc = decode(raw_bytes, path) if path.endswith(".json") else cfg_m.yaml_object(raw_bytes, path)
        if not isinstance(doc, dict):
            raise Error(f"{path}: expected a mapping")
        models.check_version(doc, path, expected=models.EVAL_FORMAT)
        if val is not None and (errs := sch_m.validate_instance(val, doc, path)):
            problems.extend(errs)
            return None
        return doc
    except Error as error:
        problems.append(str(error))
        return None


def evaluate(
    store: Store, target: str = "", *, read_only: bool = False, declared_brains_only: bool = False
) -> dict[str, Any]:
    """Run deterministic query evaluations in `evals/*.yaml` and `evals/*.json`."""
    from bf import config as cfg_m
    from bf import schemas as sch_m

    cfg, val = cfg_m.load(store), sch_m.load_validator(store, "schemas/evals.json", sch_m.EVALS_SCHEMA)
    problems, results, passed, failed = [], [], 0, 0
    cand_files = [p for p in store.files("evals", skipped=problems) if p.endswith((".yaml", ".yml", ".json"))]
    if (w := target.strip()) and not (
        cand_files := [p for p in cand_files if w in {p, PurePosixPath(p).name, PurePosixPath(p).stem}]
    ):
        raise Error(f"no eval file matching {w!r}")
    for path in cand_files:
        if (doc := load_eval_doc(store, path, val, problems)) is None:
            continue
        for case in doc["cases"] if isinstance(doc.get("cases"), list) else []:
            if not isinstance(case, dict):
                continue
            name, eng = str(case["name"]), str(case.get("engine") or "rg")
            q_str, sc, reasons = str(case["query"]), str(case.get("scope") or ""), []
            try:
                if eng in {"rg", "jq"}:
                    fn = search_rg if eng == "rg" else query_jq
                    items = fn(store, q_str, scope=sc, limit=200 if eng == "rg" else 500, read_only=read_only)["items"]
                elif eng == "read":
                    items = [read_target(store, q_str, declared_brains_only=declared_brains_only, read_only=read_only)]
                else:
                    raise Error(f"{path}: unsupported eval engine {eng!r}")
                refs = {str(it.get("ref", "")) for it in items if isinstance(it, dict)}
                rnd = encode(items).decode("utf-8").lower()
                reasons.extend(f"missing expected ref {r}" for r in (case.get("expect_refs") or []) if r not in refs)
                reasons.extend(f"matched forbidden ref {b}" for b in (case.get("forbid_refs") or []) if b in refs)
                et = case.get("expect_text") or ()
                reasons.extend(f"missing expected text {s!r}" for s in et if str(s).lower() not in rnd)
                if "min_count" in case and len(items) < int(case["min_count"]):
                    reasons.append(f"count {len(items)} < min_count {case['min_count']}")
                if "max_count" in case and len(items) > int(case["max_count"]):
                    reasons.append(f"count {len(items)} > max_count {case['max_count']}")
                if "expect_equals" in case and items != case["expect_equals"]:
                    reasons.append("items did not match expect_equals")
            except Error as error:
                reasons.append(str(error))
            passed, failed = passed + int(not reasons), failed + int(bool(reasons))
            row = {"file": path, "name": name, "engine": eng, "ok": not reasons}
            results.append({**row, **({"reasons": reasons} if reasons else {})})
    res_a = {"brain": cfg.name, "ok": failed == 0 and not problems, "passed": passed, "failed": failed}
    return {**res_a, "cases": results, "problems": problems}


def check_brain(
    store: Store,
    *,
    target: str = "",
    now: datetime | None = None,
    build_notes: bool = True,
    run_validate: bool = True,
    run_eval: bool = True,
    limit: int = 50,
    all_problems: bool = False,
    strict: bool = False,
    force: bool = False,
    declared_brains_only: bool = False,
    read_only: bool = False,
) -> dict[str, Any]:
    """Run incremental `build`, `validate`, and/or `eval` in one pass."""
    from bf import config as cfg_m
    from bf import engine as eng

    if target.strip() and not run_eval:
        raise InputError("eval target cannot be combined with --validate")
    if run_validate and not all_problems and limit < 1:
        raise InputError("limit must be at least 1")
    do_build = build_notes and not read_only
    b = eng.build(store, now=now, force=force) if do_build else None
    v_kw = {"declared_brains_only": declared_brains_only, "limit": limit, "all_problems": all_problems}
    v = eng.validate(store, **v_kw, strict=strict, force=force, read_only=read_only) if run_validate else None
    if run_eval and v is not None and not v["ok"]:
        try:
            cfg_m.load(store)
        except Error:
            run_eval = False
    ev_kw = {"read_only": read_only or not do_build, "declared_brains_only": declared_brains_only}
    ev = evaluate(store, target, **ev_kw) if run_eval else None
    parts = {k: val for k, val in (("build", b), ("validate", v), ("eval", ev)) if val is not None}
    brain_name = next(iter(parts.values()))["brain"] if parts else cfg_m.load(store).name
    return {"ok": all(bool(p["ok"]) for p in parts.values()), "brain": brain_name, **parts}
