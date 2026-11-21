"""Draft 2020-12 JSON Schemas for bf.yaml, unified JSON records, concepts, and evals."""

from __future__ import annotations

import copy
import threading
from typing import TYPE_CHECKING, Any, Literal

from bf import models
from bf.models import Error, InputError, decode, digest, encode
from bf.storage import Store

if TYPE_CHECKING:
    from jsonschema.exceptions import ValidationError
    from jsonschema.protocols import Validator

Kind = Literal["brain", "record", "concepts", "evals"]
_DRAFT, _BASE = "https://json-schema.org/draft/2020-12/schema", "https://fmind.github.io/brain-framework"
_CAL_DATE = (
    r"[0-9]{4}-(?:(?:0[13578]|1[02])-(?:0[1-9]|[12][0-9]|3[01])"
    r"|(?:0[469]|11)-(?:0[1-9]|[12][0-9]|30)|02-(?:0[1-9]|[12][0-9]))"
)
_TS_HMS = r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
_ISO_DT = rf"^(?:{_CAL_DATE}(?:T{_TS_HMS})?)?$"
_OPT_TS = rf"^(?:{_CAL_DATE}T{_TS_HMS})?$"
_BOOL, _NAME_REF = {"type": "boolean"}, {"type": "string", "pattern": models.NAME}


def _s(max_len: int = 4096, min_len: int = 0, pat: str = "") -> dict[str, Any]:
    ex = {k: v for k, v in (("minLength", min_len), ("pattern", pat)) if v}
    return {"type": "string", "maxLength": max_len, **ex}


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _i(min_val: int = 0, max_val: int | None = None) -> dict[str, Any]:
    return {"type": "integer", "minimum": min_val, **({"maximum": max_val} if max_val is not None else {})}


def _arr(items: dict[str, Any], *, max_items: int = 0, min_items: int = 0, unique: bool = False) -> dict[str, Any]:
    ex = {k: v for k, v in (("minItems", min_items), ("maxItems", max_items), ("uniqueItems", unique)) if v}
    return {"type": "array", "items": items, **ex}


def _d(schema: dict[str, Any], desc: str, **extra: Any) -> dict[str, Any]:
    return {**schema, "description": desc, **extra}


def _obj(props: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    tp: Any = ["object", "null"] if kw.get("opt") else "object"
    out: dict[str, Any] = {"type": tp, "additionalProperties": kw.get("extra", False)}
    ex = (("title", kw.get("title")), ("description", kw.get("desc")), ("minProperties", kw.get("min_props")))
    out.update({k: v for k, v in ex if v})
    if kw.get("req"):
        out["required"] = list(kw["req"])
    if n := kw.get("names"):
        out["propertyNames"] = n if isinstance(n, dict) else {"pattern": n}
    return {**out, "properties": props} if props is not None else out


def _doc(slug: str, title: str, desc: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"$schema": _DRAFT, "$id": f"{_BASE}/{slug}.schema.json", "title": title, "description": desc, **body}


_DUR_S = _s(32, pat=models.DURATION_PAT)
_DURATION, _TIMEOUT = {"anyOf": [_i(1, 315576000), _DUR_S]}, {"anyOf": [_i(1, 86400), _DUR_S]}
_OVERLAP = {"anyOf": [_i(0, 315576000), _s(32, pat=models.DURATION_ZERO_PAT)]}
_WI = {"anyOf": [{"type": "number", "minimum": 0.5, "maximum": 3600}, _DUR_S]}
_ENV_MAP = _obj(extra=_s(4096), names=r"^[A-Z_][A-Z0-9_]*$", opt=True)
_TAG_ITEMS = _arr(_s(128, min_len=1, pat=r"^[^/\\\s]+$"), max_items=200, unique=True)
_HOOK_ENUM, _ARGV_REF, _META_REF = _enum("pre-commit", "pre-push"), {"$ref": "#/$defs/Argv"}, {"$ref": "#/$defs/Meta"}
_LOOP_KEY = {"pattern": models.NAME, "not": {"enum": ["events", "cache"]}}
_SRC_REF = {**_NAME_REF, "not": {"enum": sorted(models.RESERVED_SOURCES)}}
_SCHED_ITEM = {"anyOf": [_i(1, 315576000), _s(128, min_len=1)]}
_SCHED_ANY = {"anyOf": [_SCHED_ITEM, _arr(_SCHED_ITEM, min_items=1, max_items=16)]}
_HOOK_ANY = {"anyOf": [_HOOK_ENUM, _arr(_HOOK_ENUM, min_items=1, max_items=2, unique=True)]}
_EVENT_KIND_S = _s(128, min_len=1, pat=models.EVENT_KIND_PAT)
_EVENT_ITEM = {"anyOf": [_EVENT_KIND_S, {"$ref": "#/$defs/EventMatch"}]}
_EVENTS_ANY = {"anyOf": [_EVENT_ITEM, _arr(_EVENT_ITEM, min_items=1, max_items=32)]}
_ON_VAL = {"anyOf": [{"const": "daemon"}, {"$ref": "#/$defs/Trigger"}]}
_ING_VAL = {"anyOf": [_enum("upsert", "snapshot"), {"$ref": "#/$defs/Ingest"}]}

_LOOP_PROPS: dict[str, Any] = {
    "description": _d(_s(512), "Short summary of what this loop does for humans and AI agents."),
    "on": _d(_ON_VAL, "When the loop runs: 'daemon' for continuous supervision, a trigger map, or omit for manual."),
    "window": {"$ref": "#/$defs/Window"},
    "run": _ARGV_REF,
    "ingest": _d(_ING_VAL, "Ingest stdout JSON into memories/<loop>/ ('upsert'|'snapshot') or {source, mode}."),
    "emit": _d(_EVENT_KIND_S, "Default event kind emitted from stdout JSONL to .bf/events.jsonl (e.g. loop.event)."),
    "timeout": _d(_TIMEOUT, "Max cycle runtime before terminating the process group.", default="5m"),
    "requires": _d(_arr(_s(256, min_len=1), max_items=32, unique=True), "Required CLI executables verified on PATH."),
    "env": _d(_ENV_MAP, "Loop environment overrides (expands {{brain}}, {{home}}, {{state}})."),
    "enabled": _d(_BOOL, "Enable automatic triggers and daemon supervision.", default=True),
}
_EMIT_SET = {"required": ["emit"]}
_IS_DAEMON = {"required": ["on"], "properties": {"on": {"const": "daemon"}}}
_NO_CYCLE = {"not": {"anyOf": [{"required": [k]} for k in ("window", "ingest", "timeout")]}}
_LOOP_RULES = [{"if": _EMIT_SET, "then": {"not": {"required": ["ingest"]}}}, {"if": _IS_DAEMON, "then": _NO_CYCLE}]
_WATCH_PROPS: dict[str, Any] = {
    "interval": _d(_WI, "Schedule polling interval for bf watch (500ms..1h).", default="5s"),
    "concurrency": _d(_i(1, 32), "Max concurrent cycle loops in bf watch and bf run --due (1..32).", default=4),
}
_B_MAP = _obj(extra=_s(4096, min_len=1), names=models.NAME, opt=True)
_RES_LOOP_PAT = "^(?:" + "|".join(sorted(models.RESERVED_SOURCES)) + ")$"
_RES_LOOP_SCHEMA = {
    "$ref": "#/$defs/Loop",
    "not": {"required": ["ingest"], "properties": {"ingest": {"type": "string"}}},
}
_LOOPS_MAP = {
    **_obj(extra={"$ref": "#/$defs/Loop"}, names=_LOOP_KEY, opt=True),
    "patternProperties": {_RES_LOOP_PAT: _RES_LOOP_SCHEMA},
}
_BRAIN_PROPS: dict[str, Any] = {
    "version": _d({"type": "integer", "const": models.FORMAT}, "Brain configuration format version."),
    "name": _d(_NAME_REF, "Stable lowercase identifier for this brain."),
    "description": _d(_s(1024), "Short summary of this brain's scope for humans and AI agents."),
    "timezone": _d(_s(128, min_len=1), "IANA timezone for cron schedules and new note dates.", default="UTC"),
    "brains": _d(_B_MAP, "Sibling brain aliases for bf:// links."),
    "env": _d(_ENV_MAP, "Base environment variables for loops (expands {{brain}}, {{home}}, {{state}})."),
    "loops": _d(_LOOPS_MAP, "Unified loop definitions."),
    "watch": _d(_obj(_WATCH_PROPS, opt=True), "Polling interval and concurrency for bf watch and bf run --due."),
}
_TRIG_PROPS = {
    "schedules": _d(_SCHED_ANY, "Duration (1h, 1d), @macro, 5-field cron, or a list of schedules."),
    "events": _d(_EVENTS_ANY, "Event kind, matcher object, or list subscribed from .bf/events.jsonl."),
    "hooks": _d(_HOOK_ANY, "Git hook or list of hooks that trigger this loop (pre-commit, pre-push)."),
}
_WIN_PROPS = {
    "backfill": _d(_DURATION, "Initial {{start}} window on first run (now - backfill).", default="1d"),
    "overlap": _d(_OVERLAP, "Rewind on later runs (last_anchor - overlap).", default=0),
}
_INGEST_PROPS = {
    "source": _d(_SRC_REF, "Target memories/<source>/ folder."),
    "mode": _d(_enum("upsert", "snapshot"), "Write mode: upsert or snapshot.", default="upsert"),
}
_EVENT_PROPS = {
    "kind": _d(_s(128, min_len=1), "Exact event kind to match (e.g. records.ingested)."),
    "source": _d(_s(128, min_len=1), "Exact event source to match (loop name or 'bf')."),
    "filter": _d(_s(2048, min_len=1), "Isolated jq boolean expression evaluated per event."),
}
_BRAIN_DEFS = {
    "Argv": _d(_arr(_s(4096, min_len=1), min_items=1, max_items=64), "Literal argv without a shell.", title="Argv"),
    "EventMatch": _obj(_EVENT_PROPS, min_props=1, title="EventMatch", desc="Event matcher against .bf/events.jsonl."),
    "Ingest": _obj(_INGEST_PROPS, req=("source",), title="Ingest", desc="Explicit memory ingestion target."),
    "Loop": {**_obj(_LOOP_PROPS, req=("run",), title="Loop", desc="One loop entry."), "allOf": _LOOP_RULES},
    "Trigger": _obj(_TRIG_PROPS, min_props=1, title="Trigger", desc="When a cycle loop runs."),
    "Window": _obj(_WIN_PROPS, min_props=1, title="Window", desc="Time window for {{start}}/{{end}}."),
}
_B_BODY = {**_obj(_BRAIN_PROPS, req=("version", "name")), "$defs": _BRAIN_DEFS}
BRAIN_SCHEMA = _doc("bf", "Config", "Brain Framework configuration and loop orchestration (bf.yaml).", _B_BODY)

_SOURCES_PROP = _arr(_obj({"resource": _s(8192, min_len=1)}, req=("resource",), extra=True), max_items=200)
_STALE_ANY = [_i(1, 36500), _DUR_S, {"type": "string", "minLength": 10, "pattern": _ISO_DT}]
_L_ARR = _arr({"$ref": "#/$defs/Link"}, max_items=2000)
_ATTRS_T = {"type": "object", "propertyNames": _s(256, min_len=1), "additionalProperties": _META_REF}
_RECORD_PROPS: dict[str, Any] = {
    "id": _d(_s(4096, min_len=1), "Stable source-local record identifier (required for kind='memory')."),
    "title": _d(_s(4096, min_len=1), "Human-readable title."),
    "entity": _d(_s(4096, min_len=1), "Canonical entity URI or source:id alias."),
    "type": _d(_s(128, min_len=1), "Domain subtype (e.g. guide, project, issue)."),
    "status": _d(_s(128, min_len=1), "Lifecycle status (e.g. draft, stable, deprecated)."),
    "summary": _d(_s(16384), "Short summary of the note or record."),
    "description": _d(_s(16384), "Optional longer description."),
    "ref": _d(_s(4200, min_len=1), "Stable brain-local reference (note path or source:id)."),
    "source": _d(_NAME_REF, "Owning source folder name under memories/."),
    "kind": _d(_enum("project", "concept", "action", "memory"), "Top-level record category."),
    "text": _d(_s(4 << 20), "Plain or Markdown body text."),
    "url": _d(_s(8192), "Canonical external URL."),
    "time": _d({"type": "string", "pattern": _OPT_TS}, "Event or creation UTC/offset ISO 8601 timestamp."),
    "updated": _d({"type": "string", "pattern": _ISO_DT}, "Last-updated ISO date (YYYY-MM-DD) or timestamp."),
    "stale_after": _d({"anyOf": _STALE_ANY}, "Review freshness threshold (days, duration, or ISO date)."),
    "tags": _d(_TAG_ITEMS, "Unique tags without whitespace or slashes."),
    "sources": _d(_SOURCES_PROP, "Cited source objects with resource URI and optional retrieved date."),
    "aliases": _d(_arr(_s(1024, min_len=1), max_items=200), "Alternate identifiers resolved by bf read/review."),
    "attributes": _d({"type": "object"}, "Freeform sensor-specific domain attributes."),
    "headings": _d(_arr({"$ref": "#/$defs/Heading"}), "Compiled Markdown headings with attr_list metadata."),
    "links": _d(_L_ARR, "Normalized outgoing note, record, brain, or URL links."),
    "elements": _d(_arr({"$ref": "#/$defs/Element"}), "Compiled Markdown blocks/spans with attr_list metadata."),
    "tasks": _d(_arr({"$ref": "#/$defs/Task"}), "Compiled Markdown checklist tasks."),
    "anchors": _d(_ATTRS_T, "Keyed map of #anchor id -> non-empty attr_list metadata."),
    "sha256": _d({"type": "string", "pattern": r"^[0-9a-f]{64}$"}, "SHA-256 hex digest of source Markdown bytes."),
}
_LINK_TO = _d(_s(8192, min_len=1), "Note path, memories/<source>/<sha256>.json, bf:// URI, or URL.")
_LK_K = _enum("note", "record", "brain", "url")
_LINK_P = {
    "id": _s(256, min_len=1),
    "to": _LINK_TO,
    "kind": _LK_K,
    "anchor": _s(2048),
    "meta": _META_REF,
    "section": _s(256),
}
_HEAD_P = {"id": _s(256, min_len=1), "title": _s(4096), "level": _i(1, 6), "meta": _META_REF}
_ELEM_P = {"tag": _s(64, min_len=1), "id": _s(256), "text": _s(16384), "section": _s(256), "meta": _META_REF}
_TASK_P = {"id": _s(256, min_len=1), "text": _s(4096, min_len=1), "done": _BOOL, "section": _s(256), "meta": _META_REF}
_R_DEFS = {
    "Meta": _obj(extra=_s(4096), title="Meta", names=r"^[A-Za-z_][A-Za-z0-9_.-]*$"),
    "Heading": _obj(_HEAD_P, req=("id", "title", "level"), title="Heading"),
    "Link": _obj(_LINK_P, req=("to", "kind"), title="Link"),
    "Element": _obj(_ELEM_P, req=("tag",), title="Element"),
    "Task": _obj(_TASK_P, req=("text", "done"), title="Task"),
}
_R_IF = {"if": {"required": ["kind"], "properties": {"kind": {"const": "memory"}}}, "then": {"required": ["id"]}}
_R_BODY = {**_obj(_RECORD_PROPS, req=("ref", "source", "kind", "title"), extra=True), **_R_IF, "$defs": _R_DEFS}
_R_DESC = "Unified JSON record schema enforced on compiled Markdown notes and ingested sensor records in memories/."
RECORD_SCHEMA = _doc("record", "Record", _R_DESC, _R_BODY)
_CONCEPT_PROPS = {
    "type": _d({"type": "string", "pattern": r"^[a-z][a-z0-9-]{0,63}$"}, "Concept category slug."),
    "status": _d(_enum("draft", "stable", "deprecated"), "Concept maturity status."),
    "summary": _d(_s(16384), "Short concept summary."),
    "updated": _d({"type": "string", "pattern": rf"^{_CAL_DATE}$"}, "Last-updated calendar date (YYYY-MM-DD)."),
    "tags": _d(_TAG_ITEMS, "Unique concept tags."),
    "sources": _d(_SOURCES_PROP, "Supporting evidence sources."),
}
_C_DESC = "User-customizable schema enforced on concept records compiled from concepts/ (schemas/concepts.json)."
CONCEPTS_SCHEMA = _doc("concepts", "Concept", _C_DESC, _obj(_CONCEPT_PROPS, req=("type", "status"), extra=True))
_S_ARR = _arr(_s(4096, min_len=1))
_CASE_PROPS: dict[str, Any] = {
    "name": _d(_s(256, min_len=1), "Unique eval case name."),
    "engine": _d(_enum("rg", "jq", "read"), "Retrieval engine for this case.", default="rg"),
    "query": _d(_s(8192, min_len=1), "Search regex, jq filter, or bf read target."),
    "scope": _d(_s(1024), "Optional note, source, or memories/ path restriction."),
    "expect_refs": _d(_S_ARR, "Refs that must appear in results."),
    "forbid_refs": _d(_S_ARR, "Refs that must not appear in results."),
    "expect_text": _d(_S_ARR, "Case-insensitive substrings that must appear in results."),
    "min_count": _d(_i(0), "Minimum expected result count."),
    "max_count": _d(_i(0), "Maximum expected result count."),
    "expect_equals": {"description": "Exact JSON value expected from the query items list."},
}
_EVAL_PROPS = {
    "version": _d({"type": "integer", "const": models.EVAL_FORMAT}, "Eval suite format version."),
    "cases": _d(_arr(_obj(_CASE_PROPS, req=("name", "query")), max_items=500), "Deterministic query test cases."),
}
_EV_B = _obj(_EVAL_PROPS, req=("version", "cases"))
EVALS_SCHEMA = _doc("evals", "Eval", "Deterministic query evaluations in evals/.", _EV_B)
_SCHEMAS: dict[Kind, dict[str, Any]] = {"brain": BRAIN_SCHEMA, "record": RECORD_SCHEMA} | {
    "concepts": CONCEPTS_SCHEMA,
    "evals": EVALS_SCHEMA,
}


def document(kind: Kind) -> dict[str, Any]:
    """Return a deep copy of the canonical JSON Schema for `kind`."""
    return copy.deepcopy(_SCHEMAS[kind])


def get_schema(name: str) -> dict[str, Any]:
    """Return a deep copy of a built-in schema by CLI name (`bf`, `record`, `concepts`, `evals`)."""
    key = name.strip().lower().removesuffix(".json").removesuffix(".schema")
    if (kind := "brain" if key == "bf" else key) not in _SCHEMAS:
        raise InputError(f"unknown schema {name!r}; choose bf, record, concepts, or evals")
    return document(kind)


def export_schemas() -> dict[str, dict[str, Any]]:
    """Return all built-in schemas keyed by workspace name (`bf`, `record`, `concepts`, `evals`)."""
    return {("bf" if k == "brain" else k): document(k) for k in ("brain", "record", "concepts", "evals")}


def schema_digest(raw: bytes | dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of raw schema bytes or a schema mapping."""
    return digest(raw if isinstance(raw, bytes) else encode(raw))


def schemas_digests(store: Store, *, skipped: list[str] | None = None) -> dict[str, str]:
    """Return `{schema_key: sha256, ..., '__all__': sha256}` across built-in and `schemas/*.json` files."""
    out = {k: schema_digest(doc) for k, doc in export_schemas().items()}
    for path in store.files("schemas", skipped=skipped):
        if path.count("/") == 1 and path.endswith(".json"):
            stem = path.removeprefix("schemas/").removesuffix(".json")
            out["bf" if stem == "bf.schema" else stem] = digest(store.read(path, models.MAX_SCHEMA))
    out["__all__"] = digest(encode(out))
    return out


def compile_schema(raw: object, label: str) -> Validator:
    """Validate that `raw` is a legal Draft 2020-12 JSON Schema object and return its validator."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    if not isinstance(raw, dict):
        raise Error(f"{label}: JSON Schema must be a JSON object")
    try:
        Draft202012Validator.check_schema(raw)
    except SchemaError as error:
        loc, v = ".".join(str(part) for part in error.schema_path), error.validator or "schema"
        raise Error(f"{label}: invalid JSON Schema{f' at {loc}' if loc else ''} ({v})") from error
    return Draft202012Validator(raw)


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


def _err_key(e: ValidationError) -> tuple[Any, ...]:
    return (-len(e.absolute_path), e.validator == "anyOf", [str(p) for p in e.absolute_path], str(e.validator))


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
    not_msg = (
        "value is reserved"
        if isinstance(val, dict) and "enum" in val
        else (
            "reserved loop name cannot use shorthand ingest"
            if isinstance(val, dict) and val.get("required") == ["ingest"] and "properties" in val
            else "conflicting properties"
        )
    )
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
