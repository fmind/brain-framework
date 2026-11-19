"""Strict YAML 1.2 loader, bf.yaml validation, brain discovery, and cross-brain resolution."""

from __future__ import annotations

import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Literal, cast
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
import yaml.constructor
import yaml.resolver
from croniter import croniter

from bf import models
from bf.models import Error
from bf.schemas import BRAIN_SCHEMA, compile_schema, validate_instance
from bf.storage import Store, expand, relative

LIBYAML = (
    "reading YAML needs PyYAML built with libyaml; reinstall bf with a wheel that bundles it: "
    "uv tool install --reinstall --python 3.14 brain-framework"
)
_FLOAT_RE = r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
_NULL_BOOL = (
    ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
)
_INT_SPEC = ("int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", list("-+0123456789"))
_CORE = (*_NULL_BOOL, _INT_SPEC, ("float", _FLOAT_RE, list("-+.0123456789")))
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PROTECTED_ENV = frozenset({"BF_BRAIN", "BF_STATE_DIR", "BF_START", "BF_END", "BF_TRIGGER", "LC_ALL", "LANG"})


@dataclass(frozen=True)
class EventMatch:
    kind: str = ""
    source: str = ""
    filter: str = ""


@dataclass(frozen=True)
class JobSpec:
    name: str
    run: tuple[str, ...] = ()
    input: str = ""
    ingest: str = ""
    mode: Literal["upsert", "snapshot"] = "upsert"
    allow_removal: bool = False
    action_keep: int = 0
    schedule: str = ""
    every: int | None = None
    cron: str = ""
    events: tuple[EventMatch, ...] = ()
    hooks: tuple[str, ...] = ()
    lookback: int = 86400
    overlap: int = 0
    timeout: int = 300
    enabled: bool = True
    requires: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    run: tuple[str, ...] = ()
    emit: Literal["none", "events", "messages"] = "none"
    enabled: bool = True
    requires: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchSpec:
    interval: float = 5.0
    debounce_ms: int = 300


@dataclass(frozen=True)
class Config:
    name: str
    brains: dict[str, str] = field(default_factory=dict)
    timezone: str = "UTC"
    env: dict[str, str] = field(default_factory=dict)
    jobs: dict[str, JobSpec] = field(default_factory=dict)
    services: dict[str, ServiceSpec] = field(default_factory=dict)
    watch: WatchSpec = WatchSpec()


_LOADED: dict[str, Config] = {}


def _integer(loader: yaml.constructor.SafeConstructor, node: yaml.ScalarNode) -> int:
    value = str(loader.construct_scalar(node))
    base = {"0o": 8, "0x": 16}.get(value[:2])
    try:
        number = int(value[2:], base) if base else int(value, 10)
        str(number)
    except ValueError:
        raise yaml.constructor.ConstructorError(None, None, "invalid integer", node.start_mark) from None
    return number


class _KeyError(yaml.constructor.ConstructorError):
    """Duplicate or non-string mapping key."""


def _mapping(loader: yaml.constructor.SafeConstructor, node: yaml.MappingNode) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise _KeyError(None, None, "YAML mapping keys must be unique strings", key_node.start_mark)
        result[key] = loader.construct_object(value_node)
    return result


@cache
def _strict(base: type[yaml.CSafeLoader]) -> type[yaml.CSafeLoader]:
    loader = cast("type[yaml.CSafeLoader]", type("StrictLoader", (base,), {}))
    loader.yaml_implicit_resolvers = {}
    for tag, pattern, first in _CORE:
        loader.add_implicit_resolver(f"tag:yaml.org,2002:{tag}", re.compile(pattern), first)
    loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    loader.add_constructor("tag:yaml.org,2002:int", _integer)
    loader.add_constructor("tag:yaml.org,2002:timestamp", lambda constructor, node: constructor.construct_scalar(node))
    return loader


def yaml_object(data: bytes, name: str = "", *, line: int = 1) -> dict[str, object]:
    """Parse one UTF-8 YAML 1.2 mapping without anchors, aliases, duplicate keys, or deep nesting."""
    try:
        if (base := getattr(yaml, "CSafeLoader", None)) is None:
            raise Error(LIBYAML)
        return _yaml_object(data, line, base, _strict(base))
    except Error as error:
        if not name:
            raise
        raise Error(f"{name}: {error}") from error


def _yaml_object(
    data: bytes, line: int, scanner: type[yaml.CSafeLoader], loader: type[yaml.CSafeLoader]
) -> dict[str, object]:
    if len(data) > 1 << 20:
        raise Error("YAML exceeds 1 MiB")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise Error("YAML is not UTF-8") from None
    try:
        depth = 0
        for count, event in enumerate(yaml.parse(data, Loader=scanner), 1):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise Error("YAML anchors and aliases are not supported")
            depth += isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)) - isinstance(
                event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)
            )
            if depth > 32 or count > 20_000:
                raise Error("YAML structure exceeds its limit")
        value = yaml.load(data, Loader=loader)  # noqa: S506 - restricted CSafeLoader subclass
    except Error:
        raise
    except (yaml.YAMLError, ValueError, OverflowError, LookupError, TypeError) as error:
        mark = getattr(error, "problem_mark", None)
        reason = "YAML mapping keys must be unique strings" if isinstance(error, _KeyError) else "invalid YAML"
        raise Error(f"{reason}{f' at line {mark.line + line}, column {mark.column + 1}' if mark else ''}") from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Error("YAML must contain one mapping")
    return cast("dict[str, object]", value)


@cache
def _brain_validator():
    return compile_schema(BRAIN_SCHEMA, "bf.schema.json")


def _check_tz(tz: str, label: str) -> str:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise Error(f"{label}: unknown IANA timezone {tz!r}") from error
    return tz


def _parse_events(raw: object) -> tuple[EventMatch, ...]:
    items = (x for x in raw if isinstance(x, dict)) if isinstance(raw, list) else ()
    return tuple(EventMatch(str(x.get("kind", "")), str(x.get("source", "")), str(x.get("filter", ""))) for x in items)


def _str_tuple(raw: object) -> tuple[str, ...]:
    return tuple(str(x) for x in raw) if isinstance(raw, list) else ()


def _parse_env_map(raw: object, label: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise Error(f"{label} must be a KEY: value mapping")
    out: dict[str, str] = {}
    for raw_k, raw_v in raw.items():
        key = str(raw_k)
        if not _ENV_KEY_RE.fullmatch(key):
            raise Error(f"{label}: invalid environment variable name {key!r}")
        if key in _PROTECTED_ENV:
            raise Error(f"{label}.{key}: cannot override reserved environment variable")
        if not isinstance(raw_v, str):
            raise Error(f"{label}.{key}: environment value must be a string")
        out[key] = models.clean_env(raw_v, f"{label}.{key}")
    return out


def load_dotenv(store: Store) -> dict[str, str]:
    """Load optional gitignored `.env` from brain root without shell or variable interpolation."""
    try:
        raw = store.read(".env", 65536).decode("utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError:
        raise Error(".env: file is not valid UTF-8") from None
    out: dict[str, str] = {}
    for idx, raw_ln in enumerate(raw.splitlines(), 1):
        ln = re.sub(r"^export[ \t]+", "", raw_ln.strip())
        if not ln or ln.startswith("#"):
            continue
        key, sep, rhs = ln.partition("=")
        key, rhs = key.strip(), rhs.strip()
        if not sep or not _ENV_KEY_RE.fullmatch(key):
            raise Error(f".env:{idx}: invalid environment assignment")
        if key in _PROTECTED_ENV:
            raise Error(f".env:{idx}: {key} cannot override reserved environment variable")
        if rhs.startswith("'"):
            if not (m := re.fullmatch(r"'([^']*)'(?:[ \t]+#.*)?", rhs)):
                raise Error(f".env:{idx}: unclosed quote in {key}")
            val = m[1]
        elif rhs.startswith('"'):
            if not (m := re.fullmatch(r'"((?:[^"\\]|\\.)*)"(?:[ \t]+#.*)?', rhs)):
                raise Error(f".env:{idx}: unclosed quote in {key}")
            val = re.sub(r'\\(["\\])', r"\1", m[1])
        else:
            val = re.split(r"[ \t]+#", rhs, maxsplit=1)[0].strip()
        out[key] = models.clean_env(val, f".env:{idx} ({key})")
    return out


def _parse_watch(raw: dict[str, object]) -> WatchSpec:
    iv = models.parse_duration_seconds(raw.get("interval", 5.0), "bf.yaml watch.interval")
    if not 0.5 <= iv <= 3600.0:
        raise Error("bf.yaml watch.interval must be between 500ms and 1h")
    deb_sec = models.parse_duration_seconds(raw.get("debounce", 0.3), "bf.yaml watch.debounce")
    deb_ms = round(deb_sec * 1000)
    if not 50 <= deb_ms <= 60_000:
        raise Error("bf.yaml watch.debounce must be between 50ms and 60s")
    return WatchSpec(iv, deb_ms)


def _parse_schedule(raw_sched: object, label: str) -> tuple[str, int | None, str]:
    if raw_sched is None:
        return "", None, ""
    sched = models.clean(str(raw_sched).strip(), label) if isinstance(raw_sched, str) else str(raw_sched).strip()
    is_dur = type(raw_sched) is int or (isinstance(raw_sched, str) and bool(models.DURATION_RE.fullmatch(sched)))
    if is_dur:
        return sched, models.parse_duration(raw_sched, label), ""
    if ((sched.startswith("@") and len(sched.split()) == 1) or len(sched.split()) == 5) and croniter.is_valid(sched):
        with suppress(ValueError, KeyError, TypeError):
            croniter(sched, datetime(2026, 1, 1, tzinfo=UTC)).get_prev(datetime)
            return sched, None, sched
    raise Error(f"{label}: must be a duration (e.g. 1h, 1d), @macro, or 5-field cron expression")


def _unit_meta(raw: dict[str, object], kind: str, name: str, base_env: dict[str, str]):
    env = {**base_env, **_parse_env_map(raw.get("env"), f"bf.yaml {kind}s.{name}.env")}
    return bool(raw.get("enabled", True)), _str_tuple(raw.get("requires")), env


def _parse_job(name: str, raw: dict[str, object], base_env: dict[str, str]) -> JobSpec:
    run, inp = _str_tuple(raw.get("run")), str(raw.get("input", ""))
    if inp:
        relative(inp)
    raw_ing = raw.get("ingest")
    if isinstance(raw_ing, dict):
        ingest = str(raw_ing["source"])
        mode: Literal["upsert", "snapshot"] = "snapshot" if raw_ing.get("mode") == "snapshot" else "upsert"
        allow_removal = bool(raw_ing.get("allow_removal", False))
    else:
        ingest, mode, allow_removal = str(raw_ing or ""), "upsert", False
    if ingest in models.RESERVED_SOURCES:
        raise Error(f"bf.yaml jobs.{name}.ingest: source {ingest!r} is reserved")
    action_keep = int(cast("int", raw_act["keep"])) if isinstance(raw_act := raw.get("action"), dict) else 0
    sched, every, cron = _parse_schedule(raw.get("schedule"), f"bf.yaml jobs.{name}.schedule")
    hooks = (str(raw["hook"]),) if isinstance(raw.get("hook"), str) else _str_tuple(raw.get("hook"))
    lb = models.parse_duration(raw.get("lookback", 86400), f"bf.yaml jobs.{name}.lookback")
    ov = models.parse_duration(raw.get("overlap", 0), f"bf.yaml jobs.{name}.overlap", allow_zero=True)
    to = models.parse_duration(raw.get("timeout", 300), f"bf.yaml jobs.{name}.timeout", maximum=86_400)
    en, req, env = _unit_meta(raw, "job", name, base_env)
    ev = _parse_events(raw.get("events"))
    a = (name, run, inp, ingest, mode, allow_removal, action_keep, sched, every, cron, ev, hooks, lb, ov, to, en, req)
    return JobSpec(*a, env)


def _parse_service(name: str, raw: dict[str, object], base_env: dict[str, str]) -> ServiceSpec:
    emit = cast("Literal['none', 'events', 'messages']", str(raw.get("emit", "none")))
    return ServiceSpec(name, _str_tuple(raw.get("run")), emit, *_unit_meta(raw, "service", name, base_env))


def load(store: Store) -> Config:
    """Read and validate `bf.yaml` in `store`."""
    try:
        data = store.read("bf.yaml", 1 << 20)
    except FileNotFoundError:
        raise Error("bf.yaml is missing; pass --brain PATH, set BF_BRAIN, or run bf init") from None
    if (cached := _LOADED.get(key := models.digest(data))) is not None:
        return cached
    raw = yaml_object(data, "bf.yaml")
    models.check_version(raw, "bf.yaml")
    if problems := validate_instance(_brain_validator(), raw, "bf.yaml"):
        raise Error("; ".join(problems))
    rb = raw.get("brains") if isinstance(raw.get("brains"), dict) else {}
    brains = {str(a): models.clean(str(t), f"bf.yaml brains.{a}") for a, t in cast("dict[str, object]", rb).items()}
    tz = _check_tz(str(raw.get("timezone") or "UTC"), "bf.yaml timezone")
    base_env = _parse_env_map(raw.get("env"), "bf.yaml env")
    raw_jobs = cast("dict[str, dict[str, object]]", raw["jobs"]) if isinstance(raw.get("jobs"), dict) else {}
    raw_svcs = cast("dict[str, dict[str, object]]", raw["services"]) if isinstance(raw.get("services"), dict) else {}
    jobs = {str(k): _parse_job(str(k), v, base_env) for k, v in raw_jobs.items()}
    services = {str(k): _parse_service(str(k), v, base_env) for k, v in raw_svcs.items()}
    all_names = (*jobs, *services)
    if "events" in all_names:
        raise Error("bf.yaml: unit name 'events' is reserved")
    if len(set(all_names)) != len(all_names):
        dup = next(u for i, u in enumerate(all_names) if u in all_names[:i])
        raise Error(f"bf.yaml: unit name {dup!r} is declared in multiple sections")
    w_spec = _parse_watch(cast("dict[str, object]", rw) if isinstance(rw := raw.get("watch"), dict) else {})
    if len(_LOADED) >= 64:
        _LOADED.pop(next(iter(_LOADED)))
    _LOADED[key] = cfg = Config(str(raw["name"]), brains, tz, base_env, jobs, services, w_spec)
    return cfg


def inside_brain(start: Path | None = None) -> Store | None:
    """Return the nearest enclosing user-owned brain directory, or `None` outside any brain."""
    here, owner = (start or Path.cwd()), os.geteuid()
    for up, candidate in enumerate((here, *here.parents)):
        try:
            info = (candidate / "bf.yaml").lstat()
        except FileNotFoundError, NotADirectoryError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or candidate.stat().st_uid != owner:
            rel_y = f"{'../' * up or './'}bf.yaml"
            raise Error(f"{rel_y} is not a regular file owned by you; pass --brain PATH to select a brain")
        return Store(candidate)
    return None


def _open_brain(chosen: str) -> Store:
    store = Store(expand(Path(chosen)))
    try:
        info = store.mode("bf.yaml")
    except FileNotFoundError:
        raise Error(f"{chosen} has no bf.yaml; pass the brain's root directory or run bf init") from None
    if not stat.S_ISREG(info):
        raise Error(f"{chosen}/bf.yaml must be a regular file")
    return store


def select(value: str = "") -> Store:
    """Resolve `--brain PATH` -> `BF_BRAIN` -> enclosing `bf.yaml` -> `DEFAULT_BRAIN_PATH`."""
    if value.strip():
        return _open_brain(value.strip())
    if env_brain := os.environ.get("BF_BRAIN", "").strip():
        try:
            return _open_brain(env_brain)
        except Error:
            if models.NAME_RE.fullmatch(env_brain) and (nearest := inside_brain()) is not None:
                near_name = ""
                with suppress(Error):
                    near_name = load(nearest).name
                if env_brain in {near_name, nearest.root.name}:
                    return nearest
            raise
    if nearest := inside_brain():
        return nearest
    if fallback := os.environ.get("DEFAULT_BRAIN_PATH", "").strip():
        return _open_brain(fallback)
    raise Error("no brain selected; pass --brain PATH, set BF_BRAIN or DEFAULT_BRAIN_PATH, or run inside a brain")


def resolve_brain(current: Store, cfg: Config, host: str, *, declared_only: bool = False) -> Store:
    """Resolve a `bf://<host>/...` brain target confined to user-owned brain directories."""
    if host in {"", cfg.name}:
        return current
    if declared_only and host not in cfg.brains:
        raise models.InputError(f"brain {host!r} is not declared in bf.yaml brains:")
    owner = os.geteuid()
    candidates: list[Path] = []
    if host in cfg.brains:
        raw_target = expand(Path(cfg.brains[host]))
        candidates.append(raw_target if raw_target.is_absolute() else (current.root / raw_target))
    elif models.NAME_RE.fullmatch(host):
        candidates.append(current.root.parent / host)
        with suppress(RuntimeError):
            candidates.append(Path.home() / host)
    else:
        try:
            decoded = unquote(host, errors="strict")
        except UnicodeError as error:
            raise Error("bf:// URI host contains invalid UTF-8 percent-encoding") from error
        if "\x00" not in decoded and "\\" not in decoded:
            rp = expand(Path(decoded))
            candidates.extend((rp,) if rp.is_absolute() else (current.root / rp, current.root.parent / rp))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            dir_stat, yaml_stat = resolved.stat(), (resolved / "bf.yaml").lstat()
        except OSError, ValueError:
            continue
        ok_dir = stat.S_ISDIR(dir_stat.st_mode) and dir_stat.st_uid == owner
        if ok_dir and stat.S_ISREG(yaml_stat.st_mode) and yaml_stat.st_uid == owner:
            target_store = Store(resolved)
            load(target_store)
            return target_store
    raise Error(f"unknown brain {host!r}; declare it under brains: in bf.yaml")
