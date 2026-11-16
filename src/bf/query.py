"""Offline retrieval and deterministic evaluation via `rg`, `jq`, and sandboxed `duckdb`."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import quote

from bf import config, markdown, records
from bf.config import yaml_object
from bf.models import AUTHORED, MAX_NOTE, Error, InputError, check_version, clean, clean_text, decode, encode
from bf.schemas import EVALS_SCHEMA, load_validator, validate_instance
from bf.storage import Store, relative

_DUCKDB_COLS = (
    "columns = {id: 'VARCHAR', ref: 'VARCHAR', source: 'VARCHAR', kind: 'VARCHAR', type: 'VARCHAR', "
    "status: 'VARCHAR', title: 'VARCHAR', summary: 'VARCHAR', text: 'VARCHAR', time: 'VARCHAR', "
    "updated: 'VARCHAR', url: 'VARCHAR', tags: 'JSON', aliases: 'JSON', links: 'JSON', relations: 'JSON', "
    "headings: 'JSON', elements: 'JSON', attrs: 'JSON', tasks: 'JSON', attributes: 'JSON'}"
)
_EMPTY_TABLE = (
    "CREATE TEMP TABLE records (id VARCHAR, ref VARCHAR, path VARCHAR, source VARCHAR, kind VARCHAR, type VARCHAR, "
    "status VARCHAR, title VARCHAR, summary VARCHAR, text VARCHAR, time VARCHAR, updated VARCHAR, "
    "url VARCHAR, tags JSON, aliases JSON, links JSON, relations JSON, headings JSON, elements JSON, "
    "attrs JSON, tasks JSON, attributes JSON);"
)


def _tool(name: str) -> str:
    if found := shutil.which(name):
        return found
    raise Error(f"{name} is required on PATH; install it with mise install")


def _safe_env() -> dict[str, str]:
    env = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for key in ("HOME", "TMPDIR"):
        if val := os.environ.get(key):
            env[key] = val
    return env


def _decode_obj(store: Store, path: str) -> dict[str, Any]:
    if not isinstance(parsed := decode(store.read(path), path), dict):
        raise Error(f"{path}: expected a JSON object")
    return cast("dict[str, Any]", parsed)


def _enrich_stored(rec: dict[str, Any], path: str) -> dict[str, Any]:
    out, parts = dict(rec), path.split("/")
    src = str(out.get("source") or (parts[1] if len(parts) >= 3 and parts[0] == "memories" else "memory"))
    out.setdefault("source", src)
    if "ref" not in out and isinstance(out.get("id"), str):
        out["ref"] = out["id"] if src in AUTHORED else f"{src}:{out['id']}"
    out.setdefault("kind", {"projects": "project", "concepts": "concept", "actions": "action"}.get(src, "memory"))
    return out


def _scope_paths(store: Store, scope: str) -> tuple[list[str], str]:
    if not (cleaned := scope.strip().strip("/")):
        return [p for p in store.files("memories") if p.endswith(".json")], "memories"
    relative(cleaned)
    if markdown.okf_role(cleaned) is not None:
        comp = markdown.compiled_note_path(cleaned)
        return ([comp] if store.exists(comp) else []), comp
    if cleaned.endswith(".json") and cleaned.startswith("memories/"):
        return ([cleaned] if store.exists(cleaned) else []), cleaned
    sub = cleaned if cleaned.startswith("memories/") else f"memories/{cleaned}"
    relative(sub)
    return [p for p in store.files(sub) if p.endswith(".json")], sub


def search_rg(store: Store, pattern: str, *, scope: str = "", limit: int = 20) -> dict[str, Any]:
    """Run lexical search over pure JSON memories via `rg --json --no-config --no-ignore`."""
    cfg = config.load(store)
    try:
        clean(pattern, "query")
    except Error as error:
        raise InputError(str(error)) from error
    if limit < 1 or limit > 500:
        raise InputError("limit must be between 1 and 500")
    allowed_files, sub = _scope_paths(store, scope)
    if not allowed_files:
        return {"brain": cfg.name, "engine": "rg", "query": pattern, "count": 0, "items": []}
    allowed_set = set(allowed_files)
    cmd = [_tool("rg"), "--json", "--no-config", "--no-ignore", "-i", "--max-filesize", "64M"]
    cmd.extend(["-e", pattern, str(store.root / sub)])
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_safe_env(), timeout=30, check=False)  # noqa: S603
    if proc.returncode not in {0, 1}:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "invalid regex pattern"
        raise InputError(f"rg failed: {detail}")

    matched_paths: list[str] = []
    snippets: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        event = decode(line, "rg output")
        if (
            not isinstance(event, dict)
            or event.get("type") != "match"
            or not isinstance(data := event.get("data"), dict)
        ):
            continue
        raw_path = p_obj.get("text") if isinstance(p_obj := data.get("path"), dict) else None
        if not isinstance(raw_path, str):
            continue
        try:
            rel_path = Path(raw_path).resolve().relative_to(store.root).as_posix()
        except ValueError:
            continue
        if rel_path in allowed_set and rel_path not in snippets:
            matched_paths.append(rel_path)
            l_obj = data.get("lines")
            snippets[rel_path] = (str(l_obj.get("text", "")).strip() if isinstance(l_obj, dict) else "")[:240]
            if len(matched_paths) >= limit:
                break

    items: list[dict[str, Any]] = []
    for rel_path in matched_paths:
        rec = _enrich_stored(_decode_obj(store, rel_path), rel_path)
        item = {"ref": rec.get("ref", rel_path), "source": rec.get("source", ""), "kind": rec.get("kind", "memory")}
        items.append({**item, "title": rec.get("title", ""), "path": rel_path, "snippet": snippets.get(rel_path, "")})
    return {"brain": cfg.name, "engine": "rg", "query": pattern, "count": len(items), "items": items}


def check_jq_syntax(expr: str) -> str | None:
    """Return `None` when `expr` is valid `jq` syntax, else a sanitized error string."""
    try:
        clean(expr, "jq filter")
    except Error as error:
        return str(error)
    cmd = [_tool("jq"), "-n", "--", f"empty | ({expr})"]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_safe_env(), timeout=5, check=False)  # noqa: S603
    if proc.returncode == 0:
        return None
    return proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "invalid jq syntax"


def _run_jq_stream(expr: str, payloads: list[bytes], limit: int = 100) -> list[Any]:
    cmd = [_tool("jq"), "-c", "--", expr]
    proc = subprocess.run(cmd, input=b"".join(payloads), capture_output=True, env=_safe_env(), timeout=30, check=False)  # noqa: S603
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise InputError(f"jq failed: {err.splitlines()[-1] if err else 'jq filter failed'}")
    out: list[Any] = []
    for line in proc.stdout.decode("utf-8").splitlines():
        if line.strip():
            out.append(decode(line, "jq output"))
            if len(out) >= limit:
                break
    return out


def query_jq(store: Store, expr: str, *, scope: str = "", limit: int = 100) -> dict[str, Any]:
    """Filter pure JSON memory records by streaming confined files into `jq -c`."""
    cfg = config.load(store)
    try:
        clean(expr, "jq filter")
    except Error as error:
        raise InputError(str(error)) from error
    if limit < 1 or limit > 5000:
        raise InputError("limit must be between 1 and 5000")
    files, _ = _scope_paths(store, scope)
    if not files:
        if bad := check_jq_syntax(expr):
            raise InputError(f"jq failed: {bad}")
        return {"brain": cfg.name, "engine": "jq", "query": expr, "count": 0, "items": []}
    payloads = [encode(_enrich_stored(_decode_obj(store, path), path)) for path in files]
    items = _run_jq_stream(expr, payloads, limit=limit)
    return {"brain": cfg.name, "engine": "jq", "query": expr, "count": len(items), "items": items}


def query_sql(store: Store, sql: str, *, scope: str = "") -> dict[str, Any]:
    """Execute analytical SQL over `records` in DuckDB with external access locked off."""
    cfg = config.load(store)
    clean_text(sql, "sql")
    if not (stripped := sql.strip()) or stripped.startswith("."):
        raise InputError("sql must be a nonempty SQL statement without CLI dot-commands")
    files, sub = _scope_paths(store, scope)
    if not files:
        setup = _EMPTY_TABLE
    else:
        target_path = (store.root / sub).as_posix().replace("'", "''")
        glob_pat = target_path if sub.endswith(".json") else f"{target_path}/**/*.json"
        mem_re = ("^" + re.escape((store.root / "memories").as_posix()) + "/([^/]+)/").replace("'", "''")
        src_sql = f"regexp_extract(filename, '{mem_re}', 1)"
        setup = (
            "CREATE TEMP TABLE records AS SELECT "  # noqa: S608 - glob_pat and mem_re are confined to store.root and single-quote escaped
            f"id, COALESCE(ref, CASE WHEN COALESCE(source, {src_sql}) IN ('projects', 'concepts', 'actions') "
            f"THEN id ELSE COALESCE(source, {src_sql}) || ':' || id END) AS ref, "
            f"substr(filename, {len(store.root.as_posix()) + 2}) AS path, "
            f"COALESCE(source, {src_sql}) AS source, "
            f"COALESCE(kind, CASE {src_sql} WHEN 'projects' THEN 'project' WHEN 'concepts' THEN 'concept' "
            "WHEN 'actions' THEN 'action' ELSE 'memory' END) AS kind, "
            "type, status, title, summary, text, time, updated, url, "
            "tags, aliases, links, relations, headings, elements, attrs, tasks, attributes "
            f"FROM read_json('{glob_pat}', format = 'unstructured', {_DUCKDB_COLS}, "
            "filename = true, maximum_object_size = 33554432);"
        )
    with tempfile.TemporaryDirectory(prefix="bf-duckdb-") as tmpdir:
        tmp_sql = Path(tmpdir).as_posix().replace("'", "''")
        script = (
            f"SET temp_directory = '{tmp_sql}'; SET enable_progress_bar = false; SET preserve_insertion_order = false; "
            f"SET threads = 4; SET memory_limit = '512MB'; {setup} SET enable_external_access = false; "
            f"SET lock_configuration = true;\n{stripped}"
        )
        cmd = [_tool("duckdb"), "-no-init", "-light-mode", "-batch", "-bail", "-json", "-c", script]
        proc = subprocess.run(cmd, cwd=tmpdir, capture_output=True, text=True, env=_safe_env(), timeout=60, check=False)  # noqa: S603
    if proc.returncode != 0:
        lines = [ln.strip() for ln in proc.stderr.splitlines() if ln.strip()]
        raise InputError(f"duckdb failed: {lines[0] if lines else 'SQL query failed'}")
    try:
        rows = decode(raw_out, "duckdb output") if (raw_out := proc.stdout.strip()) else []
    except Error as error:
        raise InputError("duckdb failed: pass a single SQL query returning rows") from error
    items = [dict(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    return {"brain": cfg.name, "engine": "sql", "query": stripped, "count": len(items), "items": items}


def _extract_markdown_section(text: str, anchor: str, compiled: dict[str, Any]) -> dict[str, Any]:
    if markdown.is_jq_fragment(anchor):
        return {"anchor": anchor, "jq": _run_jq_stream(anchor, [encode(compiled)], limit=500)}
    for key, out_key in (("elements", "element"), ("tasks", "task")):
        for item in compiled.get(key, []):
            if isinstance(item, dict) and item.get("id") == anchor:
                return {"anchor": anchor, out_key: item}
    heading = next((h for h in compiled.get("headings", []) if isinstance(h, dict) and h.get("id") == anchor), None)
    if heading is None:
        if isinstance(attrs := compiled.get("attrs"), dict) and anchor in attrs:
            return {"anchor": anchor, "meta": attrs[anchor]}
        raise Error(f"{compiled['ref']}: missing anchor #{anchor}")
    level, lines, title_lc = int(heading["level"]), text.splitlines(), heading["title"].lower()
    start_pat = re.compile(rf"^#{{{level}}}\s+.*")
    start_line = next(
        (i for i, ln in enumerate(lines) if start_pat.match(ln) and (f"#{anchor}" in ln or title_lc in ln.lower())), 0
    )
    end_line = next(
        (
            i
            for i in range(start_line + 1, len(lines))
            if (m := re.match(r"^(#{1,6})\s+", lines[i])) and len(m[1]) <= level
        ),
        len(lines),
    )
    return {"anchor": anchor, "heading": heading, "text": "\n".join(lines[start_line:end_line]).strip()}


def find_by_alias(store: Store, identity: str) -> tuple[str, dict[str, Any]] | None:
    """Locate a stored JSON memory whose `entity` or `aliases` equals `identity`, preferring authored notes."""

    def match_paths(paths: list[str]) -> tuple[str, dict[str, Any]] | None:
        for path in paths:
            with suppress(Error):
                parsed = _decode_obj(store, path)
                aliases = parsed.get("aliases")
                if parsed.get("entity") == identity or (isinstance(aliases, list) and identity in aliases):
                    return path, _enrich_stored(parsed, path)
        return None

    authored = sorted(p for d in AUTHORED for p in store.files(f"memories/{d}") if p.endswith(".json"))
    if (found := match_paths(authored)) is not None or not store.is_dir("memories"):
        return found
    if (rg := shutil.which("rg")) and '"' not in identity and "\\" not in identity:
        cmd = [rg, "-l", "-F", "--no-config", "--no-ignore", "--", identity, str(store.root / "memories")]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=_safe_env(), timeout=15, check=False)  # noqa: S603
        if proc.returncode in {0, 1}:
            cands: set[str] = set()
            for raw_ln in proc.stdout.splitlines():
                with suppress(ValueError):
                    rel = Path(raw_ln).resolve().relative_to(store.root).as_posix()
                    if rel.startswith("memories/") and rel.endswith(".json") and rel.split("/")[1] not in AUTHORED:
                        cands.add(rel)
            return match_paths(sorted(cands))
    return match_paths([p for p in store.files("memories") if p.endswith(".json") and p.split("/")[1] not in AUTHORED])


def read_target(store: Store, raw_target: str = "") -> dict[str, Any]:
    """Read the brain overview, an OKF note, a `#section` / `#jq` selector, a `source:id` record, or a `bf://` URL."""
    cfg = config.load(store)
    if not (target := raw_target.strip()):
        folders = (*AUTHORED, "memories", "schemas", "evals")
        counts = {f: len([p for p in store.files(f) if not p.endswith("/.forgotten")]) for f in folders}
        groups = (("job", cfg.jobs), ("channel", cfg.channels), ("server", cfg.servers))
        attention = [
            {"kind": k, "name": n, "failed": True}
            for k, units in groups
            for n, u in units.items()
            if u.enabled and store.exists(lp := f"logs/{n}.json") and _decode_obj(store, lp).get("status") == "failed"
        ]
        return {"brain": cfg.name, "kind": "overview", "counts": counts, "attention": attention}

    parsed_link = markdown.parse_link("<read>", target, cfg.name, relative_base=False, allow_record_ref=True)
    kind, to, anchor = str(parsed_link["kind"]), str(parsed_link["to"]), str(parsed_link["anchor"])
    if kind == "brain":
        host, _, rest = to.removeprefix("bf://").partition("/")
        q_rest = quote(rest, safe="/:@")
        sub = f"bf:///{q_rest}#{quote(anchor, safe='')}" if anchor else f"bf:///{q_rest}"
        return read_target(config.resolve_brain(store, cfg, host), sub)
    if kind == "url":
        raise InputError("external URLs are not read offline; pass a note path, source:id, or bf:// URI")

    def render_record(
        hit_path: str, hit_rec: dict[str, Any], default_ref: str, default_kind: str = "record"
    ) -> dict[str, Any]:
        if anchor:
            res_jq = _run_jq_stream(anchor, [encode(hit_rec)], limit=500)
            return {"brain": cfg.name, "kind": "jq", "ref": default_ref, "result": res_jq}
        ref_str = str(hit_rec.get("ref", default_ref))
        return {"brain": cfg.name, "kind": default_kind, "ref": ref_str, "path": hit_path, "record": hit_rec}

    if to.startswith("memories/") and to.endswith(".json"):
        if not store.exists(to):
            raise Error(f"record {to} not found")
        rec = _enrich_stored(_decode_obj(store, to), to)
        return render_record(to, rec, str(rec.get("ref", to)), str(rec.get("kind", "record")))
    if kind == "record":
        src, _, rec_id = to.partition(":")
        cand = records.record_file(src, rec_id)
        hit = (cand, _enrich_stored(_decode_obj(store, cand), cand)) if store.exists(cand) else find_by_alias(store, to)
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
                elif eng == "sql":
                    out = query_sql(store, q_str, scope=sc)
                else:
                    out = read_target(store, q_str)
                items = out["items"] if eng in {"rg", "jq", "sql"} else [out]
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
            results.append(
                {"file": path, "name": name, "engine": eng, "ok": ok, **({"reasons": reasons} if reasons else {})}
            )

    ok_all = failed == 0 and not problems
    return {"brain": cfg.name, "ok": ok_all, "passed": passed, "failed": failed, "cases": results, "problems": problems}
