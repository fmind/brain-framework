"""Brain lifecycle operations: init, build, validate, and review (pure JSONL, zero SQLite)."""

from __future__ import annotations

import json
import re
import subprocess
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from bf import config, markdown, models, proc, records, schemas
from bf.markdown import authored_notes
from bf.models import DEFAULT_NOTES, FORMAT, MAX_TOTAL_FILES, Error, InputError, encode, now_utc
from bf.storage import Store, expand, private_dir, writer

_WARN_FORGOTTEN = "snapshot_or_forgotten"


def _starter_bf_yaml(name: str, description: str) -> bytes:
    header = "# yaml-language-server: $schema=https://fmind.github.io/brain-framework/bf.schema.json\n"
    name_json = json.dumps(name, ensure_ascii=False)
    desc_json = json.dumps(description, ensure_ascii=False)
    return f"{header}version: {FORMAT}\nname: {name_json}\ndescription: {desc_json}\nloops: {{}}\n".encode()


def init_brain(target: Path, *, name: str = "", description: str = "") -> dict[str, Any]:
    """Initialize a new brain directory with 3x3 conventions, starter schemas, welcome note, and smoke eval."""
    exp = expand(target)
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", exp.name.lower()).strip("-"))
    default_name = (f"brain-{slug}" if slug and not slug[0].isalpha() else (slug or "brain"))[:64].rstrip("-")
    brain_name = name or default_name
    if not models.NAME_RE.fullmatch(brain_name):
        raise InputError("brain name must start with a lowercase letter and use lowercase letters, digits, hyphens")
    try:
        stripped = description.strip()
        desc = models.clean_text(stripped, "description") if stripped else "Local-first knowledge brain."
        if len(desc) > 1024:
            raise Error("description exceeds 1024 characters")
    except Error as err:
        raise InputError(str(err)) from err
    if exp.is_symlink() or (exp.exists() and not exp.is_dir()):
        raise Error(f"{target}: destination exists and is not a directory")
    if exp.is_dir() and any(exp.iterdir()):
        raise Error(f"{target}: directory is not empty; pass a new or empty folder")
    exp.mkdir(parents=True, mode=0o755, exist_ok=True)
    store = Store(exp)
    starter = resources.files("bf").joinpath("starter")
    for rel in (".gitignore", "AGENTS.md", "concepts/welcome.md", "evals/smoke.py"):
        src_name = "gitignore" if rel == ".gitignore" else rel
        store.write(rel, starter.joinpath(src_name).read_bytes(), durable=False)
    for folder in DEFAULT_NOTES:
        store.write(f"schemas/{folder}.json", encode(schemas.starter_note_schema(folder)), durable=False)
    store.write("schemas/usages.json", encode(schemas.starter_usages_schema()), durable=False)
    store.write("bf.yaml", _starter_bf_yaml(brain_name, desc), durable=False)
    for folder in ("projects", "actions", "sensors", "routines", "agents", "memories", "usages"):
        (store.root / folder).mkdir(parents=True, exist_ok=True, mode=0o755)
    private_dir(store, "logs")
    store.sync_dirs(["", "concepts", "evals", "schemas"])
    build(store)
    return {"ok": True, "brain": brain_name, "path": str(store.root)}


def _check_rec(obj: dict[str, Any], validators: list[Any], label: str, out: list[str]) -> None:
    for validator in validators:
        if validator is not None:
            out.extend(schemas.validate_instance(validator, obj, label))
    if isinstance(sa := obj.get("stale_after"), str) and models.DURATION_RE.fullmatch(sa):
        try:
            models.parse_duration_seconds(sa, f"{label}: stale_after")
        except Error as error:
            out.append(str(error))


def build(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """Compile authored Markdown notes into `memories/<dir>.jsonl`, preserving prior output on partial failure."""
    with writer(store):
        cfg = config.load(store)
        problems: list[str] = []
        base_val = schemas.base_record_validator()
        local_rec_val, local_rec_bad = None, False
        try:
            local_rec_val = schemas.load_validator(store, "schemas/record.json")
        except Error as err:
            local_rec_bad = True
            problems.append(str(err))
        dir_vals: dict[str, Any] = {}
        for folder in cfg.notes:
            try:
                dir_vals[folder] = schemas.require_validator(store, folder)
            except Error as err:
                dir_vals[folder] = None
                problems.append(str(err))

        existing_by_dir: dict[str, dict[str, bytes]] = {folder: {} for folder in cfg.notes}
        for folder in cfg.notes:
            with suppress(FileNotFoundError, Error, OSError):
                for raw_ln, obj in records.iter_jsonl(store, records.memory_file(folder)):
                    if isinstance(ref := obj.get("ref"), str) and ref.startswith(f"{folder}/"):
                        existing_by_dir[folder][ref] = raw_ln

        desired_by_dir: dict[str, dict[str, bytes]] = {folder: {} for folder in cfg.notes}
        scan_failed_dirs: set[str] = set()
        note_count = written = removed = 0

        for folder in cfg.notes:
            folder_skips: list[str] = []
            folder_paths = authored_notes(store, (folder,), skipped=folder_skips)
            if folder_skips:
                problems.extend(folder_skips)
                scan_failed_dirs.add(folder)
            if local_rec_bad or dir_vals.get(folder) is None:
                continue
            vals = [base_val, local_rec_val, dir_vals[folder]]
            for path in folder_paths:
                try:
                    raw = store.read(path, models.MAX_NOTE)
                    compiled = markdown.compile_note(path, raw, brain_name=cfg.name, note_dirs=cfg.notes)
                    note_errs: list[str] = []
                    _check_rec(compiled, vals, path, note_errs)
                    if note_errs:
                        problems.extend(note_errs)
                        if (prev := existing_by_dir[folder].get(path)) is not None:
                            desired_by_dir[folder][path] = prev
                        continue
                    line_bytes = encode(compiled).rstrip(b"\n")
                    desired_by_dir[folder][path] = line_bytes
                    note_count += 1
                    if folder not in scan_failed_dirs and existing_by_dir[folder].get(path) != line_bytes:
                        written += 1
                except (Error, OSError) as err:
                    problems.append(str(err) if isinstance(err, Error) else f"{path}: unreadable file")
                    if (prev := existing_by_dir[folder].get(path)) is not None:
                        desired_by_dir[folder][path] = prev

        touched = False
        for folder in cfg.notes:
            if local_rec_bad or dir_vals.get(folder) is None or folder in scan_failed_dirs:
                continue
            desired = desired_by_dir[folder]
            existing = existing_by_dir[folder]
            removed += sum(1 for ref in existing if ref not in desired)
            mem_file = records.memory_file(folder)
            if desired:
                payload = b"\n".join(desired[ref] for ref in sorted(desired)) + b"\n"
                cur_bytes = store.read(mem_file) if store.exists(mem_file) else b""
                if payload != cur_bytes:
                    store.write(mem_file, payload, durable=False)
                    touched = True
            elif store.exists(mem_file):
                store.delete(mem_file, durable=False)
                touched = True

        if touched and store.is_dir("memories"):
            store.sync_dirs(["memories"])
        if written or removed:
            ev = {"notes": note_count, "written": written, "removed": removed}
            records.append_event(store, "notes.built", "bf", ev, locked=True, now=now)

    return {
        "ok": not problems,
        "brain": cfg.name,
        "notes": note_count,
        "written": written,
        "removed": removed,
        "output": f"memories/{{{','.join(cfg.notes)}}}.jsonl",
        "problems": problems,
    }


def _load_source_refs(store: Store, source: str, cache: dict[str, set[str]]) -> set[str]:
    if source not in cache:
        refs: set[str] = set()
        for target in models.RECORD_DIRS:
            with suppress(FileNotFoundError, Error, OSError):
                for _, obj in records.iter_jsonl(store, records.record_file(source, target)):
                    if isinstance(r := obj.get("ref"), str) and r:
                        refs.add(r)
        cache[source] = refs
    return cache[source]


def _check_jq_filter(flt: str, label: str) -> str:
    try:
        jq_bin = proc.resolve_tool("jq")
        wrapped = f"{proc.JQ_PRELUDE}\ntry ([\n{flt}\n] | length > 0) catch false"
        cp = subprocess.run(  # noqa: S603
            [jq_bin, "-n", "-L", "/dev/null", "--", wrapped],
            capture_output=True,
            cwd="/",
            env=proc.safe_env(jq_bin),
            timeout=5,
            check=False,
        )
    except (Error, subprocess.TimeoutExpired) as err:
        return f"{label}: {err}"
    if cp.returncode != 0:
        err_t = cp.stderr.decode("utf-8", "replace").strip()
        return f"{label}: invalid jq event filter ({err_t.splitlines()[-1] if err_t else 'jq exited nonzero'})"
    return ""


def validate(store: Store, *, limit: int = 50, all_problems: bool = False, strict: bool = False) -> dict[str, Any]:
    """Strictly read-only config, schemas, JSONL records (`memories/` & `usages/`), notes, loops, and link checks."""
    from bf import loops as lp_m

    if not all_problems and limit < 1:
        raise InputError("limit must be at least 1")
    errors: list[str] = []
    warnings: list[str] = []
    by_kind: dict[str, int] = {}
    seen_msgs: set[str] = set()

    def add(bucket: list[str], kind: str, *msgs: str) -> None:
        for msg in msgs:
            if msg not in seen_msgs:
                seen_msgs.add(msg)
                bucket.append(msg)
                by_kind[kind] = by_kind.get(kind, 0) + 1

    try:
        cfg = config.load(store)
    except Error as err:
        sm = {"errors": 1, "warnings": 0, "truncated": False, "by_kind": {"config": 1}}
        return {
            "ok": False,
            "brain": store.root.name,
            "notes": 0,
            "records": 0,
            "usages": 0,
            "errors_count": 1,
            "warnings_count": 0,
            "summary": sm,
            "problems": [str(err)],
        }
    try:
        config.load_dotenv(store)
    except Error as err:
        add(errors, "config", str(err))

    validators: dict[str, Any] = {}
    schema_skips: list[str] = []
    for sp in store.files("schemas", skipped=schema_skips):
        if len(sp.split("/")) != 2 or not sp.endswith(".json"):
            add(errors, "schema", f"{sp}: schemas/ holds only flat <source>.json files")
        else:
            stem = sp[8:-5]
            try:
                validators[stem] = schemas.load_validator(store, sp)
            except Error as err:
                validators[stem] = None
                add(errors, "schema", str(err))
    add(errors, "schema", *schema_skips)
    base_rec_val = schemas.base_record_validator()
    local_rec_val = validators.get("record")
    for folder in cfg.notes:
        if folder not in validators:
            add(errors, "schema", f"schemas/{folder}.json: missing required schema for note folder {folder!r}")

    note_skips: list[str] = []
    authored_list = authored_notes(store, cfg.notes, skipped=note_skips)
    add(errors, "note", *note_skips)
    authored_set = set(authored_list)

    notes_set, reserved = frozenset(cfg.notes), cfg.reserved_sources
    forgotten_map: dict[str, set[str]] = {}
    built_notes: dict[str, dict[str, dict[str, Any]]] = {folder: {} for folder in cfg.notes}
    source_refs: dict[str, set[str]] = {}
    rec_count = usage_count = total_scanned = 0

    for bucket in models.RECORD_DIRS:
        b_skips: list[str] = []
        b_dirs, b_files = store.subdirs(bucket, skipped=b_skips)
        for sub in b_dirs:
            add(
                errors,
                bucket[:-1] if bucket == "usages" else "memory",
                f"{bucket}/{sub}: {bucket}/ holds flat <source>.jsonl files",
            )
        for rec_path in b_files:
            leaf = rec_path.removeprefix(f"{bucket}/")
            if leaf.endswith(".forgotten"):
                src = leaf.removesuffix(".forgotten")
                if not models.NAME_RE.fullmatch(src) or src in reserved:
                    add(errors, "memory", f"{rec_path}: invalid or reserved source {src!r}")
                    continue
                try:
                    raw_fg = store.read(rec_path, models.MAX_NOTE).decode("utf-8").splitlines()
                    forgotten_map.setdefault(src, set()).update(
                        f"{src}:{line}" for ln in raw_fg if (line := ln.strip()) and not line.startswith("#")
                    )
                except (Error, UnicodeDecodeError) as err:
                    add(
                        errors, "memory", str(err) if isinstance(err, Error) else f"{rec_path}: file is not valid UTF-8"
                    )
                continue
            if not leaf.endswith(".jsonl"):
                add(errors, "memory", f"{rec_path}: {bucket}/ holds flat <source>.jsonl files")
                continue
            src = leaf.removesuffix(".jsonl")
            is_note_dir = bucket == "memories" and src in notes_set
            if not is_note_dir and (not models.NAME_RE.fullmatch(src) or src in reserved):
                add(errors, "memory", f"{rec_path}: invalid or reserved source {src!r}")
                continue
            if is_note_dir:
                try:
                    for _, obj in records.iter_jsonl(store, rec_path, check_duplicates=True):
                        ref_s = str(obj.get("ref") or "")
                        if not ref_s or not ref_s.startswith(f"{src}/") or ref_s not in authored_set:
                            add(errors, "note", f"{rec_path}: orphaned compiled note {ref_s!r}; run bf build")
                        elif ref_s in built_notes[src]:
                            add(errors, "note", f"{rec_path}: duplicate compiled note {ref_s!r}; run bf build")
                        else:
                            built_notes[src][ref_s] = obj
                except Error as err:
                    add(errors, "note", str(err))
                continue

            if bucket == "usages":
                if "usages" not in validators:
                    add(errors, "schema", "schemas/usages.json: missing required schema for usages/")
                rec_vals = [base_rec_val, local_rec_val, validators.get("usages"), validators.get(src)]
                exp_kind, err_cat = "usage", "usage"
            else:
                if src not in validators:
                    add(errors, "schema", f"schemas/{src}.json: missing required schema for source {src!r}")
                rec_vals = [base_rec_val, local_rec_val, validators.get(src)]
                exp_kind, err_cat = "memory", "memory"

            seen_ids: set[str] = set()
            refs_for_src = source_refs.setdefault(src, set())
            try:
                for idx, (_, rec_obj) in enumerate(records.iter_jsonl(store, rec_path, check_duplicates=True), 1):
                    if (total_scanned := total_scanned + 1) > MAX_TOTAL_FILES:
                        raise Error(f"{bucket} exceeds the {MAX_TOTAL_FILES:,}-record total scan limit")
                    if bucket == "usages":
                        usage_count += 1
                    else:
                        rec_count += 1
                    rec_id_str = rid if isinstance(rid := rec_obj.get("id"), str) else ""
                    if rec_id_str:
                        refs_for_src.add(f"{src}:{rec_id_str}")
                        if rec_id_str in seen_ids:
                            add(errors, err_cat, f"{rec_path}:{idx}: duplicate record id {rec_id_str!r} in {src}")
                        seen_ids.add(rec_id_str)
                    rec_errs: list[str] = []
                    label = f"{rec_path}:{idx}"
                    _check_rec(rec_obj, rec_vals, label, rec_errs)
                    checks = (("source", src), ("kind", exp_kind))
                    if rec_id_str:
                        checks = (*checks, ("ref", f"{src}:{rec_id_str}"))
                    for key, exp in checks:
                        if rec_obj.get(key) != exp:
                            rec_errs.append(f"{label}: {key} must equal {exp!r}")
                    if isinstance(lks := rec_obj.get("links"), list):
                        rec_errs.extend(
                            f"{label}: {prob}"
                            for lnk in lks
                            if isinstance(lnk, dict)
                            and (prob := markdown.record_link_problem(lnk, note_dirs=cfg.notes, reserved=reserved))
                        )
                    if rec_errs:
                        add(errors, err_cat, *rec_errs)
            except Error as err:
                if "total scan limit" in str(err):
                    raise
                add(errors, err_cat, str(err))
        add(errors, "memory", *b_skips)

    for lp_name, loop in cfg.loops.items():
        if not loop.enabled:
            continue
        try:
            _, ing_src, ing_tgt, _, _ = lp_m.resolve_loop(store, loop, reserved)
        except Error as err:
            add(errors, "loop", str(err))
        else:
            if ing_src:
                if ing_tgt == "usages" and "usages" not in validators:
                    add(errors, "schema", f"loops.{lp_name}: missing required schemas/usages.json")
                elif ing_tgt == "memories" and ing_src not in validators:
                    add(errors, "schema", f"loops.{lp_name}: missing required schemas/{ing_src}.json")
        for ev_rule in loop.events:
            if ev_rule.filter and (jq_err := _check_jq_filter(ev_rule.filter, f"loops.{lp_name}")):
                add(errors, "loop", jq_err)

    anchors: dict[tuple[str, str], set[str]] = {}
    note_links: dict[str, list[dict[str, Any]]] = {}
    for ref in authored_list:
        src = ref.split("/")[0]
        try:
            item = markdown.compile_note(
                ref, store.read(ref, models.MAX_NOTE), brain_name=cfg.name, note_dirs=cfg.notes
            )
        except (Error, OSError) as err:
            add(errors, "note", str(err) if isinstance(err, Error) else f"{ref}: unreadable file")
            continue
        anchors[("", ref)] = markdown.compiled_anchors(item)
        note_links[ref] = list(item.get("links") or ())
        note_errs: list[str] = []
        _check_rec(item, [base_rec_val, local_rec_val, validators.get(src)], ref, note_errs)
        built_obj = built_notes[src].get(ref)
        if built_obj is None:
            note_errs.append(f"{records.memory_file(src)}: missing compiled note for {ref}; run bf build")
        elif built_obj != item:
            note_errs.append(f"{records.memory_file(src)}: out of sync with {ref}; run bf build")
        if note_errs:
            add(errors, "note", *note_errs)

    exists_cache: dict[str, bool] = dict.fromkeys(authored_list, True)
    siblings: dict[str, Store | None] = {}
    sib_refs: dict[tuple[str, str], set[str]] = {}
    snap_srcs = {lp.ingest for lp in cfg.loops.values() if lp.ingest and lp.mode == "snapshot"}
    no_frag = "record links do not support #anchor fragments"

    def resolve_sib(alias: str, origin: str) -> Store | None:
        if alias not in siblings:
            siblings[alias] = None
            try:
                siblings[alias] = config.resolve_brain(store, cfg, alias)
            except Error as err:
                add(errors, "link", f"{origin}: {err}")
        return siblings[alias]

    def check_anchor(target: Store, brain_key: str, rel: str, anc: str, origin: str) -> None:
        if rel.endswith(".md"):
            key = (brain_key, rel)
            if key not in anchors:
                try:
                    anchors[key] = markdown.anchor_ids(rel, target.read(rel, models.MAX_NOTE))
                except Error as err:
                    add(errors, "link", f"{origin}: {err}")
                    return
            if anc not in anchors[key]:
                tgt = f"cross-brain anchor bf://{brain_key}/{rel}#{anc}" if brain_key else f"anchor #{anc} in {rel}"
                add(errors, "link", f"{origin}: missing {tgt}")

    for alias in cfg.brains:
        resolve_sib(alias, f"bf.yaml brains.{alias}")
    for ref, links in sorted(note_links.items()):
        for link in (lk for lk in links if lk["kind"] != "url"):
            kind, to, anc = link["kind"], str(link["to"]), str(link.get("anchor") or "")
            origin = f"{ref} (#{sec})" if (sec := str(link.get("section") or "")) else ref
            if kind == "brain":
                sib_name, _, rest = to.removeprefix("bf://").partition("/")
                if (sib := resolve_sib(sib_name, origin)) is not None:
                    sib_cfg = config.load(sib)
                    if (parsed_r := markdown.parse_record_ref(rest, sib_cfg.reserved_sources)) is not None:
                        if anc:
                            add(errors, "link", f"{origin}: {no_frag}")
                        else:
                            s_src = parsed_r[0]
                            s_key = (sib_name, s_src)
                            if s_key not in sib_refs:
                                tmp: dict[str, set[str]] = {}
                                sib_refs[s_key] = _load_source_refs(sib, s_src, tmp)
                            if rest not in sib_refs[s_key]:
                                add(errors, "link", f"{origin}: broken cross-brain link bf://{sib_name}/{rest}")
                    elif not sib.exists(rest):
                        add(errors, "link", f"{origin}: broken cross-brain link bf://{sib_name}/{rest}")
                    elif anc:
                        check_anchor(sib, sib_name, rest, anc, origin)
            elif kind == "record" and anc:
                add(errors, "link", f"{origin}: {no_frag}")
            elif kind == "record":
                parsed_r = markdown.parse_record_ref(to, reserved)
                if parsed_r is None:
                    add(errors, "link", f"{origin}: invalid record link {to}")
                else:
                    src = parsed_r[0]
                    refs_set = _load_source_refs(store, src, source_refs)
                    if to not in refs_set:
                        if src in snap_srcs or to in forgotten_map.get(src, ()):
                            add(
                                warnings,
                                _WARN_FORGOTTEN,
                                f"{origin}: warning: missing record target {to} (snapshot or forgotten)",
                            )
                        else:
                            add(errors, "link", f"{origin}: broken link to {to}")
            elif not exists_cache.setdefault(to, to in exists_cache or store.exists(to)):
                add(errors, "link", f"{origin}: broken link to {to}")
            elif anc:
                check_anchor(store, "", to, anc, origin)

    msgs, err_n, warn_n = [*errors, *warnings], len(errors), len(warnings)
    summary = {
        "errors": err_n,
        "warnings": warn_n,
        "truncated": not all_problems and len(msgs) > limit,
        "by_kind": by_kind,
    }
    ok_all = err_n == 0 and (not strict or warn_n == 0)
    shown = msgs if all_problems else msgs[:limit]
    return {
        "ok": ok_all,
        "brain": cfg.name,
        "notes": len(authored_list),
        "records": rec_count,
        "usages": usage_count,
        "errors_count": err_n,
        "warnings_count": warn_n,
        "summary": summary,
        "problems": shown,
    }


def _ts_key(raw: str) -> str:
    with suppress(ValueError, OverflowError):
        dt = datetime.fromisoformat(f"{raw}T23:59:59.999999+00:00" if len(raw) == 10 else raw)
        if dt.tzinfo is not None:
            return dt.astimezone(UTC).isoformat(timespec="microseconds")
    return raw


def _note_updated(rec: dict[str, Any]) -> str:
    if upd := str(rec.get("updated") or ""):
        return upd
    if isinstance(srcs := rec.get("sources"), list):
        rets = [
            str(r)
            for s in srcs
            if isinstance(s, dict) and isinstance(r := s.get("retrieved"), str) and len(r) >= 10 and r[:4].isdigit()
        ]
        return max(rets, default="")
    return ""


def review(store: Store, *, now: datetime | None = None, limit: int = 20, all_items: bool = False) -> dict[str, Any]:
    """Read-only staleness (`newer_evidence`, `due`, `future_updated`) and open task summary over JSONL."""
    if not all_items and limit < 1:
        raise InputError("limit must be at least 1")
    cfg = config.load(store)
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    today, now_iso = instant.date(), now_utc(instant)
    problems: list[str] = []

    notes_set = frozenset(cfg.notes)
    prefixes = (f"bf://{cfg.name}/", "bf:///")

    def norm(ref_s: str) -> str:
        bare = ref_s.partition("#")[0]
        return next((bare.removeprefix(p) for p in prefixes if bare.startswith(p)), bare)

    note_entries: list[dict[str, Any]] = []
    note_time: dict[str, str] = {}
    incoming: dict[str, set[str]] = {}

    for folder in cfg.notes:
        mem_path = records.memory_file(folder)
        if not store.exists(mem_path):
            continue
        try:
            for _, obj in records.iter_jsonl(store, mem_path):
                if obj.get("kind") != "note" or not isinstance(obj.get("ref"), str):
                    continue
                ref_val = str(obj["ref"])
                note_entries.append(obj)
                if upd := _note_updated(obj):
                    note_time[ref_val] = _ts_key(upd)
                for lk in obj.get("links") or ():
                    if isinstance(lk, dict) and isinstance(lk.get("to"), str):
                        tgt = norm(str(lk["to"]))
                        if tgt and tgt != ref_val:
                            incoming.setdefault(tgt, set()).add(ref_val)
        except Error as err:
            problems.append(str(err))

    rec_time: dict[str, str] = {}
    for src in records.valid_record_sources(store, "memories", cfg.notes, skipped=problems):
        if src in notes_set:
            continue
        mem_path = records.memory_file(src)
        try:
            for idx, raw_ln in enumerate(store.iter_lines(mem_path, models.MAX_FILE), 1):
                if not (stripped := raw_ln.strip()):
                    continue
                try:
                    obj = models.decode(stripped, f"{mem_path}:{idx}", check_duplicates=False)
                except Error as err:
                    problems.append(str(err))
                    continue
                if not isinstance(obj, dict):
                    continue
                rec_id = str(obj.get("id") or "")
                ref_val = str(obj.get("ref") or (f"{src}:{rec_id}" if rec_id else ""))
                if not ref_val:
                    continue
                if ts := str(obj.get("updated") or obj.get("time") or ""):
                    rec_time[ref_val] = max(rec_time.get(ref_val, ""), _ts_key(ts))
                if isinstance(lks := obj.get("links"), list):
                    for lk in lks:
                        if isinstance(lk, dict) and isinstance(lk.get("to"), str) and (tgt := norm(str(lk["to"]))):
                            incoming.setdefault(tgt, set()).add(ref_val)
        except (Error, OSError) as err:
            problems.append(str(err) if isinstance(err, Error) else f"{mem_path}: unreadable file")

    stale: list[dict[str, Any]] = []
    tasks_open = 0
    today_iso = today.isoformat()
    for entry in sorted(note_entries, key=lambda x: str(x["ref"])):
        ref, status = str(entry["ref"]), str(entry.get("status") or "stable")
        if status == "deprecated":
            continue
        tasks_open += sum(1 for t in (entry.get("tasks") or ()) if isinstance(t, dict) and not t.get("done"))
        reasons: list[str] = []
        newer: list[str] = []
        due = False
        upd = _note_updated(entry)
        if upd and ((len(upd) == 10 and upd > today_iso) or (len(upd) > 10 and _ts_key(upd) > _ts_key(now_iso))):
            reasons.append("future_updated")
        sa = entry.get("stale_after")
        if type(sa) is int:
            due = True
            with suppress(ValueError):
                due = not upd or (today - date.fromisoformat(upd[:10])).days >= sa
        elif isinstance(sa, str) and sa:
            due = True
            is_dur = bool(models.DURATION_RE.fullmatch(sa))
            if ref_date := (upd if is_dur else sa):
                try:
                    dt = datetime.fromisoformat(
                        f"{ref_date}T00:00:00Z" if len(ref_date) == 10 else ref_date
                    ).astimezone(UTC)
                    due = instant >= (dt + timedelta(seconds=models.parse_duration_seconds(sa)) if is_dur else dt)
                except Error, OverflowError, ValueError:
                    due = now_iso >= sa
        if due:
            reasons.append("due")
        if ref in note_time:
            outs = {
                norm(str(lk["to"]))
                for lk in (entry.get("links") or ())
                if isinstance(lk, dict) and lk.get("kind") in {"note", "record"} and lk.get("to") != ref
            }
            linked = outs | incoming.get(ref, set())
            for raw_id in (entry.get("entity"), *(entry["aliases"] if isinstance(entry.get("aliases"), list) else ())):
                if isinstance(raw_id, str) and raw_id:
                    nr = norm(raw_id)
                    bare = nr.removeprefix("urn:")
                    linked.update(*(incoming.get(k, ()) for k in (nr, bare, f"urn:{bare}")))
            linked.discard(ref)
            if newer := [t for t in sorted(linked) if (rec_time.get(t) or note_time.get(t) or "") > note_time[ref]]:
                reasons.append("newer_evidence")
        if reasons:
            newer_d: dict[str, Any] = (
                {"newer": newer[:5], **({"newer_count": len(newer), "truncated": True} if len(newer) > 5 else {})}
                if newer
                else {}
            )
            stale.append({"ref": ref, "title": str(entry.get("title") or ""), "reasons": reasons, **newer_d})

    stale.sort(key=lambda x: (-len(x["reasons"]), -int(x.get("newer_count") or len(x.get("newer", ()))), x["ref"]))
    stale_out = stale if all_items else stale[:limit]
    truncated = (not all_items) and len(stale) > limit
    return {
        "ok": not problems,
        "brain": cfg.name,
        "total_stale": len(stale),
        "total_tasks_open": tasks_open,
        "stale": stale_out,
        "truncated": truncated,
        "problems": list(dict.fromkeys(problems)),
    }
