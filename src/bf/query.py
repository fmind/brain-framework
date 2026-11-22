"""Strictly read-only offline retrieval over `memories/*.jsonl` and `usages/*.jsonl` via `rg` and `jq`."""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable, Iterable
from contextlib import suppress
from typing import IO, Any
from urllib.parse import quote, urlsplit

from bf import models, proc, records
from bf.models import MAX_BATCH, MAX_NOTE, Error, InputError, clean, decode, terminal
from bf.storage import Store, relative


def _as_input[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return fn(*args, **kwargs)
    except Error as error:
        raise InputError(str(error)) from error


def _target_files(store: Store, scope: str, kind: str, note_dirs: tuple[str, ...]) -> tuple[list[str], str]:
    from bf import markdown as md

    notes_set = frozenset(note_dirs)

    def allowed(bucket: records.Target, source: str) -> bool:
        if kind == "note":
            return bucket == "memories" and source in notes_set
        if kind == "memory":
            return bucket == "memories" and source not in notes_set
        if kind == "usage":
            return bucket == "usages"
        return True

    def valid_source(bucket: records.Target, source: str) -> bool:
        if bucket == "memories" and source in notes_set:
            return True
        return bool(
            models.NAME_RE.fullmatch(source) and source not in models.RESERVED_SOURCES and source not in notes_set
        )

    if stripped := scope.strip():
        _as_input(relative, cl := stripped.rstrip("/"))
        if md.is_authored_note(store, cl, note_dirs):
            src = cl.split("/")[0]
            mf = records.memory_file(src)
            return ([mf] if allowed("memories", src) and store.exists(mf) else []), cl
        if cl in {"memories", "usages"}:
            bucket: records.Target = "usages" if cl == "usages" else "memories"
            if not store.is_dir(bucket):
                return [], ""
            sources = records.valid_record_sources(store, bucket, note_dirs)
            return [records.record_file(s, bucket) for s in sources if allowed(bucket, s)], ""
        for bucket in models.RECORD_DIRS:
            pfx = f"{bucket}/"
            if cl.startswith(pfx):
                rest = cl.removeprefix(pfx)
                if "/" in rest:
                    return [], ""
                src = rest.removesuffix(".jsonl")
                if not valid_source(bucket, src):
                    return [], ""
                rf = records.record_file(src, bucket)
                return ([rf] if allowed(bucket, src) and store.exists(rf) else []), ""
        src = cl.split("/")[0]
        for bucket in ("usages", "memories") if kind == "usage" else ("memories", "usages"):
            if not valid_source(bucket, src):
                continue
            rf = records.record_file(src, bucket)
            if allowed(bucket, src) and store.exists(rf):
                return [rf], (cl if "/" in cl else "")
        return [], ""

    buckets: tuple[records.Target, ...] = ("usages",) if kind == "usage" else ("memories",)
    files: list[str] = []
    for bucket in buckets:
        if store.is_dir(bucket):
            sources = records.valid_record_sources(store, bucket, note_dirs)
            files.extend(records.record_file(s, bucket) for s in sources if allowed(bucket, s))
    return files, ""


def _ref_matches(rec: dict[str, Any], scope_ref: str) -> bool:
    if not scope_ref:
        return True
    ref = str(rec.get("ref") or "")
    return (
        ref == scope_ref
        or ref == f"{scope_ref}.md"
        or ref == f"{scope_ref}/index.md"
        or ref.startswith(f"{scope_ref}/")
    )


def _extract_snippet(rec: dict[str, Any], rx: re.Pattern[str] | None, width: int = 240) -> tuple[str, str]:
    def center(raw: str, pat: re.Pattern[str] | None = rx) -> str:
        if len(flat := re.sub(r"\s+", " ", raw).strip()) <= width:
            return flat
        pos = m.start() if pat and (m := pat.search(flat)) else 0
        start = 0 if pos <= width // 3 else min(pos - width // 3, len(flat) - width)
        return flat[start : start + width]

    if rx is not None:
        for key, tf, af in (("headings", "title", "id"), ("tasks", "text", "section")):
            for item in rec.get(key) or ():
                if isinstance(item, dict) and rx.search(txt := str(item.get(tf) or "")):
                    return center(txt), str(item.get(af) or "")
        if isinstance(txt := rec.get("text"), str) and (match := rx.search(txt)):
            anc, cur = "", 0
            for heading in rec.get("headings") or ():
                if (
                    isinstance(heading, dict)
                    and (t0 := heading.get("title"))
                    and 0 <= (ix := txt.find(str(t0), cur)) <= match.start()
                ):
                    anc, cur = str(heading.get("id") or ""), ix + len(str(t0))
            return center(txt), anc
        priority = ("title", "summary")
        stack: list[tuple[str, Any]] = [(k, rec[k]) for k in priority if k in rec]
        stack.extend((k, v) for k, v in rec.items() if k not in {"sha256", "kind", "source", *priority})
        while stack:
            fk, val = stack.pop(0)
            if isinstance(val, str) and rx.search(val):
                ttl = str(rec.get("title") or "")
                snip = f"{ttl} ({val})" if fk in {"id", "ref"} and ttl else val
                return center(snip, None if fk in {"id", "ref"} else rx), ""
            if isinstance(val, (list, dict)):
                stack.extend(((fk, x) for x in val) if isinstance(val, list) else val.items())
    return center(str(rec.get("summary") or rec.get("title") or rec.get("text") or rec.get("ref") or "")), ""


def _summary_row(rec: dict[str, Any], *, snippet: str = "", anchor: str = "") -> dict[str, Any]:
    row: dict[str, Any] = {
        "ref": rec.get("ref", ""),
        "source": rec.get("source", ""),
        "kind": rec.get("kind", "memory"),
        "title": rec.get("title", ""),
    }
    if upd := rec.get("updated") or rec.get("time"):
        row["updated"] = upd
    if snippet:
        row["snippet"] = snippet
    elif summ := rec.get("summary"):
        row["summary"] = summ
    if anchor:
        row["anchor"] = anchor
    return row


def _rg_run(
    store: Store,
    pattern: str,
    files: list[str],
    *,
    scope_ref: str = "",
    limit: int | None = None,
    json_out: bool = False,
) -> tuple[list[tuple[bytes, str]], bool]:
    rg_bin = proc.resolve_tool("rg")

    def fail(rc: int, err_b: bytes) -> None:
        if rc not in {0, 1} or (rc == 1 and err_b.strip()):
            et = err_b.decode("utf-8", "replace").strip()
            msg = f"rg failed: {et.splitlines()[-1] if et else 'invalid regex pattern'}"
            raise InputError(msg) if rc == 2 else Error(msg)

    if not files:
        try:
            kw: dict[str, Any] = {"input": b"", "capture_output": True, "cwd": "/", "env": proc.safe_env(rg_bin)}
            chk = subprocess.run([rg_bin, "--no-config", "-e", pattern], **kw, timeout=5, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as err:
            raise Error("rg timed out after 5s") from err
        fail(chk.returncode, chk.stderr)
        return [], False

    flags = ["--no-config", "--no-ignore", "--no-follow"]
    flags.extend(("--json", "-i") if json_out else ("--no-filename", "--no-line-number", "-i"))
    cmd = [rg_bin, *flags, "-e", pattern, *(str(store.root / f) for f in files)]
    hits: list[tuple[bytes, str]] = []

    def read_rg(stdout: IO[bytes]) -> bool:
        while ln := stdout.readline(MAX_BATCH + 1):
            if len(ln) > MAX_BATCH:
                raise Error(f"rg output line exceeds {MAX_BATCH} bytes")
            if not (stripped := ln.strip()):
                continue
            sub = ""
            if json_out:
                try:
                    ev = decode(stripped, "rg output", check_duplicates=False)
                except Error:
                    continue
                if (
                    not isinstance(ev, dict)
                    or ev.get("type") != "match"
                    or not isinstance(data := ev.get("data"), dict)
                ):
                    continue
                raw_txt = data.get("lines", {}).get("text") if isinstance(data.get("lines"), dict) else None
                if not isinstance(raw_txt, str) or not (line_b := raw_txt.strip().encode("utf-8")):
                    continue
                if (
                    isinstance(subs := data.get("submatches"), list)
                    and subs
                    and isinstance(subs[0], dict)
                    and isinstance(m := subs[0].get("match"), dict)
                    and isinstance(mt := m.get("text"), str)
                ):
                    sub = mt
            else:
                line_b = stripped
            if scope_ref:
                try:
                    parsed = decode(line_b, "rg output", check_duplicates=False)
                except Error:
                    continue
                if not isinstance(parsed, dict) or not _ref_matches(parsed, scope_ref):
                    continue
            hits.append((line_b, sub))
            if limit is not None and len(hits) > limit:
                return False
        return True

    rc, err_bytes, timed_out = proc.stream_proc(cmd, rg_bin, read_rg)
    if not (trunc := limit is not None and len(hits) > limit):
        if timed_out:
            raise Error("rg timed out after 30s")
        fail(rc, err_bytes)
    return hits[:limit], trunc


def _run_jq_stream(lines: Iterable[bytes], expr: str, limit: int) -> tuple[list[Any], bool]:
    jq_bin, feeder_err, out = proc.resolve_tool("jq"), [], []

    def feed_jq(stdin: IO[bytes], stop: threading.Event) -> None:
        try:
            for data in lines:
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

    cmd = [jq_bin, "-L", "/dev/null", "-c", "--", f"{proc.JQ_PRELUDE}\n{expr}"]
    rc, err_bytes, timed_out = proc.stream_proc(cmd, jq_bin, read_jq, feed_jq)
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


def _iter_file_lines(store: Store, files: list[str], scope_ref: str) -> Iterable[bytes]:
    total = 0
    for path in files:
        if not scope_ref:
            for raw_ln in store.iter_lines(path, models.MAX_FILE):
                if not raw_ln.strip():
                    continue
                total += 1
                if total > models.MAX_TOTAL_FILES:
                    raise Error(f"records exceed the {models.MAX_TOTAL_FILES:,}-record total scan limit")
                yield raw_ln
        else:
            for raw_ln, obj in records.iter_jsonl(store, path):
                total += 1
                if total > models.MAX_TOTAL_FILES:
                    raise Error(f"records exceed the {models.MAX_TOTAL_FILES:,}-record total scan limit")
                if _ref_matches(obj, scope_ref):
                    yield raw_ln


def query(
    store: Store,
    pattern: str | None = None,
    *,
    jq_expr: str | None = None,
    scope: str = "",
    kind: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Read-only query over `memories/*.jsonl` (and `usages/*.jsonl` when scoped) via `rg` and `jq`."""
    from bf import config as cfg_m

    if scope and not scope.strip():
        raise InputError("scope must be nonempty")
    if pattern is None and jq_expr is None and not (kind or scope.strip()):
        raise InputError("pass PATTERN, --jq EXPR, or a filter (--kind, --scope)")
    for label, val in (("query", pattern), ("jq filter", jq_expr)):
        if val is not None:
            _as_input(clean, val, label)
    if limit < 1 or limit > 5000:
        raise InputError("limit must be between 1 and 5000")
    if kind not in {"", "note", "memory", "usage"}:
        raise InputError("kind must be 'note', 'memory', or 'usage'")
    cfg = cfg_m.load(store)
    files, scope_ref = _target_files(store, scope, kind, cfg.notes)
    extra: dict[str, Any] = {}

    if pattern is not None and jq_expr is None:
        eng_name, q_str = "rg", pattern
        raw_hits, trunc = _rg_run(store, pattern, files, scope_ref=scope_ref, limit=limit, json_out=True)
        items: list[Any] = []
        for raw_ln, sub in raw_hits:
            if isinstance(rec := decode(raw_ln, "memories", check_duplicates=False), dict):
                rx = re.compile(re.escape(sub), re.IGNORECASE) if sub else None
                snip, anc = _extract_snippet(rec, rx)
                items.append(_summary_row(rec, snippet=snip, anchor=anc))
    elif pattern is not None and jq_expr is not None:
        eng_name, q_str, extra = "rg+jq", pattern, {"jq": jq_expr}
        raw_hits, _ = _rg_run(store, pattern, files, scope_ref=scope_ref, limit=None, json_out=False)
        items, trunc = _run_jq_stream((ln for ln, _ in raw_hits), jq_expr, limit)
    elif jq_expr is not None:
        eng_name, q_str = "jq", jq_expr
        items, trunc = _run_jq_stream(_iter_file_lines(store, files, scope_ref), jq_expr, limit)
    else:
        eng_name, q_str = "list", scope or kind
        items, trunc = [], False
        for path in files:
            for _, rec in records.iter_jsonl(store, path):
                if scope_ref and not _ref_matches(rec, scope_ref):
                    continue
                if len(items) >= limit:
                    trunc = True
                    break
                items.append(_summary_row(rec))
            if trunc:
                break

    return {
        "ok": True,
        "brain": cfg.name,
        "engine": eng_name,
        "query": q_str,
        **extra,
        "count": len(items),
        "truncated": trunc,
        "items": items,
    }


def _find_record(store: Store, source: str, ref: str) -> tuple[str, dict[str, Any]] | None:
    for bucket in models.RECORD_DIRS:
        rec_path = records.record_file(source, bucket)
        if not store.exists(rec_path):
            continue
        for _, rec in records.iter_jsonl(store, rec_path):
            if rec.get("ref") == ref:
                return rec_path, rec
    return None


def find_by_alias(
    store: Store, identity: str, note_dirs: Iterable[str] = models.DEFAULT_NOTES
) -> tuple[str, dict[str, Any]] | None:
    """Locate a JSONL record whose `entity` or `aliases` equals `identity`, preferring authored notes."""
    notes_set = frozenset(note_dirs)
    sources = records.valid_record_sources(store, "memories", note_dirs)
    note_files = [records.memory_file(s) for s in sources if s in notes_set]
    mem_files = [records.memory_file(s) for s in sources if s not in notes_set]
    usage_files = [records.record_file(s, "usages") for s in records.valid_record_sources(store, "usages", note_dirs)]
    for group in (note_files, mem_files, usage_files):
        if not group:
            continue
        matched: list[tuple[str, dict[str, Any]]] = []
        for path in group:
            with suppress(Error, OSError):
                for _, rec in records.iter_jsonl(store, path):
                    if rec.get("entity") == identity or (isinstance(al := rec.get("aliases"), list) and identity in al):
                        matched.append((path, rec))
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
    elif isinstance(rec := cand.get("record"), dict) and isinstance(rec.get("text"), str) and rec["text"]:
        holder = cand["record"] = dict(rec)
    if holder is not None:
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
    bulky = ("record", "text", "heading")
    keep_last = ("path", "anchor", "kind", "brain", "ref", "ok")
    extra = [k for k in cand if k not in {*bulky, *keep_last, "truncated"}]
    for drop in (*bulky, *extra, *keep_last):
        if len(terminal(cand)) <= max_bytes:
            break
        cand.pop(drop, None)
    if len(terminal(cand)) > max_bytes:
        raise InputError(f"max-bytes {max_bytes} too small")
    return cand


def read_target(store: Store, raw_target: str, *, raw: bool = False, max_bytes: int | None = None) -> dict[str, Any]:
    """Read-only resolver for a note, `note#anchor` section, `source:id` record, alias, or `bf://` URI."""
    from bf import config as cfg_m
    from bf import markdown as md

    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise InputError("max_bytes must be at least 1")
    if not (target := raw_target.strip()):
        raise InputError("target is required; use bf status for brain overview")
    cfg = cfg_m.load(store)
    reserved = cfg.reserved_sources
    parsed = _as_input(md.parse_link, "<read>", target, cfg.name, note_dirs=cfg.notes, relative_base=False)
    kind, to, anchor = str(parsed["kind"]), str(parsed["to"]), str(parsed["anchor"])
    if kind == "brain":
        host, _, rest = to.removeprefix("bf://").partition("/")
        uri = urlsplit(target.partition("#")[0]) if target.lower().startswith("bf://") else None
        raw_r = uri.path.lstrip("/") if uri else ""
        http_q = uri.query if uri and raw_r.partition(":")[2].lower().startswith(("https://", "http://")) else ""
        fwd = (
            (f"{raw_r}?{http_q}" if http_q else raw_r)
            if uri and md.is_record_ref(raw_r, models.RESERVED_SOURCES)
            else quote(rest, safe="/:@")
        )
        sub = f"bf:///{fwd}" + (f"#{quote(anchor, safe='')}" if anchor else "")
        sib = cfg_m.resolve_brain(store, cfg, host)
        return read_target(sib, sub, raw=raw, max_bytes=max_bytes)

    def rec_out(hp: str, hr: dict[str, Any], default_ref: str) -> dict[str, Any]:
        if raw:
            raise InputError("--raw applies only to Markdown notes or plain files")
        return {
            "ok": True,
            "brain": cfg.name,
            "kind": str(hr.get("kind", "memory")),
            "ref": str(hr.get("ref", default_ref)),
            "path": hp,
            "record": hr,
        }

    if kind == "url":
        hit = find_by_alias(store, to, cfg.notes) or (
            find_by_alias(store, to[4:], cfg.notes) if to[:4] == "urn:" else None
        )
        if hit is None:
            raise InputError("external URLs are not read offline; pass a note path, source:id, or bf:// URI")
        out = rec_out(hit[0], hit[1], to)
    elif anchor and kind == "record":
        raise InputError("JSON records do not support #fragments; pipe bf read into jq")
    elif kind == "record":
        parsed_r = md.parse_record_ref(to, reserved)
        found = _find_record(store, parsed_r[0], to) if parsed_r is not None else None
        if found is not None:
            out = rec_out(found[0], found[1], to)
        elif (alias_hit := find_by_alias(store, to, cfg.notes)) is not None:
            out = rec_out(alias_hit[0], alias_hit[1], to)
        else:
            raise Error(f"record {to} not found")
    elif any(part.startswith(".") for part in to.split("/")) or to.split("/")[0].lower() == "logs":
        raise InputError(f"{to}: private or runtime paths are not readable via bf read")
    else:
        if not store.exists(to) and to.split("/")[0] in cfg.notes and not to.endswith(".md"):
            for cand in (f"{to}.md", f"{to}/index.md"):
                if store.exists(cand):
                    to = cand
                    break
        if not store.exists(to):
            raise Error(f"note {to} not found")
        raw_bytes = store.read(to, MAX_NOTE)
        text = raw_bytes.decode("utf-8").removeprefix("\ufeff")
        is_note = md.is_authored_note(store, to, cfg.notes)
        if anchor:
            if not is_note:
                raise Error(f"{to}: missing anchor #{anchor}")
            if raw:
                raise InputError("--raw cannot be combined with #anchor")
            comp = md.compile_note(to, raw_bytes, brain_name=cfg.name, note_dirs=cfg.notes)
            sec = md.extract_section(text, anchor, comp)
            out = {"ok": True, "brain": cfg.name, "kind": "section", "ref": to, **sec}
        elif raw or not is_note:
            out = {"ok": True, "brain": cfg.name, "kind": "note" if is_note else "file", "ref": to, "text": text}
        else:
            comp = md.compile_note(to, raw_bytes, brain_name=cfg.name, note_dirs=cfg.notes)
            out = {
                "ok": True,
                "brain": cfg.name,
                "kind": "note",
                "ref": to,
                "path": records.memory_file(to.split("/")[0]),
                "record": comp,
            }
    return _cap_json_bytes(out, max_bytes) if max_bytes is not None else out
