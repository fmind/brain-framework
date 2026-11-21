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


def _opt(schema: dict[str, Any]) -> dict[str, Any]:
    return {**schema, "type": ["object", "null"]}


def _obj(props: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "additionalProperties": kw.get("extra", False)}
    ex = (("title", kw.get("title")), ("required", list(kw["req"]) if kw.get("req") else None))
    out.update({k: v for k, v in ex if v})
    if kw.get("names"):
        out["propertyNames"] = {"pattern": kw["names"]}
    return {**out, "properties": props} if props is not None else out


def _doc(slug: str, title: str, desc: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"$schema": _DRAFT, "$id": f"{_BASE}/{slug}.schema.json", "title": title, "description": desc, **body}


_DUR_S = _s(32, pat=models.DURATION_PAT)
_DURATION = {"anyOf": [_i(1, 315576000), _DUR_S]}
_OVERLAP = {"anyOf": [_i(0, 315576000), _s(32, pat=models.DURATION_ZERO_PAT)]}
_TIMEOUT = {"anyOf": [_i(1, 86400), _DUR_S]}
_WI = {"anyOf": [{"type": "number", "minimum": 0.5, "maximum": 3600}, _DUR_S]}
_WD = {"anyOf": [{"type": "number", "minimum": 0.05, "maximum": 60}, _DUR_S]}
_ENV_MAP = _obj(extra=_s(4096), names=r"^[A-Z_][A-Z0-9_]*$")
_TAG_ITEMS = _arr(_s(128, min_len=1, pat=r"^[^/\\\s]+$"), max_items=200, unique=True)
_HOOK_ENUM, _ARGV_REF, _META_REF = _enum("pre-commit", "pre-push"), {"$ref": "#/$defs/Argv"}, {"$ref": "#/$defs/Meta"}
_UNIT_BASE = {"enabled": _BOOL, "requires": _arr(_s(256, min_len=1), max_items=32, unique=True), "env": _ENV_MAP}
_SCHED_ITEM = {"anyOf": [_i(1, 315576000), _s(128, min_len=1)]}
_SCHED_ANY = {"anyOf": [_SCHED_ITEM, _arr(_SCHED_ITEM, min_items=1, max_items=16)]}
_HOOK_ANY = {"anyOf": [_HOOK_ENUM, _arr(_HOOK_ENUM, min_items=1, max_items=2, unique=True)]}
_EMIT_VAL = {"anyOf": [_BOOL, _s(128, min_len=1, pat=models.EVENT_KIND_PAT)]}

_LOOP_PROPS: dict[str, Any] = {
    "run": _ARGV_REF,
    "daemon": _BOOL,
    "input": _s(1024, min_len=1),
    "action": {"$ref": "#/$defs/Action"},
    "ingest": {"anyOf": [_NAME_REF, {"$ref": "#/$defs/Ingest"}]},
    "emit": _EMIT_VAL,
    "schedule": _SCHED_ANY,
    "events": _arr({"$ref": "#/$defs/EventMatch"}, min_items=1, max_items=32),
    "hook": _HOOK_ANY,
    "lookback": _DURATION,
    "overlap": _OVERLAP,
    "timeout": _TIMEOUT,
    **_UNIT_BASE,
}
_CYCLE_ONLY = ("schedule", "events", "hook", "input", "ingest", "action", "lookback", "overlap", "timeout")
_EMIT_SET = {"required": ["emit"], "properties": {"emit": {"not": {"const": False}}}}
_NO_ING_ACT = {"not": {"anyOf": [{"required": ["ingest"]}, {"required": ["action"]}]}}
_IS_DAEMON = {"required": ["daemon"], "properties": {"daemon": {"const": True}}}
_NO_CYCLE = {"not": {"anyOf": [{"required": [k]} for k in _CYCLE_ONLY]}}
_LOOP_RULES = [
    {"not": {"required": ["ingest", "action"]}},
    {"if": _EMIT_SET, "then": _NO_ING_ACT},
    {"if": _IS_DAEMON, "then": _NO_CYCLE},
]
_BRAIN_PROPS: dict[str, Any] = {
    "version": {"type": "integer", "const": models.FORMAT, "description": "Brain configuration format version."},
    "name": {**_NAME_REF, "description": "Stable lowercase identifier for this brain."},
    "timezone": _s(128, min_len=1),
    "brains": _opt(_obj(extra=_s(4096, min_len=1), names=models.NAME)),
    "env": _opt(_ENV_MAP),
    "loops": _opt(_obj(extra={"$ref": "#/$defs/Loop"}, names=models.NAME)),
    "watch": _opt(_obj({"interval": _WI, "debounce": _WD, "concurrency": _i(1, 32)})),
}
_INGEST_PROPS = {"source": _NAME_REF, "mode": _enum("upsert", "snapshot"), "allow_removal": _BOOL}
_EVENT_PROPS = {"kind": _s(128, min_len=1), "source": _s(128, min_len=1), "filter": _s(2048, min_len=1)}
_BRAIN_DEFS = {
    "Action": _obj({"keep": _i(1, 1000)}, req=("keep",), title="Action"),
    "Argv": {"title": "Argv", **_arr(_s(4096, min_len=1), min_items=1, max_items=64)},
    "EventMatch": _obj(_EVENT_PROPS, title="EventMatch"),
    "Ingest": _obj(_INGEST_PROPS, req=("source",), title="Ingest"),
    "Loop": {**_obj(_LOOP_PROPS, req=("run",), title="Loop"), "allOf": _LOOP_RULES},
}
BRAIN_SCHEMA = _doc(
    "bf",
    "Config",
    "Brain Framework configuration and loop orchestration (bf.yaml).",
    {**_obj(_BRAIN_PROPS, req=("version", "name")), "$defs": _BRAIN_DEFS},
)

_SOURCES_PROP = _arr(_obj({"resource": _s(8192, min_len=1)}, req=("resource",), extra=True), max_items=200)
_STALE_ANY = [_i(1, 36500), _DUR_S, {"type": "string", "minLength": 10, "pattern": _ISO_DT}]
_RECORD_PROPS: dict[str, Any] = {
    **dict.fromkeys(("id", "title", "entity"), _s(4096, min_len=1)),
    **dict.fromkeys(("type", "status"), _s(128, min_len=1)),
    **dict.fromkeys(("summary", "description"), _s(16384)),
    "ref": _s(4200, min_len=1),
    "source": _NAME_REF,
    "kind": _enum("project", "concept", "action", "memory"),
    "text": _s(4 << 20),
    "url": _s(8192),
    "time": {"type": "string", "pattern": _OPT_TS},
    "updated": {"type": "string", "pattern": _ISO_DT},
    "stale_after": {"anyOf": _STALE_ANY},
    "tags": _TAG_ITEMS,
    "sources": _SOURCES_PROP,
    "aliases": _arr(_s(1024, min_len=1), max_items=200),
    "attributes": {"type": "object"},
    "headings": _arr({"$ref": "#/$defs/Heading"}),
    "links": _arr({"$ref": "#/$defs/Link"}, max_items=2000),
    **{f"{n.lower()}s": _arr({"$ref": f"#/$defs/{n}"}) for n in ("Element", "Task")},
    "attrs": {"type": "object", "propertyNames": _s(256, min_len=1), "additionalProperties": _META_REF},
    "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
    "relations": False,
}
_LINK_TO = {**_s(8192, min_len=1), "description": "Note path, memories/<source>/<sha256>.json, bf:// URI, or URL."}
_LINK_P = {
    "to": _LINK_TO,
    "kind": _enum("note", "record", "brain", "url"),
    "anchor": _s(2048),
    "meta": _META_REF,
    "section": _s(256),
}
_HEAD_P = {"id": _s(256, min_len=1), "title": _s(4096), "level": _i(1, 6), "meta": _META_REF}
_ELEM_P = {"tag": _s(64, min_len=1), "id": _s(256), "text": _s(16384), "section": _s(256), "meta": _META_REF}
_TASK_P = {"id": _s(256, min_len=1), "text": _s(4096, min_len=1), "done": _BOOL, "section": _s(256), "meta": _META_REF}
_REC_BODY = {
    **_obj(_RECORD_PROPS, req=("ref", "source", "kind", "title"), extra=True),
    "if": {"required": ["kind"], "properties": {"kind": {"const": "memory"}}},
    "then": {"required": ["id"]},
    "$defs": {
        "Meta": _obj(extra=_s(4096), title="Meta", names=r"^[A-Za-z_][A-Za-z0-9_.-]*$"),
        "Heading": _obj(_HEAD_P, req=("id", "title", "level"), title="Heading"),
        "Link": _obj(_LINK_P, req=("to", "kind"), title="Link"),
        "Element": _obj(_ELEM_P, req=("tag",), title="Element"),
        "Task": _obj(_TASK_P, req=("text", "done"), title="Task"),
    },
}
RECORD_SCHEMA = _doc(
    "record",
    "Record",
    "Unified JSON record schema enforced on compiled Markdown notes and ingested sensor records in memories/.",
    _REC_BODY,
)
_CONCEPT_PROPS = {
    "type": {"type": "string", "pattern": r"^[a-z][a-z0-9-]{0,63}$"},
    "status": _enum("draft", "stable", "deprecated"),
    "summary": _s(16384),
    "updated": {"type": "string", "pattern": rf"^{_CAL_DATE}$"},
    "tags": _TAG_ITEMS,
    "sources": _SOURCES_PROP,
}
CONCEPTS_SCHEMA = _doc(
    "concepts",
    "Concept",
    "User-customizable schema enforced on concept records compiled from concepts/ (schemas/concepts.json).",
    _obj(_CONCEPT_PROPS, req=("type", "status"), extra=True),
)
_S_ARR = _arr(_s(4096, min_len=1))
_CASE_PROPS: dict[str, Any] = {
    "name": _s(256, min_len=1),
    "engine": _enum("rg", "jq", "read"),
    "query": _s(8192, min_len=1),
    "scope": _s(1024),
    **dict.fromkeys(("expect_refs", "forbid_refs", "expect_text"), _S_ARR),
    **dict.fromkeys(("min_count", "max_count"), _i(0)),
    "expect_equals": {},
}
_EVAL_PROPS = {
    "version": {"type": "integer", "const": models.FORMAT},
    "cases": _arr(_obj(_CASE_PROPS, req=("name", "query")), max_items=500),
}
EVALS_SCHEMA = _doc(
    "evals", "Eval", "Deterministic query evaluations in evals/.", _obj(_EVAL_PROPS, req=("version", "cases"))
)
_SCHEMAS: dict[Kind, dict[str, Any]] = {
    "brain": BRAIN_SCHEMA,
    "record": RECORD_SCHEMA,
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


def _describe_error(error: ValidationError) -> str:
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
    if rule in {"minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems"}:
        return f"{prefix}violates {rule} {val}"
    table = {
        "type": f"expected type {val}",
        "enum": f"must be one of {val}",
        "const": f"must equal {val!r}",
        "pattern": f"does not match pattern {val}",
        "uniqueItems": "items must be unique",
        "propertyNames": "invalid property name",
        "not": "conflicting properties",
    }
    return f"{prefix}{table.get(rule, f'failed schema rule {rule}')}"


def validate_instance(validator: Validator, instance: object, label: str) -> list[str]:
    """Validate `instance` and return up to 5 sanitized diagnostic strings without echoing values."""
    errs = sorted(validator.iter_errors(instance), key=lambda e: ([str(p) for p in e.absolute_path], str(e.validator)))
    msgs = list(dict.fromkeys(f"{label}: {_describe_error(err)}" for err in errs))
    return [*msgs[:5], f"{label}: and {len(msgs) - 5} more schema errors"] if len(msgs) > 5 else msgs
