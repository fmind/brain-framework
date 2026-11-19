"""Draft 2020-12 JSON Schemas for bf.yaml, unified JSON records, concepts, and evals."""

from __future__ import annotations

import copy
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.protocols import Validator

from bf.models import DURATION_PAT, DURATION_ZERO_PAT, FORMAT, MAX_SCHEMA, NAME, Error, decode, digest, encode
from bf.storage import Store

Kind = Literal["brain", "record", "concepts", "evals"]
_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_BASE = "https://fmind.github.io/brain-framework"
_TS_HMS = r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
_ISO_DT = rf"^(?:[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}(?:T{_TS_HMS})?)?$"
_BOOL: dict[str, Any] = {"type": "boolean"}
_NAME_REF: dict[str, Any] = {"type": "string", "pattern": NAME}


def _s(max_len: int = 4096, *, min_len: int = 0, pat: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"type": "string", "maxLength": max_len}
    return {**out, **({"minLength": min_len} if min_len else {}), **({"pattern": pat} if pat else {})}


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _i(min_val: int = 0, max_val: int | None = None) -> dict[str, Any]:
    return {"type": "integer", "minimum": min_val, **({"maximum": max_val} if max_val is not None else {})}


def _arr(items: dict[str, Any], *, max_items: int = 0, min_items: int = 0, unique: bool = False) -> dict[str, Any]:
    extra = {k: v for k, v in (("minItems", min_items), ("maxItems", max_items), ("uniqueItems", unique)) if v}
    return {"type": "array", "items": items, **extra}


def _opt(schema: dict[str, Any]) -> dict[str, Any]:
    return {**schema, "type": ["object", "null"]}


def _obj(props: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "additionalProperties": kw.get("extra", False)}
    if kw.get("title"):
        out["title"] = kw["title"]
    if kw.get("req"):
        out["required"] = list(kw["req"])
    if kw.get("names"):
        out["propertyNames"] = {"pattern": kw["names"]}
    return {**out, "properties": props} if props is not None else out


def _doc(slug: str, title: str, desc: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"$schema": _DRAFT, "$id": f"{_BASE}/{slug}.schema.json", "title": title, "description": desc, **body}


_DURATION = {"anyOf": [_i(1, 315576000), _s(32, pat=DURATION_PAT)]}
_OVERLAP = {"anyOf": [_i(0, 315576000), _s(32, pat=DURATION_ZERO_PAT)]}
_TIMEOUT = {"anyOf": [_i(1, 86400), _s(32, pat=DURATION_PAT)]}
_WATCH_INTERVAL = {"anyOf": [{"type": "number", "minimum": 0.5, "maximum": 3600}, _s(32, pat=DURATION_PAT)]}
_DEBOUNCE = {"anyOf": [{"type": "number", "minimum": 0.05, "maximum": 60}, _s(32, pat=DURATION_PAT)]}
_REQUIRES = _arr(_s(256, min_len=1), max_items=32, unique=True)
_ENV_MAP = _obj(extra=_s(4096), names=r"^[A-Z_][A-Z0-9_]*$")
_TAG_ITEMS = _arr(_s(128, min_len=1, pat=r"^[^/\\\s]+$"), max_items=200, unique=True)
_HOOK_ENUM = _enum("pre-commit", "pre-push")
_ARGV_REF = {"$ref": "#/$defs/Argv"}
_META_REF = {"$ref": "#/$defs/Meta"}
_WATCH_PROPS = {"interval": _WATCH_INTERVAL, "debounce": _DEBOUNCE}
_SCHEDULE = {"anyOf": [_i(1, 315576000), _s(128, min_len=1)]}
_UNIT_BASE = {"enabled": _BOOL, "requires": _REQUIRES, "env": _ENV_MAP}
_JOB_IO = {"run": _ARGV_REF, "input": _s(1024, min_len=1), "ingest": {"anyOf": [_NAME_REF, {"$ref": "#/$defs/Ingest"}]}}
_JOB_OUT = {"action": {"$ref": "#/$defs/Action"}, "schedule": _SCHEDULE}
_HOOK_ANY = {"anyOf": [_HOOK_ENUM, _arr(_HOOK_ENUM, min_items=1, max_items=2, unique=True)]}
_JOB_TRIG = {"events": _arr({"$ref": "#/$defs/EventMatch"}, min_items=1, max_items=32), "hook": _HOOK_ANY}
_JOB_TIMES = {"lookback": _DURATION, "overlap": _OVERLAP, "timeout": _TIMEOUT}
_JOB_PROPS: dict[str, Any] = {**_JOB_IO, **_JOB_OUT, **_JOB_TRIG, **_JOB_TIMES, **_UNIT_BASE}
_SVC_PROPS = {"run": _ARGV_REF, "emit": _enum("none", "events", "messages"), **_UNIT_BASE}
_VER_PROP = {"type": "integer", "const": FORMAT, "description": "Brain configuration format version."}
_NAME_PROP = {**_NAME_REF, "description": "Stable lowercase identifier for this brain."}
_BRAIN_HEAD: dict[str, Any] = {"version": _VER_PROP, "name": _NAME_PROP, "timezone": _s(128, min_len=1)}
_BRAIN_HEAD["brains"] = _opt(_obj(extra=_s(4096, min_len=1), names=NAME))
_U_KINDS = (("jobs", "Job"), ("services", "Service"))
_BRAIN_UNITS = {k: _opt(_obj(extra={"$ref": f"#/$defs/{t}"}, names=NAME)) for k, t in _U_KINDS}
_BRAIN_PROPS: dict[str, Any] = {**_BRAIN_HEAD, "env": _opt(_ENV_MAP), **_BRAIN_UNITS, "watch": _opt(_obj(_WATCH_PROPS))}
_INGEST_PROPS = {"source": _NAME_REF, "mode": _enum("upsert", "snapshot"), "allow_removal": _BOOL}
_EV_MATCH_PROPS = {"kind": _s(128, min_len=1), "source": _s(128, min_len=1), "filter": _s(2048, min_len=1)}
_ARGV_DEF = {"title": "Argv", **_arr(_s(4096, min_len=1), min_items=1, max_items=64)}
_DEFS_A = {"Action": _obj({"keep": _i(1, 1000)}, req=("keep",), title="Action"), "Argv": _ARGV_DEF}
_EV_DEF, _ING_DEF = _obj(_EV_MATCH_PROPS, title="EventMatch"), _obj(_INGEST_PROPS, req=("source",), title="Ingest")
_DEFS_B = {"EventMatch": _EV_DEF, "Ingest": _ING_DEF}
_JOB_DEF = {**_obj(_JOB_PROPS, req=("run",), title="Job"), "not": {"required": ["ingest", "action"]}}
_BRAIN_DEFS = {**_DEFS_A, **_DEFS_B, "Job": _JOB_DEF, "Service": _obj(_SVC_PROPS, req=("run",), title="Service")}
_BRAIN_BODY = {**_obj(_BRAIN_PROPS, req=("version", "name")), "$defs": _BRAIN_DEFS}
BRAIN_SCHEMA = _doc("bf", "Config", "Brain Framework configuration and orchestration (bf.yaml).", _BRAIN_BODY)

_STALE_ANY = [_i(1, 36500), _s(32, pat=DURATION_PAT), {"type": "string", "minLength": 10, "pattern": _ISO_DT}]
_REC_PROPS: dict[str, Any] = {"id": _s(4096, min_len=1), "ref": _s(4200, min_len=1), "source": _NAME_REF}
_REC_PROPS.update({"kind": _enum("project", "concept", "action", "memory"), "title": _s(4096, min_len=1)})
_REC_PROPS.update({"text": _s(4 << 20), "type": _s(128, min_len=1), "status": _s(128, min_len=1)})
_OPT_TS = rf"^(?:[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T{_TS_HMS})?$"
_REC_PROPS.update({"summary": _s(16384), "description": _s(16384), "url": _s(8192)})
_REC_PROPS["time"] = {"type": "string", "pattern": _OPT_TS}
_REC_PROPS.update({"updated": {"type": "string", "pattern": _ISO_DT}, "stale_after": {"anyOf": _STALE_ANY}})
_REC_PROPS.update({"tags": _TAG_ITEMS, "aliases": _arr(_s(1024, min_len=1), max_items=200)})
_REC_PROPS.update({"attributes": {"type": "object"}, "headings": _arr({"$ref": "#/$defs/Heading"})})
_REC_PROPS["links"] = _arr({"$ref": "#/$defs/Link"}, max_items=2000)
_REC_PROPS.update({"elements": _arr({"$ref": "#/$defs/Element"}), "tasks": _arr({"$ref": "#/$defs/Task"})})
_REC_PROPS["attrs"] = {"type": "object", "propertyNames": _s(256, min_len=1), "additionalProperties": _META_REF}
_REC_PROPS["sha256"] = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}

_LINK_PROPS = {"to": _s(8192, min_len=1), "kind": _enum("note", "record", "brain", "url"), "anchor": _s(2048)}
_LINK_PROPS.update({"meta": _META_REF, "section": _s(256)})
_ELEM_PROPS = {"tag": _s(64, min_len=1), "id": _s(256), "text": _s(16384), "section": _s(256), "meta": _META_REF}
_HEAD_PROPS = {"id": _s(256, min_len=1), "title": _s(4096), "level": _i(1, 6), "meta": _META_REF}
_TASK_PROPS = {"id": _s(256, min_len=1), "text": _s(4096, min_len=1), "done": _BOOL, "section": _s(256)}
_TASK_PROPS["meta"] = _META_REF
_META_DEF = _obj(extra=_s(4096), title="Meta", names=r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_RD_A = {"Meta": _META_DEF, "Heading": _obj(_HEAD_PROPS, req=("id", "title", "level", "meta"), title="Heading")}
_RD_B = {"Link": _obj(_LINK_PROPS, req=("to", "kind", "anchor", "meta", "section"), title="Link")}
_RD_C = {"Element": _obj(_ELEM_PROPS, req=("tag", "id", "text", "section", "meta"), title="Element")}
_RD_D = {"Task": _obj(_TASK_PROPS, req=("text", "done", "section", "meta"), title="Task")}
_REC_DEFS = {**_RD_A, **_RD_B, **_RD_C, **_RD_D}
_REC_DESC = "Unified JSON record schema enforced on compiled Markdown notes and ingested sensor records in memories/."
_REC_REQ = ("id", "ref", "source", "kind", "title")
RECORD_SCHEMA = _doc("record", "Record", _REC_DESC, {**_obj(_REC_PROPS, req=_REC_REQ, extra=True), "$defs": _REC_DEFS})

_CONCEPT_PROPS: dict[str, Any] = {"type": {"type": "string", "pattern": r"^[a-z][a-z0-9-]{0,63}$"}}
_CONCEPT_PROPS.update({"status": _enum("draft", "stable", "deprecated"), "summary": _s(16384)})
_CONCEPT_PROPS.update({"updated": {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}, "tags": _TAG_ITEMS})
_CONCEPT_PROPS["sources"] = _arr(_obj({"resource": _s(8192, min_len=1)}, req=("resource",), extra=True), max_items=200)
_CONCEPT_DESC = "User-customizable schema enforced on concept records compiled from concepts/ (schemas/concepts.json)."
CONCEPTS_SCHEMA = _doc("concepts", "Concept", _CONCEPT_DESC, _obj(_CONCEPT_PROPS, req=("type", "status"), extra=True))

_CASE_PROPS: dict[str, Any] = {"name": _s(256, min_len=1), "engine": _enum("rg", "jq", "read")}
_CASE_PROPS.update({"query": _s(8192, min_len=1), "scope": _s(1024), "expect_refs": _arr(_s(4096, min_len=1))})
_CASE_PROPS.update({"forbid_refs": _arr(_s(4096, min_len=1)), "expect_text": _arr(_s(4096, min_len=1))})
_CASE_PROPS.update({"min_count": _i(0), "max_count": _i(0), "expect_equals": {}})
_EV_CASES = _arr(_obj(_CASE_PROPS, req=("name", "query")), max_items=500)
_EV_DESC = "Deterministic query evaluations in evals/ (schemas/evals.json)."
_EVAL_BODY = _obj({"version": {"type": "integer", "const": FORMAT}, "cases": _EV_CASES}, req=("version", "cases"))
EVALS_SCHEMA = _doc("evals", "Eval", _EV_DESC, _EVAL_BODY)
_SCHEMAS: dict[Kind, dict[str, Any]] = {"brain": BRAIN_SCHEMA, "record": RECORD_SCHEMA, "concepts": CONCEPTS_SCHEMA}
_SCHEMAS["evals"] = EVALS_SCHEMA


def document(kind: Kind) -> dict[str, Any]:
    """Return a deep copy of the canonical JSON Schema for `kind`."""
    return copy.deepcopy(_SCHEMAS[kind])


def compile_schema(raw: object, label: str) -> Validator:
    """Validate that `raw` is a legal Draft 2020-12 JSON Schema object and return its validator."""
    if not isinstance(raw, dict):
        raise Error(f"{label}: JSON Schema must be a JSON object")
    try:
        Draft202012Validator.check_schema(raw)
    except SchemaError as error:
        loc, v = ".".join(str(part) for part in error.schema_path), error.validator or "schema"
        raise Error(f"{label}: invalid JSON Schema{f' at {loc}' if loc else ''} ({v})") from error
    return Draft202012Validator(raw)


_VALIDATORS: dict[str, Validator] = {}


def load_validator(store: Store, path: str, fallback: dict[str, Any] | None = None) -> Validator | None:
    """Load and compile `path` from `store`, falling back to `fallback` when the file is absent."""
    try:
        raw: bytes | None = store.read(path, MAX_SCHEMA)
    except FileNotFoundError:
        if fallback is None:
            return None
        raw = None
    key = f"fallback:{digest(encode(fallback))}" if raw is None else digest(raw)
    if (cached := _VALIDATORS.get(key)) is None:
        if len(_VALIDATORS) >= 64:
            _VALIDATORS.pop(next(iter(_VALIDATORS)))
        _VALIDATORS[key] = cached = compile_schema(fallback if raw is None else decode(raw, path), path)
    return cached


def _describe_error(error: ValidationError) -> str:
    loc = ".".join(str(part) for part in error.absolute_path)
    prefix, rule, val = (f"{loc}: " if loc else ""), str(error.validator), error.validator_value
    if rule == "required" and isinstance(val, list):
        missing = [k for k in val if not isinstance(error.instance, dict) or k not in error.instance]
        return f"{prefix}missing required {', '.join(repr(k) for k in missing)}"
    if rule == "additionalProperties" and val is False and isinstance(error.instance, dict):
        allowed = set(error.schema.get("properties", {})) if isinstance(error.schema, dict) else set()
        return f"{prefix}unknown property {', '.join(repr(k) for k in sorted(set(error.instance) - allowed))}"
    if rule in {"minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems"}:
        return f"{prefix}violates {rule} {val}"
    t1 = {"type": f"expected type {val}", "enum": f"must be one of {val}", "const": f"must equal {val!r}"}
    t2 = {"pattern": f"does not match pattern {val}", "uniqueItems": "items must be unique"}
    table = {**t1, **t2, "propertyNames": "invalid property name", "not": "conflicting properties"}
    return f"{prefix}{table.get(rule, f'failed schema rule {rule}')}"


def validate_instance(validator: Validator, instance: object, label: str) -> list[str]:
    """Validate `instance` and return up to 5 sanitized diagnostic strings without echoing values."""
    errs = sorted(validator.iter_errors(instance), key=lambda e: ([str(p) for p in e.absolute_path], str(e.validator)))
    if not errs:
        return []
    messages = list(dict.fromkeys(f"{label}: {_describe_error(err)}" for err in errs))
    return [*messages[:5], f"{label}: and {len(messages) - 5} more schema errors"] if len(messages) > 5 else messages
