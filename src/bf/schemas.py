"""Draft 2020-12 JSON Schemas for bf.yaml and unified JSONL records, plus fast native core validation."""

from __future__ import annotations

import copy
import re
import threading
from typing import TYPE_CHECKING, Any, Literal

from bf import models, refs
from bf.models import Error, InputError, decode, digest
from bf.storage import Store

if TYPE_CHECKING:
    from jsonschema.exceptions import ValidationError
    from jsonschema.protocols import Validator

Kind = Literal["bf", "record"]
_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_BASE = "https://fmind.github.io/brain-framework"
_CAL_DATE = (
    r"[0-9]{4}-(?:(?:0[13578]|1[02])-(?:0[1-9]|[12][0-9]|3[01])"
    r"|(?:0[469]|11)-(?:0[1-9]|[12][0-9]|30)|02-(?:0[1-9]|[12][0-9]))"
)
_TS_HMS = r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
_ISO_DT = rf"^{_CAL_DATE}(?:T{_TS_HMS})?$"
_ISO_DT_RE = re.compile(rf"^{_CAL_DATE}(?:T{_TS_HMS})?\Z")
_BOOL = {"type": "boolean"}
_NAME_REF = {"type": "string", "pattern": models.NAME}
_ENV_KEY_PAT = r"^[A-Z_][A-Z0-9_]*$"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
_TAG_BAD_RE = re.compile(r"[/\\\s]")
_URL_PAT = r"^(?:https?://[^\s/?#]+\S*|(?:mailto|urn):\S+)$"


def _str(max_len: int = 4096, min_len: int = 0, pat: str = "") -> dict[str, Any]:
    extra = {key: val for key, val in (("minLength", min_len), ("pattern", pat)) if val}
    return {"type": "string", "maxLength": max_len, **extra}


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _int(min_val: int = 0, max_val: int | None = None) -> dict[str, Any]:
    return {"type": "integer", "minimum": min_val, **({"maximum": max_val} if max_val is not None else {})}


def _arr(items: dict[str, Any], *, max_items: int = 0, min_items: int = 0, unique: bool = False) -> dict[str, Any]:
    extra = {
        key: val for key, val in (("minItems", min_items), ("maxItems", max_items), ("uniqueItems", unique)) if val
    }
    return {"type": "array", "items": items, **extra}


def _desc(schema: dict[str, Any], text: str, **extra: Any) -> dict[str, Any]:
    return {**schema, "description": text, **extra}


def _obj(props: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    tp: Any = ["object", "null"] if kw.get("opt") else "object"
    out: dict[str, Any] = {"type": tp, "additionalProperties": kw.get("extra", False)}
    extra_meta = (("title", kw.get("title")), ("description", kw.get("desc")), ("minProperties", kw.get("min_props")))
    out.update({key: val for key, val in extra_meta if val})
    if kw.get("req"):
        out["required"] = list(kw["req"])
    if names := kw.get("names"):
        out["propertyNames"] = names if isinstance(names, dict) else {"pattern": names}
    return {**out, "properties": props} if props is not None else out


def _doc(slug: str, title: str, desc: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"$schema": _DRAFT, "$id": f"{_BASE}/{slug}.schema.json", "title": title, "description": desc, **body}


_DUR_STR = _str(32, pat=models.DURATION_PAT)
_OVERLAP = {"anyOf": [{"const": 0}, _str(32, pat=models.DURATION_ZERO_PAT)]}
_ENV_MAP = _obj(extra=_str(4096), names=_ENV_KEY_PAT, opt=True)
_ENV_FROM = _arr(_str(128, min_len=1, pat=_ENV_KEY_PAT), max_items=128, unique=True)
_TAG_ITEMS = _arr(_str(128, min_len=1, pat=r"^[^/\\\s]+$"), max_items=200, unique=True)
_META_REF = {"$ref": "#/$defs/Meta"}
_SRC_REF = {**_NAME_REF, "not": {"enum": sorted(models.RESERVED_SOURCES)}}
_EVENT_KIND_STR = _str(128, min_len=1, pat=models.EVENT_KIND_PAT)
_CRON_STR = _str(128, min_len=1, pat=r"^(?:@[A-Za-z]+|[^\s]+(?:[ \t]+[^\s]+){4})$")
_WHERE_SCALAR: dict[str, Any] = {"type": ["string", "number", "integer", "boolean", "null"]}
_WHERE_PRED: dict[str, Any] = {
    "anyOf": [
        _WHERE_SCALAR,
        _obj({"in": _arr(_WHERE_SCALAR, min_items=1, max_items=64)}, req=("in",)),
        _obj({"exists": _BOOL}, req=("exists",)),
        _obj({"prefix": _str(512, min_len=1)}, req=("prefix",)),
    ]
}
_WHERE_MAP = _obj(extra=_WHERE_PRED, names=r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$", min_props=1)
_EVENT_VAL = {"anyOf": [_EVENT_KIND_STR, {"$ref": "#/$defs/EventMatch"}]}
_TRIG_ITEM = {"anyOf": [_DUR_STR, _CRON_STR, {"$ref": "#/$defs/ScheduleTrigger"}, {"$ref": "#/$defs/EventTrigger"}]}
_WHEN_VAL = {"anyOf": [{"const": "daemon"}, _TRIG_ITEM, _arr(_TRIG_ITEM, min_items=1, max_items=32)]}
_POOL_INT = _int(1, 32)
_POOL_MAP = {**_obj(extra=_POOL_INT, names=models.NAME, min_props=1), "maxProperties": 32}
_CONCURRENCY_VAL = {"anyOf": [_POOL_INT, _POOL_MAP]}
_INGEST_VAL = {"anyOf": [_enum("upsert", "snapshot"), {"$ref": "#/$defs/Ingest"}]}

_LOOP_PROPS: dict[str, Any] = {
    "description": _desc(_str(512), "Short description of what this loop does for humans and AI agents."),
    "when": _desc(
        _WHEN_VAL,
        "When the loop runs: 'daemon', duration ('1h'), cron ('0 8 * * 0'), {every|cron, backfill?, overlap?}, {event, debounce?}, list, or omit for manual.",
    ),
    "window": {"$ref": "#/$defs/Window"},
    "run": {"$ref": "#/$defs/Argv"},
    "ingest": _desc(
        _INGEST_VAL,
        "Explicitly ingest stdout JSONL into memories/<source>.jsonl ('upsert'|'snapshot'|{source?, mode?, schema?}).",
    ),
    "emit": _desc(_EVENT_KIND_STR, "Default event kind emitted from stdout JSONL to .bf/state/events.jsonl."),
    "pool": _desc(_NAME_REF, "Named concurrency pool declared under bf.yaml concurrency: (default: 'default')."),
    "timeout": _desc(_DUR_STR, "Max cycle runtime as explicit duration (1s..24h, e.g. 5m, 15m).", default="5m"),
    "env": _desc(_ENV_MAP, "Explicit loop environment variable values (blocks linker/interpreter hijack keys)."),
    "env_from": _desc(_ENV_FROM, "Host or .env variable names explicitly allowed into this loop."),
    "enabled": _desc(_BOOL, "Enable automatic triggers and daemon supervision.", default=True),
}
_EMIT_SET = {"type": "object", "required": ["emit"]}
_IS_DAEMON = {"type": "object", "required": ["when"], "properties": {"when": {"const": "daemon"}}}
_NO_CYCLE = {"not": {"anyOf": [{"required": [k]} for k in ("window", "ingest", "timeout", "pool")]}}
_LOOP_RULES = [{"if": _EMIT_SET, "then": {"not": {"required": ["ingest"]}}}, {"if": _IS_DAEMON, "then": _NO_CYCLE}]

_BRAIN_PROPS: dict[str, Any] = {
    "version": _desc({"type": "integer", "const": models.FORMAT}, "Brain configuration format version."),
    "name": _desc(_NAME_REF, "Stable lowercase identifier for this brain."),
    "description": _desc(_str(1024), "Short description of this brain's scope for humans and AI agents."),
    "timezone": _desc(_str(128, min_len=1), "IANA timezone for cron schedules.", default="UTC"),
    "notes": _desc(
        _arr(_SRC_REF, min_items=1, max_items=32, unique=True),
        "Top-level Markdown note folders compiled into memories/_notes.jsonl.",
        default=list(models.DEFAULT_NOTES),
    ),
    "brains": _desc(_obj(extra=_str(4096, min_len=1), names=models.NAME, opt=True), "Sibling brain aliases for bf://."),
    "search": _obj(
        {
            "exclude": _desc(
                _arr(_SRC_REF, max_items=128, unique=True),
                "Memory sources excluded from unscoped bf query by default (override via --scope or --all-sources).",
            )
        },
        min_props=1,
        opt=True,
        desc="Default retrieval settings.",
    ),
    "concurrency": _desc(
        _CONCURRENCY_VAL,
        "Max concurrent cycle loops (1..32) or named concurrency pools ({default: 4, agents: 2}).",
        default=4,
    ),
    "env": _desc(_ENV_MAP, "Shared explicit environment variables for loops."),
    "loops": _desc(
        _obj(extra={"anyOf": [{"$ref": "#/$defs/Loop"}, {"type": "null"}]}, names=models.NAME, opt=True),
        "Unified loop definitions.",
    ),
}
_WIN_PROPS = {
    "backfill": _desc(_DUR_STR, "Initial BF_START window on first run (now - backfill).", default="1d"),
    "overlap": _desc(_OVERLAP, "Rewind on later runs (last_anchor - overlap).", default="0s"),
}

_BRAIN_DEFS = {
    "Argv": _desc(
        _arr(_str(4096, min_len=1), min_items=1, max_items=64),
        "Literal argv without a shell (supports {{brain}}, {{loop}}, {{state}}, {{start}}, {{end}}, {{trigger}}, {{run}}).",
        title="Argv",
    ),
    "EventMatch": _obj(
        {
            "kind": _desc(
                _EVENT_KIND_STR,
                "Exact event kind to match (lowercase letters, digits, '.', '_', '-'; e.g. records.ingested).",
            ),
            "source": _desc(_NAME_REF, "Exact event source to match (lowercase loop name or 'bf')."),
            "where": _desc(_WHERE_MAP, "Declarative dot-path predicates evaluated against the event object."),
        },
        min_props=1,
        title="EventMatch",
        desc="Event matcher against .bf/state/events.jsonl.",
    ),
    "EventTrigger": _obj(
        {
            "event": _desc(_EVENT_VAL, "Event kind string or matcher object."),
            "debounce": _desc(_DUR_STR, "Trailing-edge coalesce window before firing (1s..1h, e.g. 10s)."),
        },
        req=("event",),
        title="EventTrigger",
        desc="Event subscription trigger with optional trailing-edge debounce.",
    ),
    "Ingest": _obj(
        {
            "source": _desc(_SRC_REF, "Target memories/<source>.jsonl stem (defaults to loop name)."),
            "mode": _desc(_enum("upsert", "snapshot"), "Write mode: upsert or snapshot.", default="upsert"),
            "schema": _desc(_SRC_REF, "Optional schemas/<schema>.json name (for year-partitioned sources)."),
        },
        min_props=1,
        title="Ingest",
        desc="Explicit record ingestion target.",
    ),
    "Loop": {**_obj(_LOOP_PROPS, title="Loop", desc="One loop entry."), "allOf": _LOOP_RULES},
    "ScheduleTrigger": {
        **_obj(
            {
                "every": _desc(_DUR_STR, "Interval schedule duration (e.g. 1h, 1d)."),
                "cron": _desc(_CRON_STR, "5-field POSIX cron expression or @macro."),
                "backfill": _desc(_DUR_STR, "Per-trigger initial BF_START window override."),
                "overlap": _desc(_OVERLAP, "Per-trigger BF_START rewind override."),
            },
            title="ScheduleTrigger",
            desc="Interval or cron trigger with optional per-trigger BF_START window override.",
        ),
        "oneOf": [{"required": ["every"]}, {"required": ["cron"]}],
    },
    "Window": _obj(_WIN_PROPS, min_props=1, title="Window", desc="Time window for BF_START/BF_END."),
}
BF_SCHEMA = _doc(
    "bf",
    "Config",
    "Brain Framework configuration and loop orchestration (bf.yaml).",
    {**_obj(_BRAIN_PROPS, req=("version", "name")), "$defs": _BRAIN_DEFS},
)

_STALE_ANY = [_DUR_STR, {"type": "string", "minLength": 10, "pattern": _ISO_DT}]
_RECORD_PROPS: dict[str, Any] = {
    "id": _desc(_str(4096, min_len=1), "Stable source-local record identifier (required for memory)."),
    "title": _desc(_str(4096, min_len=1), "Human-readable title."),
    "entity": _desc(_str(4096, min_len=1), "Canonical entity URI or source:id alias."),
    "type": _desc(_str(128, min_len=1), "Domain subtype (e.g. guide, project, issue, usage)."),
    "status": _desc(_str(128, min_len=1), "Lifecycle status (e.g. draft, stable, active, open, done)."),
    "description": _desc(_str(16384), "Short description of the note or record."),
    "ref": _desc(_str(4200, min_len=1), "Universal brain-local reference (note path or source:id)."),
    "source": _desc(_NAME_REF, "Owning source name (note dir or memories/<source>.jsonl)."),
    "kind": _desc(_enum("note", "memory"), "Top-level record kind (note or memory)."),
    "text": _desc(_str(4 << 20), "Plain or Markdown body text."),
    "url": _desc(_str(8192, min_len=1, pat=_URL_PAT), "Canonical external URL (https, http, mailto, or urn)."),
    "created": _desc({"type": "string", "minLength": 10, "pattern": _ISO_DT}, "Creation or event ISO date/timestamp."),
    "updated": _desc({"type": "string", "minLength": 10, "pattern": _ISO_DT}, "Last-updated ISO date/timestamp."),
    "ingested_at": _desc({"type": "string", "minLength": 10, "pattern": _ISO_DT}, "UTC ingestion timestamp."),
    "stale_after": _desc({"anyOf": _STALE_ANY}, "Review freshness threshold (explicit duration like 90d or ISO date)."),
    "tags": _desc(_TAG_ITEMS, "Unique tags without whitespace or slashes."),
    "aliases": _desc(_arr(_str(1024, min_len=1), max_items=200), "Alternate identifiers resolved by bf read/review."),
    "attributes": _desc({"type": "object"}, "Optional sensor-specific domain attributes."),
    "headings": _desc(_arr({"$ref": "#/$defs/Heading"}), "Compiled Markdown headings with optional attr_list meta."),
    "links": _desc(_arr({"$ref": "#/$defs/Link"}, max_items=2000), "Normalized outgoing links."),
    "tasks": _desc(_arr({"$ref": "#/$defs/Task"}), "Compiled Markdown checklist tasks."),
    "sha256": _desc({"type": "string", "pattern": r"^[0-9a-f]{64}$"}, "SHA-256 hex digest of source Markdown bytes."),
}
_RECORD_DEFS = {
    "Meta": _obj(extra=_str(4096), title="Meta", names=r"^[A-Za-z_][A-Za-z0-9_.-]*$"),
    "Heading": _obj(
        {"id": _str(256, min_len=1), "title": _str(4096), "level": _int(1, 6), "meta": _META_REF},
        req=("id", "title", "level"),
        title="Heading",
    ),
    "Link": _obj(
        {
            "to": _desc(_str(8192, min_len=1), "Universal ref: note path, source:id, bf://<brain>/<ref>, or URL."),
            "kind": _enum("note", "record", "brain", "url"),
            "anchor": _str(2048),
            "meta": _META_REF,
            "section": _str(256),
        },
        req=("to", "kind"),
        title="Link",
    ),
    "Task": _obj(
        {"text": _str(4096, min_len=1), "done": _BOOL, "section": _str(256), "meta": _META_REF},
        req=("text", "done"),
        title="Task",
    ),
}
_RECORD_IF = {
    "if": {"required": ["kind"], "properties": {"kind": {"const": "memory"}}},
    "then": {"required": ["id"]},
}
RECORD_SCHEMA = _doc(
    "record",
    "Record",
    "Unified JSON record schema enforced natively on compiled notes and ingested records in memories/*.jsonl.",
    {**_obj(_RECORD_PROPS, req=("ref", "source", "kind", "title"), extra=True), **_RECORD_IF, "$defs": _RECORD_DEFS},
)

_SCHEMAS: dict[Kind, dict[str, Any]] = {"bf": BF_SCHEMA, "record": RECORD_SCHEMA}

_STARTER_STATUSES: dict[str, tuple[str, ...]] = {
    "concepts": ("draft", "stable", "deprecated"),
    "projects": ("active", "paused", "done", "archived"),
    "actions": ("open", "done", "cancelled"),
}


def starter_note_schema(folder: str) -> dict[str, Any]:
    """Return an optional idiomatic Draft 2020-12 note schema for `schemas/<folder>.json`."""
    label, statuses = folder.replace("-", " ").title(), _STARTER_STATUSES.get(folder, ("draft", "stable", "deprecated"))
    props = {
        "type": _str(128, min_len=1),
        "status": _enum(*statuses),
        "description": _str(16384),
    }
    req = ("type", "status", "created") if folder == "actions" else ("type", "status")
    return {
        "$schema": _DRAFT,
        **_obj(
            props,
            req=req,
            extra=True,
            title=f"{label}Note",
            desc=f"Optional domain schema for authored Markdown notes in {folder}/.",
        ),
    }


def document(kind: Kind) -> dict[str, Any]:
    """Return a deep copy of the canonical JSON Schema for `kind`."""
    return copy.deepcopy(_SCHEMAS[kind])


def get_schema(name: str) -> dict[str, Any]:
    """Return a deep copy of a built-in schema by CLI name (`bf`, `record`)."""
    key = name.strip().lower().removesuffix(".json").removesuffix(".schema")
    if key not in _SCHEMAS:
        raise InputError(f"unknown schema {name!r}; choose bf or record")
    return document(key)


def export_schemas() -> dict[str, dict[str, Any]]:
    """Return all built-in schemas keyed by CLI name (`bf`, `record`)."""
    return {k: document(k) for k in ("bf", "record")}


def _check_meta(val: object, path: str, out: list[str]) -> None:
    if not isinstance(val, dict):
        out.append(f"{path}: expected type object")
        return
    for k, v in val.items():
        if not isinstance(k, str) or not refs.META_KEY_RE.fullmatch(k) or "\n" in k:
            out.append(f"{path}: invalid or reserved property name")
            return
        if not isinstance(v, str) or len(v) > 4096:
            out.append(f"{path}.{k}: expected string up to 4096 chars")
            return


def validate_core_record(obj: object, label: str) -> list[str]:
    """Fast native Python enforcement of `RECORD_SCHEMA` (~50x faster than `jsonschema`, strict dates & URLs)."""
    if not isinstance(obj, dict):
        return [f"{label}: expected type object"]
    out: list[str] = [
        f"{label}: missing required {req_k!r}" for req_k in ("ref", "source", "kind", "title") if req_k not in obj
    ]
    if "kind" in obj:
        kind = obj["kind"]
        if not isinstance(kind, str) or kind not in {"note", "memory"}:
            out.append(f"{label}: kind: must be one of ['note', 'memory']")
        elif kind == "memory" and "id" not in obj:
            out.append(f"{label}: missing required 'id'")

    str_limits = (
        ("id", 1, 4096),
        ("title", 1, 4096),
        ("entity", 1, 4096),
        ("type", 1, 128),
        ("status", 1, 128),
        ("description", 0, 16384),
        ("ref", 1, 4200),
        ("text", 0, 4 << 20),
    )
    for key, lo, hi in str_limits:
        if key in obj:
            val = obj[key]
            if not isinstance(val, str):
                out.append(f"{label}: {key}: expected type string")
            elif len(val) < lo:
                out.append(f"{label}: {key}: violates minLength {lo}")
            elif len(val) > hi:
                out.append(f"{label}: {key}: violates maxLength {hi}")

    if "source" in obj:
        sv = obj["source"]
        if not isinstance(sv, str) or "\n" in sv or not models.NAME_RE.fullmatch(sv):
            out.append(f"{label}: source: does not match pattern {models.NAME}")

    if "url" in obj:
        uv = obj["url"]
        if not isinstance(uv, str) or len(uv) > 8192:
            out.append(f"{label}: url: expected string up to 8192 chars")
        else:
            try:
                refs.validate_url(uv, f"{label}: url")
            except Error as err:
                out.append(str(err))

    for dt_key in ("created", "updated", "ingested_at"):
        if dt_key in obj:
            dv = obj[dt_key]
            if not isinstance(dv, str) or not _ISO_DT_RE.fullmatch(dv):
                out.append(f"{label}: {dt_key}: must be ISO date (YYYY-MM-DD) or timestamp with timezone")
            else:
                try:
                    models.normalize_iso_date_or_time(dv, f"{label}: {dt_key}")
                except Error as err:
                    out.append(str(err))

    if "stale_after" in obj:
        sa = obj["stale_after"]
        if not isinstance(sa, str) or (
            not (len(sa) <= 32 and models.DURATION_RE.fullmatch(sa)) and not _ISO_DT_RE.fullmatch(sa)
        ):
            out.append(f"{label}: stale_after: expected explicit duration (<=32 chars) or ISO date/timestamp")
        else:
            try:
                models.validate_stale_after(sa, f"{label}: stale_after")
            except Error as err:
                out.append(str(err))

    if "tags" in obj:
        tv = obj["tags"]
        if not isinstance(tv, list):
            out.append(f"{label}: tags: expected type array")
        elif len(tv) > 200:
            out.append(f"{label}: tags: violates maxItems 200")
        else:
            seen_tags: set[str] = set()
            for item in tv:
                if not isinstance(item, str) or not item or len(item) > 128 or _TAG_BAD_RE.search(item):
                    out.append(f"{label}: tags: items must be nonempty tokens without spaces or slashes")
                    break
                if item in seen_tags:
                    out.append(f"{label}: tags: items must be unique")
                    break
                seen_tags.add(item)

    if "aliases" in obj:
        av = obj["aliases"]
        if not isinstance(av, list):
            out.append(f"{label}: aliases: expected type array")
        elif len(av) > 200:
            out.append(f"{label}: aliases: violates maxItems 200")
        elif any(not isinstance(x, str) or not x or len(x) > 1024 for x in av):
            out.append(f"{label}: aliases: items must be nonempty strings up to 1024 chars")

    if "attributes" in obj and not isinstance(obj["attributes"], dict):
        out.append(f"{label}: attributes: expected type object")

    if "sha256" in obj:
        sh = obj["sha256"]
        if not isinstance(sh, str) or not _SHA256_RE.fullmatch(sh):
            out.append(f"{label}: sha256: must be 64 lowercase hex characters")

    if "headings" in obj:
        hv = obj["headings"]
        if not isinstance(hv, list):
            out.append(f"{label}: headings: expected type array")
        else:
            for idx, h in enumerate(hv):
                if not isinstance(h, dict):
                    out.append(f"{label}: headings.{idx}: expected type object")
                    break
                if extra := set(h) - {"id", "title", "level", "meta"}:
                    out.append(f"{label}: headings.{idx}: unknown property {', '.join(repr(k) for k in sorted(extra))}")
                    break
                hid, htitle, hlvl = h.get("id"), h.get("title"), h.get("level")
                if not isinstance(hid, str) or not (1 <= len(hid) <= 256):
                    out.append(f"{label}: headings.{idx}.id: invalid string")
                    break
                if not isinstance(htitle, str) or len(htitle) > 4096:
                    out.append(f"{label}: headings.{idx}.title: invalid string")
                    break
                if type(hlvl) is not int or not (1 <= hlvl <= 6):
                    out.append(f"{label}: headings.{idx}.level: expected integer 1..6")
                    break
                if "meta" in h:
                    _check_meta(h["meta"], f"{label}: headings.{idx}.meta", out)

    if "links" in obj:
        lv = obj["links"]
        if not isinstance(lv, list):
            out.append(f"{label}: links: expected type array")
        elif len(lv) > 2000:
            out.append(f"{label}: links: violates maxItems 2000")
        else:
            for idx, lk in enumerate(lv):
                if not isinstance(lk, dict):
                    out.append(f"{label}: links.{idx}: expected type object")
                    break
                if extra := set(lk) - {"to", "kind", "anchor", "meta", "section"}:
                    out.append(f"{label}: links.{idx}: unknown property {', '.join(repr(k) for k in sorted(extra))}")
                    break
                lto, lkind = lk.get("to"), lk.get("kind")
                if not isinstance(lto, str) or not (1 <= len(lto) <= 8192):
                    out.append(f"{label}: links.{idx}.to: invalid string")
                    break
                if not isinstance(lkind, str) or lkind not in {"note", "record", "brain", "url"}:
                    out.append(f"{label}: links.{idx}.kind: must be one of ['note', 'record', 'brain', 'url']")
                    break
                if "anchor" in lk and (not isinstance(lk["anchor"], str) or len(lk["anchor"]) > 2048):
                    out.append(f"{label}: links.{idx}.anchor: invalid string")
                    break
                if "section" in lk and (not isinstance(lk["section"], str) or len(lk["section"]) > 256):
                    out.append(f"{label}: links.{idx}.section: invalid string")
                    break
                if "meta" in lk:
                    _check_meta(lk["meta"], f"{label}: links.{idx}.meta", out)

    if "tasks" in obj:
        tkv = obj["tasks"]
        if not isinstance(tkv, list):
            out.append(f"{label}: tasks: expected type array")
        else:
            for idx, tk in enumerate(tkv):
                if not isinstance(tk, dict):
                    out.append(f"{label}: tasks.{idx}: expected type object")
                    break
                if extra := set(tk) - {"text", "done", "section", "meta"}:
                    out.append(f"{label}: tasks.{idx}: unknown property {', '.join(repr(k) for k in sorted(extra))}")
                    break
                if not isinstance(tk.get("text"), str) or not (1 <= len(tk["text"]) <= 4096):
                    out.append(f"{label}: tasks.{idx}.text: invalid string")
                    break
                if type(tk.get("done")) is not bool:
                    out.append(f"{label}: tasks.{idx}.done: expected type boolean")
                    break
                if "section" in tk and (not isinstance(tk["section"], str) or len(tk["section"]) > 256):
                    out.append(f"{label}: tasks.{idx}.section: invalid string")
                    break
                if "meta" in tk:
                    _check_meta(tk["meta"], f"{label}: tasks.{idx}.meta", out)

    return [*out[:5], f"{label}: and {len(out) - 5} more schema errors"] if len(out) > 5 else out


def compile_schema(raw: object, label: str) -> Validator:
    """Validate that `raw` is a legal Draft 2020-12 JSON Schema object and return its validator."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    if not isinstance(raw, dict):
        raise Error(f"{label}: JSON Schema must be a JSON object")
    try:
        Draft202012Validator.check_schema(raw)
    except SchemaError as error:
        loc = ".".join(str(part) for part in error.schema_path)
        validator_name = error.validator or "schema"
        raise Error(f"{label}: invalid JSON Schema{f' at {loc}' if loc else ''} ({validator_name})") from error
    return Draft202012Validator(raw)


_VALIDATORS: dict[str, Validator] = {}
_VALIDATORS_LOCK = threading.Lock()


def load_validator(store: Store, path: str) -> Validator | None:
    """Load and compile optional `path` from `store`, returning `None` when absent."""
    try:
        raw = store.read(path, models.MAX_SCHEMA)
    except FileNotFoundError:
        return None
    key = digest(raw)
    with _VALIDATORS_LOCK:
        cached = _VALIDATORS.get(key)
    if cached is None:
        compiled = compile_schema(decode(raw, path), path)
        with _VALIDATORS_LOCK:
            if len(_VALIDATORS) >= 64:
                _VALIDATORS.pop(next(iter(_VALIDATORS)))
            _VALIDATORS[key] = cached = compiled
    return cached


def _err_key(error: ValidationError) -> tuple[Any, ...]:
    return (
        -len(error.absolute_path),
        error.validator == "anyOf",
        [str(p) for p in error.absolute_path],
        str(error.validator),
    )


def _describe_error(error: ValidationError) -> str:
    if error.validator == "anyOf" and isinstance(error.instance, (dict, list)):
        skip = {"type", "enum", "const"}
        if sub := [e for e in error.context if e.absolute_path != error.absolute_path or e.validator not in skip]:
            return _describe_error(min(sub, key=_err_key))
    if (
        error.validator == "anyOf"
        and type(error.instance) is int
        and (sub := [e for e in error.context if e.validator in {"minimum", "maximum"}])
    ):
        return _describe_error(min(sub, key=_err_key))
    loc = ".".join(str(part) for part in error.absolute_path)
    prefix, rule, val = (f"{loc}: " if loc else ""), str(error.validator), error.validator_value
    if error.schema is False:
        return f"{prefix}property is forbidden"
    if rule == "required" and isinstance(val, list):
        missing = [k for k in val if not isinstance(error.instance, dict) or k not in error.instance]
        return f"{prefix}missing required {', '.join(repr(k) for k in missing)}"
    if rule == "additionalProperties" and val is False and isinstance(error.instance, dict):
        allowed = set(error.schema.get("properties", {})) if isinstance(error.schema, dict) else set()
        return f"{prefix}unknown property {', '.join(repr(k) for k in sorted(set(error.instance) - allowed))}"
    if rule in {"minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems", "minProperties"}:
        return f"{prefix}violates {rule} {val}"
    not_msg = "value is reserved" if isinstance(val, dict) and "enum" in val else "conflicting properties"
    table = {
        "type": f"expected type {val}",
        "enum": f"must be one of {val}",
        "const": f"must equal {val!r}",
        "pattern": f"does not match pattern {val}",
        "uniqueItems": "items must be unique",
        "propertyNames": "invalid or reserved property name",
        "not": not_msg,
    }
    return f"{prefix}{table.get(rule, f'failed schema rule {rule}')}"


def validate_instance(validator: Validator, instance: object, label: str) -> list[str]:
    """Validate `instance` and return up to 5 sanitized diagnostic strings, failing closed on external `$ref`/regex errors."""
    try:
        errs = sorted(
            validator.iter_errors(instance),
            key=lambda e: ([str(p) for p in e.absolute_path], str(e.validator)),
        )
    except re.error:
        return [f"{label}: schema contains an invalid regular expression"]
    except MemoryError:
        raise
    except Exception as err:
        name = type(err).__name__
        if "Unresolvable" in name or "Referencing" in name:
            return [f"{label}: schema contains an unresolvable external $ref"]
        raise Error(f"{label}: schema validation failed ({name})") from err
    msgs = list(dict.fromkeys(f"{label}: {_describe_error(err)}" for err in errs))
    return [*msgs[:5], f"{label}: and {len(msgs) - 5} more schema errors"] if len(msgs) > 5 else msgs
