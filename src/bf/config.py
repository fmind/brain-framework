"""Strict YAML 1.2 loader, bf.yaml validation, defaults merging, and brain discovery."""

from __future__ import annotations

import os
import re
import stat
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bf import models
from bf.models import DEFAULT_NOTES, RESERVED_SOURCES, Error, InputError
from bf.schemas import BF_SCHEMA, compile_schema, validate_instance
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
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PROTECTED_ENV = frozenset(
    {"BF_BRAIN", "BF_LOOP", "BF_STATE", "BF_START", "BF_END", "BF_TRIGGER", "BF_RUN", "LC_ALL", "LANG"}
)


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
    backfill: int | None = None
    overlap: int | None = None


@dataclass(frozen=True)
class LoopSpec:
    name: str
    run: tuple[str, ...] = ()
    description: str = ""
    daemon: bool = False
    ingest: str = ""
    target: Literal["memories", "usages"] = "memories"
    mode: Literal["upsert", "snapshot"] = "upsert"
    explicit_ingest: bool = False
    emit: str = ""
    schedules: tuple[ScheduleSpec, ...] = ()
    events: tuple[EventMatch, ...] = ()
    backfill: int = 86400
    overlap: int = 0
    timeout: int = 300
    explicit_timeout: bool = False
    enabled: bool = True
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    name: str
    description: str = ""
    notes: tuple[str, ...] = DEFAULT_NOTES
    brains: dict[str, str] = field(default_factory=dict)
    timezone: str = "UTC"
    concurrency: int = 4
    env: dict[str, str] = field(default_factory=dict)
    loops: dict[str, LoopSpec] = field(default_factory=dict)

    @property
    def reserved_sources(self) -> frozenset[str]:
        return RESERVED_SOURCES | frozenset(self.notes)


_LOADED: dict[str, Config] = {}
_LOADED_LOCK = threading.Lock()


def _integer(loader: SafeConstructor, node: ScalarNode) -> int:
    val = str(loader.construct_scalar(node))
    base = {"0o": 8, "0x": 16}.get(val[:2])
    try:
        number = int(val[2:], base) if base else int(val, 10)
        str(number)
    except ValueError:
        import yaml.constructor

        raise yaml.constructor.ConstructorError(None, None, "invalid integer", node.start_mark) from None
    return number


class _KeyError(ValueError):
    def __init__(self, mark: object = None) -> None:
        super().__init__("YAML mapping keys must be unique strings")
        self.problem_mark = mark


def _mapping(loader: SafeConstructor, node: MappingNode) -> dict[str, object]:
    out: dict[str, object] = {}
    for key_node, val_node in node.value:
        if not isinstance(key := loader.construct_object(key_node), str) or key in out:
            raise _KeyError(key_node.start_mark)
        out[key] = loader.construct_object(val_node)
    return out


@cache
def _strict(base: type[yaml.CSafeLoader]) -> type[yaml.CSafeLoader]:
    import yaml.resolver

    loader = cast("type[yaml.CSafeLoader]", type("StrictLoader", (base,), {}))
    loader.yaml_implicit_resolvers = {}
    for tag, pat, first in _CORE:
        loader.add_implicit_resolver(f"tag:yaml.org,2002:{tag}", re.compile(pat), first)
    loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    loader.add_constructor("tag:yaml.org,2002:int", _integer)
    loader.add_constructor("tag:yaml.org,2002:timestamp", lambda c, n: c.construct_scalar(n))
    return loader


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
def _bf_validator():
    return compile_schema(BF_SCHEMA, "bf.schema.json")


def _check_tz(tz: str, label: str) -> str:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise Error(f"{label}: unknown IANA timezone {tz!r}") from error
    return tz


def _str_tuple(raw: object) -> tuple[str, ...]:
    return tuple(str(item) for item in raw) if isinstance(raw, list) else ()


def _check_env(key: str, val: str, label: str, key_err: str) -> str:
    if not _ENV_KEY_RE.fullmatch(key):
        raise Error(key_err)
    if key in _PROTECTED_ENV:
        raise Error(f"{label}: cannot override reserved environment variable")
    return models.clean_env(val, label)


def _parse_env_map(raw: object, label: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): _check_env(str(k), str(v), f"{label}.{k}", f"{label}: invalid environment variable name {k!r}")
        for k, v in raw.items()
    }


def load_dotenv(store: Store) -> dict[str, str]:
    """Load optional gitignored `.env` from brain root without shell or variable interpolation."""
    try:
        raw = store.read(".env", 65536, private=True).decode("utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError:
        raise Error(".env: file is not valid UTF-8") from None
    out: dict[str, str] = {}
    for idx, raw_line in enumerate(raw.splitlines(), 1):
        if not (line := re.sub(r"^export[ \t]+", "", raw_line.strip())) or line.startswith("#"):
            continue
        key, sep, rhs = (part.strip() for part in line.partition("="))
        if not sep or not _ENV_KEY_RE.fullmatch(key):
            raise Error(f".env:{idx}: invalid environment assignment")
        if rhs.startswith(("'", '"')):
            pat = r"'([^']*)'(?:[ \t]+#.*)?" if rhs[0] == "'" else r'"((?:[^"\\]|\\.)*)"(?:[ \t]+#.*)?'
            if not (match := re.fullmatch(pat, rhs)):
                raise Error(f".env:{idx}: unclosed quote in {key}")
            val = match[1] if rhs[0] == "'" else re.sub(r'\\(["\\])', r"\1", match[1])
        else:
            val = re.split(r"[ \t]+#", rhs, maxsplit=1)[0].strip()
        out[key] = _check_env(key, val, f".env:{idx} ({key})", f".env:{idx}: invalid environment assignment")
    return out


def prev_cron(expr: str, when: datetime) -> datetime:
    """Return the previous scheduled fire time for 5-field or `@macro` cron `expr` at or before `when`."""
    from croniter import croniter

    return cast("datetime", croniter(expr, when).get_prev(datetime))


def next_cron(expr: str, when: datetime) -> datetime:
    """Return the next scheduled fire time for 5-field or `@macro` cron `expr` after `when`."""
    from croniter import croniter

    return cast("datetime", croniter(expr, when).get_next(datetime))


def parse_dt(raw: str) -> datetime | None:
    with suppress(ValueError):
        return dt if raw and (dt := datetime.fromisoformat(raw)).tzinfo is not None else None
    return None


def backoff_cap(loop: LoopSpec) -> int:
    return min((s.every for s in loop.schedules if s.every is not None), default=3600)


def backoff_remaining(doc: Mapping[str, object], cap: int, now: datetime) -> float:
    fails = int(val) if isinstance(val := doc.get("consecutive_failures"), int) else 0
    last_dt = parse_dt(str(doc.get("last_end") or "")) if fails > 0 else None
    if last_dt is None:
        return 0.0
    wait = min(60 * (1 << min(fails - 1, 10)), cap)
    return max(0.0, wait - (now - last_dt).total_seconds())


def sched_anchor(doc: Mapping[str, object], sched: ScheduleSpec | None = None) -> str:
    if sched is not None and isinstance(sl := doc.get("schedule_last"), Mapping):
        return str(sl.get(sched.raw) or "")
    return str(doc.get("last_scheduled") or "")


def due_schedule(
    loop: LoopSpec, state_doc: Mapping[str, object], now: datetime, *, tz_name: str = "UTC"
) -> ScheduleSpec | None:
    """Return the due `ScheduleSpec` (preferring widest backfill if multiple are due), or `None`."""
    if not loop.schedules or backoff_remaining(state_doc, backoff_cap(loop), now) > 0:
        return None
    tz, local_now, due = None, None, []
    for sched in loop.schedules:
        last_dt = parse_dt(sched_anchor(state_doc, sched))
        if sched.every is not None:
            if last_dt is None or (now - last_dt).total_seconds() >= sched.every:
                due.append(sched)
            continue
        if tz is None or local_now is None:
            tz = ZoneInfo(tz_name)
            local_now = (now.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=1)).astimezone(tz)
        with suppress(ValueError, KeyError, TypeError):
            if last_dt is None or last_dt.astimezone(tz) < prev_cron(sched.cron, local_now):
                due.append(sched)
    return max(due, key=lambda s: s.backfill if s.backfill is not None else loop.backfill) if due else None


def next_schedule_delay(
    loop: LoopSpec, state_doc: Mapping[str, object], now: datetime, *, tz_name: str = "UTC"
) -> float | None:
    """Return seconds until `loop`'s earliest next schedule or backoff expiry, or `None` if unscheduled."""
    if (backoff_rem := backoff_remaining(state_doc, backoff_cap(loop), now)) > 0:
        return backoff_rem
    if not loop.schedules:
        return None
    utc_now, tz = now.astimezone(UTC), ZoneInfo(tz_name)
    local_now = utc_now.astimezone(tz)
    cron_now = (utc_now.replace(microsecond=0) + timedelta(seconds=1)).astimezone(tz)
    soonest: float | None = None
    for sched in loop.schedules:
        if (last_dt := parse_dt(sched_anchor(state_doc, sched))) is None:
            return 0.5
        if sched.every is not None:
            rem = max(0.5, sched.every - (now - last_dt).total_seconds())
            soonest = rem if soonest is None else min(soonest, rem)
        else:
            with suppress(ValueError, KeyError, TypeError):
                if last_dt.astimezone(tz) < prev_cron(sched.cron, cron_now):
                    return 0.5
                rem = max(0.5, (next_cron(sched.cron, local_now).astimezone(UTC) - utc_now).total_seconds())
                soonest = rem if soonest is None else min(soonest, rem)
    return soonest


def _validate_cron(raw_expr: str, label: str) -> str:
    expr = models.clean(raw_expr.strip(), label)
    if (expr.startswith("@") and len(expr.split()) == 1) or len(expr.split()) == 5:
        from croniter import croniter

        if croniter.is_valid(expr):
            with suppress(ValueError, KeyError, TypeError):
                prev_cron(expr, datetime(2026, 1, 1, tzinfo=UTC))
                return expr
    raise Error(f"{label}: must be a duration (e.g. 1h, 1d), @macro, or 5-field cron expression")


def _parse_trigger_window(item: Mapping[str, object], label: str) -> tuple[int | None, int | None]:
    backfill = models.parse_duration(item["backfill"], f"{label}.backfill") if "backfill" in item else None
    overlap = models.parse_duration(item["overlap"], f"{label}.overlap", allow_zero=True) if "overlap" in item else None
    return backfill, overlap


def _parse_event_value(raw: object) -> EventMatch:
    if isinstance(raw, str):
        return EventMatch(kind=raw)
    if isinstance(raw, dict):
        return EventMatch(str(raw.get("kind", "")), str(raw.get("source", "")), str(raw.get("filter", "")))
    return EventMatch()


def _parse_when(raw_when: object, label: str) -> tuple[bool, tuple[ScheduleSpec, ...], tuple[EventMatch, ...]]:
    if raw_when is None:
        return False, (), ()
    if raw_when == "daemon":
        return True, (), ()
    items = raw_when if isinstance(raw_when, list) else [raw_when]
    schedules: list[ScheduleSpec] = []
    seen_sched: set[str] = set()
    events: list[EventMatch] = []

    def add_sched(spec: ScheduleSpec) -> None:
        if spec.raw in seen_sched:
            raise Error(f"{label}: duplicate schedule trigger {spec.raw!r}")
        seen_sched.add(spec.raw)
        schedules.append(spec)

    for item in items:
        if isinstance(item, dict):
            if "every" in item:
                every_raw = item["every"]
                secs = models.parse_duration(every_raw, f"{label}.every")
                bf_w, ov_w = _parse_trigger_window(item, label)
                add_sched(ScheduleSpec(f"every:{str(every_raw).strip()}", every=secs, backfill=bf_w, overlap=ov_w))
            elif "cron" in item:
                expr = _validate_cron(str(item["cron"]), f"{label}.cron")
                bf_w, ov_w = _parse_trigger_window(item, label)
                add_sched(ScheduleSpec(f"cron:{expr}", cron=expr, backfill=bf_w, overlap=ov_w))
            elif "event" in item:
                events.append(_parse_event_value(item["event"]))
        elif type(item) is int or (isinstance(item, str) and bool(models.DURATION_RE.fullmatch(item.strip()))):
            secs = models.parse_duration(item, label)
            add_sched(ScheduleSpec(f"every:{str(item).strip()}", every=secs))
        else:
            expr = _validate_cron(str(item), label)
            add_sched(ScheduleSpec(f"cron:{expr}", cron=expr))
    return False, tuple(schedules), tuple(events)


def _validate_defaults(defaults: dict[str, object]) -> None:
    if isinstance(win := defaults.get("window"), dict):
        if "backfill" in win:
            models.parse_duration(win["backfill"], "bf.yaml defaults.window.backfill")
        if "overlap" in win:
            models.parse_duration(win["overlap"], "bf.yaml defaults.window.overlap", allow_zero=True)
    if "timeout" in defaults:
        models.parse_duration(defaults["timeout"], "bf.yaml defaults.timeout", maximum=86_400)


def _parse_loop(
    name: str, raw: dict[str, object], *, defaults: dict[str, object], reserved: frozenset[str]
) -> LoopSpec:
    label = f"bf.yaml loops.{name}"
    daemon, schedules, events = _parse_when(raw.get("when"), f"{label}.when")
    emit = str(raw.get("emit") or "")
    raw_ingest = raw.get("ingest")
    explicit_ingest = raw_ingest is not None
    target: Literal["memories", "usages"] = "memories"
    if isinstance(raw_ingest, str):
        ingest, mode = name, cast("Literal['upsert', 'snapshot']", raw_ingest)
    elif isinstance(raw_ingest, dict):
        ingest = str(raw_ingest.get("source") or name)
        target = "usages" if raw_ingest.get("target") == "usages" else "memories"
        mode = "snapshot" if raw_ingest.get("mode") == "snapshot" else "upsert"
    else:
        ingest, mode = "", "upsert"
    if ingest and ingest in reserved:
        raise Error(f"{label}.ingest: source {ingest!r} is reserved")

    def_win = dw if isinstance(dw := defaults.get("window"), dict) else {}
    own_win = rw if isinstance(rw := raw.get("window"), dict) else {}
    win = {**def_win, **own_win}
    explicit_timeout = "timeout" in raw or "timeout" in defaults
    timeout_val = raw["timeout"] if "timeout" in raw else defaults.get("timeout", 300)
    return LoopSpec(
        name=name,
        run=_str_tuple(raw.get("run")),
        description=models.clean_text(str(raw.get("description") or ""), f"{label}.description"),
        daemon=daemon,
        ingest=ingest,
        target=target,
        mode=mode,
        explicit_ingest=explicit_ingest,
        emit=emit,
        schedules=schedules,
        events=events,
        backfill=models.parse_duration(win.get("backfill", 86400), f"{label}.window.backfill"),
        overlap=models.parse_duration(win.get("overlap", 0), f"{label}.window.overlap", allow_zero=True),
        timeout=models.parse_duration(timeout_val, f"{label}.timeout", maximum=86_400),
        explicit_timeout=explicit_timeout,
        enabled=bool(raw.get("enabled", True)),
        env=_parse_env_map(raw.get("env"), f"{label}.env"),
    )


def _parse_config(raw: dict[str, object]) -> Config:
    if problems := validate_instance(_bf_validator(), raw, "bf.yaml"):
        raise Error("; ".join(problems))
    description = models.clean_text(str(raw.get("description") or ""), "bf.yaml description")
    raw_notes = raw.get("notes")
    notes = tuple(str(folder) for folder in raw_notes) if isinstance(raw_notes, list) else DEFAULT_NOTES
    reserved = RESERVED_SOURCES | frozenset(notes)
    raw_brains = rb if isinstance(rb := raw.get("brains"), dict) else {}
    brains = {str(alias): models.clean(str(target), f"bf.yaml brains.{alias}") for alias, target in raw_brains.items()}
    tz = _check_tz(str(raw.get("timezone") or "UTC"), "bf.yaml timezone")
    concurrency = c if isinstance(c := raw.get("concurrency"), int) else 4
    base_env = _parse_env_map(raw.get("env"), "bf.yaml env")
    defaults = rd if isinstance(rd := raw.get("defaults"), dict) else {}
    if defaults:
        _validate_defaults(defaults)
    raw_loops = rl if isinstance(rl := raw.get("loops"), dict) else {}
    loops = {
        str(k): _parse_loop(str(k), v, defaults=defaults, reserved=reserved)
        for k, v in raw_loops.items()
        if isinstance(v, dict)
    }
    return Config(str(raw["name"]), description, notes, brains, tz, concurrency, base_env, loops)


def load(store: Store) -> Config:
    """Read and validate `bf.yaml` in `store`."""
    try:
        data = store.read("bf.yaml", 1 << 20)
    except FileNotFoundError:
        raise Error("bf.yaml is missing; pass --brain PATH, set BRAIN_LOCATION, or run bf init") from None
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


def inside_brain() -> Store | None:
    """Return the nearest enclosing user-owned brain directory, or `None` outside any brain."""
    here, owner = Path.cwd(), os.geteuid()
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
    """Resolve `--brain PATH` -> enclosing `bf.yaml` -> `BF_BRAIN` -> `BRAIN_LOCATION` -> `~/brain`."""
    if value.strip():
        return _open_brain(value.strip())
    if nearest := inside_brain():
        return nearest
    if env_brain := os.environ.get("BF_BRAIN", "").strip():
        return _open_brain(env_brain)
    if location := os.environ.get("BRAIN_LOCATION", "").strip():
        return _open_brain(location)
    with suppress(RuntimeError, OSError):
        default_dir = Path.home() / "brain"
        info = (default_dir / "bf.yaml").lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and default_dir.stat().st_uid == os.geteuid():
            return Store(default_dir)
    raise Error("no brain selected; pass --brain PATH, set BRAIN_LOCATION, run inside a brain, or run bf init ~/brain")


def resolve_brain(current: Store, cfg: Config, host: str) -> Store:
    """Resolve a `bf://<host>/...` brain target strictly from self or `bf.yaml` `brains:`."""
    if host in {"", cfg.name}:
        return current
    if host not in cfg.brains:
        raise InputError(f"brain {host!r} is not declared in bf.yaml brains:")
    raw_t = expand(Path(cfg.brains[host]))
    cand = raw_t if raw_t.is_absolute() else (current.root / raw_t)
    owner = os.geteuid()
    with suppress(OSError, ValueError):
        res = cand.resolve(strict=True)
        ds, ys = res.stat(), (res / "bf.yaml").lstat()
        if stat.S_ISDIR(ds.st_mode) and ds.st_uid == owner and stat.S_ISREG(ys.st_mode) and ys.st_uid == owner:
            target = Store(res)
            load(target)
            return target
    raise Error(f"unknown or inaccessible brain {host!r}; check brains.{host} in bf.yaml")
