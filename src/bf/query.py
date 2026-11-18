"""Offline retrieval and deterministic evaluation via `rg` and `jq`."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import IO, Any, cast
from urllib.parse import quote

from bf import config, engine, markdown, records
from bf.config import yaml_object
from bf.models import AUTHORED, MAX_BATCH, MAX_NOTE, Error, InputError, check_version, clean, decode, encode
from bf.schemas import EVALS_SCHEMA, load_validator, validate_instance
from bf.storage import Store, relative

JQ_PRELUDE = (
    'def modulemeta: error("disabled"); def modulemeta($m): error("disabled"); '
    "def get_jq_origin: null; def get_prog_origin: null; def get_search_list: [];"
)


def _tool(name: str) -> str:
    if found := shutil.which(name):
        return found
    raise Error(f"{name} is required on PATH; install it with mise install")


def safe_env() -> dict[str, str]:
    """Return a minimal locale-only environment without host variables."""
    return {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}


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


def _decode_obj(store: Store, path: str, *, check_duplicates: bool = True) -> dict[str, Any]:
    if not isinstance(parsed := decode(store.read(path), path, check_duplicates=check_duplicates), dict):
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


def _resolve_scope(store: Store, scope: str) -> tuple[str | None, str | None]:
    """Return `(single_file, directory)` in `memories/` after `O_NOFOLLOW` confinement checks."""
    if not (stripped := scope.strip()):
        return None, ("memories" if store.is_dir("memories") else None)
    cleaned = _arg_rel(stripped.rstrip("/"))
    if markdown.okf_role(cleaned) is not None:
        comp = markdown.compiled_note_path(cleaned)
        return (comp if store.exists(comp) else None), None
    if cleaned.endswith(".json") and cleaned.startswith("memories/"):
        return (cleaned if store.exists(cleaned) else None), None
    sub = _arg_rel(cleaned if cleaned == "memories" or cleaned.startswith("memories/") else f"memories/{cleaned}")
    return None, (sub if store.is_dir(sub) else None)


def _read_stderr(handle: IO[bytes]) -> bytes:
    handle.seek(max(0, handle.seek(0, os.SEEK_END) - 8192))
    return handle.read(8192)


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
    """Run lexical search over pure JSON memories via `rg --json --no-config --no-ignore --no-follow`."""
    cfg = config.load(store)
    _arg_clean(pattern, "query")
    if limit < 1 or limit > 500:
        raise InputError("limit must be between 1 and 500")
    single_file, sub_dir = _resolve_scope(store, scope)
    target_rel = single_file or sub_dir
    rg_bin = _tool("rg")
    if target_rel is None:
        r_kw: dict[str, Any] = {"input": b"", "capture_output": True, "cwd": "/", "env": safe_env(), "timeout": 5}
        chk = subprocess.run([rg_bin, "--no-config", "-e", pattern], **r_kw, check=False)  # noqa: S603
        if chk.returncode not in {0, 1}:
            err_txt = chk.stderr.decode("utf-8", "replace").strip()
            raise InputError(f"rg failed: {err_txt.splitlines()[-1] if err_txt else 'invalid regex pattern'}")
        return {"brain": cfg.name, "engine": "rg", "query": pattern, "count": 0, "truncated": False, "items": []}
    cmd = [rg_bin, "--json", "--no-config", "--no-ignore", "--no-follow", "--max-count", "1", "-i"]
    if sub_dir is not None:
        cmd.extend(["--glob", "*.json"])
    cmd.extend(["--max-filesize", "64M", "-e", pattern, str(store.root / target_rel)])
    matched_paths: list[str] = []
    snippets: dict[str, str] = {}
    timed_out, eof = threading.Event(), False
    with (
        tempfile.TemporaryFile(mode="w+b") as err_f,
        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_f, cwd="/", env=safe_env()) as proc,  # noqa: S603
    ):
        timer = threading.Timer(30.0, lambda: (timed_out.set(), proc.kill()) if proc.poll() is None else None)
        timer.daemon = True
        timer.start()
        try:
            for raw_line in cast("IO[bytes]", proc.stdout):
                if not (stripped := raw_line.strip()):
                    continue
                event = decode(stripped, "rg output", check_duplicates=False)
                if not isinstance(event, dict) or event.get("type") != "match":
                    continue
                data = event.get("data")
                raw_path = (
                    data["path"].get("text") if isinstance(data, dict) and isinstance(data.get("path"), dict) else None
                )
                if not isinstance(data, dict) or not isinstance(raw_path, str):
                    continue
                with suppress(ValueError):
                    rel_path = Path(raw_path).resolve().relative_to(store.root).as_posix()
                    if rel_path.startswith("memories/") and rel_path.endswith(".json") and rel_path not in snippets:
                        matched_paths.append(rel_path)
                        snippets[rel_path] = _rg_snippet(data)
                        if len(matched_paths) > limit:
                            break
            else:
                eof = True
        finally:
            rc = _finish_proc(proc, timer, eof=eof)
        err_bytes = _read_stderr(err_f)

    if not (truncated := len(matched_paths) > limit):
        if timed_out.is_set():
            raise Error("rg timed out after 30s")
        if rc not in {0, 1}:
            err_txt = err_bytes.decode("utf-8", "replace").strip()
            raise InputError(f"rg failed: {err_txt.splitlines()[-1] if err_txt else 'invalid regex pattern'}")

    items: list[dict[str, Any]] = []
    for rel_path in matched_paths[:limit]:
        rec = _decode_obj(store, rel_path, check_duplicates=False)
        item = {"ref": rec.get("ref", rel_path), "source": rec.get("source", ""), "kind": rec.get("kind", "memory")}
        items.append({**item, "title": rec.get("title", ""), "path": rel_path, "snippet": snippets.get(rel_path, "")})
    head = {"brain": cfg.name, "engine": "rg", "query": pattern, "count": len(items)}
    return {**head, "truncated": truncated, "items": items}


def query_jq(store: Store, expr: str, *, scope: str = "", limit: int = 20) -> dict[str, Any]:
    """Filter pure JSON memory records by streaming confined files directly into isolated `jq -c`."""
    cfg = config.load(store)
    _arg_clean(expr, "jq filter")
    if limit < 1 or limit > 5000:
        raise InputError("limit must be between 1 and 5000")
    single_file, sub_dir = _resolve_scope(store, scope)
    jq_cmd = [_tool("jq"), "-L", "/dev/null", "-c", "--", f"{JQ_PRELUDE}\n{expr}"]

    stop_feed, timed_out = threading.Event(), threading.Event()
    feeder_err: list[Error | OSError] = []
    out: list[Any] = []
    truncated, eof, pipe = False, False, subprocess.PIPE
    with (
        tempfile.TemporaryFile(mode="w+b") as err_f,
        subprocess.Popen(jq_cmd, stdin=pipe, stdout=pipe, stderr=err_f, cwd="/", env=safe_env()) as proc,  # noqa: S603
    ):
        timer = threading.Timer(30.0, lambda: (timed_out.set(), proc.kill()) if proc.poll() is None else None)
        timer.daemon = True
        timer.start()

        def feed() -> None:
            stdin = cast("IO[bytes]", proc.stdin)
            try:
                if single_file is not None and not stop_feed.is_set():
                    data = store.read(single_file)
                    stdin.write(data if data.endswith(b"\n") else data + b"\n")
                elif sub_dir is not None:
                    for _, data in store.iter_read_files(sub_dir, suffix=".json"):
                        if stop_feed.is_set():
                            break
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
        feeder.start()
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
        err_bytes = _read_stderr(err_f)

    if not truncated:
        if timed_out.is_set():
            raise InputError("jq failed: timed out after 30s")
        if feeder_err:
            raise feeder_err[0]
        if rc != 0:
            err = err_bytes.decode("utf-8", "replace").strip()
            raise InputError(f"jq failed: {err.splitlines()[-1] if err else 'jq filter failed'}")
    items = out[:limit]
    head = {"brain": cfg.name, "engine": "jq", "query": expr, "count": len(items)}
    return {**head, "truncated": truncated, "items": items}


def find_by_alias(store: Store, identity: str) -> tuple[str, dict[str, Any]] | None:
    """Locate a stored JSON memory whose `entity` or `aliases` equals `identity`, preferring authored notes."""

    def match_paths(paths: list[str]) -> tuple[str, dict[str, Any]] | None:
        for path in paths:
            with suppress(Error):
                parsed = _decode_obj(store, path, check_duplicates=False)
                aliases = parsed.get("aliases")
                if parsed.get("entity") == identity or (isinstance(aliases, list) and identity in aliases):
                    return path, parsed
        return None

    authored = sorted(p for d in AUTHORED for p in store.files(f"memories/{d}") if p.endswith(".json"))
    if (found := match_paths(authored)) is not None or not store.is_dir("memories"):
        return found
    if (rg := shutil.which("rg")) and '"' not in identity and "\\" not in identity:
        flags = ("-l", "-F", "--no-config", "--no-ignore", "--no-follow", "--glob", "*.json", "--")
        cmd = [rg, *flags, identity, str(store.root / "memories")]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd="/", env=safe_env(), timeout=15, check=False)  # noqa: S603
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


def read_target(store: Store, raw_target: str, *, declared_brains_only: bool = False) -> dict[str, Any]:
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
        sib = config.resolve_brain(store, cfg, host, declared_only=declared_brains_only)
        return read_target(sib, sub, declared_brains_only=declared_brains_only)

    def render_record(hit_path: str, hit_rec: dict[str, Any], def_ref: str, def_kind: str = "record") -> dict[str, Any]:
        ref_str = str(hit_rec.get("ref", def_ref))
        return {"brain": cfg.name, "kind": def_kind, "ref": ref_str, "path": hit_path, "record": hit_rec}

    if kind == "url":
        if (
            hit := find_by_alias(store, to) or (find_by_alias(store, to[4:]) if to[:4] == "urn:" else None)
        ) is not None:
            return render_record(hit[0], hit[1], to)
        raise InputError("external URLs are not read offline; pass a note path, source:id, or bf:// URI")
    if anchor and (kind == "record" or to.endswith(".json")):
        raise InputError("JSON records do not support #fragments; pipe bf read into jq")
    if any(part.startswith(".") for part in to.split("/")) or to.split("/")[0].lower() == "logs":
        raise InputError(f"{to}: private or runtime paths are not readable via bf read")

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
        compiled, _ = engine.load_or_compile_note(store, to, raw_bytes, brain_name=cfg.name)
        if anchor:
            sec = markdown.extract_section(text, anchor, compiled)
            return {"brain": cfg.name, "kind": "section", "ref": to, **sec}
        return {"brain": cfg.name, "kind": compiled["kind"], "ref": to, "record": compiled, "markdown": text}
    return {"brain": cfg.name, "kind": "file", "ref": to, "text": text}


def load_eval_doc(store: Store, path: str, val: Any, problems: list[str]) -> dict[str, Any] | None:
    """Read, version-check, and schema-validate one `evals/*.{yaml,yml,json}` suite."""
    try:
        raw_bytes = store.read(path, 1 << 20)
        doc = decode(raw_bytes, path) if path.endswith(".json") else yaml_object(raw_bytes, path)
        if not isinstance(doc, dict):
            raise Error(f"{path}: expected a mapping")
        check_version(doc, path)
        if val is not None and (errs := validate_instance(val, doc, path)):
            problems.extend(errs)
            return None
        return doc
    except Error as error:
        problems.append(str(error))
        return None


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
        if (doc := load_eval_doc(store, path, val, problems)) is None:
            continue

        for case in cases if isinstance(cases := doc.get("cases"), list) else []:
            if not isinstance(case, dict):
                continue
            name, eng = str(case["name"]), str(case.get("engine") or "rg")
            q_str, sc = str(case["query"]), str(case.get("scope") or "")
            reasons: list[str] = []
            try:
                if eng not in {"rg", "jq", "read"}:
                    raise Error(f"{path}: unsupported eval engine {eng!r}")
                out = (
                    search_rg(store, q_str, scope=sc, limit=200)
                    if eng == "rg"
                    else query_jq(store, q_str, scope=sc, limit=500)
                    if eng == "jq"
                    else read_target(store, q_str)
                )
                items = out["items"] if eng in {"rg", "jq"} else [out]
                refs = {str(it.get("ref", "")) for it in items if isinstance(it, dict)}
                rendered = encode(items).decode("utf-8").lower()
                reasons.extend(f"missing expected ref {r}" for r in (case.get("expect_refs") or []) if r not in refs)
                reasons.extend(f"matched forbidden ref {b}" for b in (case.get("forbid_refs") or []) if b in refs)
                exp_txt = case.get("expect_text") or []
                reasons.extend(f"missing expected text {s!r}" for s in exp_txt if str(s).lower() not in rendered)
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
