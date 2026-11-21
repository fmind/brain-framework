"""Brain lifecycle operations: init, build, index, validate, review, check, and new."""

from __future__ import annotations

import json
import re
import stat
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from bf import config, markdown, models, records, schemas
from bf.models import AUTHORED, FORMAT, Error, InputError, decode, encode, now_utc
from bf.storage import Store, expand, private_dir, writer

if TYPE_CHECKING:
    from collections.abc import Sequence

_NOTE_REQ = frozenset(("ref", "source", "kind", "title", "text", "sha256"))
_OPT_LISTS = ("tags", "links", "headings", "elements", "tasks")
_NAMED_BF_URI = re.compile(rb"(?i)bf://[^/]")
_IDX_OPT = ("title", "status", "updated", "tags", "aliases", "entity", "stale_after", "summary")
_OMIT_KEYS = (*_OPT_LISTS, "attrs", "sources", *_IDX_OPT[4:])
_VC_KEYS = ("source_schemas", "clean_records", "clean_notes")
_WF = "snapshot_or_forgotten"


def _json_b(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()


def init_brain(target: Path, *, name: str = "") -> dict[str, Any]:
    """Initialize a new brain directory with minimal starter files, `schemas/concepts.json`, and `/bf`."""
    exp = expand(target)
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", exp.name.lower()).strip("-"))
    dname = (f"brain-{slug}" if slug and not slug[0].isalpha() else (slug or "brain"))[:64].rstrip("-")
    if not models.NAME_RE.fullmatch(brain_name := name or dname):
        raise InputError("brain name must start with a lowercase letter and use lowercase letters, digits, hyphens")
    if exp.is_symlink() or (exp.exists() and not exp.is_dir()):
        raise Error(f"{target}: destination exists and is not a directory")
    if exp.is_dir() and any(exp.iterdir()):
        raise Error(f"{target}: directory is not empty; pass a new or empty folder")
    exp.mkdir(parents=True, mode=0o755, exist_ok=True)
    store, pkg = Store(exp), resources.files("bf")
    st = pkg.joinpath("starter")
    bf_y = (
        f"version: {FORMAT}\nname: {brain_name}\nloops:\n"
        "  build:\n    run: [bf, build]\n    hook: pre-commit\n"
        "  validate:\n    run: [bf, validate]\n    hook: pre-commit\n"
    )
    for rel in (".gitignore", "AGENTS.md", "concepts/welcome.md", "evals/smoke.yaml"):
        store.write(rel, st.joinpath("gitignore" if rel == ".gitignore" else rel).read_bytes(), durable=False)
    store.write("bf.yaml", bf_y.encode(), durable=False)
    store.write("schemas/concepts.json", _json_b(schemas.document("concepts")), durable=False)
    store.write("skills/bf/SKILL.md", pkg.joinpath("skills/bf/SKILL.md").read_bytes(), durable=False)
    for folder in ("projects", "actions", "memories", ".agents/skills"):
        (store.root / folder).mkdir(parents=True, exist_ok=True, mode=0o755)
    private_dir(store, "logs")
    (store.root / ".agents" / "skills" / "bf").symlink_to("../../skills/bf")
    store.sync_dirs(["", "concepts", "evals", "schemas", "skills", "skills/bf", ".agents", ".agents/skills"])
    build(store)
    return {"brain": brain_name, "path": str(store.root)}


def load_or_compile_note(
    store: Store, path: str, raw: bytes, *, brain_name: str
) -> tuple[dict[str, Any], bytes | None]:
    """Return `(compiled_note, matching_disk_bytes)` reusing up-to-date `memories/*.json` when safe."""
    if not _NAMED_BF_URI.search(raw):
        with suppress(Error, OSError):
            raw_mem = store.read(mem_p := markdown.compiled_note_path(path))
            c = decode(raw_mem, mem_p, check_duplicates=False)
            if isinstance(c, dict) and c.keys() >= _NOTE_REQ and c.get("ref") == path and "id" not in c:
                ok = c.get("sha256") == models.digest(raw) and ("attrs" not in c or isinstance(c["attrs"], dict))
                ok = ok and all(k not in c or isinstance(c[k], list) for k in _OPT_LISTS)
                ok = ok and not any(c.get(k) in ("", {}, [], None) for k in _OMIT_KEYS if k in c)
                lks = (lk for lk in c.get("links", ()) if isinstance(lk, dict) and lk.get("kind") == "record")
                xs = (x for k in _OPT_LISTS[1:] for x in c.get(k, ()) if isinstance(x, dict))
                ok = ok and all(markdown.is_memory_record_path(str(lk.get("to", ""))) for lk in lks)
                if ok and not any(x.get(f) in ("", {}) for x in xs for f in ("anchor", "section", "meta", "id")):
                    return dict(c), raw_mem
    return markdown.compile_note(path, raw, brain_name=brain_name), None


def _file_sig(store: Store, rel: str) -> list[int] | None:
    with suppress(Error, OSError):
        if stat.S_ISREG((st := store.stat(rel)).st_mode):
            return [st.st_ino, st.st_ctime_ns, st.st_mtime_ns, st.st_size]
    return None


def _load_cache(store: Store) -> dict[str, Any]:
    with suppress(FileNotFoundError):
        bf_c = private_dir(store, ".bf", "cache", create=False)
        with suppress(Error):
            d = decode(bf_c.read("state.json", models.MAX_FILE), ".bf/cache/state.json", check_duplicates=False)
            if isinstance(d, dict) and d.get("version") == FORMAT:
                return dict(d)
    return {"version": FORMAT, "index_stats": {}, "validate": {}}


def _save_cache(
    store: Store, key: str, val: dict[str, Any], *, locked: bool = False, index: bytes | None = None, base: Any = None
) -> None:
    def _write() -> None:
        cur = _load_cache(store)
        if base is not None and (cur.get("index_stats") or {}) != base:
            return
        cur[key] = val
        if index is not None:
            private_dir(store).write("index.jsonl", index, mode=0o600, durable=False)
        private_dir(store, ".bf", "cache").write("state.json", encode(cur), mode=0o600, durable=False)
        store.sync_dirs([".bf", ".bf/cache"])

    if locked:
        _write()
    else:
        with suppress(Error, PermissionError, OSError), writer(store):
            _write()


def _index_entry(mp: str, source: str, rec: dict[str, Any], *, is_note: bool) -> dict[str, Any]:
    rid = str(rec.get("id") or "")
    ref = rec["ref"] if is_note else str(rec.get("ref") or (f"{source}:{rid}" if rid else mp))
    kind, src_v = (rec["kind"], rec["source"]) if is_note else (str(rec.get("kind") or "memory"), source)
    out: dict[str, Any] = {"path": mp, "kind": kind, "ref": ref, "source": src_v}
    out |= {k: val for k in (*_IDX_OPT, "sha256" if is_note else "id") if (val := rec.get(k))}
    if not is_note and not out.get("updated") and (tm := str(rec.get("time") or "")):
        out["updated"] = tm
    if is_note:
        if open_n := sum(1 for t in (rec.get("tasks") or ()) if isinstance(t, dict) and not t.get("done")):
            out["tasks_open"] = open_n
        if not rec.get("updated"):
            ret: list[str] = []
            for s in rec["sources"] if isinstance(rec.get("sources"), list) else ():
                if isinstance(s, dict) and isinstance(r := s.get("retrieved"), str) and r:
                    with suppress(Error):
                        ret.append((models.normalize_date if len(r) == 10 else models.normalize_time)(r, "retrieved"))
            if eu := max(ret, key=lambda v: f"{v}T23:59:59Z" if len(v) == 10 else v, default=""):
                out["evidence_updated"] = eu
    if raw_l := [e for e in (rec.get("links") or ()) if isinstance(e, dict) and isinstance(e.get("to"), str)]:
        out["links"] = [{"to": str(e["to"]), "kind": str(e.get("kind") or "")} for e in raw_l]
    return out


def _sync_index(
    store: Store,
    *,
    compiled_notes: dict[str, dict[str, Any]] | None = None,
    problems: list[str] | None = None,
    locked: bool = False,
    read_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache, existing, index_ok, req_k = _load_cache(store), {}, False, ("path", "kind", "ref", "source")
    with suppress(FileNotFoundError, Error):
        lns = private_dir(store, create=False).read("index.jsonl", models.MAX_FILE).splitlines()
        parsed = [decode(s, ".bf/index.jsonl", check_duplicates=False) for ln in lns if (s := ln.strip())]
        if all(isinstance(o, dict) and all(isinstance(o.get(k), str) for k in req_k) for o in parsed):
            existing, index_ok = {str(o["path"]): o for o in parsed if isinstance(o, dict)}, True
    idx_st, new_ent, new_st, dirty = dict(cache.get("index_stats") or {}), {}, {}, not index_ok
    for mp, _ in store.walk("memories", ordered=False, skipped=problems):
        parts = mp.split("/")
        if len(parts) < 3 or not mp.endswith(".json"):
            continue
        is_auth, sig = (src := parts[1]) in AUTHORED, _file_sig(store, mp)
        if not is_auth and (len(parts) != 3 or not models.NAME_RE.fullmatch(src) or src in models.RESERVED_SOURCES):
            continue
        if is_auth and compiled_notes is not None:
            if (rec := compiled_notes.get(mp)) is None:
                continue
        elif sig is not None and sig == idx_st.get(mp) and mp in existing:
            new_ent[mp], new_st[mp] = existing[mp], sig
            continue
        else:
            try:
                rec = decode(store.read(mp, models.MAX_FILE), mp, check_duplicates=False)
            except Error as err:
                if not is_auth and problems is not None:
                    problems.append(str(err))
                continue
            if not isinstance(rec, dict):
                continue
            if is_auth and (
                rec.get("kind") not in {"project", "concept", "action"}
                or not all(isinstance(rec.get(k), str) for k in ("ref", "source"))
            ):
                continue
        new_ent[mp] = (entry := _index_entry(mp, src, rec, is_note=is_auth))
        if sig is not None:
            new_st[mp] = sig
        dirty = dirty or existing.get(mp) != entry
    sorted_ent = [new_ent[p] for p in sorted(new_ent)]
    if not read_only and (dirty or len(new_ent) != len(existing) or new_st != idx_st):
        cache["index_stats"], idx_b = new_st, b"".join(encode(e) for e in sorted_ent)
        _save_cache(store, "index_stats", new_st, locked=locked, index=idx_b, base=None if locked else idx_st)
    return sorted_ent, cache


def load_index(store: Store, *, read_only: bool = False) -> list[dict[str, Any]]:
    """Return sorted `.bf/index.jsonl` metadata entries after incremental stat synchronization."""
    return _sync_index(store, read_only=read_only)[0]


def build(
    store: Store,
    *,
    now: datetime | None = None,
    incremental: bool = True,
    force: bool = False,
    emit_event: bool = True,
) -> dict[str, Any]:
    """Compile all OKF notes into compact JSON files under `memories/{concepts,projects,actions}/`."""
    cfg, problems, synced, notes = config.load(store), [], set(), {}
    for path in (p for f in AUTHORED for p in store.files(f, skipped=problems) if markdown.okf_role(p) is not None):
        try:
            raw = store.read(path, models.MAX_NOTE)
            if incremental and not force:
                compiled, raw_mem = load_or_compile_note(store, path, raw, brain_name=cfg.name)
                if raw_mem == encode(compiled):
                    synced.add(markdown.compiled_note_path(path))
            else:
                compiled = markdown.compile_note(path, raw, brain_name=cfg.name)
            notes[path] = compiled
        except (Error, OSError) as err:
            problems.append(str(err) if isinstance(err, Error) else f"{path}: unreadable file")
    desired = {markdown.compiled_note_path(ref): note for ref, note in notes.items()}
    to_del = [p for f in AUTHORED for p in store.files(f"memories/{f}") if p not in desired and p.endswith(".json")]
    to_write: list[tuple[str, bytes]] = []
    for mp, note in sorted(desired.items()):
        if mp not in synced and (payload := encode(note)) != (store.read(mp) if store.exists(mp) else b""):
            to_write.append((mp, payload))
    written, removed = len(to_write), len(to_del)
    if to_write or to_del:
        with writer(store):
            for ex in to_del:
                store.delete(ex, durable=False)
            for mp, payload in to_write:
                store.write(mp, payload, durable=False)
            store.sync_dirs({p.rsplit("/", 1)[0] for p in (*to_del, *(p for p, _ in to_write))})
            _sync_index(store, compiled_notes=desired, problems=problems, locked=True)
            if emit_event:
                ev = {"notes": len(notes), "written": written, "removed": removed}
                records.append_event(store, "notes.built", "bf", ev, locked=True, now=now)
    else:
        _sync_index(store, compiled_notes=desired, problems=problems, locked=False)
    out_h = {"ok": not problems, "brain": cfg.name, "notes": len(notes), "written": written, "removed": removed}
    return {**out_h, "output": "memories/{concepts,projects,actions}", "problems": problems}


def sync_notes(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """Incrementally synchronize dirty authored notes and `.bf/index.jsonl` without emitting `notes.built`."""
    return build(store, now=now, incremental=True, emit_event=False)


def _read_obj(store: Store, path: str) -> tuple[dict[str, Any] | None, str]:
    try:
        obj = decode(store.read(path, models.MAX_FILE), path)
    except Error as error:
        return None, str(error)
    return (obj, "") if isinstance(obj, dict) else (None, f"{path}: expected a JSON object")


def _check_rec(obj: dict[str, Any], rec_v: Any, src_v: Any, label: str, out: list[str]) -> None:
    for val in (v for v in (rec_v, src_v) if v is not None):
        out.extend(schemas.validate_instance(val, obj, label))
    if isinstance(sa := obj.get("stale_after"), str) and models.DURATION_RE.fullmatch(sa):
        try:
            models.parse_duration_seconds(sa, f"{label}: stale_after")
        except Error as error:
            out.append(str(error))


def parse_fields(fields: Sequence[str] | str | None) -> tuple[str, ...] | None:
    """Validate and split `--fields` into a tuple of non-empty field names."""
    if fields is None:
        return None
    wanted = tuple(f.strip() for f in (fields.split(",") if isinstance(fields, str) else list(fields)))
    if not wanted or any(not f for f in wanted):
        raise InputError("fields must be nonempty field names")
    return wanted


def validate(
    store: Store,
    *,
    declared_brains_only: bool = False,
    limit: int = 50,
    all_problems: bool = False,
    strict: bool = False,
    force: bool = False,
    read_only: bool = False,
) -> dict[str, Any]:
    """Read-only structural, JSON Schema, eval, and graph link validation with optional digest cache."""
    from bf import query

    if not all_problems and limit < 1:
        raise InputError("limit must be at least 1")
    errors, warnings, by_kind, seen_msgs = [], [], {}, set()

    def add(bucket: list[str], kind: str, *msgs: str) -> None:
        for m in (m for m in msgs if m not in seen_msgs):
            seen_msgs.add(m)
            bucket.append(m)
            by_kind[kind] = by_kind.get(kind, 0) + 1

    try:
        cfg = config.load(store)
    except Error as err:
        sm = {"errors": 1, "warnings": 0, "truncated": False, "by_kind": {"config": 1}}
        head = {"ok": False, "brain": store.root.name, "notes": 0, "records": 0, "errors_count": 1, "warnings_count": 0}
        return {**head, "summary": sm, "problems": [str(err)]}
    try:
        config.load_dotenv(store)
    except Error as err:
        add(errors, "config", str(err))
    s_vals: dict[str, Any] = {}
    for sp in store.files("schemas", skipped=(s_skip := [])):
        if len(sp.split("/")) != 2 or not sp.endswith(".json"):
            add(errors, "schema", f"{sp}: schemas/ holds only flat <source>.json files")
        else:
            try:
                s_vals[sp[8:-5]] = schemas.load_validator(store, sp)
            except Error as err:
                s_vals[sp[8:-5]] = None
                add(errors, "schema", str(err))
    add(errors, "schema", *s_skip)
    for sk_n in (k for k in ("record", "evals", "concepts") if k not in s_vals):
        s_vals[sk_n] = schemas.load_validator(store, f"schemas/{sk_n}.json", schemas.document(sk_n))
    rec_v, ev_v, s_dig = s_vals.get("record"), s_vals.get("evals"), {"record": ""}
    with suppress(Error):
        s_dig = schemas.schemas_digests(store, skipped=[])
    cache = _load_cache(store)
    vc = v if isinstance(v := cache.get("validate"), dict) else {}
    rec_ok = not force and bool(s_dig["record"]) and vc.get("record_schema") == s_dig["record"]
    c_src, c_recs, c_notes = (vc[k] if isinstance(vc.get(k), dict) else {} for k in _VC_KEYS)
    notes_ok, f_skip, authored_refs = rec_ok and vc.get("brain") == cfg.name, [], {}
    for p in (p for f in AUTHORED for p in store.files(f, skipped=f_skip) if markdown.okf_role(p) is not None):
        authored_refs[markdown.compiled_note_path(p)] = p
    add(errors, "note", *f_skip)
    seen_built, seen_per_src, fg_mem, rec_count = set(), {}, {}, 0
    known_exists, new_recs, new_notes, note_links, anchors = set(authored_refs.values()), {}, {}, {}, {}
    for mp, _ in store.walk("memories", ordered=False, skipped=(m_skip := [])):
        known_exists.add(mp)
        parts = mp.split("/")
        if len(parts) >= 3 and parts[1] in AUTHORED:
            if not mp.endswith(".json"):
                add(errors, "note", f"{mp}: compiled note memories must be .json files")
            elif mp not in authored_refs:
                add(errors, "note", f"{mp}: orphaned compiled note; run bf build")
            else:
                seen_built.add(mp)
        elif len(parts) == 3 and parts[2] == ".forgotten":
            try:
                lns, rf = store.read(mp, models.MAX_NOTE).decode("utf-8").splitlines(), records.record_file
                fg_mem[parts[1]] = {rf(parts[1], s) for ln in lns if (s := ln.strip()) and not s.startswith("#")}
            except (Error, UnicodeDecodeError) as err:
                add(errors, "memory", str(err) if isinstance(err, Error) else f"{mp}: file is not valid UTF-8")
        elif len(parts) != 3 or not mp.endswith(".json"):
            add(errors, "memory", f"{mp}: memories/ holds <source>/<sha256>.json files")
        elif not models.NAME_RE.fullmatch(src := parts[1]) or src in models.RESERVED_SOURCES:
            add(errors, "memory", f"{mp}: invalid or reserved source directory {src!r}")
        else:
            sig, c_rec = _file_sig(store, mp), c_recs.get(mp)
            hit = rec_ok and c_src.get(src, "") == s_dig.get(src, "") and isinstance(c_rec, list) and len(c_rec) == 5
            if hit and sig is not None and sig == c_rec[:4]:
                rec_count, rec_id_str, new_recs[mp] = rec_count + 1, str(c_rec[4]), c_rec
            elif (ro := _read_obj(store, mp))[0] is None:
                add(errors, "memory", ro[1])
                continue
            else:
                rec_obj, rec_count, rec_errs = ro[0], rec_count + 1, []
                _check_rec(rec_obj, rec_v, s_vals.get(src), mp, rec_errs)
                if rec_id_str := (rid if isinstance(rid := rec_obj.get("id"), str) else ""):
                    cks = (("source", src), ("ref", f"{src}:{rec_id_str}"), ("kind", "memory"))
                    rec_errs.extend(f"{mp}: {k} must equal {e!r}" for k, e in cks if k in rec_obj and rec_obj[k] != e)
                if "relations" in rec_obj:
                    rec_errs.append(f"{mp}: 'relations' was removed; use 'links'")
                if isinstance(lks := rec_obj.get("links"), list):
                    lk_p = markdown.record_link_problem
                    rec_errs.extend(f"{mp}: {p}" for x in lks if isinstance(x, dict) and (p := lk_p(x)))
                if rec_errs:
                    add(errors, "memory", *rec_errs)
                elif sig is not None:
                    new_recs[mp] = [*sig, rec_id_str]
            if rec_id_str:
                if rec_id_str in (s_seen := seen_per_src.setdefault(src, set())):
                    add(errors, "memory", f"{mp}: duplicate record id {rec_id_str!r} in {src}")
                s_seen.add(rec_id_str)
    add(errors, "memory", *m_skip)
    for mp, ref in sorted(authored_refs.items()):
        src, md_sig, cn = ref.split("/")[0], _file_sig(store, ref), c_notes.get(ref)
        mem_sig = _file_sig(store, mp) if mp in seen_built else None
        hit = notes_ok and c_src.get(src, "") == s_dig.get(src, "") and isinstance(cn, list) and len(cn) == 4
        if hit and md_sig == cn[0] and mem_sig is not None and mem_sig == cn[1]:
            anchors[("", ref)], note_links[ref], new_notes[ref] = set(cn[2]), list(cn[3]), cn
            continue
        try:
            item = markdown.compile_note(ref, store.read(ref, models.MAX_NOTE), brain_name=cfg.name)
        except (Error, OSError) as err:
            add(errors, "note", str(err) if isinstance(err, Error) else f"{ref}: unreadable file")
            continue
        anchors[("", ref)], note_links[ref], note_errs = set(item.get("attrs") or ()), list(item.get("links") or ()), []
        _check_rec(item, rec_v, s_vals.get(src), ref, note_errs)
        if mp not in seen_built and not store.exists(mp):
            note_errs.append(f"{mp}: missing compiled note for {ref}; run bf build")
        elif (br := _read_obj(store, mp))[0] is None or br[0] != item:
            note_errs.append(br[1] or f"{mp}: out of sync with {ref}; run bf build")
        if note_errs:
            add(errors, "note", *note_errs)
        elif md_sig is not None and mem_sig is not None:
            new_notes[ref] = [md_sig, mem_sig, sorted(anchors[("", ref)]), note_links[ref]]
    for ev_p in store.files("evals", skipped=(e_skip := [])):
        if not ev_p.endswith((".yaml", ".yml", ".json")):
            add(errors, "eval", f"{ev_p}: evals/ holds .yaml or .json files")
        else:
            query.load_eval_doc(store, ev_p, ev_v, ev_errs := [])
            add(errors, "eval", *ev_errs)
    add(errors, "eval", *e_skip)
    exists_cache: dict[str, bool] = dict.fromkeys(known_exists, True)
    siblings: dict[str, Store | None] = {}
    snap_srcs = {lp.ingest for lp in cfg.loops.values() if lp.ingest and lp.mode == "snapshot"}
    no_frag = "JSON record links do not support #anchor fragments"

    def resolve_sib(alias: str, origin: str) -> Store | None:
        if alias not in siblings:
            siblings[alias] = None
            try:
                siblings[alias] = config.resolve_brain(store, cfg, alias, declared_only=declared_brains_only)
            except Error as err:
                add(errors, "link", f"{origin}: {err}")
        return siblings[alias]

    def check_anc(target: Store, b_key: str, rel: str, anc: str, origin: str) -> None:
        if rel.endswith(".json"):
            add(errors, "link", f"{origin}: {no_frag}")
        elif rel.endswith(".md"):
            if (key := (b_key, rel)) not in anchors:
                try:
                    anchors[key] = markdown.anchor_ids(rel, target.read(rel, models.MAX_NOTE))
                except Error as err:
                    add(errors, "link", f"{origin}: {err}")
                    return
            if anc not in anchors[key]:
                tgt = f"cross-brain anchor bf://{b_key}/{rel}#{anc}" if b_key else f"anchor #{anc} in {rel}"
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
                    if anc and rest.endswith(".json"):
                        add(errors, "link", f"{origin}: {no_frag}")
                    elif ":" in rest or not sib.exists(rest):
                        add(errors, "link", f"{origin}: broken cross-brain link bf://{sib_name}/{rest}")
                    elif anc:
                        check_anc(sib, sib_name, rest, anc, origin)
            elif kind == "record" and anc:
                add(errors, "link", f"{origin}: {no_frag}")
            elif kind == "record":
                src = to.split("/")[1] if markdown.is_memory_record_path(to) else ""
                if not src:
                    add(errors, "link", f"{origin}: invalid record link {to}")
                elif not exists_cache.setdefault(to, to in exists_cache or store.exists(to)):
                    if src in snap_srcs or to in fg_mem.get(src, ()):
                        add(warnings, _WF, f"{origin}: warning: missing record target {to} (snapshot or forgotten)")
                    else:
                        add(errors, "link", f"{origin}: broken link to {to}")
            elif not exists_cache.setdefault(to, to in exists_cache or store.exists(to)):
                add(errors, "link", f"{origin}: broken link to {to}")
            elif anc:
                check_anc(store, "", to, anc, origin)
    if not read_only:
        src_dig = {k: v for k, v in s_dig.items() if k not in {"bf", "record", "evals", "__all__"}}
        v_head = {"brain": cfg.name, "record_schema": s_dig["record"], "source_schemas": src_dig}
        _save_cache(store, "validate", {**v_head, "clean_records": new_recs, "clean_notes": new_notes})
    msgs, err_n, warn_n = [*errors, *warnings], len(errors), len(warnings)
    sm = {"errors": err_n, "warnings": warn_n, "truncated": not all_problems and len(msgs) > limit, "by_kind": by_kind}
    ok_all, shown = err_n == 0 and (not strict or warn_n == 0), msgs if all_problems else msgs[:limit]
    vh = {"ok": ok_all, "brain": cfg.name, "notes": len(authored_refs), "records": rec_count}
    return {**vh, "errors_count": err_n, "warnings_count": warn_n, "summary": sm, "problems": shown}


def _ts_key(raw: str) -> str:
    with suppress(ValueError, OverflowError):
        if (dt := datetime.fromisoformat(f"{raw}T23:59:59.999999+00:00" if len(raw) == 10 else raw)).tzinfo is not None:
            return dt.astimezone(UTC).isoformat(timespec="microseconds")
    return raw


def review(
    store: Store,
    *,
    now: datetime | None = None,
    limit: int = 20,
    all_items: bool = False,
    fields: Sequence[str] | str | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    """Staleness, `newer_evidence`, open `draft` actions, and open task summary via `.bf/index.jsonl`."""
    if not all_items and limit < 1:
        raise InputError("limit must be at least 1")
    wanted, cfg = parse_fields(fields), config.load(store)
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    today, now_iso = instant.date(), now_utc(instant)
    sync_probs = [] if read_only else sync_notes(store, now=now)["problems"]
    entries, _ = _sync_index(store, problems=(idx_probs := []), read_only=read_only)
    problems = list(dict.fromkeys([*sync_probs, *idx_probs]))
    rec_t, note_t, mem_ref, incoming, note_ent = {}, {}, {}, {}, []
    pfx = (f"bf://{cfg.name}/", "bf:///")

    def norm(r: str) -> str:
        t = r.partition("#")[0].partition("?")[0]
        return next((t.removeprefix(p) for p in pfx if t.startswith(p)), t)

    for e in entries:
        mp, kind, rv = str(e["path"]), str(e["kind"]), str(e["ref"])
        mem_ref[mp] = rv
        if is_n := kind in {"project", "concept", "action"}:
            note_ent.append(e)
            if u := str(e.get("updated") or e.get("evidence_updated") or ""):
                note_t[rv] = _ts_key(u)
        elif ts := str(e.get("updated") or ""):
            rec_t[rv] = max(rec_t.get(rv, ""), _ts_key(ts))
        for lk in e.get("links") or ():
            if (not is_n or lk["to"] != rv) and (tgt := norm(str(lk["to"]))):
                incoming.setdefault(tgt, set()).add(rv)
    stale, actions_list, tasks_open, today_iso = [], [], 0, today.isoformat()
    for e in sorted(note_ent, key=lambda x: str(x["ref"])):
        ref, st = str(e["ref"]), str(e.get("status", "stable"))
        if st == "deprecated":
            continue
        tasks_open += int(e.get("tasks_open") or 0)
        if e["kind"] == "action" and st == "draft":
            ext = {fk: e[fk] for fk in ("updated", "summary") if isinstance(e.get(fk), str) and e[fk]}
            actions_list.append({"ref": ref, "title": str(e.get("title") or ""), **ext})
        reasons, newer, due, upd = [], [], False, str(e.get("updated") or e.get("evidence_updated") or "")
        if upd and ((len(upd) == 10 and upd > today_iso) or (len(upd) > 10 and _ts_key(upd) > _ts_key(now_iso))):
            reasons.append("future_updated")
        if type(sa := e.get("stale_after")) is int:
            due = True
            with suppress(ValueError):
                due = not upd or (today - date.fromisoformat(upd[:10])).days >= sa
        elif isinstance(sa, str) and sa:
            due = True
            if rd := (upd if (is_d := bool(models.DURATION_RE.fullmatch(sa))) else sa):
                try:
                    dt = datetime.fromisoformat(f"{rd}T00:00:00Z" if len(rd) == 10 else rd).astimezone(UTC)
                    due = instant >= (dt + timedelta(seconds=models.parse_duration_seconds(sa)) if is_d else dt)
                except Error, OverflowError, ValueError:
                    due = now_iso >= sa
        if due:
            reasons.append("due")
        if ref in note_t:
            outs = [lk["to"] for lk in e.get("links") or () if lk["kind"] in {"note", "record"} and lk["to"] != ref]
            linked = {mem_ref.get(t, t) for t in outs} | incoming.get(ref, set()) | incoming.get(str(e["path"]), set())
            for raw_id in (e.get("entity"), *(e["aliases"] if isinstance(e.get("aliases"), list) else ())):
                if isinstance(raw_id, str) and raw_id:
                    bare = (nr := norm(raw_id)).removeprefix("urn:")
                    linked.update(*(incoming.get(k, ()) for k in (nr, bare, f"urn:{bare}")))
                    if mf := markdown.record_path(bare):
                        linked |= incoming.get(mf, set())
            linked.discard(ref)
            if newer := [t for t in sorted(linked) if (rec_t.get(t) or note_t.get(t) or "") > note_t[ref]]:
                reasons.append("newer_evidence")
        if reasons:
            n_d = {"newer": newer[:5], **({"newer_count": len(newer), "truncated": True} if len(newer) > 5 else {})}
            stale.append({"ref": ref, "title": str(e.get("title") or ""), "reasons": reasons, **(n_d if newer else {})})
    stale.sort(key=lambda x: (-len(x["reasons"]), -int(x.get("newer_count") or len(x.get("newer", ()))), x["ref"]))
    actions_list.sort(key=lambda a: (note_t.get(a["ref"], ""), a["ref"]), reverse=True)
    s_out, a_out = (stale, actions_list) if all_items else (stale[:limit], actions_list[:limit])
    if wanted is not None:
        s_out, a_out = ([{k: it[k] for k in wanted if k in it} for it in xs] for xs in (s_out, a_out))
    trunc = (not all_items) and (len(stale) > limit or len(actions_list) > limit)
    rh = {"ok": not problems, "brain": cfg.name, "total_stale": len(stale), "total_actions_open": len(actions_list)}
    rt = {"total_tasks_open": tasks_open, "stale": s_out, "actions_open": a_out, "truncated": trunc}
    return {**rh, **rt, "problems": problems}


def check_brain(
    store: Store, *, target: str = "", now: datetime | None = None, strict: bool = False, force: bool = False
) -> dict[str, Any]:
    """Run incremental `build`, `validate`, and `eval` in one pass."""
    from bf import query

    b = build(store, now=now, force=force)
    v = validate(store, strict=strict, force=force)
    ev = query.evaluate(store, target)
    return {"ok": bool(b["ok"] and v["ok"] and ev["ok"]), "brain": v["brain"], "build": b, "validate": v, "eval": ev}


def new_note(
    store: Store,
    cfg: config.Config | None,
    kind: str,
    slug: str,
    *,
    title: str = "",
    status: str = "",
    note_type: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Scaffold a `concept`, `project`, or `action` Markdown note via `Store` and rebuild."""
    if kind not in {"concept", "project", "action"}:
        raise InputError("kind must be concept, project, or action")
    if not re.fullmatch(r"^[a-z0-9][a-z0-9-]{0,63}$", slug):
        raise InputError("slug must use lowercase letters, digits, and hyphens (max 64 chars)")
    resolved = cfg or config.load(store)
    day = (now or datetime.now(UTC)).astimezone(ZoneInfo(resolved.timezone)).date().isoformat()
    if store.exists(rel := f"actions/{day}_{slug}/ACTION.md" if kind == "action" else f"{kind}s/{slug}.md"):
        raise Error(f"{rel}: note already exists")
    try:
        n_title = models.clean(title.strip(), "title") if title.strip() else slug.replace("-", " ").title()
        st = models.clean(status.strip(), "status") if status.strip() else ("draft" if kind == "action" else "stable")
        nt = models.clean(note_type.strip(), "type") if note_type.strip() else ("guide" if kind == "concept" else kind)
        fm_txt = config.dump_yaml({"type": nt, "status": st, "updated": day}).decode("utf-8")
        raw = f"---\n{fm_txt}---\n\n# {n_title}\n".encode()
        comp = markdown.compile_note(rel, raw, brain_name=resolved.name)
    except Error as err:
        raise InputError(str(err)) from err
    rec_v = schemas.load_validator(store, "schemas/record.json", schemas.document("record"))
    con_v = schemas.load_validator(store, "schemas/concepts.json", schemas.document("concepts"))
    _check_rec(comp, rec_v, con_v if kind == "concept" else None, rel, v_probs := [])
    if v_probs:
        raise InputError("; ".join(v_probs))
    store.write(rel, raw)
    b = build(store, now=now)
    out = {
        "ok": bool(b["ok"]),
        "brain": resolved.name,
        "kind": kind,
        "path": rel,
        "compiled": markdown.compiled_note_path(rel),
    }
    return {**out, "problems": b["problems"]} if b["problems"] else out
