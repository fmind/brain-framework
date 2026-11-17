"""Offline retrieval and deterministic evaluation via `rg` and `jq`."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import IO, Any, cast
from urllib.parse import quote

from bf import config, markdown, records
from bf.config import yaml_object
from bf.models import AUTHORED, MAX_BATCH, MAX_NOTE, Error, InputError, check_version, clean, decode, encode
from bf.schemas import EVALS_SCHEMA, load_validator, validate_instance
from bf.storage import Store, relative


def _tool(name: str) -> str:
    if found := shutil.which(name):
        return found
    raise Error(f"{name} is required on PATH; install it with mise install")


def _safe_env() -> dict[str, str]:
    return {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _finish_proc(proc: subprocess.Popen[bytes], timer: threading.Timer, *, eof: bool) -> int:
    if not eof and proc.poll() is None:
        with suppress(OSError):
            proc.terminate()
    try:
        return proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with suppress(OSError):
            proc.kill()
        return proc.wait(timeout=2)
    finally:
        timer.cancel()


def _decode_obj(store: Store, path: str) -> dict[str, Any]:
    if not isinstance(parsed := decode(store.read(path), path), dict):
        raise Error(f"{path}: expected a JSON object")
    return cast("dict[str, Any]", parsed)


def _arg_clean(value: str, label: str) -> str:
    try:
        return clean(value, label)
    except Error as error:
        raise InputError(str(error)) from error


def _arg_rel(value: str) -> str:
    try:
        relative(value)
    except Error as error:
        raise InputError(str(error)) from error
    return value


def _scope_paths(store: Store, scope: str) -> tuple[list[str], str]:
    if not (stripped := scope.strip()):
        return [p for p in store.files("memories") if p.endswith(".json")], "memories"
    cleaned = _arg_rel(stripped.rstrip("/"))
    if markdown.okf_role(cleaned) is not None:
        comp = markdown.compiled_note_path(cleaned)
        return ([comp] if store.exists(comp) else []), comp
    if cleaned.endswith(".json") and cleaned.startswith("memories/"):
        return ([cleaned] if store.exists(cleaned) else []), cleaned
    sub = _arg_rel(cleaned if cleaned == "memories" or cleaned.startswith("memories/") else f"memories/{cleaned}")
    return [p for p in store.files(sub) if p.endswith(".json")], sub


def _rg_snippet(data: dict[str, Any], width: int = 240) -> str:
    raw = str(l_obj.get("text", "")) if isinstance(l_obj := data.get("lines"), dict) else ""
    text, subs = raw.strip(), data.get("submatches")
    b0 = subs[0].get("start") if isinstance(subs, list) and subs and isinstance(subs[0], dict) else 0
    if len(text) > width and type(b0) is int and b0 > 0:
        pos = max(0, len(raw.encode("utf-8")[:b0].decode("utf-8", "ignore")) - (len(raw) - len(raw.lstrip())))
        if pos > width // 3:
            start = min(pos - width // 3, len(text) - width)
            return text[start : start + width]
    return text[:width]


def search_rg(store: Store, pattern: str, *, scope: str = "", limit: int = 20) -> dict[str, Any]:
    """Run lexical search over pure JSON memories via `rg --json --no-config --no-ignore`."""
    cfg = config.load(store)
    _arg_clean(pattern, "query")
    if limit < 1 or limit > 500:
        raise InputError("limit must be between 1 and 500")
    allowed_files, sub = _scope_paths(store, scope)
    if not allowed_files:
        return {"brain": cfg.name, "engine": "rg", "query": pattern, "count": 0, "truncated": False, "items": []}
    allowed_set = set(allowed_files)
    cmd = [_tool("rg"), "--json", "--no-config", "--no-ignore", "--max-count", "1", "-i", "--max-filesize", "64M"]
    cmd.extend(["-e", pattern, str(store.root / sub)])
    matched_paths: list[str] = []
    snippets: dict[str, str] = {}
    timed_out, eof = threading.Event(), False
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_safe_env()) as proc:  # noqa: S603
        timer = threading.Timer(30.0, lambda: (timed_out.set(), proc.kill()) if proc.poll() is None else None)
        timer.daemon = True
        timer.start()
        try:
            for raw_line in cast("IO[bytes]", proc.stdout):
                if not (stripped := raw_line.strip()):
                    continue
                event = decode(stripped, "rg output")
                if not isinstance(event, dict) or event.get("type") != "match":
                    continue
                data = event.get("data")
                if not isinstance(data, dict) or not isinstance(
                    raw_path := data["path"].get("text") if isinstance(data.get("path"), dict) else None, str
                ):
                    continue
                with suppress(ValueError):
                    rel_path = Path(raw_path).resolve().relative_to(store.root).as_posix()
                    if rel_path in allowed_set and rel_path not in snippets:
                        matched_paths.append(rel_path)
                        snippets[rel_path] = _rg_snippet(data)
                        if len(matched_paths) > limit:
                            break
            else:
                eof = True
        finally:
            rc = _finish_proc(proc, timer, eof=eof)
        err_bytes = proc.stderr.read(8192) if proc.stderr is not None else b""

    if not (truncated := len(matched_paths) > limit):
        if timed_out.is_set():
            raise Error("rg timed out after 30s")
        if rc not in {0, 1}:
            err_txt = err_bytes.decode("utf-8", "replace").strip()
            raise InputError(f"rg failed: {err_txt.splitlines()[-1] if err_txt else 'invalid regex pattern'}")

    items: list[dict[str, Any]] = []
    for rel_path in matched_paths[:limit]:
        rec = _decode_obj(store, rel_path)
        item = {"ref": rec.get("ref", rel_path), "source": rec.get("source", ""), "kind": rec.get("kind", "memory")}
        items.append({**item, "title": rec.get("title", ""), "path": rel_path, "snippet": snippets.get(rel_path, "")})
    return {
        "brain": cfg.name,
        "engine": "rg",
        "query": pattern,
        "count": len(items),
        "truncated": truncated,
        "items": items,
    }


def query_jq(store: Store, expr: str, *, scope: str = "", limit: int = 20) -> dict[str, Any]:
    """Filter pure JSON memory records by streaming confined files directly into `jq -c`."""
    cfg = config.load(store)
    _arg_clean(expr, "jq filter")
    if limit < 1 or limit > 5000:
        raise InputError("limit must be between 1 and 5000")
    files, _ = _scope_paths(store, scope)
    jq_bin = _tool("jq")
    if not files:
        try:
            run_kw: dict[str, Any] = {"input": "", "capture_output": True, "text": True, "env": _safe_env()}
            chk = subprocess.run([jq_bin, "-c", "--", expr], **run_kw, timeout=5, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as error:
            raise Error("jq syntax check timed out") from error
        if chk.returncode != 0:
            err = chk.stderr.strip().splitlines()[-1] if chk.stderr.strip() else "invalid jq syntax"
            raise InputError(f"jq failed: {err}")
        return {"brain": cfg.name, "engine": "jq", "query": expr, "count": 0, "truncated": False, "items": []}

    stop_feed, timed_out = threading.Event(), threading.Event()
    feeder_err: list[Error | OSError] = []
    err_chunks: list[bytes] = []
    out: list[Any] = []
    truncated, eof, pipe = False, False, subprocess.PIPE
    with subprocess.Popen([jq_bin, "-c", "--", expr], stdin=pipe, stdout=pipe, stderr=pipe, env=_safe_env()) as proc:  # noqa: S603
        timer = threading.Timer(30.0, lambda: (timed_out.set(), proc.kill()) if proc.poll() is None else None)
        timer.daemon = True
        timer.start()

        def drain_err() -> None:
            if (stderr := proc.stderr) is None:
                return
            total = 0
            while chunk := stderr.read(4096):
                if total < 8192:
                    err_chunks.append(chunk[: 8192 - total])
                    total += len(chunk)

        def feed() -> None:
            stdin = cast("IO[bytes]", proc.stdin)
            try:
                for path in files:
                    if stop_feed.is_set():
                        break
                    data = store.read(path)
                    stdin.write(data if data.endswith(b"\n") else data + b"\n")
                stdin.flush()
            except BrokenPipeError:
                pass
            except (Error, OSError) as error:
                if not stop_feed.is_set():
                    feeder_err.append(error)
            finally:
                with suppress(OSError):
                    stdin.close()

        feeder = threading.Thread(target=feed, name="bf-jq-feed", daemon=True)
        err_reader = threading.Thread(target=drain_err, name="bf-jq-err", daemon=True)
        feeder.start()
        err_reader.start()
        try:
            stdout = cast("IO[bytes]", proc.stdout)
            while raw_line := stdout.readline(MAX_BATCH + 1):
                if len(raw_line) > MAX_BATCH:
                    raise InputError(f"jq output line exceeds {MAX_BATCH} bytes")
                if stripped := raw_line.strip():
                    out.append(decode(stripped, "jq output"))
                    if len(out) > limit:
                        truncated = True
                        break
            else:
                eof = True
        finally:
            stop_feed.set()
            rc = _finish_proc(proc, timer, eof=eof)
            feeder.join(timeout=5)
            err_reader.join(timeout=2)
        err_bytes = b"".join(err_chunks)

    if not truncated:
        if timed_out.is_set():
            raise InputError("jq failed: timed out after 30s")
        if feeder_err:
            raise feeder_err[0]
        if rc != 0:
            err = err_bytes.decode("utf-8", "replace").strip()
            raise InputError(f"jq failed: {err.splitlines()[-1] if err else 'jq filter failed'}")
    items = out[:limit]
    return {
        "brain": cfg.name,
        "engine": "jq",
        "query": expr,
        "count": len(items),
        "truncated": truncated,
        "items": items,
    }


def _strip_heading_md(raw: str) -> str:
    txt = re.sub(r"\s*\{[^}\n]+\}\s*$", "", re.sub(r"^#{1,6}\s+", "", raw.strip()))
    txt = re.sub(r"\[([^\]]+)\]\([^)]*\)|[`*_~]", lambda m: m[1] or "", txt)
    return re.sub(r"\s+", " ", txt).strip().lower()


def _extract_markdown_section(text: str, anchor: str, compiled: dict[str, Any]) -> dict[str, Any]:
    for key, out_key in (("elements", "element"), ("tasks", "task")):
        for item in compiled.get(key, []):
            if isinstance(item, dict) and item.get("id") == anchor:
                return {"anchor": anchor, out_key: item}
    headings = compiled.get("headings", [])
    idx_h = next(((i, h) for i, h in enumerate(headings) if isinstance(h, dict) and h.get("id") == anchor), None)
    if idx_h is None:
        if isinstance(attrs := compiled.get("attrs"), dict) and anchor in attrs:
            return {"anchor": anchor, "meta": attrs[anchor]}
        raise Error(f"{compiled['ref']}: missing anchor #{anchor}")
    level, lines, title_lc = int(idx_h[1]["level"]), text.splitlines(), idx_h[1]["title"].lower()
    hs = [h for h in headings[: idx_h[0]] if isinstance(h, dict) and int(h["level"]) == level]
    want_same = sum(1 for h in hs if h["title"].lower() == title_lc)
    fm_end = next((i + 1 for i in range(1, len(lines)) if lines[i].rstrip() == "---"), 0)
    offset = fm_end if lines and lines[0].rstrip() == "---" else 0
    heading = idx_h[1]
    start_line: int | None = None
    end_line, fence, seen_same = len(lines), "", 0
    for i in range(offset, len(lines)):
        ln = lines[i]
        if m_f := markdown.FENCE.fullmatch(ln):
            if fence and m_f[1][0] == fence[0] and len(m_f[1]) >= len(fence) and not m_f[2].strip():
                fence = ""
            elif not fence and not (m_f[1][0] == "`" and "`" in m_f[2]):
                fence = m_f[1]
            continue
        if fence:
            continue
        if m := re.match(r"^(#{1,6})\s+", ln):
            if start_line is None and len(m[1]) == level:
                attr_m = re.search(r"\{([^{}\n]+)\}\s*$", ln.rstrip())
                id_m = re.search(r"(?:^|\s|[:{])#([^\s{}]+)", attr_m[1]) if attr_m else None
                same_t = _strip_heading_md(ln) == title_lc
                if (id_m and id_m[1] == anchor) or (not id_m and same_t and seen_same == want_same):
                    start_line = i
                seen_same += int(same_t)
            elif start_line is not None and len(m[1]) <= level:
                end_line = i
                break
    sec_txt = "\n".join(lines[start_line:end_line]).strip() if start_line is not None else ""
    return {"anchor": anchor, "heading": heading, "text": sec_txt}


def find_by_alias(store: Store, identity: str) -> tuple[str, dict[str, Any]] | None:
    """Locate a stored JSON memory whose `entity` or `aliases` equals `identity`, preferring authored notes."""

    def match_paths(paths: list[str]) -> tuple[str, dict[str, Any]] | None:
        for path in paths:
            with suppress(Error):
                parsed = _decode_obj(store, path)
                aliases = parsed.get("aliases")
                if parsed.get("entity") == identity or (isinstance(aliases, list) and identity in aliases):
                    return path, parsed
        return None

    authored = sorted(p for d in AUTHORED for p in store.files(f"memories/{d}") if p.endswith(".json"))
    if (found := match_paths(authored)) is not None or not store.is_dir("memories"):
        return found
    if (rg := shutil.which("rg")) and '"' not in identity and "\\" not in identity:
        cmd = [rg, "-l", "-F", "--no-config", "--no-ignore", "--", identity, str(store.root / "memories")]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=_safe_env(), timeout=15, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as error:
            raise Error("rg alias lookup timed out") from error
        if proc.returncode in {0, 1}:
            cands: set[str] = set()
            for raw_ln in proc.stdout.splitlines():
                with suppress(ValueError):
                    rel = Path(raw_ln).resolve().relative_to(store.root).as_posix()
                    if rel.startswith("memories/") and rel.endswith(".json") and rel.split("/")[1] not in AUTHORED:
                        cands.add(rel)
            return match_paths(sorted(cands))
    return match_paths([p for p in store.files("memories") if p.endswith(".json") and p.split("/")[1] not in AUTHORED])


def read_target(store: Store, raw_target: str) -> dict[str, Any]:
    """Read an OKF note, `note#anchor` section, `source:id` record, JSON memory, or `bf://` URI."""
    cfg = config.load(store)
    if not (target := raw_target.strip()):
        raise InputError("target is required; use bf status for brain overview")

    try:
        parsed_link = markdown.parse_link("<read>", target, cfg.name, relative_base=False, allow_record_ref=True)
    except Error as error:
        raise InputError(str(error)) from error
    kind, to, anchor = str(parsed_link["kind"]), str(parsed_link["to"]), str(parsed_link["anchor"])
    if kind == "brain":
        host, _, rest = to.removeprefix("bf://").partition("/")
        q_rest = quote(rest, safe="/:@")
        sub = f"bf:///{q_rest}#{quote(anchor, safe='')}" if anchor else f"bf:///{q_rest}"
        return read_target(config.resolve_brain(store, cfg, host), sub)
    if kind == "url":
        raise InputError("external URLs are not read offline; pass a note path, source:id, or bf:// URI")
    if anchor and (kind == "record" or to.endswith(".json")):
        raise InputError("JSON records do not support #fragments; pipe bf read into jq")
    if any(part.startswith(".") for part in to.split("/")) or to.split("/")[0].lower() == "logs":
        raise InputError(f"{to}: private or runtime paths are not readable via bf read")

    def render_record(
        hit_path: str, hit_rec: dict[str, Any], default_ref: str, default_kind: str = "record"
    ) -> dict[str, Any]:
        ref_str = str(hit_rec.get("ref", default_ref))
        return {"brain": cfg.name, "kind": default_kind, "ref": ref_str, "path": hit_path, "record": hit_rec}

    if to.startswith("memories/") and to.endswith(".json"):
        if not store.exists(to):
            raise Error(f"record {to} not found")
        rec = _decode_obj(store, to)
        return render_record(to, rec, str(rec.get("ref", to)), str(rec.get("kind", "record")))
    if kind == "record":
        src, _, rec_id = to.partition(":")
        cand = records.record_file(src, rec_id)
        hit = (cand, _decode_obj(store, cand)) if store.exists(cand) else find_by_alias(store, to)
        if hit is None:
            raise Error(f"record {to} not found")
        return render_record(hit[0], hit[1], to)
    if not store.exists(to):
        raise Error(f"note {to} not found")
    raw_bytes = store.read(to, MAX_NOTE)
    text = raw_bytes.decode("utf-8").removeprefix("\ufeff")
    if markdown.okf_role(to) is not None:
        compiled = markdown.compile_note(to, raw_bytes, brain_name=cfg.name)
        if anchor:
            sec = _extract_markdown_section(text, anchor, compiled)
            return {"brain": cfg.name, "kind": "section", "ref": to, **sec}
        return {"brain": cfg.name, "kind": compiled["kind"], "ref": to, "record": compiled, "markdown": text}
    return {"brain": cfg.name, "kind": "file", "ref": to, "text": text}


def evaluate(store: Store, target: str = "") -> dict[str, Any]:
    """Run deterministic query evaluations in `evals/*.yaml` and `evals/*.json`."""
    cfg = config.load(store)
    val = load_validator(store, "schemas/evals.json", EVALS_SCHEMA)
    problems: list[str] = []
    results: list[dict[str, Any]] = []
    passed = failed = 0
    cand_files = [p for p in store.files("evals", skipped=problems) if p.endswith((".yaml", ".yml", ".json"))]
    if wanted := target.strip():
        cand_files = [p for p in cand_files if p == wanted or PurePosixPath(p).stem == wanted]
        if not cand_files:
            raise InputError(f"no eval file matching {wanted!r}")

    for path in cand_files:
        try:
            raw_bytes = store.read(path, 1 << 20)
            doc = decode(raw_bytes, path) if path.endswith(".json") else yaml_object(raw_bytes, path)
            if not isinstance(doc, dict):
                raise Error(f"{path}: expected a mapping")
            check_version(doc, path)
            if val is not None and (errs := validate_instance(val, doc, path)):
                problems.extend(errs)
                continue
        except Error as error:
            problems.append(str(error))
            continue

        for case in cases if isinstance(cases := doc.get("cases"), list) else []:
            if not isinstance(case, dict):
                continue
            name, eng = str(case["name"]), str(case.get("engine") or "rg")
            q_str, sc = str(case["query"]), str(case.get("scope") or "")
            reasons: list[str] = []
            try:
                if eng == "rg":
                    out = search_rg(store, q_str, scope=sc, limit=200)
                elif eng == "jq":
                    out = query_jq(store, q_str, scope=sc, limit=500)
                elif eng == "read":
                    out = read_target(store, q_str)
                else:
                    raise Error(f"{path}: unsupported eval engine {eng!r}")
                items = out["items"] if eng in {"rg", "jq"} else [out]
                refs = {str(it.get("ref", "")) for it in items if isinstance(it, dict)}
                rendered = encode(out).decode("utf-8").lower()
                reasons.extend(f"missing expected ref {r}" for r in (case.get("expect_refs") or []) if r not in refs)
                reasons.extend(f"matched forbidden ref {b}" for b in (case.get("forbid_refs") or []) if b in refs)
                reasons.extend(
                    f"missing expected text {s!r}"
                    for s in (case.get("expect_text") or [])
                    if str(s).lower() not in rendered
                )
                if "min_count" in case and len(items) < int(case["min_count"]):
                    reasons.append(f"count {len(items)} < min_count {case['min_count']}")
                if "max_count" in case and len(items) > int(case["max_count"]):
                    reasons.append(f"count {len(items)} > max_count {case['max_count']}")
                if "expect_equals" in case and items != case["expect_equals"]:
                    reasons.append("items did not match expect_equals")
            except Error as error:
                reasons.append(str(error))

            ok = not reasons
            passed, failed = passed + int(ok), failed + int(not ok)
            row: dict[str, Any] = {"file": path, "name": name, "engine": eng, "ok": ok}
            results.append({**row, **({"reasons": reasons} if reasons else {})})

    ok_all = failed == 0 and not problems
    return {"brain": cfg.name, "ok": ok_all, "passed": passed, "failed": failed, "cases": results, "problems": problems}
