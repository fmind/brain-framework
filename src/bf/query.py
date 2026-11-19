"""Offline retrieval and deterministic evaluation via `rg` and `jq`."""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable
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
    "def get_jq_origin: null; def get_prog_origin: null; def get_search_list: []; def env: {}; {} as $ENV |"
)
_SHIM_A = ("PATH", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")
_SHIM_B = ("MISE_DATA_DIR", "MISE_CONFIG_DIR", "MISE_CACHE_DIR", "MISE_STATE_DIR", "MISE_TRUSTED_CONFIG_PATHS")
_SHIM_ENV = (*_SHIM_A, *_SHIM_B)
_TOOL_CACHE: dict[tuple[str, ...], str] = {}


def _is_shim(path: str) -> bool:
    with suppress(OSError):
        p = Path(path)
        if p.resolve().name == "mise":
            return True
        with p.open("rb") as handle:
            return handle.read(2) == b"#!"
    return False


def resolve_tool(name: str) -> str:
    """Resolve `name` on PATH, unwrapping `mise` shims to the underlying executable."""
    if not (found := shutil.which(name)):
        raise Error(f"{name} is required on PATH; install it with mise install")
    key = (name, found, *(os.environ.get(k, "") for k in _SHIM_ENV))
    if (cached := _TOOL_CACHE.get(key)) is not None and Path(cached).is_file():
        return cached
    target, is_mise = found, False
    with suppress(OSError, subprocess.SubprocessError):
        if is_mise := (real := Path(found).resolve()).name == "mise":
            kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "cwd": "/"}
            proc = subprocess.run([str(real), "which", name], **kw, env=safe_env(found), timeout=5, check=False)  # noqa: S603
            cand = proc.stdout.strip() if proc.returncode == 0 else ""
            if cand and (cp := Path(cand)).is_absolute() and cp.is_file():
                target = cand
    if not is_mise or target != found:
        if len(_TOOL_CACHE) >= 32:
            _TOOL_CACHE.pop(next(iter(_TOOL_CACHE)))
        _TOOL_CACHE[key] = target
    return target


def safe_env(binary: str = "") -> dict[str, str]:
    """Return a locale-only env for native binaries, or minimal non-secret shim vars for wrapper scripts."""
    base = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
    return {**{k: v for k in _SHIM_ENV if (v := os.environ.get(k))}, **base} if binary and _is_shim(binary) else base


def drain_tail(
    stream: IO[bytes], limit: int, stop: threading.Event | None = None
) -> tuple[threading.Thread, bytearray]:
    """Drain `stream` in a daemon thread while retaining at most the last `limit` bytes in memory."""
    buf, fd, poller = bytearray(), None, None
    with suppress(OSError, ValueError):
        fd, poller = stream.fileno(), select.poll()
        poller.register(fd, select.POLLIN)

    def _read() -> None:
        with suppress(OSError, ValueError):
            while True:
                if fd is not None and poller is not None:
                    done = bool(stop and stop.is_set())
                    if not poller.poll(0 if done else 200):
                        if done:
                            break
                        continue
                    chunk = os.read(fd, 8192)
                else:
                    chunk = stream.read(8192)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > limit * 2:
                    del buf[:-limit]
            if len(buf) > limit:
                del buf[:-limit]

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    return thread, buf


def _rg_fail(rc: int, err_bytes: bytes) -> None:
    if rc not in {0, 1} or (rc == 1 and err_bytes.strip()):
        err_txt = err_bytes.decode("utf-8", "replace").strip()
        msg = f"rg failed: {err_txt.splitlines()[-1] if err_txt else 'invalid regex pattern'}"
        raise InputError(msg) if rc == 2 else Error(msg)


def stop_proc(proc: subprocess.Popen[bytes], wait: float = 2.0) -> int:
    """Terminate a subprocess process group (`SIGTERM`, then `SIGKILL` after `wait`s)."""
    with suppress(OSError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        return proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        with suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with suppress(OSError, subprocess.TimeoutExpired):
            return proc.wait(timeout=2)
        return -1 if proc.returncode is None else proc.returncode


type _OutReader = Callable[[IO[bytes]], bool]
type _InFeed = Callable[[IO[bytes], threading.Event], None]


def _stream_proc(cmd: list[str], binary: str, rd: _OutReader, fd: _InFeed | None = None) -> tuple[int, bytes, bool]:
    timed_out, stop_feed, err_stop, pipe = threading.Event(), threading.Event(), threading.Event(), subprocess.PIPE
    stdin_mode = pipe if fd is not None else subprocess.DEVNULL
    with subprocess.Popen(  # noqa: S603
        cmd, stdin=stdin_mode, stdout=pipe, stderr=pipe, cwd="/", env=safe_env(binary), start_new_session=True
    ) as proc:
        err_t, err_buf = drain_tail(cast("IO[bytes]", proc.stderr), 8192, err_stop)

        def _timeout() -> None:
            if proc.poll() is None:
                timed_out.set()
                with suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)

        timer = threading.Timer(30.0, _timeout)
        timer.daemon = True
        timer.start()
        feeder: threading.Thread | None = None
        if fd is not None:
            feeder = threading.Thread(target=fd, args=(cast("IO[bytes]", proc.stdin), stop_feed), daemon=True)
            feeder.start()
        eof = False
        try:
            eof = rd(cast("IO[bytes]", proc.stdout))
        finally:
            stop_feed.set()
            try:
                rc = proc.wait(timeout=5) if eof else stop_proc(proc, wait=5.0)
            except subprocess.TimeoutExpired:
                rc = stop_proc(proc, wait=0.0)
            finally:
                timer.cancel()
                err_stop.set()
            if feeder is not None:
                feeder.join(timeout=5)
            err_t.join(timeout=2)
        return rc, bytes(err_buf), timed_out.is_set()


def _decode_obj(store: Store, path: str, *, check_duplicates: bool = True) -> dict[str, Any]:
    if not isinstance(parsed := decode(store.read(path), path, check_duplicates=check_duplicates), dict):
        raise Error(f"{path}: expected a JSON object")
    return cast("dict[str, Any]", parsed)


def _as_input[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return fn(*args, **kwargs)
    except Error as error:
        raise InputError(str(error)) from error


def _resolve_scope(store: Store, scope: str) -> tuple[str | None, str | None]:
    """Return `(single_file, directory)` in `memories/` after `O_NOFOLLOW` confinement checks."""
    if not (stripped := scope.strip()):
        return None, ("memories" if store.is_dir("memories") else None)
    cleaned = stripped.rstrip("/")
    _as_input(relative, cleaned)
    if markdown.okf_role(cleaned) is not None:
        comp = markdown.compiled_note_path(cleaned)
        return (comp if store.exists(comp) else None), None
    if cleaned.endswith(".json") and cleaned.startswith("memories/"):
        return (cleaned if store.exists(cleaned) else None), None
    sub = cleaned if cleaned == "memories" or cleaned.startswith("memories/") else f"memories/{cleaned}"
    _as_input(relative, sub)
    return None, (sub if store.is_dir(sub) else None)


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


def _mem_rel(store: Store, raw_path: str) -> str | None:
    with suppress(ValueError):
        rel = Path(raw_path).resolve().relative_to(store.root).as_posix()
        if rel.startswith("memories/") and rel.endswith(".json"):
            return rel
    return None


def search_rg(store: Store, pattern: str, *, scope: str = "", limit: int = 20) -> dict[str, Any]:
    """Run lexical search over pure JSON memories via `rg --json --no-config --no-ignore --no-follow`."""
    cfg = config.load(store)
    _as_input(clean, pattern, "query")
    if limit < 1 or limit > 500:
        raise InputError("limit must be between 1 and 500")
    single_file, sub_dir = _resolve_scope(store, scope)
    target_rel, rg_bin = single_file or sub_dir, resolve_tool("rg")
    if target_rel is None:
        r_kw: dict[str, Any] = {"input": b"", "capture_output": True, "cwd": "/", "env": safe_env(rg_bin), "timeout": 5}
        try:
            chk = subprocess.run([rg_bin, "--no-config", "-e", pattern], **r_kw, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as error:
            raise Error("rg timed out after 5s") from error
        _rg_fail(chk.returncode, chk.stderr)
        return {"brain": cfg.name, "engine": "rg", "query": pattern, "count": 0, "truncated": False, "items": []}
    cmd = [rg_bin, "--json", "--no-config", "--no-ignore", "--no-follow", "--max-count", "1", "-i"]
    if sub_dir is not None:
        cmd.extend(["--glob", "*.json"])
    cmd.extend(["--max-filesize", "64M", "-e", pattern, str(store.root / target_rel)])
    matched_paths, snippets = [], {}

    def read_rg(stdout: IO[bytes]) -> bool:
        for raw_line in stdout:
            if not (stripped := raw_line.strip()):
                continue
            ev = decode(stripped, "rg output", check_duplicates=False)
            if not isinstance(ev, dict) or ev.get("type") != "match" or not isinstance(data := ev.get("data"), dict):
                continue
            raw_path = data["path"].get("text") if isinstance(data.get("path"), dict) else None
            if isinstance(raw_path, str) and (rel := _mem_rel(store, raw_path)) and rel not in snippets:
                matched_paths.append(rel)
                snippets[rel] = _rg_snippet(data)
                if len(matched_paths) > limit:
                    return False
        return True

    rc, err_bytes, timed_out = _stream_proc(cmd, rg_bin, read_rg)
    if not (truncated := len(matched_paths) > limit):
        if timed_out:
            raise Error("rg timed out after 30s")
        _rg_fail(rc, err_bytes)
    items = []
    for p in matched_paths[:limit]:
        r = _decode_obj(store, p, check_duplicates=False)
        item = {"ref": r.get("ref", p), "source": r.get("source", ""), "kind": r.get("kind", "memory")}
        items.append({**item, "title": r.get("title", ""), "path": p, "snippet": snippets.get(p, "")})
    head = {"brain": cfg.name, "engine": "rg", "query": pattern, "count": len(items)}
    return {**head, "truncated": truncated, "items": items}


def query_jq(store: Store, expr: str, *, scope: str = "", limit: int = 20) -> dict[str, Any]:
    """Filter pure JSON memory records by streaming confined files directly into isolated `jq -c`."""
    cfg = config.load(store)
    _as_input(clean, expr, "jq filter")
    if limit < 1 or limit > 5000:
        raise InputError("limit must be between 1 and 5000")
    single_file, sub_dir = _resolve_scope(store, scope)
    jq_bin = resolve_tool("jq")
    jq_cmd = [jq_bin, "-L", "/dev/null", "-c", "--", f"{JQ_PRELUDE}\n{expr}"]
    feeder_err: list[Error | OSError] = []
    out: list[Any] = []

    def feed_jq(stdin: IO[bytes], stop: threading.Event) -> None:
        try:
            blobs: Any = ((single_file, store.read(single_file)),) if single_file is not None else ()
            blobs = store.iter_read_files(sub_dir, suffix=".json") if single_file is None and sub_dir else blobs
            for _, data in blobs:
                if stop.is_set():
                    break
                stdin.write(data if data.endswith(b"\n") else data + b"\n")
            stdin.flush()
        except BrokenPipeError:
            pass
        except (Error, OSError) as error:
            if not stop.is_set():
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

    rc, err_bytes, timed_out = _stream_proc(jq_cmd, jq_bin, read_jq, feed_jq)
    if not (truncated := len(out) > limit):
        if timed_out:
            raise Error("jq timed out after 30s")
        if feeder_err:
            raise feeder_err[0]
        if rc != 0:
            err = err_bytes.decode("utf-8", "replace").strip()
            msg = f"jq failed: {err.splitlines()[-1] if err else 'jq filter failed'}"
            raise InputError(msg) if rc == 3 or (rc == 5 and "parse error:" not in err) else Error(msg)
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
    rg: str | None = None
    with suppress(Error):
        rg = resolve_tool("rg")
    if rg and '"' not in identity and "\\" not in identity:
        flags = ("-l", "-F", "--no-config", "--no-ignore", "--no-follow", "--glob", "*.json", "--")
        rkw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "cwd": "/"}
        cmd = [rg, *flags, identity, str(store.root / "memories")]
        try:
            proc = subprocess.run(cmd, **rkw, env=safe_env(rg), timeout=15, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as error:
            raise Error("rg alias lookup timed out") from error
        if proc.returncode == 0 or (proc.returncode == 1 and not proc.stderr.strip()):
            rels = (_mem_rel(store, ln) for ln in proc.stdout.splitlines())
            return match_paths(sorted({r for r in rels if r and r.split("/")[1] not in AUTHORED}))
    return match_paths([p for p in store.files("memories") if p.endswith(".json") and p.split("/")[1] not in AUTHORED])


def read_target(store: Store, raw_target: str, *, declared_brains_only: bool = False) -> dict[str, Any]:
    """Read an OKF note, `note#anchor` section, `source:id` record, JSON memory, or `bf://` URI."""
    cfg = config.load(store)
    if not (target := raw_target.strip()):
        raise InputError("target is required; use bf status for brain overview")
    parsed_link = _as_input(markdown.parse_link, "<read>", target, cfg.name, relative_base=False, allow_record_ref=True)
    kind, to, anchor = str(parsed_link["kind"]), str(parsed_link["to"]), str(parsed_link["anchor"])
    if kind == "brain":
        host, _, rest = to.removeprefix("bf://").partition("/")
        q_rest = quote(rest, safe="/:@")
        sub = f"bf:///{q_rest}#{quote(anchor, safe='')}" if anchor else f"bf:///{q_rest}"
        sib = config.resolve_brain(store, cfg, host, declared_only=declared_brains_only)
        return read_target(sib, sub, declared_brains_only=declared_brains_only)

    def render_record(hit_path: str, hit_rec: dict[str, Any], def_ref: str, def_kind: str = "record") -> dict[str, Any]:
        ref_s = str(hit_rec.get("ref", def_ref))
        return {"brain": cfg.name, "kind": def_kind, "ref": ref_s, "path": hit_path, "record": hit_rec}

    if kind == "url":
        if hit := find_by_alias(store, to) or (find_by_alias(store, to[4:]) if to[:4] == "urn:" else None):
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
        if (hit := (cand, _decode_obj(store, cand)) if store.exists(cand) else find_by_alias(store, to)) is None:
            raise Error(f"record {to} not found")
        return render_record(hit[0], hit[1], to)
    if not store.exists(to):
        raise Error(f"note {to} not found")
    raw_bytes = store.read(to, MAX_NOTE)
    text = raw_bytes.decode("utf-8").removeprefix("\ufeff")
    if markdown.okf_role(to) is not None:
        compiled, _ = engine.load_or_compile_note(store, to, raw_bytes, brain_name=cfg.name)
        if anchor:
            return {"brain": cfg.name, "kind": "section", "ref": to, **markdown.extract_section(text, anchor, compiled)}
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
    cfg, val = config.load(store), load_validator(store, "schemas/evals.json", EVALS_SCHEMA)
    problems, results, passed, failed = [], [], 0, 0
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
                if eng == "rg":
                    items = search_rg(store, q_str, scope=sc, limit=200)["items"]
                elif eng == "jq":
                    items = query_jq(store, q_str, scope=sc, limit=500)["items"]
                elif eng == "read":
                    items = [read_target(store, q_str)]
                else:
                    raise Error(f"{path}: unsupported eval engine {eng!r}")
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
