"""Strict YAML 1.2 loader/dumper, bf.yaml validation, brain discovery, and cross-brain resolution."""

from __future__ import annotations

import os
import re
import stat
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bf import models
from bf.models import Error
from bf.schemas import BRAIN_SCHEMA, compile_schema, validate_instance
from bf.storage import Store, expand

if TYPE_CHECKING:
    import yaml
    from yaml import MappingNode, ScalarNode
    from yaml.constructor import SafeConstructor

LIBYAML = (
    "reading YAML needs PyYAML built with libyaml; reinstall bf with a wheel that bundles it: "
    "uv tool install --reinstall --python 3.14 brain-framework"
)
_FLOAT_RE = r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
_CORE = (
    ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", list("-+0123456789")),
    ("float", _FLOAT_RE, list("-+.0123456789")),
)
_CORE_PATS = tuple(re.compile(pat) for _, pat, _ in _CORE)
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PROTECTED_ENV = frozenset({"BF_BRAIN", "BF_STATE_DIR", "BF_START", "BF_END", "BF_TRIGGER", "LC_ALL", "LANG"})


@dataclass(frozen=True)
class EventMatch:
    kind: str = ""
    source: str = ""
    filter: str = ""


@dataclass(frozen=True)
class ScheduleSpec:
    raw: str
    every: int | None = None
    cron: str = ""


@dataclass(frozen=True)
class LoopSpec:
    name: str
    run: tuple[str, ...] = ()
    description: str = ""
    daemon: bool = False
    ingest: str = ""
    mode: Literal["upsert", "snapshot"] = "upsert"
    emit: str = ""
    schedules: tuple[ScheduleSpec, ...] = ()
    events: tuple[EventMatch, ...] = ()
    hooks: tuple[str, ...] = ()
    backfill: int = 86400
    overlap: int = 0
    timeout: int = 300
    enabled: bool = True
    requires: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchSpec:
    interval: float = 5.0
    concurrency: int = 4


@dataclass(frozen=True)
class Config:
    name: str
    description: str = ""
    brains: dict[str, str] = field(default_factory=dict)
    timezone: str = "UTC"
    env: dict[str, str] = field(default_factory=dict)
    loops: dict[str, LoopSpec] = field(default_factory=dict)
    watch: WatchSpec = WatchSpec()


_LOADED: dict[str, Config] = {}
_LOADED_LOCK = threading.Lock()


def _integer(loader: SafeConstructor, node: ScalarNode) -> int:
    val = str(loader.construct_scalar(node))
    base = {"0o": 8, "0x": 16}.get(val[:2])
    try:
        n = int(val[2:], base) if base else int(val, 10)
        str(n)
    except ValueError:
        import yaml.constructor

        raise yaml.constructor.ConstructorError(None, None, "invalid integer", node.start_mark) from None
    return n


class _KeyError(ValueError):
    def __init__(self, mark: object = None) -> None:
        super().__init__("YAML mapping keys must be unique strings")
        self.problem_mark = mark


def _mapping(loader: SafeConstructor, node: MappingNode) -> dict[str, object]:
    out: dict[str, object] = {}
    for kn, vn in node.value:
        if not isinstance(key := loader.construct_object(kn), str) or key in out:
            raise _KeyError(kn.start_mark)
        out[key] = loader.construct_object(vn)
    return out


@cache
def _strict(base: type[yaml.CSafeLoader]) -> type[yaml.CSafeLoader]:
    import yaml.resolver

    ldr = cast("type[yaml.CSafeLoader]", type("StrictLoader", (base,), {}))
    ldr.yaml_implicit_resolvers = {}
    for tag, pat, first in _CORE:
        ldr.add_implicit_resolver(f"tag:yaml.org,2002:{tag}", re.compile(pat), first)
    ldr.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    ldr.add_constructor("tag:yaml.org,2002:int", _integer)
    ldr.add_constructor("tag:yaml.org,2002:timestamp", lambda c, n: c.construct_scalar(n))
    return ldr


def dump_yaml(obj: dict[str, Any]) -> bytes:
    """Serialize `obj` as YAML 1.2 SafeDumper UTF-8 bytes, quoting implicit scalars."""
    import yaml

    dumper = type("_Yaml12Dumper", (yaml.SafeDumper,), {})
    dumper.add_representer(
        str,
        lambda d, s: d.represent_scalar(
            "tag:yaml.org,2002:str", s, style="'" if any(p.fullmatch(s) for p in _CORE_PATS) else None
        ),
    )
    return yaml.dump(obj, Dumper=dumper, sort_keys=False, allow_unicode=True).encode("utf-8")


def yaml_object(data: bytes, name: str = "", *, line: int = 1) -> dict[str, object]:
    """Parse one UTF-8 YAML 1.2 mapping without anchors, aliases, duplicate keys, or deep nesting."""
    import yaml

    prefix = f"{name}: " if name else ""
    try:
        if (base := getattr(yaml, "CSafeLoader", None)) is None:
            raise Error(LIBYAML)
        if len(data) > 1 << 20:
            raise Error("YAML exceeds 1 MiB")
        data.decode("utf-8")
        depth = 0
        for count, ev in enumerate(yaml.parse(data, Loader=base), 1):
            if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None):
                raise Error("YAML anchors and aliases are not supported")
            depth += isinstance(ev, (yaml.MappingStartEvent, yaml.SequenceStartEvent))
            depth -= isinstance(ev, (yaml.MappingEndEvent, yaml.SequenceEndEvent))
            if depth > 32 or count > 20_000:
                raise Error("YAML structure exceeds its limit")
        val = yaml.load(data, Loader=_strict(base))  # noqa: S506 - restricted CSafeLoader subclass
        if val is not None and not isinstance(val, dict):
            raise Error("YAML must contain one mapping")
        return {} if val is None else cast("dict[str, object]", val)
    except UnicodeDecodeError as err:
        raise Error(f"{prefix}YAML is not UTF-8") from err
    except (yaml.YAMLError, ValueError, OverflowError, LookupError, TypeError) as err:
        mark = getattr(err, "problem_mark", None)
        why = "YAML mapping keys must be unique strings" if isinstance(err, _KeyError) else "invalid YAML"
        loc = f" at line {mark.line + line}, column {mark.column + 1}" if mark else ""
        raise Error(f"{prefix}{why}{loc}") from err
    except Error as error:
        raise (Error(f"{prefix}{error}") if name else error) from error


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
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    return tuple(
        EventMatch(kind=x)
        if isinstance(x, str)
        else EventMatch(str(x.get("kind", "")), str(x.get("source", "")), str(x.get("filter", "")))
        for x in items
        if isinstance(x, (str, dict))
    )


def _str_tuple(raw: object) -> tuple[str, ...]:
    return tuple(str(x) for x in raw) if isinstance(raw, list) else ()


def _check_env(key: str, val: str, label: str, key_err: str) -> str:
    if not _ENV_KEY_RE.fullmatch(key):
        raise Error(key_err)
    if key in _PROTECTED_ENV:
        raise Error(f"{label}: cannot override reserved environment variable")
    return models.clean_env(val, label)


def _parse_env_map(raw: object, label: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise Error(f"{label} must be a KEY: value mapping")
    out: dict[str, str] = {}
    for rk, rv in raw.items():
        if not isinstance(rv, str):
            raise Error(f"{label}.{rk}: environment value must be a string")
        k = str(rk)
        out[k] = _check_env(k, rv, f"{label}.{k}", f"{label}: invalid environment variable name {k!r}")
    return out


def load_dotenv(store: Store) -> dict[str, str]:
    """Load optional gitignored `.env` from brain root without shell or variable interpolation."""
    try:
        raw = store.read(".env", 65536, private=True).decode("utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError:
        raise Error(".env: file is not valid UTF-8") from None
    out: dict[str, str] = {}
    for idx, raw_ln in enumerate(raw.splitlines(), 1):
        if not (ln := re.sub(r"^export[ \t]+", "", raw_ln.strip())) or ln.startswith("#"):
            continue
        key, sep, rhs = (p.strip() for p in ln.partition("="))
        if not sep or not _ENV_KEY_RE.fullmatch(key):
            raise Error(f".env:{idx}: invalid environment assignment")
        if rhs.startswith(("'", '"')):
            pat = r"'([^']*)'(?:[ \t]+#.*)?" if rhs[0] == "'" else r'"((?:[^"\\]|\\.)*)"(?:[ \t]+#.*)?'
            if not (m := re.fullmatch(pat, rhs)):
                raise Error(f".env:{idx}: unclosed quote in {key}")
            val = m[1] if rhs[0] == "'" else re.sub(r'\\(["\\])', r"\1", m[1])
        else:
            val = re.split(r"[ \t]+#", rhs, maxsplit=1)[0].strip()
        out[key] = _check_env(key, val, f".env:{idx} ({key})", f".env:{idx}: invalid environment assignment")
    return out


def _parse_watch(raw: dict[str, object]) -> WatchSpec:
    iv = models.parse_duration_seconds(raw.get("interval", 5.0), "bf.yaml watch.interval")
    conc = raw.get("concurrency", 4)
    if not 0.5 <= iv <= 3600.0:
        raise Error("bf.yaml watch.interval must be between 500ms and 1h")
    if type(conc) is not int or not 1 <= conc <= 32:
        raise Error("bf.yaml watch.concurrency must be an integer between 1 and 32")
    return WatchSpec(iv, conc)


def prev_cron(expr: str, when: datetime) -> datetime:
    """Return the previous scheduled fire time for 5-field or `@macro` cron `expr` at or before `when`."""
    from croniter import croniter

    return cast("datetime", croniter(expr, when).get_prev(datetime))


def _parse_one_schedule(raw_sched: object, label: str) -> ScheduleSpec:
    sched = models.clean(str(raw_sched).strip(), label) if isinstance(raw_sched, str) else str(raw_sched).strip()
    if type(raw_sched) is int or (isinstance(raw_sched, str) and bool(models.DURATION_RE.fullmatch(sched))):
        return ScheduleSpec(sched, models.parse_duration(raw_sched, label), "")
    if (sched.startswith("@") and len(sched.split()) == 1) or len(sched.split()) == 5:
        from croniter import croniter

        if croniter.is_valid(sched):
            with suppress(ValueError, KeyError, TypeError):
                prev_cron(sched, datetime(2026, 1, 1, tzinfo=UTC))
                return ScheduleSpec(sched, None, sched)
    raise Error(f"{label}: must be a duration (e.g. 1h, 1d), @macro, or 5-field cron expression")


def _parse_schedules(raw_sched: object, label: str) -> tuple[ScheduleSpec, ...]:
    if raw_sched is None:
        return ()
    items = raw_sched if isinstance(raw_sched, list) else [raw_sched]
    return tuple(_parse_one_schedule(item, label) for item in items)


def _parse_loop(name: str, raw: dict[str, object]) -> LoopSpec:
    label = f"bf.yaml loops.{name}"
    run = _str_tuple(raw.get("run"))
    desc = models.clean_text(str(raw.get("description") or ""), f"{label}.description")
    raw_on = raw.get("on")
    daemon = raw_on == "daemon"
    trig = raw_on if isinstance(raw_on, dict) else {}
    raw_ing = raw.get("ingest")
    if isinstance(raw_ing, str):
        ingest, mode = name, cast("Literal['upsert', 'snapshot']", raw_ing)
    elif isinstance(raw_ing, dict):
        ingest = str(raw_ing["source"])
        mode = "snapshot" if raw_ing.get("mode") == "snapshot" else "upsert"
    else:
        ingest, mode = "", "upsert"
    if ingest in models.RESERVED_SOURCES:
        raise Error(f"{label}.ingest: source {ingest!r} is reserved")
    emit = str(raw.get("emit") or "")
    schedules = _parse_schedules(trig.get("schedules"), f"{label}.on.schedules")
    hooks = (str(trig["hooks"]),) if isinstance(trig.get("hooks"), str) else _str_tuple(trig.get("hooks"))
    win = w if isinstance(w := raw.get("window"), dict) else {}
    return LoopSpec(
        name=name,
        run=run,
        description=desc,
        daemon=daemon,
        ingest=ingest,
        mode=mode,
        emit=emit,
        schedules=schedules,
        events=_parse_events(trig.get("events")),
        hooks=hooks,
        backfill=models.parse_duration(win.get("backfill", 86400), f"{label}.window.backfill"),
        overlap=models.parse_duration(win.get("overlap", 0), f"{label}.window.overlap", allow_zero=True),
        timeout=models.parse_duration(raw.get("timeout", 300), f"{label}.timeout", maximum=86_400),
        enabled=bool(raw.get("enabled", True)),
        requires=_str_tuple(raw.get("requires")),
        env=_parse_env_map(raw.get("env"), f"{label}.env"),
    )


def _parse_config(raw: dict[str, object]) -> Config:
    if problems := validate_instance(_brain_validator(), raw, "bf.yaml"):
        raise Error("; ".join(problems))
    desc = models.clean_text(str(raw.get("description") or ""), "bf.yaml description")
    raw_brains = rb if isinstance(rb := raw.get("brains"), dict) else {}
    brains = {str(a): models.clean(str(t), f"bf.yaml brains.{a}") for a, t in raw_brains.items()}
    tz = _check_tz(str(raw.get("timezone") or "UTC"), "bf.yaml timezone")
    base_env = _parse_env_map(raw.get("env"), "bf.yaml env")
    raw_loops = rl if isinstance(rl := raw.get("loops"), dict) else {}
    loops = {str(k): _parse_loop(str(k), v) for k, v in raw_loops.items() if isinstance(v, dict)}
    watch_cfg = _parse_watch(rw if isinstance(rw := raw.get("watch"), dict) else {})
    return Config(str(raw["name"]), desc, brains, tz, base_env, loops, watch_cfg)


def load(store: Store) -> Config:
    """Read and validate `bf.yaml` in `store`."""
    try:
        data = store.read("bf.yaml", 1 << 20)
    except FileNotFoundError:
        raise Error("bf.yaml is missing; pass --brain PATH, set BF_BRAIN, or run bf init") from None
    key = models.digest(data)
    with _LOADED_LOCK:
        cached = _LOADED.get(key)
    if cached is not None:
        return cached
    raw = yaml_object(data, "bf.yaml")
    models.check_version(raw, "bf.yaml")
    cfg = _parse_config(raw)
    with _LOADED_LOCK:
        if len(_LOADED) >= 64:
            _LOADED.pop(next(iter(_LOADED)))
        _LOADED[key] = cfg
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
    owner, candidates = os.geteuid(), []
    if host in cfg.brains:
        raw_t = expand(Path(cfg.brains[host]))
        candidates.append(raw_t if raw_t.is_absolute() else (current.root / raw_t))
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
    for cand in candidates:
        with suppress(OSError, ValueError):
            res = cand.resolve(strict=True)
            ds, ys = res.stat(), (res / "bf.yaml").lstat()
            if stat.S_ISDIR(ds.st_mode) and ds.st_uid == owner and stat.S_ISREG(ys.st_mode) and ys.st_uid == owner:
                target = Store(res)
                load(target)
                return target
    raise Error(f"unknown brain {host!r}; declare it under brains: in bf.yaml")
