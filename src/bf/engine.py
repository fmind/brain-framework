"""Brain lifecycle operations: init, action, build, validate, and review."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from bf import config, markdown, models, query, records, schemas
from bf.config import yaml_object
from bf.models import Error, InputError, clean, decode, encode, now_utc
from bf.storage import Store, expand, writer

_STARTER_FOLDERS = ("projects", "actions", "memories", "logs")


def _slugify(value: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-"))


def init_brain(target: Path, *, name: str = "") -> dict[str, Any]:
    """Initialize a new brain directory with flat folders, default JSON Schemas, docs, evals, and `/bf`."""
    expanded = expand(target)
    if expanded.exists():
        if not expanded.is_dir():
            raise Error(f"{target}: destination exists and is not a directory")
        if any(expanded.iterdir()):
            raise Error(f"{target}: directory is not empty; pass a new or empty folder")
    else:
        expanded.mkdir(parents=True, mode=0o755)
    slug = _slugify(expanded.name)
    default_name = (f"brain-{slug}" if slug and not slug[0].isalpha() else (slug or "brain"))[:64].rstrip("-")
    brain_name = name or default_name
    if not models.NAME_RE.fullmatch(brain_name):
        raise InputError(
            "brain name must start with a lowercase letter and use only lowercase letters, digits, and hyphens"
        )
    store = Store(expanded)
    starter = resources.files("bf").joinpath("starter")
    starter_bf_yaml = (
        f"version: {models.FORMAT}\nname: {brain_name}\n"
        "jobs:\n  build:\n    builtin: build\n  validate:\n    builtin: validate\n    hook: pre-commit\n"
        'servers:\n  mcp:\n    run: [bf, mcp, --transport, http, --port, "8765"]\n    enabled: false\n'
    )
    store.write("bf.yaml", starter_bf_yaml.encode())
    store.write(".gitignore", starter.joinpath("gitignore").read_bytes())
    for rel in ("AGENTS.md", "concepts/welcome.md", "docs/index.md", "evals/smoke.yaml"):
        store.write(rel, starter.joinpath(rel).read_bytes())
    for kind in ("record", "concepts", "logs", "evals"):
        doc_bytes = (json.dumps(schemas.document(kind), ensure_ascii=False, indent=2) + "\n").encode()
        store.write(f"schemas/{kind}.json", doc_bytes)
    store.write("skills/bf/SKILL.md", resources.files("bf").joinpath("skills/bf/SKILL.md").read_bytes())
    for folder in _STARTER_FOLDERS:
        (store.root / folder).mkdir(exist_ok=True, mode=0o755)
    agents_skills = store.root / ".agents" / "skills"
    agents_skills.mkdir(parents=True, exist_ok=True, mode=0o755)
    if not (link_path := agents_skills / "bf").exists():
        link_path.symlink_to("../../skills/bf")
    build(store)
    return {"brain": brain_name, "path": str(store.root)}


def action(
    store: Store, topic: str, *, status: str = "draft", summary: str = "", now: datetime | None = None
) -> dict[str, Any]:
    """Create or update an action workspace `actions/YYYY-MM-DD_<slug>/ACTION.md` and compile its JSON memory."""
    cfg = config.load(store)
    cleaned_topic = clean(topic, "action topic")
    if status not in {"draft", "stable", "deprecated"}:
        raise InputError("action status must be draft, stable, or deprecated")
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    today_iso = instant.date().isoformat()
    dated = re.match(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})_(.+)$", cleaned_topic)
    prefix, raw_tail = (dated[1], dated[2]) if dated else (today_iso, cleaned_topic)
    slug_tail = _slugify(raw_tail)[:48].rstrip("-")
    if not slug_tail:
        raise InputError("action topic must contain at least one ASCII letter or digit")
    slug = f"{prefix}_{slug_tail}"
    note_path = f"actions/{slug}/ACTION.md"
    mem_path = markdown.compiled_note_path(note_path)

    with writer(store):
        created = not store.exists(note_path)
        if created:
            sum_line = f"summary: {json.dumps(clean(summary, 'summary'))}\n" if summary.strip() else ""
            content = (
                f"---\ntype: action\nstatus: {status}\nupdated: {today_iso}\n{sum_line}---\n\n"
                f"# {cleaned_topic}\n\n## Plan {{#plan}}\n\n- [ ] Capture inputs, decisions, and verification.\n"
            ).encode()
            store.write(note_path, content)
            for sub in ("inputs", "outputs"):
                (store.root / "actions" / slug / sub).mkdir(parents=True, exist_ok=True, mode=0o755)
        else:
            raw = store.read(note_path, models.MAX_NOTE).decode("utf-8")
            if m := re.match(r"\A---\r?\n([\s\S]*?\r?\n)---(\r?\n|$)", raw):
                fm_txt, n_st = re.subn(r"(?m)^status:[ \t]*\S+[ \t]*$", f"status: {status}", m[1], count=1)
                if not n_st:
                    fm_txt += f"status: {status}\n"
                fm_txt, n_up = re.subn(r"(?m)^updated:[ \t]*\S+[ \t]*$", f"updated: {today_iso}", fm_txt, count=1)
                if not n_up:
                    fm_txt += f"updated: {today_iso}\n"
                raw = f"---\n{fm_txt}---{m[2]}{raw[m.end() :]}"
            store.write(note_path, raw.encode("utf-8"))
        compiled = markdown.compile_note(note_path, store.read(note_path, models.MAX_NOTE), brain_name=cfg.name)
        store.write(mem_path, encode(compiled))
        ev_payload = {"ref": note_path, "created": created, "status": status}
        records.append_event(store, "action.updated", "actions", ev_payload, locked=True, now=instant)
    return {"brain": cfg.name, "ref": note_path, "memory": mem_path, "created": created, "status": status}


def _collect_notes(store: Store, brain_name: str, problems: list[str]) -> dict[str, dict[str, Any]]:
    notes: dict[str, dict[str, Any]] = {}
    for folder in models.AUTHORED:
        for path in store.files(folder, skipped=problems):
            if markdown.okf_role(path) is not None:
                try:
                    notes[path] = markdown.compile_note(path, store.read(path, models.MAX_NOTE), brain_name=brain_name)
                except (Error, OSError) as error:
                    problems.append(str(error) if isinstance(error, Error) else f"{path}: unreadable file")
    return notes


def build(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """Compile all OKF notes into pure JSON files under `memories/{concepts,projects,actions}/`."""
    cfg = config.load(store)
    problems: list[str] = []
    notes = _collect_notes(store, cfg.name, problems)
    desired = {markdown.compiled_note_path(ref): encode(note) for ref, note in notes.items()}
    written = removed = 0
    with writer(store):
        for folder in models.AUTHORED:
            for existing in store.files(f"memories/{folder}"):
                if existing not in desired and existing.endswith(".json"):
                    store.delete(existing)
                    removed += 1
        for mem_path, payload in sorted(desired.items()):
            if not store.exists(mem_path) or store.read(mem_path) != payload:
                store.write(mem_path, payload)
                written += 1
        if written or removed:
            ev_payload = {"notes": len(notes), "written": written, "removed": removed}
            records.append_event(store, "notes.built", "bf", ev_payload, locked=True, now=now)
    res = {"brain": cfg.name, "notes": len(notes), "written": written, "removed": removed}
    return {**res, "output": "memories/{concepts,projects,actions}", "problems": problems}


def _check_cross_brain(
    sibling_store: Store, sibling_name: str, rest: str, anchor: str, origin: str, problems: list[str]
) -> None:
    if not sibling_store.exists(rest):
        problems.append(f"{origin}: broken cross-brain link bf://{sibling_name}/{rest}")
    elif anchor:
        if rest.endswith(".json") or markdown.is_jq_fragment(anchor):
            if bad := query.check_jq_syntax(anchor):
                problems.append(f"{origin}: invalid jq fragment #{anchor}: {bad}")
        elif rest.endswith(".md"):
            try:
                if anchor not in markdown.anchor_ids(rest, sibling_store.read(rest, models.MAX_NOTE)):
                    problems.append(f"{origin}: missing cross-brain anchor bf://{sibling_name}/{rest}#{anchor}")
            except Error as error:
                problems.append(f"{origin}: {error}")


def validate(store: Store) -> dict[str, Any]:
    """Read-only structural, JSON Schema, log, eval, and graph link validation across the brain."""
    problems: list[str] = []
    try:
        cfg = config.load(store)
    except Error as error:
        return {"valid": False, "brain": store.root.name, "notes": 0, "records": 0, "problems": [str(error)]}
    try:
        config.load_dotenv(store)
    except Error as error:
        problems.append(str(error))

    for schema_path in store.files("schemas", skipped=problems):
        if len(schema_path.split("/")) != 2 or not schema_path.endswith(".json"):
            problems.append(f"{schema_path}: schemas/ holds only flat <source>.json files")
            continue
        try:
            schemas.load_validator(store, schema_path)
        except Error as error:
            problems.append(str(error))

    def safe_validator(path: str, fallback: dict[str, Any] | None = None):
        try:
            return schemas.load_validator(store, path, fallback)
        except Error:
            return None

    record_val = safe_validator("schemas/record.json", schemas.RECORD_SCHEMA)
    logs_val = safe_validator("schemas/logs.json", schemas.LOGS_SCHEMA)
    evals_val = safe_validator("schemas/evals.json", schemas.EVALS_SCHEMA)
    concepts_val = safe_validator("schemas/concepts.json", schemas.CONCEPTS_SCHEMA)
    source_vals = {
        "concepts": concepts_val,
        "projects": safe_validator("schemas/projects.json"),
        "actions": safe_validator("schemas/actions.json"),
    }

    def check_stale_after(obj: dict[str, Any], label: str) -> None:
        if isinstance(sa := obj.get("stale_after"), str) and models.DURATION_RE.fullmatch(sa):
            try:
                models.parse_duration_seconds(sa, f"{label}: stale_after")
            except Error as error:
                problems.append(str(error))

    notes = _collect_notes(store, cfg.name, problems)
    for ref, item in sorted(notes.items()):
        if record_val is not None:
            problems.extend(schemas.validate_instance(record_val, item, ref))
        if (src_val := source_vals.get(str(item["source"]))) is not None:
            problems.extend(schemas.validate_instance(src_val, item, ref))
        check_stale_after(item, ref)

    record_count = 0
    seen_per_source: dict[str, set[str]] = {}
    for mem_path in store.files("memories", skipped=problems):
        parts = mem_path.split("/")
        if len(parts) >= 3 and parts[1] in models.AUTHORED:
            if not mem_path.endswith(".json"):
                problems.append(f"{mem_path}: compiled note memories must be .json files")
            continue
        if len(parts) == 3 and parts[2] == ".forgotten":
            continue
        if len(parts) != 3 or not mem_path.endswith(".json"):
            problems.append(f"{mem_path}: memories/ holds <source>/<sha256>.json files")
            continue
        source = parts[1]
        if not models.NAME_RE.fullmatch(source) or source in models.RESERVED_SOURCES:
            problems.append(f"{mem_path}: invalid or reserved source directory {source!r}")
            continue
        source_vals.setdefault(source, safe_validator(f"schemas/{source}.json"))
        try:
            rec = decode(store.read(mem_path), mem_path)
            if not isinstance(rec, dict):
                problems.append(f"{mem_path}: expected a JSON object")
                continue
        except Error as error:
            problems.append(str(error))
            continue
        record_count += 1
        if record_val is not None:
            problems.extend(schemas.validate_instance(record_val, rec, mem_path))
        if (src_val := source_vals[source]) is not None:
            problems.extend(schemas.validate_instance(src_val, rec, mem_path))
        if isinstance(rec_id := rec.get("id"), str) and rec_id:
            src_seen = seen_per_source.setdefault(source, set())
            if rec_id in src_seen:
                problems.append(f"{mem_path}: duplicate record id {rec_id!r} in {source}")
            src_seen.add(rec_id)
            if "source" in rec and rec["source"] != source:
                problems.append(f"{mem_path}: source must equal {source!r}")
            if "ref" in rec and rec["ref"] != f"{source}:{rec_id}":
                problems.append(f"{mem_path}: ref must equal '{source}:{rec_id}'")
        check_stale_after(rec, mem_path)

    for log_path in store.files("logs", skipped=problems):
        if not log_path.endswith(".json"):
            problems.append(f"{log_path}: logs/ holds only .json files")
            continue
        try:
            log_doc = decode(store.read(log_path), log_path)
            if logs_val is not None:
                problems.extend(schemas.validate_instance(logs_val, log_doc, log_path))
        except Error as error:
            problems.append(str(error))

    for eval_path in store.files("evals", skipped=problems):
        if not eval_path.endswith((".yaml", ".yml", ".json")):
            problems.append(f"{eval_path}: evals/ holds .yaml or .json files")
            continue
        try:
            raw_ev = store.read(eval_path, 1 << 20)
            ev_doc = decode(raw_ev, eval_path) if eval_path.endswith(".json") else yaml_object(raw_ev, eval_path)
            if not isinstance(ev_doc, dict):
                problems.append(f"{eval_path}: expected a mapping")
                continue
            models.check_version(ev_doc, eval_path)
            if evals_val is not None:
                problems.extend(schemas.validate_instance(evals_val, ev_doc, eval_path))
        except Error as error:
            problems.append(str(error))

    anchor_cache: dict[str, set[str]] = {ref: set(note["attrs"]) for ref, note in notes.items()}
    siblings: dict[str, Store | None] = {}
    for alias in cfg.brains:
        try:
            siblings[alias] = config.resolve_brain(store, cfg, alias)
        except Error as error:
            problems.append(f"bf.yaml brains.{alias}: {error}")
            siblings[alias] = None

    for ref, note in sorted(notes.items()):
        for link in note["links"]:
            kind, to, anchor, sec = link["kind"], str(link["to"]), str(link["anchor"]), str(link["section"])
            origin = f"{ref} (#{sec})" if sec else ref
            if kind == "url":
                continue
            if kind == "brain":
                sib_name, _, rest = to.removeprefix("bf://").partition("/")
                if sib_name not in siblings:
                    try:
                        siblings[sib_name] = config.resolve_brain(store, cfg, sib_name)
                    except Error as error:
                        problems.append(f"{origin}: {error}")
                        siblings[sib_name] = None
                if (sib_store := siblings.get(sib_name)) is not None:
                    _check_cross_brain(sib_store, sib_name, rest, anchor, origin, problems)
                continue
            if not store.exists(to):
                problems.append(f"{origin}: broken link to {to}")
            elif anchor:
                if to.endswith(".json") or markdown.is_jq_fragment(anchor):
                    if bad := query.check_jq_syntax(anchor):
                        problems.append(f"{origin}: invalid jq fragment #{anchor}: {bad}")
                else:
                    if to not in anchor_cache and to.endswith(".md"):
                        try:
                            anchor_cache[to] = markdown.anchor_ids(to, store.read(to, models.MAX_NOTE))
                        except Error as error:
                            problems.append(f"{origin}: {error}")
                            continue
                    if to in anchor_cache and anchor not in anchor_cache[to]:
                        problems.append(f"{origin}: missing anchor #{anchor} in {to}")

    return {
        "valid": not problems,
        "brain": cfg.name,
        "notes": len(notes),
        "records": record_count,
        "problems": problems,
    }


def _note_updated(note: dict[str, Any]) -> str:
    if isinstance(u := note.get("updated"), str) and u:
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

    for mem_path in store.files("memories", skipped=problems):
        parts = mem_path.split("/")
        if len(parts) != 3 or parts[1] in models.AUTHORED or not mem_path.endswith(".json"):
            continue
        source = parts[1]
        if not models.NAME_RE.fullmatch(source) or source in models.RESERVED_SOURCES:
            continue
        try:
            rec = decode(store.read(mem_path), mem_path)
            if not isinstance(rec, dict):
                continue
        except Error as error:
            problems.append(str(error))
            continue
        rec_ref = str(rec.get("ref") or (f"{source}:{rec['id']}" if rec.get("id") else mem_path))
        mem_to_ref[mem_path] = rec_ref
        if raw_ts := str(rec.get("updated") or rec.get("time") or ""):
            norm_ts = f"{raw_ts}T23:59:59Z" if len(raw_ts) == 10 else raw_ts
            record_times[rec_ref] = max(record_times.get(rec_ref, ""), norm_ts)
            for alias in rec.get("aliases") or [] if isinstance(rec.get("aliases"), list) else []:
                if isinstance(alias, str) and alias:
                    record_times[alias] = max(record_times.get(alias, ""), norm_ts)
        for entry in rec.get("links") or [] if isinstance(rec.get("links"), list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("to"), str):
                add_in(str(entry["to"]), rec_ref)
            elif isinstance(entry, str):
                add_in(entry, rec_ref)
        for tgt, _ in markdown.extract_relations(rec.get("relations"), mem_path, strict=False):
            add_in(tgt, rec_ref)
        if isinstance(proj := rec.get("project"), str) and proj:
            add_in(proj, rec_ref)

    note_times = {r: f"{u}T23:59:59Z" if len(u) == 10 else u for r, n in notes.items() if (u := _note_updated(n))}
    for ref, note in notes.items():
        for link in note["links"]:
            if link["to"] != ref:
                add_in(str(link["to"]), ref)

    stale: list[dict[str, Any]] = []
    actions_list: list[dict[str, Any]] = []
    tasks_open = 0
    for ref, note in sorted(notes.items()):
        if (status := str(note.get("status", "stable"))) == "deprecated":
            continue
        tasks_open += sum(1 for task in note["tasks"] if not task["done"])
        if note["kind"] == "action" and status == "draft":
            entry: dict[str, Any] = {"ref": ref, "title": note["title"]}
            for fk in ("updated", "summary"):
                if isinstance(note.get(fk), str) and note[fk]:
                    entry[fk] = note[fk]
            actions_list.append(entry)

        reasons: list[str] = []
        newer: list[str] = []
        upd = _note_updated(note)
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
                if models.DURATION_RE.fullmatch(sa):
                    dur_s = models.parse_duration_seconds(sa)
                    base_iso = f"{upd}T00:00:00+00:00" if len(upd) == 10 else upd
                    cutoff = (datetime.fromisoformat(base_iso).astimezone(UTC) if base_iso else instant) + timedelta(
                        seconds=dur_s
                    )
                else:
                    cutoff = datetime.fromisoformat(f"{sa}T00:00:00+00:00" if len(sa) == 10 else sa).astimezone(UTC)
                if instant >= cutoff:
                    reasons.append("due")
            except Error, OverflowError, ValueError:
                if now_iso >= sa:
                    reasons.append("due")

        if ref in note_times:
            note_cutoff = note_times[ref]
            out_refs = {
                mem_to_ref.get(str(lk["to"]), str(lk["to"]))
                for lk in note["links"]
                if lk["kind"] in {"note", "record"} and str(lk["to"]) != ref
            }
            linked_refs = out_refs | incoming.get(ref, set())
            aliases = note.get("aliases")
            for raw_id in (note.get("entity"), *(aliases if isinstance(aliases, list) else ())):
                if isinstance(raw_id, str) and raw_id:
                    linked_refs |= incoming.get(norm_tgt(raw_id), set())
            linked_refs.discard(ref)
            newer = [t for t in sorted(linked_refs) if (record_times.get(t) or note_times.get(t) or "") > note_cutoff]
            if newer:
                reasons.append("newer_evidence")

        if reasons:
            stale.append(
                {"ref": ref, "title": note["title"], "reasons": reasons, **({"newer": newer} if newer else {})}
            )

    return {"brain": cfg.name, "stale": stale, "actions": actions_list, "tasks_open": tasks_open, "problems": problems}
