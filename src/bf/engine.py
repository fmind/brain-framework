"""Brain lifecycle operations: init, build, validate, and review."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from bf import config, markdown, models, query, records, schemas
from bf.models import AUTHORED, Error, InputError, decode, encode, now_utc
from bf.storage import Store, expand, private_dir, writer

_STARTER_FOLDERS = ("projects", "actions", "memories")
_LIST_KEYS = ("links", "headings", "elements", "tasks")
_NOTE_KEYS = frozenset(("id", "ref", "source", "kind", "title", "text", "tags", "attrs", "sha256", *_LIST_KEYS))
_NAMED_BF_URI = re.compile(rb"(?i)bf://[^/]")


def _slugify(value: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-"))


def init_brain(target: Path, *, name: str = "") -> dict[str, Any]:
    """Initialize a new brain directory with flat folders, default JSON Schemas, docs, evals, and `/bf`."""
    expanded = expand(target)
    slug = _slugify(expanded.name)
    default_name = (f"brain-{slug}" if slug and not slug[0].isalpha() else (slug or "brain"))[:64].rstrip("-")
    brain_name = name or default_name
    if not models.NAME_RE.fullmatch(brain_name):
        raise InputError(
            "brain name must start with a lowercase letter and use only lowercase letters, digits, and hyphens"
        )
    if expanded.exists() or expanded.is_symlink():
        if expanded.is_symlink() or not expanded.is_dir():
            raise Error(f"{target}: destination exists and is not a directory")
        if any(expanded.iterdir()):
            raise Error(f"{target}: directory is not empty; pass a new or empty folder")
    else:
        expanded.mkdir(parents=True, mode=0o755)
    store = Store(expanded)
    starter = resources.files("bf").joinpath("starter")
    starter_bf_yaml = (
        "# yaml-language-server: $schema=schemas/bf.schema.json\n"
        f"version: {models.FORMAT}\nname: {brain_name}\n"
        "jobs:\n  build:\n    run: [bf, build]\n    hook: pre-commit\n"
        "  validate:\n    run: [bf, validate]\n    hook: pre-commit\n"
    )
    store.write("bf.yaml", starter_bf_yaml.encode())
    store.write(".gitignore", starter.joinpath("gitignore").read_bytes())
    for rel in ("AGENTS.md", "concepts/welcome.md", "docs/index.md", "evals/smoke.yaml"):
        store.write(rel, starter.joinpath(rel).read_bytes())
    for kind in ("brain", "record", "concepts", "evals"):
        s_name = "bf.schema.json" if kind == "brain" else f"{kind}.json"
        doc_b = (json.dumps(schemas.document(kind), ensure_ascii=False, indent=2) + "\n").encode()
        store.write(f"schemas/{s_name}", doc_b)
    store.write("skills/bf/SKILL.md", resources.files("bf").joinpath("skills/bf/SKILL.md").read_bytes())
    for folder in _STARTER_FOLDERS:
        (store.root / folder).mkdir(exist_ok=True, mode=0o755)
    private_dir(store, "logs")
    (agents_skills := store.root / ".agents" / "skills").mkdir(parents=True, exist_ok=True, mode=0o755)
    if not (link_path := agents_skills / "bf").exists():
        link_path.symlink_to("../../skills/bf")
    build(store)
    return {"brain": brain_name, "path": str(store.root)}


def load_or_compile_note(
    store: Store, path: str, raw: bytes, *, brain_name: str
) -> tuple[dict[str, Any], bytes | None]:
    """Return `(compiled_note, matching_disk_bytes)` reusing up-to-date `memories/*.json` when safe."""
    if not _NAMED_BF_URI.search(raw):
        mem_path = markdown.compiled_note_path(path)
        with suppress(Error, OSError):
            raw_mem = store.read(mem_path)
            c = decode(raw_mem, mem_path, check_duplicates=False)
            ok = (
                isinstance(c, dict)
                and c.keys() >= _NOTE_KEYS
                and c.get("ref") == path
                and c.get("sha256") == models.digest(raw)
            )
            if ok and isinstance(c.get("attrs"), dict) and all(isinstance(c.get(k), list) for k in _LIST_KEYS):
                return dict(c), raw_mem
    return markdown.compile_note(path, raw, brain_name=brain_name), None


def _collect_notes(store: Store, brain_name: str, problems: list[str], **opts: Any) -> dict[str, dict[str, Any]]:
    authored_mem, synced_mem = opts.get("authored_mem"), opts.get("synced_mem")
    use_cache, notes = bool(opts.get("use_cache", True)), {}
    for folder in models.AUTHORED:
        for path in store.files(folder, skipped=problems):
            if markdown.okf_role(path) is None:
                continue
            mem_path = markdown.compiled_note_path(path)
            if authored_mem is not None:
                authored_mem.add(mem_path)
            try:
                raw = store.read(path, models.MAX_NOTE)
                if use_cache:
                    compiled, raw_mem = load_or_compile_note(store, path, raw, brain_name=brain_name)
                    if synced_mem is not None and raw_mem is not None and raw_mem == encode(compiled):
                        synced_mem.add(mem_path)
                else:
                    compiled = markdown.compile_note(path, raw, brain_name=brain_name)
                notes[path] = compiled
            except (Error, OSError) as error:
                problems.append(str(error) if isinstance(error, Error) else f"{path}: unreadable file")
    return notes


def build(store: Store, *, now: datetime | None = None, incremental: bool = False) -> dict[str, Any]:
    """Compile all OKF notes into pure JSON files under `memories/{concepts,projects,actions}/`."""
    cfg = config.load(store)
    problems: list[str] = []
    synced_mem: set[str] = set()
    notes = _collect_notes(store, cfg.name, problems, synced_mem=synced_mem, use_cache=incremental)
    desired_notes = {markdown.compiled_note_path(ref): note for ref, note in notes.items()}
    written = removed = 0
    with writer(store):
        for folder in models.AUTHORED:
            for existing in store.files(f"memories/{folder}"):
                if existing not in desired_notes and existing.endswith(".json"):
                    store.delete(existing)
                    removed += 1
        for mem_path, note in sorted(desired_notes.items()):
            if mem_path in synced_mem:
                continue
            payload = encode(note)
            with suppress(FileNotFoundError):
                if store.read(mem_path) == payload:
                    continue
            store.write(mem_path, payload)
            written += 1
        if written or removed:
            ev_payload = {"notes": len(notes), "written": written, "removed": removed}
            records.append_event(store, "notes.built", "bf", ev_payload, locked=True, now=now)
    res = {"ok": not problems, "brain": cfg.name, "notes": len(notes), "written": written, "removed": removed}
    return {**res, "output": "memories/{concepts,projects,actions}", "problems": problems}


def validate(store: Store, *, declared_brains_only: bool = False) -> dict[str, Any]:
    """Read-only structural, JSON Schema, eval, and graph link validation across the brain."""
    problems: list[str] = []
    try:
        cfg = config.load(store)
    except Error as error:
        return {"ok": False, "brain": store.root.name, "notes": 0, "records": 0, "problems": [str(error)]}
    try:
        config.load_dotenv(store)
    except Error as error:
        problems.append(str(error))

    loaded_schemas: dict[str, Any] = {}
    for schema_path in store.files("schemas", skipped=problems):
        if len(schema_path.split("/")) != 2 or not schema_path.endswith(".json"):
            problems.append(f"{schema_path}: schemas/ holds only flat <source>.json files")
            continue
        try:
            loaded_schemas[schema_path] = schemas.load_validator(store, schema_path)
        except Error as error:
            problems.append(str(error))

    def safe_validator(path: str, fallback: dict[str, Any] | None = None) -> Any:
        if path in loaded_schemas:
            return loaded_schemas[path]
        with suppress(Error):
            return schemas.load_validator(store, path, fallback)
        return None

    record_val = safe_validator("schemas/record.json", schemas.RECORD_SCHEMA)
    evals_val = safe_validator("schemas/evals.json", schemas.EVALS_SCHEMA)
    source_vals: dict[str, Any] = {k: safe_validator(f"schemas/{k}.json") for k in ("projects", "actions")}
    source_vals["concepts"] = safe_validator("schemas/concepts.json", schemas.CONCEPTS_SCHEMA)

    def decode_dict(raw_b: bytes, label: str) -> dict[str, Any] | None:
        try:
            if isinstance(obj := decode(raw_b, label), dict):
                return obj
            problems.append(f"{label}: expected a JSON object")
        except Error as error:
            problems.append(str(error))
        return None

    def check_rec_schemas(obj: dict[str, Any], src: str, label: str) -> None:
        for val in (record_val, source_vals.get(src)):
            if val is not None:
                problems.extend(schemas.validate_instance(val, obj, label))
        if isinstance(sa := obj.get("stale_after"), str) and models.DURATION_RE.fullmatch(sa):
            try:
                models.parse_duration_seconds(sa, f"{label}: stale_after")
            except Error as error:
                problems.append(str(error))

    authored_mem, seen_built, seen_per_source, record_count = set(), set(), {}, 0
    notes = _collect_notes(store, cfg.name, problems, authored_mem=authored_mem, use_cache=False)
    note_by_mem = {markdown.compiled_note_path(ref): (ref, note) for ref, note in notes.items()}
    known_exists: set[str] = set(notes)
    for mem_path, raw_mem in store.walk("memories", suffix=".json", limit=models.MAX_FILE, skipped=problems):
        known_exists.add(mem_path)
        parts = mem_path.split("/")
        if len(parts) >= 3 and parts[1] in models.AUTHORED:
            if not mem_path.endswith(".json"):
                problems.append(f"{mem_path}: compiled note memories must be .json files")
            elif mem_path not in authored_mem:
                problems.append(f"{mem_path}: orphaned compiled note; run bf build")
            elif raw_mem is not None:
                seen_built.add(mem_path)
                if (built_rec := decode_dict(raw_mem, mem_path)) is not None and mem_path in note_by_mem:
                    ref, item = note_by_mem[mem_path]
                    if built_rec != item:
                        problems.append(f"{mem_path}: out of sync with {ref}; run bf build")
            continue
        if len(parts) == 3 and parts[2] == ".forgotten":
            try:
                store.read(mem_path, models.MAX_NOTE).decode("utf-8")
            except (Error, UnicodeDecodeError) as error:
                problems.append(str(error) if isinstance(error, Error) else f"{mem_path}: file is not valid UTF-8")
            continue
        if len(parts) != 3 or not mem_path.endswith(".json") or raw_mem is None:
            problems.append(f"{mem_path}: memories/ holds <source>/<sha256>.json files")
            continue
        source = parts[1]
        if not models.NAME_RE.fullmatch(source) or source in models.RESERVED_SOURCES:
            problems.append(f"{mem_path}: invalid or reserved source directory {source!r}")
            continue
        if source not in source_vals:
            source_vals[source] = safe_validator(f"schemas/{source}.json")
        if (rec := decode_dict(raw_mem, mem_path)) is None:
            continue
        record_count += 1
        check_rec_schemas(rec, source, mem_path)
        if isinstance(rec_id := rec.get("id"), str) and rec_id:
            src_seen = seen_per_source.setdefault(source, set())
            if rec_id in src_seen:
                problems.append(f"{mem_path}: duplicate record id {rec_id!r} in {source}")
            src_seen.add(rec_id)
            checks = (("source", source), ("ref", f"{source}:{rec_id}"), ("kind", "memory"))
            problems.extend(f"{mem_path}: {k} must equal {exp!r}" for k, exp in checks if k in rec and rec[k] != exp)
        if "relations" in rec:
            problems.append(f"{mem_path}: 'relations' was removed; use 'links'")
        lks = rec["links"] if isinstance(rec.get("links"), list) else ()
        problems.extend(
            f"{mem_path}: {p}" for x in lks if isinstance(x, dict) and (p := markdown.record_link_problem(x))
        )

    for ref, item in sorted(notes.items()):
        check_rec_schemas(item, str(item["source"]), ref)
        mem_path = markdown.compiled_note_path(ref)
        if mem_path not in seen_built and not store.exists(mem_path):
            problems.append(f"{mem_path}: missing compiled note for {ref}; run bf build")

    for eval_path in store.files("evals", skipped=problems):
        if not eval_path.endswith((".yaml", ".yml", ".json")):
            problems.append(f"{eval_path}: evals/ holds .yaml or .json files")
        else:
            query.load_eval_doc(store, eval_path, evals_val, problems)

    anchor_cache: dict[tuple[str, str], set[str]] = {("", ref): set(note["attrs"]) for ref, note in notes.items()}
    exists_cache: dict[str, bool] = dict.fromkeys(known_exists, True)
    siblings: dict[str, Store | None] = {}

    def resolve_sib(alias: str, origin: str) -> Store | None:
        if alias not in siblings:
            siblings[alias] = None
            try:
                siblings[alias] = config.resolve_brain(store, cfg, alias, declared_only=declared_brains_only)
            except Error as error:
                problems.append(f"{origin}: {error}")
        return siblings[alias]

    def check_anchor(target: Store, b_key: str, rel: str, anchor: str, origin: str) -> None:
        if rel.endswith(".json"):
            problems.append(f"{origin}: JSON record links do not support #anchor fragments")
            return
        if not rel.endswith(".md"):
            return
        if (key := (b_key, rel)) not in anchor_cache:
            try:
                anchor_cache[key] = markdown.anchor_ids(rel, target.read(rel, models.MAX_NOTE))
            except Error as error:
                problems.append(f"{origin}: {error}")
                return
        if anchor not in anchor_cache[key]:
            msg = (
                f"missing cross-brain anchor bf://{b_key}/{rel}#{anchor}"
                if b_key
                else f"missing anchor #{anchor} in {rel}"
            )
            problems.append(f"{origin}: {msg}")

    for alias in cfg.brains:
        resolve_sib(alias, f"bf.yaml brains.{alias}")

    for ref, note in sorted(notes.items()):
        for link in note["links"]:
            kind, to, anchor, sec = link["kind"], str(link["to"]), str(link["anchor"]), str(link["section"])
            origin = f"{ref} (#{sec})" if sec else ref
            if kind == "url":
                continue
            if kind == "brain":
                sib_name, _, rest = to.removeprefix("bf://").partition("/")
                if (sib := resolve_sib(sib_name, origin)) is None:
                    continue
                if ":" in rest:
                    problems.append(f"{origin}: cross-brain link bf://{sib_name}/{rest} must use a path, not source:id")
                elif not sib.exists(rest):
                    problems.append(f"{origin}: broken cross-brain link bf://{sib_name}/{rest}")
                elif anchor:
                    check_anchor(sib, sib_name, rest, anchor, origin)
                continue
            if to not in exists_cache:
                exists_cache[to] = store.exists(to)
            if not exists_cache[to]:
                problems.append(f"{origin}: broken link to {to}")
            elif anchor:
                if kind == "record":
                    problems.append(f"{origin}: JSON record links do not support #anchor fragments")
                else:
                    check_anchor(store, "", to, anchor, origin)

    return {"ok": not problems, "brain": cfg.name, "notes": len(notes), "records": record_count, "problems": problems}


def _note_updated(note: dict[str, Any]) -> str:
    if isinstance(u := note.get("updated"), str) and u:
        if len(u) > 10:
            with suppress(Error):
                return models.normalize_time(u, "updated")
        return u
    retrieved: list[str] = []
    for s in src if isinstance(src := note.get("sources"), list) else ():
        if isinstance(s, dict) and isinstance(r := s.get("retrieved"), str) and r:
            with suppress(Error):
                norm = models.normalize_date(r, "retrieved") if len(r) == 10 else models.normalize_time(r, "retrieved")
                retrieved.append(norm)
    return max(retrieved, key=lambda v: f"{v}T23:59:59Z" if len(v) == 10 else v, default="")


def review(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """Read-only staleness, `newer_evidence`, open `draft` actions, and open task summary."""
    cfg = config.load(store)
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    today = instant.date()
    today_iso, now_iso = today.isoformat(), now_utc(instant)

    problems: list[str] = []
    notes = _collect_notes(store, cfg.name, problems)
    record_times: dict[str, str] = {}
    mem_to_ref: dict[str, str] = {markdown.compiled_note_path(ref): ref for ref in notes}
    incoming: dict[str, set[str]] = {}

    def norm_tgt(raw_tgt: str) -> str:
        tgt = raw_tgt.partition("#")[0].partition("?")[0]
        return next((tgt.removeprefix(p) for p in (f"bf://{cfg.name}/", "bf:///") if tgt.startswith(p)), tgt)

    def add_in(raw_tgt: str, ref_val: str) -> None:
        if tgt := norm_tgt(raw_tgt):
            incoming.setdefault(tgt, set()).add(ref_val)

    def ts_key(raw: str) -> str:
        with suppress(ValueError, OverflowError):
            dt = datetime.fromisoformat(f"{raw}T23:59:59.999999+00:00" if len(raw) == 10 else raw)
            if dt.tzinfo is not None:
                return dt.astimezone(UTC).isoformat(timespec="microseconds")
        return raw

    for mem_path, raw_mem in store.iter_read_files("memories", ordered=False, skip_dirs=AUTHORED, skipped=problems):
        parts = mem_path.split("/")
        if len(parts) != 3 or not models.NAME_RE.fullmatch(source := parts[1]) or source in models.RESERVED_SOURCES:
            continue
        try:
            if not isinstance(rec := decode(raw_mem, mem_path, check_duplicates=False), dict):
                continue
        except Error as error:
            problems.append(str(error))
            continue
        rec_ref = str(rec.get("ref") or (f"{source}:{rec['id']}" if rec.get("id") else mem_path))
        mem_to_ref[mem_path] = rec_ref
        if raw_ts := str(rec.get("updated") or rec.get("time") or ""):
            record_times[rec_ref] = max(record_times.get(rec_ref, ""), ts_key(raw_ts))
        for entry in rec.get("links") or [] if isinstance(rec.get("links"), list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("to"), str):
                add_in(str(entry["to"]), rec_ref)

    note_times = {}
    for r, n in notes.items():
        if u := _note_updated(n):
            note_times[r] = ts_key(u0 if isinstance(u0 := n.get("updated"), str) and u0 else u)
    for ref, note in notes.items():
        for link in note["links"]:
            if link["to"] != ref:
                add_in(str(link["to"]), ref)

    stale, actions_list, tasks_open = [], [], 0
    for ref, note in sorted(notes.items()):
        if (status := str(note.get("status", "stable"))) == "deprecated":
            continue
        tasks_open += sum(1 for task in note["tasks"] if not task["done"])
        if note["kind"] == "action" and status == "draft":
            ext = {fk: note[fk] for fk in ("updated", "summary") if isinstance(note.get(fk), str) and note[fk]}
            actions_list.append({"ref": ref, "title": note["title"], **ext})

        reasons, newer, upd = [], [], _note_updated(note)
        if upd and ((len(upd) == 10 and upd > today_iso) or (len(upd) > 10 and upd > now_iso)):
            reasons.append("future_updated")

        sa = note.get("stale_after")
        if type(sa) is int:
            try:
                if not upd or (today - date.fromisoformat(upd[:10])).days >= sa:
                    reasons.append("due")
            except ValueError:
                reasons.append("due")
        elif isinstance(sa, str) and sa:
            try:
                is_dur = bool(models.DURATION_RE.fullmatch(sa))
                raw_dt = upd if is_dur else sa
                if not raw_dt:
                    reasons.append("due")
                else:
                    iso = f"{raw_dt}T00:00:00Z" if len(raw_dt) == 10 else raw_dt
                    dt = datetime.fromisoformat(iso).astimezone(UTC)
                    if instant >= (dt + timedelta(seconds=models.parse_duration_seconds(sa)) if is_dur else dt):
                        reasons.append("due")
            except Error, OverflowError, ValueError:
                if now_iso >= sa:
                    reasons.append("due")

        if ref in note_times:
            note_cutoff = note_times[ref]
            out_lks = (str(lk["to"]) for lk in note["links"] if lk["kind"] in {"note", "record"} and lk["to"] != ref)
            in_lks = incoming.get(ref, set()) | incoming.get(markdown.compiled_note_path(ref), set())
            linked_refs = {mem_to_ref.get(t, t) for t in out_lks} | in_lks
            aliases = note.get("aliases")
            for raw_id in (note.get("entity"), *(aliases if isinstance(aliases, list) else ())):
                if isinstance(raw_id, str) and raw_id:
                    bare = norm_tgt(raw_id).removeprefix("urn:")
                    for key in (norm_tgt(raw_id), bare, f"urn:{bare}"):
                        linked_refs |= incoming.get(key, set())
                    src, sep, rid = bare.partition(":")
                    if sep and models.NAME_RE.fullmatch(src) and rid:
                        linked_refs |= incoming.get(records.record_file(src, rid), set())
            linked_refs.discard(ref)
            newer = [t for t in sorted(linked_refs) if (record_times.get(t) or note_times.get(t) or "") > note_cutoff]
            if newer:
                reasons.append("newer_evidence")

        if reasons:
            item_st: dict[str, Any] = {"ref": ref, "title": note["title"], "reasons": reasons}
            stale.append({**item_st, "newer": newer} if newer else item_st)

    actions_list.sort(key=lambda a: note_times.get(a["ref"], ""), reverse=True)
    head = {"ok": not problems, "brain": cfg.name, "stale": stale, "actions": actions_list}
    return {**head, "tasks_open": tasks_open, "problems": problems}
