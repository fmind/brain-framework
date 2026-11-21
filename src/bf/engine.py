"""Brain lifecycle operations: init, build, validate, review, and new."""

from __future__ import annotations

import re
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from bf import config, index, markdown, models, records, schemas
from bf.models import AUTHORED, FORMAT, MAX_TOTAL_FILES, Error, InputError, decode, encode, now_utc
from bf.storage import Store, expand, private_dir, writer

if TYPE_CHECKING:
    from collections.abc import Sequence

_NOTE_REQ = frozenset(("ref", "source", "kind", "title", "text", "sha256"))
_OPT_LISTS = ("tags", "links", "headings", "elements", "tasks")
_NAMED_BF_URI = re.compile(rb"(?i)bf://[^/]")
_OMIT_KEYS = (*_OPT_LISTS, "anchors", "sources", "aliases", "entity", "stale_after", "summary")
_NOTE_KINDS = frozenset(("project", "concept", "action"))
_WF = "snapshot_or_forgotten"


load_index = index.load_index


def init_brain(target: Path, *, name: str = "", description: str = "") -> dict[str, Any]:
    """Initialize a new brain directory with minimal starter files and `/bf`."""
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
    pkg = resources.files("bf")
    starter = pkg.joinpath("starter")
    hdr = b"# yaml-language-server: $schema=https://fmind.github.io/brain-framework/bf.schema.json\n"
    loops_doc = {
        k: {"description": d, "on": {"hooks": "pre-commit"}, "run": cmd}
        for k, d, cmd in (
            ("build", "Compile Markdown notes into memories/.", ["bf", "build"]),
            ("check", "Validate schemas, notes, records, and links.", ["bf", "check", "--validate"]),
        )
    }
    bf_doc = {"version": FORMAT, "name": brain_name, "description": desc, "loops": loops_doc}
    for rel in (".gitignore", "AGENTS.md", "concepts/welcome.md", "evals/smoke.yaml"):
        src_name = "gitignore" if rel == ".gitignore" else rel
        store.write(rel, starter.joinpath(src_name).read_bytes(), durable=False)
    store.write("bf.yaml", hdr + config.dump_yaml(bf_doc), durable=False)
    store.write("skills/bf/SKILL.md", pkg.joinpath("skills/bf/SKILL.md").read_bytes(), durable=False)
    for folder in ("projects", "actions", "memories", ".agents/skills"):
        (store.root / folder).mkdir(parents=True, exist_ok=True, mode=0o755)
    private_dir(store, "logs")
    (store.root / ".agents" / "skills" / "bf").symlink_to("../../skills/bf")
    store.sync_dirs(["", "concepts", "evals", "skills", "skills/bf", ".agents", ".agents/skills"])
    build(store)
    return {"brain": brain_name, "path": str(store.root)}


def load_or_compile_note(
    store: Store, path: str, data: bytes, *, brain_name: str
) -> tuple[dict[str, Any], bytes | None]:
    """Return `(compiled_note, matching_disk_bytes)` reusing up-to-date `memories/*.json` when safe."""
    if not _NAMED_BF_URI.search(data):
        with suppress(Error, OSError):
            mem_path = markdown.compiled_note_path(path)
            raw_mem = store.read(mem_path)
            cached = decode(raw_mem, mem_path, check_duplicates=False)
            if (
                isinstance(cached, dict)
                and cached.keys() >= _NOTE_REQ
                and cached.get("ref") == path
                and "id" not in cached
                and "attrs" not in cached
                and cached.get("sha256") == models.digest(data)
            ):
                anc = cached.get("anchors")
                anc_ok = anc is None or (
                    isinstance(anc, dict) and bool(anc) and all(isinstance(v, dict) and bool(v) for v in anc.values())
                )
                lists_ok = all(k not in cached or isinstance(cached[k], list) for k in _OPT_LISTS)
                omit_ok = not any(cached.get(k) in ("", {}, [], None) for k in _OMIT_KEYS if k in cached)
                if anc_ok and lists_ok and omit_ok:
                    rec_links = (
                        lk for lk in cached.get("links", ()) if isinstance(lk, dict) and lk.get("kind") == "record"
                    )
                    links_ok = all(markdown.is_memory_record_path(str(lk.get("to", ""))) for lk in rec_links)
                    items = (x for k in _OPT_LISTS[1:] for x in cached.get(k, ()) if isinstance(x, dict))
                    clean_items = not any(
                        x.get(f) in ("", {}) for x in items for f in ("anchor", "section", "meta", "id")
                    )
                    if links_ok and clean_items:
                        return dict(cached), raw_mem
    return markdown.compile_note(path, data, brain_name=brain_name), None


def build(
    store: Store,
    *,
    now: datetime | None = None,
    incremental: bool = True,
    force: bool = False,
    emit_event: bool = True,
) -> dict[str, Any]:
    """Compile all OKF notes into compact JSON files under `memories/{concepts,projects,actions}/`."""
    cfg = config.load(store)
    problems: list[str] = []
    synced: set[str] = set()
    notes: dict[str, dict[str, Any]] = {}
    for path in index.authored_notes(store, skipped=problems):
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
            index.sync_authored_index(store, desired, locked=True)
            if emit_event:
                ev = {"notes": len(notes), "written": written, "removed": removed}
                records.append_event(store, "notes.built", "bf", ev, locked=True, now=now)
    else:
        index.sync_authored_index(store, desired, locked=False)
    return {
        "ok": not problems,
        "brain": cfg.name,
        "notes": len(notes),
        "written": written,
        "removed": removed,
        "output": "memories/{concepts,projects,actions}",
        "problems": problems,
    }


def sync_notes(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """Incrementally synchronize dirty authored notes and authored index shards without emitting `notes.built`."""
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
    """Read-only structural, JSON Schema, eval, and graph link validation with per-source digest caching."""
    from bf import query

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
            "errors_count": 1,
            "warnings_count": 0,
            "summary": sm,
            "problems": [str(err)],
        }
    try:
        config.load_dotenv(store)
    except Error as err:
        add(errors, "config", str(err))

    s_vals: dict[str, Any] = {}
    s_skip: list[str] = []
    for sp in store.files("schemas", skipped=s_skip):
        if len(sp.split("/")) != 2 or not sp.endswith(".json"):
            add(errors, "schema", f"{sp}: schemas/ holds only flat <source>.json files")
        else:
            try:
                s_vals[sp[8:-5]] = schemas.load_validator(store, sp)
            except Error as err:
                s_vals[sp[8:-5]] = None
                add(errors, "schema", str(err))
    add(errors, "schema", *s_skip)
    for sk_n in ("record", "evals", "concepts"):
        if sk_n not in s_vals:
            s_vals[sk_n] = schemas.load_validator(store, f"schemas/{sk_n}.json", schemas.document(sk_n))
    rec_v, ev_v = s_vals.get("record"), s_vals.get("evals")
    s_dig: dict[str, str] = {"record": ""}
    with suppress(Error):
        s_dig = schemas.schemas_digests(store, skipped=[])

    f_skip: list[str] = []
    authored_refs = {markdown.compiled_note_path(p): p for p in index.authored_notes(store, skipped=f_skip)}
    add(errors, "note", *f_skip)

    seen_built: set[str] = set()
    fg_mem: dict[str, set[str]] = {}
    known_exists: set[str] = set(authored_refs.values())
    val_updates: dict[str, dict[str, Any]] = {}
    rec_count = 0
    total_scanned = 0

    m_skip: list[str] = []
    mem_dirs, mem_top_files = store.subdirs("memories", skipped=m_skip)
    for top_f in mem_top_files:
        known_exists.add(top_f)
        add(errors, "memory", f"{top_f}: memories/ holds <source>/<sha256>.json files")
    for src in mem_dirs:
        is_auth = src in AUTHORED
        bad_src = not is_auth and (not models.NAME_RE.fullmatch(src) or src in models.RESERVED_SOURCES)
        shard = {} if (force or is_auth or bad_src) else index.load_validate_shard(store, src)
        shard_ok = (
            not force
            and bool(s_dig["record"])
            and shard.get("record_schema") == s_dig["record"]
            and shard.get("source_schema", "") == s_dig.get(src, "")
        )
        old_clean = shard["clean"] if shard_ok and isinstance(shard.get("clean"), dict) else {}
        new_clean: dict[str, list[Any]] = {}
        seen_ids: set[str] = set()
        for mp, st_info in store.walk_stats(f"memories/{src}", skipped=m_skip, ordered=True):
            total_scanned += 1
            if total_scanned > MAX_TOTAL_FILES:
                raise Error(f"memories exceeds the {MAX_TOTAL_FILES:,}-entry total scan limit")
            known_exists.add(mp)
            parts = mp.split("/")
            if is_auth:
                if not mp.endswith(".json"):
                    add(errors, "note", f"{mp}: compiled note memories must be .json files")
                elif mp not in authored_refs:
                    add(errors, "note", f"{mp}: orphaned compiled note; run bf build")
                else:
                    seen_built.add(mp)
            elif len(parts) == 3 and parts[2] == ".forgotten":
                try:
                    raw_fg = store.read(mp, models.MAX_NOTE).decode("utf-8").splitlines()
                    fg_mem[src] = {
                        records.record_file(src, s) for ln in raw_fg if (s := ln.strip()) and not s.startswith("#")
                    }
                except (Error, UnicodeDecodeError) as err:
                    add(errors, "memory", str(err) if isinstance(err, Error) else f"{mp}: file is not valid UTF-8")
            elif bad_src:
                add(errors, "memory", f"{mp}: invalid or reserved source directory {src!r}")
            elif len(parts) != 3 or not mp.endswith(".json"):
                add(errors, "memory", f"{mp}: memories/ holds <source>/<sha256>.json files")
            else:
                sig = index.stat_sig(st_info)
                c_rec = old_clean.get(mp)
                if (
                    shard_ok
                    and isinstance(c_rec, list)
                    and len(c_rec) == 5
                    and sig == c_rec[:4]
                    and isinstance(c_rec[4], str)
                ):
                    rec_count += 1
                    rec_id_str = c_rec[4]
                    new_clean[mp] = c_rec
                else:
                    rec_obj, read_err = _read_obj(store, mp)
                    if rec_obj is None:
                        add(errors, "memory", read_err)
                        continue
                    rec_count += 1
                    rec_errs: list[str] = []
                    _check_rec(rec_obj, rec_v, s_vals.get(src), mp, rec_errs)
                    rec_id_str = rid if isinstance(rid := rec_obj.get("id"), str) else ""
                    if rec_id_str:
                        for key, exp in (("source", src), ("ref", f"{src}:{rec_id_str}"), ("kind", "memory")):
                            if key in rec_obj and rec_obj[key] != exp:
                                rec_errs.append(f"{mp}: {key} must equal {exp!r}")
                    if isinstance(lks := rec_obj.get("links"), list):
                        rec_errs.extend(
                            f"{mp}: {prob}"
                            for lnk in lks
                            if isinstance(lnk, dict) and (prob := markdown.record_link_problem(lnk))
                        )
                    if rec_errs:
                        add(errors, "memory", *rec_errs)
                    else:
                        new_clean[mp] = [*sig, rec_id_str]
                if rec_id_str:
                    if rec_id_str in seen_ids:
                        add(errors, "memory", f"{mp}: duplicate record id {rec_id_str!r} in {src}")
                    seen_ids.add(rec_id_str)
        if not is_auth and not bad_src and (not shard_ok or new_clean != old_clean):
            val_updates[src] = {
                "record_schema": s_dig["record"],
                "source_schema": s_dig.get(src, ""),
                "clean": new_clean,
            }
    add(errors, "memory", *m_skip)

    anchors: dict[tuple[str, str], set[str]] = {}
    note_links: dict[str, list[dict[str, Any]]] = {}
    notes_by_src: dict[str, list[tuple[str, str]]] = {f: [] for f in AUTHORED}
    for mp, ref in sorted(authored_refs.items()):
        notes_by_src[ref.split("/")[0]].append((mp, ref))
    for src, pairs in notes_by_src.items():
        shard = {} if force else index.load_validate_shard(store, src)
        shard_ok = (
            not force
            and bool(s_dig["record"])
            and shard.get("brain") == cfg.name
            and shard.get("record_schema") == s_dig["record"]
            and shard.get("source_schema", "") == s_dig.get(src, "")
        )
        old_notes = shard["clean"] if shard_ok and isinstance(shard.get("clean"), dict) else {}
        new_notes: dict[str, list[Any]] = {}
        for mp, ref in pairs:
            md_sig = index.file_sig(store, ref)
            mem_sig = index.file_sig(store, mp) if mp in seen_built else None
            cn = old_notes.get(ref)
            if shard_ok and index.valid_cached_note_entry(cn, md_sig, mem_sig):
                anchors[("", ref)] = set(cn[2])
                note_links[ref] = list(cn[3])
                new_notes[ref] = cn
                continue
            try:
                item = markdown.compile_note(ref, store.read(ref, models.MAX_NOTE), brain_name=cfg.name)
            except (Error, OSError) as err:
                add(errors, "note", str(err) if isinstance(err, Error) else f"{ref}: unreadable file")
                continue
            item_anc = markdown.compiled_anchors(item)
            anchors[("", ref)] = item_anc
            note_links[ref] = list(item.get("links") or ())
            note_errs: list[str] = []
            _check_rec(item, rec_v, s_vals.get(src), ref, note_errs)
            if mp not in seen_built and not store.exists(mp):
                note_errs.append(f"{mp}: missing compiled note for {ref}; run bf build")
            else:
                built_obj, built_err = _read_obj(store, mp)
                if built_obj is None or built_obj != item:
                    note_errs.append(built_err or f"{mp}: out of sync with {ref}; run bf build")
            if note_errs:
                add(errors, "note", *note_errs)
            elif md_sig is not None and mem_sig is not None:
                new_notes[ref] = [md_sig, mem_sig, sorted(item_anc), note_links[ref]]
        if not shard_ok or new_notes != old_notes:
            val_updates[src] = {
                "brain": cfg.name,
                "record_schema": s_dig["record"],
                "source_schema": s_dig.get(src, ""),
                "clean": new_notes,
            }

    e_skip: list[str] = []
    for ev_p in store.files("evals", skipped=e_skip):
        if not ev_p.endswith((".yaml", ".yml", ".json")):
            add(errors, "eval", f"{ev_p}: evals/ holds .yaml or .json files")
        else:
            ev_errs: list[str] = []
            query.load_eval_doc(store, ev_p, ev_v, ev_errs)
            add(errors, "eval", *ev_errs)
    add(errors, "eval", *e_skip)

    if index.skill_is_outdated(store):
        add(warnings, "skill_outdated", "skills/bf/SKILL.md: warning: differs from installed bf; refresh from package")

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
            key = (b_key, rel)
            if key not in anchors:
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
                    if ":" in rest or not sib.exists(rest):
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

    if not read_only and val_updates and s_dig["record"]:
        index.save_validate_shards(store, val_updates)
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
        "notes": len(authored_refs),
        "records": rec_count,
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


def review(
    store: Store,
    *,
    now: datetime | None = None,
    limit: int = 20,
    all_items: bool = False,
    fields: Sequence[str] | str | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    """Staleness, `newer_evidence`, open `draft` actions, and open task summary via per-source index shards."""
    if not all_items and limit < 1:
        raise InputError("limit must be at least 1")
    wanted, cfg = parse_fields(fields), config.load(store)
    instant = datetime.now(UTC) if now is None else now.astimezone(UTC)
    today, now_iso = instant.date(), now_utc(instant)
    sync_probs = [] if read_only else sync_notes(store, now=now)["problems"]
    idx_probs: list[str] = []
    entries = index.load_index(store, problems=idx_probs, read_only=read_only)
    problems = list(dict.fromkeys([*sync_probs, *idx_probs]))

    rec_t: dict[str, str] = {}
    note_t: dict[str, str] = {}
    mem_ref: dict[str, str] = {}
    incoming: dict[str, set[str]] = {}
    note_ent: list[dict[str, Any]] = []
    pfx = (f"bf://{cfg.name}/", "bf:///")

    def norm(ref_s: str) -> str:
        t = ref_s.partition("#")[0].partition("?")[0]
        return next((t.removeprefix(p) for p in pfx if t.startswith(p)), t)

    for entry in entries:
        mp, kind, rv = str(entry["path"]), str(entry["kind"]), str(entry["ref"])
        mem_ref[mp] = rv
        is_note = kind in _NOTE_KINDS
        if is_note:
            note_ent.append(entry)
            if u := str(entry.get("updated") or entry.get("evidence_updated") or ""):
                note_t[rv] = _ts_key(u)
        elif ts := str(entry.get("updated") or ""):
            rec_t[rv] = max(rec_t.get(rv, ""), _ts_key(ts))
        for lk in entry.get("links") or ():
            if (not is_note or lk["to"] != rv) and (tgt := norm(str(lk["to"]))):
                incoming.setdefault(tgt, set()).add(rv)

    stale: list[dict[str, Any]] = []
    actions_list: list[dict[str, Any]] = []
    tasks_open = 0
    today_iso = today.isoformat()
    for entry in sorted(note_ent, key=lambda x: str(x["ref"])):
        ref, st = str(entry["ref"]), str(entry.get("status", "stable"))
        if st == "deprecated":
            continue
        tasks_open += int(entry.get("tasks_open") or 0)
        if entry["kind"] == "action" and st == "draft":
            ext = {fk: entry[fk] for fk in ("updated", "summary") if isinstance(entry.get(fk), str) and entry[fk]}
            actions_list.append({"ref": ref, "title": str(entry.get("title") or ""), **ext})
        reasons: list[str] = []
        newer: list[str] = []
        due = False
        upd = str(entry.get("updated") or entry.get("evidence_updated") or "")
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
            if rd := (upd if is_dur else sa):
                try:
                    dt = datetime.fromisoformat(f"{rd}T00:00:00Z" if len(rd) == 10 else rd).astimezone(UTC)
                    due = instant >= (dt + timedelta(seconds=models.parse_duration_seconds(sa)) if is_dur else dt)
                except Error, OverflowError, ValueError:
                    due = now_iso >= sa
        if due:
            reasons.append("due")
        if ref in note_t:
            outs = [lk["to"] for lk in entry.get("links") or () if lk["kind"] in {"note", "record"} and lk["to"] != ref]
            linked = (
                {mem_ref.get(t, t) for t in outs} | incoming.get(ref, set()) | incoming.get(str(entry["path"]), set())
            )
            for raw_id in (entry.get("entity"), *(entry["aliases"] if isinstance(entry.get("aliases"), list) else ())):
                if isinstance(raw_id, str) and raw_id:
                    nr = norm(raw_id)
                    bare = nr.removeprefix("urn:")
                    linked.update(*(incoming.get(k, ()) for k in (nr, bare, f"urn:{bare}")))
                    if mf := markdown.record_path(bare):
                        linked |= incoming.get(mf, set())
            linked.discard(ref)
            if newer := [t for t in sorted(linked) if (rec_t.get(t) or note_t.get(t) or "") > note_t[ref]]:
                reasons.append("newer_evidence")
        if reasons:
            n_d: dict[str, Any] = (
                {"newer": newer[:5], **({"newer_count": len(newer), "truncated": True} if len(newer) > 5 else {})}
                if newer
                else {}
            )
            stale.append({"ref": ref, "title": str(entry.get("title") or ""), "reasons": reasons, **n_d})

    stale.sort(key=lambda x: (-len(x["reasons"]), -int(x.get("newer_count") or len(x.get("newer", ()))), x["ref"]))
    actions_list.sort(key=lambda a: (note_t.get(a["ref"], ""), a["ref"]), reverse=True)
    s_out, a_out = (stale, actions_list) if all_items else (stale[:limit], actions_list[:limit])
    if wanted is not None:
        s_out = [{k: it[k] for k in wanted if k in it} for it in s_out]
        a_out = [{k: it[k] for k in wanted if k in it} for it in a_out]
    trunc = (not all_items) and (len(stale) > limit or len(actions_list) > limit)
    return {
        "ok": not problems,
        "brain": cfg.name,
        "total_stale": len(stale),
        "total_actions_open": len(actions_list),
        "total_tasks_open": tasks_open,
        "stale": s_out,
        "actions_open": a_out,
        "truncated": trunc,
        "problems": problems,
    }


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
    if kind not in _NOTE_KINDS:
        raise InputError("kind must be concept, project, or action")
    if not re.fullmatch(r"^[a-z0-9][a-z0-9-]{0,63}$", slug):
        raise InputError("slug must use lowercase letters, digits, and hyphens (max 64 chars)")
    resolved = cfg or config.load(store)
    day = (now or datetime.now(UTC)).astimezone(ZoneInfo(resolved.timezone)).date().isoformat()
    rel = f"actions/{day}_{slug}/ACTION.md" if kind == "action" else f"{kind}s/{slug}.md"
    if store.exists(rel):
        raise Error(f"{rel}: note already exists")
    try:
        n_title = models.clean(title.strip(), "title") if title.strip() else slug.replace("-", " ").title()
        st = models.clean(status.strip(), "status") if status.strip() else ("draft" if kind == "action" else "stable")
        nt = models.clean(note_type.strip(), "type") if note_type.strip() else ("guide" if kind == "concept" else kind)
        raw = f"---\n{config.dump_yaml({'type': nt, 'status': st, 'updated': day}).decode('utf-8')}---\n\n# {n_title}\n".encode()
        comp = markdown.compile_note(rel, raw, brain_name=resolved.name)
    except Error as err:
        raise InputError(str(err)) from err
    rec_v = schemas.load_validator(store, "schemas/record.json", schemas.document("record"))
    con_v = schemas.load_validator(store, "schemas/concepts.json", schemas.document("concepts"))
    v_probs: list[str] = []
    _check_rec(comp, rec_v, con_v if kind == "concept" else None, rel, v_probs)
    if v_probs:
        raise InputError("; ".join(v_probs))
    store.write(rel, raw)
    built = build(store, now=now)
    compiled_path = markdown.compiled_note_path(rel)
    out: dict[str, Any] = {
        "ok": bool(built["ok"]),
        "brain": resolved.name,
        "kind": kind,
        "path": rel,
        "compiled": compiled_path,
    }
    return {**out, "problems": built["problems"]} if built["problems"] else out
