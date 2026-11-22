"""Draft 2020-12 JSON Schemas for bf.yaml and unified JSONL records, plus required per-source schemas."""

from __future__ import annotations

import copy
import threading
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

from bf import models
from bf.models import Error, InputError, decode, digest, encode
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
_ISO_DT = rf"^(?:{_CAL_DATE}(?:T{_TS_HMS})?)?$"
_OPT_TS = rf"^(?:{_CAL_DATE}T{_TS_HMS})?$"
_BOOL = {"type": "boolean"}
_NAME_REF = {"type": "string", "pattern": models.NAME}


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
_DURATION = {"anyOf": [_int(1, 315576000), _DUR_STR]}
_TIMEOUT = {"anyOf": [_int(1, 86400), _DUR_STR]}
_OVERLAP = {"anyOf": [_int(0, 315576000), _str(32, pat=models.DURATION_ZERO_PAT)]}
_ENV_MAP = _obj(extra=_str(4096), names=r"^[A-Z_][A-Z0-9_]*$", opt=True)
_TAG_ITEMS = _arr(_str(128, min_len=1, pat=r"^[^/\\\s]+$"), max_items=200, unique=True)
_META_REF = {"$ref": "#/$defs/Meta"}
_LOOP_KEY = {"pattern": models.NAME, "not": {"enum": ["events"]}}
_SRC_REF = {**_NAME_REF, "not": {"enum": sorted(models.RESERVED_SOURCES)}}
_EVENT_KIND_STR = _str(128, min_len=1, pat=models.EVENT_KIND_PAT)
_EVENT_VAL = {"anyOf": [_EVENT_KIND_STR, {"$ref": "#/$defs/EventMatch"}]}
_TRIG_SCALAR = {"anyOf": [_int(1, 315576000), _str(128, min_len=1)]}
_TRIG_ITEM = {
    "anyOf": [
        _TRIG_SCALAR,
        {"$ref": "#/$defs/EveryTrigger"},
        {"$ref": "#/$defs/CronTrigger"},
        {"$ref": "#/$defs/EventTrigger"},
    ]
}
_WHEN_VAL = {"anyOf": [{"const": "daemon"}, _TRIG_ITEM, _arr(_TRIG_ITEM, min_items=1, max_items=32)]}
_INGEST_VAL = {"anyOf": [_enum("upsert", "snapshot"), {"$ref": "#/$defs/Ingest"}]}

_LOOP_PROPS: dict[str, Any] = {
    "description": _desc(_str(512), "Short summary of what this loop does for humans and AI agents."),
    "when": _desc(_WHEN_VAL, "When the loop runs: 'daemon', a trigger, a list of triggers, or omit for manual."),
    "window": {"$ref": "#/$defs/Window"},
    "run": {"$ref": "#/$defs/Argv"},
    "ingest": _desc(
        _INGEST_VAL, "Ingest stdout JSONL into memories/ or usages/ ('upsert'|'snapshot' or {source?, target?, mode?})."
    ),
    "emit": _desc(_EVENT_KIND_STR, "Default event kind emitted from stdout JSONL to logs/events.jsonl."),
    "timeout": _desc(_TIMEOUT, "Max cycle runtime before terminating the process group.", default="5m"),
    "env": _desc(_ENV_MAP, "Loop environment variable overrides."),
    "enabled": _desc(_BOOL, "Enable automatic triggers and daemon supervision.", default=True),
}
_DEFAULTS_PROPS: dict[str, Any] = {
    "window": {"$ref": "#/$defs/Window"},
    "timeout": _desc(_TIMEOUT, "Default cycle timeout for loops.", default="5m"),
}
_EMIT_SET = {"type": "object", "required": ["emit"]}
_IS_DAEMON = {"type": "object", "required": ["when"], "properties": {"when": {"const": "daemon"}}}
_NO_CYCLE = {"not": {"anyOf": [{"required": [k]} for k in ("window", "ingest", "timeout")]}}
_LOOP_RULES = [{"if": _EMIT_SET, "then": {"not": {"required": ["ingest"]}}}, {"if": _IS_DAEMON, "then": _NO_CYCLE}]

_BRAIN_PROPS: dict[str, Any] = {
    "version": _desc({"type": "integer", "const": models.FORMAT}, "Brain configuration format version."),
    "name": _desc(_NAME_REF, "Stable lowercase identifier for this brain."),
    "description": _desc(_str(1024), "Short summary of this brain's scope for humans and AI agents."),
    "timezone": _desc(_str(128, min_len=1), "IANA timezone for cron schedules.", default="UTC"),
    "notes": _desc(
        _arr(_SRC_REF, min_items=1, max_items=32, unique=True),
        "Top-level Markdown note folders compiled into memories/<dir>.jsonl.",
        default=list(models.DEFAULT_NOTES),
    ),
    "brains": _desc(_obj(extra=_str(4096, min_len=1), names=models.NAME, opt=True), "Sibling brain aliases for bf://."),
    "concurrency": _desc(_int(1, 32), "Max concurrent cycle loops in bf watch and bf run --due (1..32).", default=4),
    "env": _desc(_ENV_MAP, "Shared environment variables for loops."),
    "defaults": {"$ref": "#/$defs/Defaults"},
    "loops": _desc(_obj(extra={"$ref": "#/$defs/Loop"}, names=_LOOP_KEY, opt=True), "Unified loop definitions."),
}
_WIN_PROPS = {
    "backfill": _desc(_DURATION, "Initial BF_START window on first run (now - backfill).", default="1d"),
    "overlap": _desc(_OVERLAP, "Rewind on later runs (last_anchor - overlap).", default=0),
}
_BRAIN_DEFS = {
    "Argv": _desc(
        _arr(_str(4096, min_len=1), min_items=1, max_items=64), "Literal argv without a shell.", title="Argv"
    ),
    "CronTrigger": _obj(
        {"cron": _desc(_str(128, min_len=1), "5-field cron expression or @macro."), **_WIN_PROPS},
        req=("cron",),
        title="CronTrigger",
        desc="Cron schedule trigger with optional per-trigger window.",
    ),
    "Defaults": _obj(_DEFAULTS_PROPS, opt=True, title="Defaults", desc="Shared loop defaults (timeout, window)."),
    "EventMatch": _obj(
        {
            "kind": _desc(_str(128, min_len=1), "Exact event kind to match (e.g. records.ingested)."),
            "source": _desc(_str(128, min_len=1), "Exact event source to match (loop name or 'bf')."),
            "filter": _desc(_str(2048, min_len=1), "Isolated jq boolean expression evaluated per event."),
        },
        min_props=1,
        title="EventMatch",
        desc="Event matcher against logs/events.jsonl.",
    ),
    "EventTrigger": _obj(
        {"event": _desc(_EVENT_VAL, "Event kind string or matcher object.")},
        req=("event",),
        title="EventTrigger",
        desc="Event subscription trigger.",
    ),
    "EveryTrigger": _obj(
        {"every": _desc(_DURATION, "Repeat interval duration (e.g. 15m, 1h, 1d)."), **_WIN_PROPS},
        req=("every",),
        title="EveryTrigger",
        desc="Interval schedule trigger with optional per-trigger window.",
    ),
    "Ingest": _obj(
        {
            "source": _desc(_SRC_REF, "Target <target>/<source>.jsonl stem (defaults to loop name)."),
            "target": _desc(_enum("memories", "usages"), "Record bucket: memories or usages.", default="memories"),
            "mode": _desc(_enum("upsert", "snapshot"), "Write mode: upsert or snapshot.", default="upsert"),
        },
        min_props=1,
        title="Ingest",
        desc="Explicit record ingestion target.",
    ),
    "Loop": {**_obj(_LOOP_PROPS, title="Loop", desc="One loop entry."), "allOf": _LOOP_RULES},
    "Window": _obj(_WIN_PROPS, min_props=1, title="Window", desc="Default time window for BF_START/BF_END."),
}
BF_SCHEMA = _doc(
    "bf",
    "Config",
    "Brain Framework configuration and loop orchestration (bf.yaml).",
    {**_obj(_BRAIN_PROPS, req=("version", "name")), "$defs": _BRAIN_DEFS},
)

_STALE_ANY = [_int(1, 36500), _DUR_STR, {"type": "string", "minLength": 10, "pattern": _ISO_DT}]
_RECORD_PROPS: dict[str, Any] = {
    "id": _desc(_str(4096, min_len=1), "Stable source-local record identifier (required for memory/usage)."),
    "title": _desc(_str(4096, min_len=1), "Human-readable title."),
    "entity": _desc(_str(4096, min_len=1), "Canonical entity URI or source:id alias."),
    "type": _desc(_str(128, min_len=1), "Domain subtype (e.g. guide, project, issue, session)."),
    "status": _desc(_str(128, min_len=1), "Lifecycle status (e.g. draft, stable, deprecated)."),
    "summary": _desc(_str(16384), "Short summary of the note or record."),
    "ref": _desc(_str(4200, min_len=1), "Universal brain-local reference (note path or source:id)."),
    "source": _desc(_NAME_REF, "Owning source name (<bucket>/<source>.jsonl)."),
    "kind": _desc(_enum("note", "memory", "usage"), "Top-level record kind (note, memory, or usage)."),
    "text": _desc(_str(4 << 20), "Plain or Markdown body text."),
    "url": _desc(_str(8192), "Canonical external URL."),
    "time": _desc({"type": "string", "pattern": _OPT_TS}, "Event or creation UTC/offset ISO 8601 timestamp."),
    "updated": _desc({"type": "string", "pattern": _ISO_DT}, "Last-updated ISO date (YYYY-MM-DD) or timestamp."),
    "stale_after": _desc({"anyOf": _STALE_ANY}, "Review freshness threshold (days, duration, or ISO date)."),
    "tags": _desc(_TAG_ITEMS, "Unique tags without whitespace or slashes."),
    "aliases": _desc(_arr(_str(1024, min_len=1), max_items=200), "Alternate identifiers resolved by bf read/review."),
    "attributes": _desc({"type": "object"}, "Freeform sensor-specific domain attributes."),
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
    "if": {"required": ["kind"], "properties": {"kind": {"enum": ["memory", "usage"]}}},
    "then": {"required": ["id"]},
}
RECORD_SCHEMA = _doc(
    "record",
    "Record",
    "Unified JSON record schema enforced on compiled notes and ingested records in memories/*.jsonl and usages/*.jsonl.",
    {**_obj(_RECORD_PROPS, req=("ref", "source", "kind", "title"), extra=True), **_RECORD_IF, "$defs": _RECORD_DEFS},
)

_SCHEMAS: dict[Kind, dict[str, Any]] = {"bf": BF_SCHEMA, "record": RECORD_SCHEMA}


def starter_note_schema(folder: str) -> dict[str, Any]:
    """Return a minimal Draft 2020-12 OKF note schema for `schemas/<folder>.json`."""
    label = folder.replace("-", " ").title()
    return {
        "$schema": _DRAFT,
        "title": f"{label}Note",
        "description": f"Domain schema for authored Markdown notes in {folder}/.",
        "type": "object",
        "required": ["type", "status"],
        "additionalProperties": True,
        "properties": {
            "type": {"type": "string", "minLength": 1, "maxLength": 128},
            "status": {"type": "string", "enum": ["draft", "stable", "deprecated"]},
        },
    }


def starter_usages_schema() -> dict[str, Any]:
    """Return a Draft 2020-12 FinOps/prompt telemetry schema for `schemas/usages.json`."""
    return {
        "$schema": _DRAFT,
        "title": "UsageRecord",
        "description": "Shared domain schema for token, FinOps, and prompt usage records in usages/*.jsonl.",
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "harness": {"type": "string", "minLength": 1, "maxLength": 128},
            "model": {"type": "string", "minLength": 1, "maxLength": 256},
            "session_id": {"type": "string", "minLength": 1, "maxLength": 256},
            "input_tokens": {"type": "integer", "minimum": 0},
            "output_tokens": {"type": "integer", "minimum": 0},
            "cached_tokens": {"type": "integer", "minimum": 0},
            "cost_usd": {"type": "number", "minimum": 0},
            "duration_ms": {"type": "integer", "minimum": 0},
            "prompts": {"type": "integer", "minimum": 0},
        },
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


@cache
def base_record_validator() -> Validator:
    """Return the unconditionally enforced bundled `RECORD_SCHEMA` validator."""
    return compile_schema(RECORD_SCHEMA, "record.schema.json")


_VALIDATORS: dict[str, Validator] = {}
_VALIDATORS_LOCK = threading.Lock()


def load_validator(store: Store, path: str, fallback: dict[str, Any] | None = None) -> Validator | None:
    """Load and compile `path` from `store`, falling back to `fallback` when the file is absent."""
    raw: bytes | None = None
    try:
        raw = store.read(path, models.MAX_SCHEMA)
    except FileNotFoundError:
        if fallback is None:
            return None
    key = f"fallback:{digest(encode(fallback))}" if raw is None else digest(raw)
    with _VALIDATORS_LOCK:
        cached = _VALIDATORS.get(key)
    if cached is None:
        compiled = compile_schema(fallback if raw is None else decode(raw, path), path)
        with _VALIDATORS_LOCK:
            if len(_VALIDATORS) >= 64:
                _VALIDATORS.pop(next(iter(_VALIDATORS)))
            _VALIDATORS[key] = cached = compiled
    return cached


def require_validator(store: Store, source: str) -> Validator:
    """Require and compile `schemas/<source>.json`, failing closed when absent."""
    path = f"schemas/{source}.json"
    if (validator := load_validator(store, path)) is None:
        raise Error(f"{path}: missing required schema for source {source!r}")
    return validator


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
    """Validate `instance` and return up to 5 sanitized diagnostic strings without echoing values."""
    errs = sorted(validator.iter_errors(instance), key=lambda e: ([str(p) for p in e.absolute_path], str(e.validator)))
    msgs = list(dict.fromkeys(f"{label}: {_describe_error(err)}" for err in errs))
    return [*msgs[:5], f"{label}: and {len(msgs) - 5} more schema errors"] if len(msgs) > 5 else msgs
