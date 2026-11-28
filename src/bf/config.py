"""bf.yaml v19 validation, `.env` loading, loop resolution, script confinement, and brain discovery."""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import stat
import sys
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfoNotFoundError

from bf import cron, models
from bf.cron import ScheduleSpec
from bf.models import DEFAULT_NOTES, RESERVED_SOURCES, Error, InputError
from bf.schemas import BF_SCHEMA, compile_schema, validate_instance
from bf.storage import Store, expand, relative
from bf.yaml_io import yaml_object

if TYPE_CHECKING:
    from jsonschema.protocols import Validator

LOOP_DIRS = ("sensors", "routines", "agents")
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*\Z")
_BLOCKED_ENV_EXACT = frozenset(
    {
        "LC_ALL",
        "LANG",
        "BASH_ENV",
        "BASHOPTS",
        "ENV",
        "FPATH",
        "IFS",
        "SHELL",
        "SHELLOPTS",
        "PS4",
        "PROMPT_COMMAND",
        "ZDOTDIR",
        "PATH",
        "HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SSLKEYLOGFILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "CURL_HOME",
        "NODE_EXTRA_CA_CERTS",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PERL5OPT",
        "PERL5LIB",
        "PERLLIB",
        "RUBYOPT",
        "RUBYLIB",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "CLASSPATH",
        "SSH_ASKPASS",
        "SUDO_ASKPASS",
        "GCONV_PATH",
        "GLIBC_TUNABLES",
    }
)
_BLOCKED_ENV_PREFIXES = ("BF_", "LD_", "DYLD_", "PYTHON", "GIT_", "XDG_", "NPM_CONFIG_", "OPENSSL_")


@dataclass(frozen=True)
class EventMatch:
    kind: str = ""
    source: str = ""
    where: dict[str, Any] = field(default_factory=dict)
    debounce: int = 0


@dataclass(frozen=True)
class LoopSpec:
    name: str
    run: tuple[str, ...] = ()
    description: str = ""
    daemon: bool = False
    ingest: str = ""
    mode: Literal["upsert", "snapshot"] = "upsert"
    schema_name: str = ""
    emit: str = ""
    schedules: tuple[ScheduleSpec, ...] = ()
    events: tuple[EventMatch, ...] = ()
    backfill: int = 86400
    overlap: int = 0
    timeout: int = 300
    enabled: bool = True
    env: dict[str, str] = field(default_factory=dict)
    env_from: tuple[str, ...] = ()
    pool: str = "default"


@dataclass(frozen=True)
class Config:
    name: str
    description: str = ""
    notes: tuple[str, ...] = DEFAULT_NOTES
    brains: dict[str, str] = field(default_factory=dict)
    search_exclude: tuple[str, ...] = ()
    timezone: str = "UTC"
    concurrency: int = 4
    env: dict[str, str] = field(default_factory=dict)
    loops: dict[str, LoopSpec] = field(default_factory=dict)
    pools: dict[str, int] = field(default_factory=dict)

    @property
    def reserved_sources(self) -> frozenset[str]:
        return RESERVED_SOURCES | frozenset(self.notes)

    def effective_pools(self) -> dict[str, int]:
        """Return named concurrency pool limits (defaulting to `{'default': concurrency}`)."""
        if not self.pools:
            return {"default": self.concurrency}
        return {"default": self.concurrency, **self.pools}

    def total_concurrency(self) -> int:
        """Return bounded total worker count across all declared pools (`1..64`)."""
        return min(64, max(1, sum(self.effective_pools().values())))


_LOADED: dict[str, Config] = {}
_LOADED_LOCK = threading.Lock()


@cache
def _bf_validator() -> Validator:
    return compile_schema(BF_SCHEMA, "bf.schema.json")


def _check_tz(tz: str, label: str) -> str:
    if "\n" in tz or "\r" in tz:
        raise Error(f"{label}: unknown IANA timezone {tz!r}")
    try:
        cron.resolve_tz(tz)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise Error(f"{label}: unknown IANA timezone {tz!r}") from error
    return tz


def is_blocked_env_key(key: str) -> bool:
    """Return True if `key` is reserved or can hijack dynamic linkers, TLS/proxies, interpreters, or shells."""
    return key in _BLOCKED_ENV_EXACT or key.startswith(_BLOCKED_ENV_PREFIXES)


def _check_env(key: str, val: str, label: str, key_err: str) -> str:
    if "\n" in key or not _ENV_KEY_RE.fullmatch(key):
        raise Error(key_err)
    if is_blocked_env_key(key):
        raise Error(f"{label}: cannot override reserved or protected environment variable {key!r}")
    return models.clean_env(val, label)


def _parse_env_map(raw: object, label: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): _check_env(str(k), str(v), f"{label}.{k}", f"{label}: invalid environment variable name {k!r}")
        for k, v in raw.items()
    }


def _parse_env_from(raw: object, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw:
        key = str(item)
        if "\n" in key or not _ENV_KEY_RE.fullmatch(key):
            raise Error(f"{label}: invalid environment variable name {key!r}")
        if is_blocked_env_key(key):
            raise Error(f"{label}: cannot allowlist reserved or protected variable {key!r}")
        out.append(key)
    return tuple(dict.fromkeys(out))


def load_dotenv(store: Store) -> dict[str, str]:
    """Load optional gitignored `0600` `.env` from brain root without shell or variable interpolation."""
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


def normalize_brain_script(tok: str, brain_root: Path | None = None) -> str:
    """Return confined brain-relative script path without `.`/`..` segments when `tok` targets a brain file, else `''`."""
    if not tok or tok.startswith(("-", "http://", "https://", "bf://", "~", "{{state}}")):
        return ""
    raw = _extract_loop_script_target(tok, brain_root)
    if raw is None and "/" in tok and not tok.startswith("/"):
        raw = tok
    if not raw:
        return ""
    try:
        return "/".join(relative(raw))
    except Error:
        return ""


def _extract_loop_script_target(tok: str, brain_root: Path | None, *, loop_name: str = "") -> str | None:
    """Return raw brain-relative path string if `tok` targets a path inside the brain (single-pass, non-recursive)."""
    if tok in {"{{brain}}", "{{state}}"}:
        return None
    if tok.startswith("{{brain}}/"):
        return tok.removeprefix("{{brain}}/")
    if tok.startswith("{{state}}/"):
        relative(tok.removeprefix("{{state}}/"))
        return None
    root_str = brain_root.as_posix().rstrip("/") if brain_root is not None else ""
    state_dir = f"{root_str}/.bf/state/{loop_name}" if (root_str and loop_name) else ""
    cur = tok
    if root_str and "{{" in cur and "}}" in cur:
        preview = {"brain": root_str, "loop": loop_name, "state": state_dir or f"{root_str}/.bf/state/loop"}
        cur = models.PLACEHOLDER_RE.sub(lambda m: preview.get(m[1], m[0]), cur)
        if cur in {root_str, preview["state"]}:
            return None
    if state_dir and (cur == state_dir or cur.startswith(f"{state_dir}/")):
        if cur != state_dir:
            relative(cur[len(state_dir) + 1 :])
        return None
    if cur.startswith("./"):
        return cur[2:]
    if cur.startswith("/"):
        if not root_str:
            return None
        if cur.startswith(f"{root_str}/"):
            return cur[len(root_str) + 1 :]
        collapsed = "/" + cur.lstrip("/")
        norm = posixpath.normpath(collapsed)
        if collapsed.startswith(f"{root_str}/") or norm.startswith(f"{root_str}/"):
            return cur
        return None
    if "/" in cur:
        head = cur.split("/", 1)[0]
        norm_head = posixpath.normpath(cur).split("/", 1)[0]
        brain_dirs = (*LOOP_DIRS, "evals", "clients", "prompts")
        if head in brain_dirs or norm_head in brain_dirs:
            return cur
        segs = cur.split("/")
        if root_str and ("." in segs or ".." in segs):
            joined = posixpath.normpath(posixpath.join(root_str, cur))
            if joined == root_str or joined.startswith(f"{root_str}/"):
                return cur
    return None


def _check_brain_relative_arg(store: Store, raw: str, name: str) -> None:
    """Reject any brain-relative path argument containing symlinks inside `store.root` without false-positiving non-paths."""
    if (
        raw in {"", ".", ".."}
        or raw.startswith(("/", "-", "http://", "https://", "bf://", "~"))
        or "\x00" in raw
        or "\\" in raw
    ):
        return
    cur = store.root
    for part in (p for p in raw.split("/") if p and p != "."):
        if part == "..":
            return
        cur = cur / part
        try:
            info = cur.lstat()
        except OSError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise Error(f"loops.{name}: symlink {raw} is not allowed inside brain")


def _check_script_arg(store: Store, tok: str, name: str, *, flag_rhs: bool = False) -> None:
    if tok.startswith(("http://", "https://", "bf://", "~")):
        return
    if (raw_rel := _extract_loop_script_target(tok, store.root, loop_name=name)) is not None:
        relative(raw_rel)
        if not (store.exists(raw_rel) or store.is_dir(raw_rel)):
            what = f"path {raw_rel} must exist" if flag_rhs else f"script {raw_rel} must be a regular file"
            raise Error(f"loops.{name}: {what} inside brain")
    else:
        _check_brain_relative_arg(store, tok, name)


def validate_loop_scripts(store: Store, argv: Sequence[str], name: str) -> None:
    """Verify every brain-relative or `{{brain}}/...` script path in `argv` has no `.`/`..` segments and exists via `O_NOFOLLOW`."""
    if not argv:
        return
    if argv[0] != "bf" and "/" in argv[0]:
        if (raw0 := _extract_loop_script_target(argv[0], store.root, loop_name=name)) is not None:
            relative(raw0)
            if not store.exists(raw0):
                raise Error(f"loops.{name}: script {raw0} must be a regular file inside brain")
        elif not Path(argv[0]).is_absolute():
            cleaned = argv[0].removeprefix("./")
            relative(cleaned)
            if not store.exists(cleaned):
                raise Error(f"loops.{name}: command {argv[0]} must be a regular file inside brain")
    for tok in argv[1:]:
        if tok.startswith("-"):
            if "=" in tok:
                _check_script_arg(store, tok.split("=", 1)[1], name, flag_rhs=True)
            continue
        _check_script_arg(store, tok, name)


def resolve_loop(
    store: Store, loop: LoopSpec, reserved: frozenset[str] = models.RESERVED_SOURCES
) -> tuple[tuple[str, ...], str, Literal["upsert", "snapshot"], str, int]:
    """Resolve `(argv, ingest_source, ingest_mode, schema_name, timeout)` with zero hidden ingest/timeout magic.

    Folder conventions (`sensors/`, `routines/`, `agents/`) locate the script ONLY when `run:` is omitted;
    `ingest:` and `timeout:` always come explicitly from `bf.yaml`.
    """
    if loop.ingest and loop.ingest in reserved:
        raise Error(f"loops.{loop.name}: ingest source {loop.ingest!r} is reserved")
    if loop.run:
        argv = loop.run
    else:
        found: list[tuple[str, os.stat_result]] = []
        for fld in LOOP_DIRS:
            for ext in ("", ".sh", ".py"):
                cand = f"{fld}/{loop.name}{ext}"
                try:
                    info = store.stat(cand)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise Error(f"loops.{loop.name}: {cand}: expected a regular file")
                found.append((cand, info))
        if not found:
            raise Error(f"loops.{loop.name}: no run command and no script found in sensors/, routines/, or agents/")
        if len(found) > 1:
            names = ", ".join(c for c, _ in found)
            raise Error(f"loops.{loop.name}: ambiguous convention scripts ({names}); keep one or set run:")
        cand, info = found[0]
        if info.st_mode & stat.S_IXUSR:
            argv = (cand,)
        elif cand.endswith(".sh"):
            argv = ("bash", cand)
        elif cand.endswith(".py"):
            argv = (shutil.which("python3") or sys.executable, cand)
        else:
            raise Error(f"loops.{loop.name}: {cand} is not executable; run chmod +x {cand}")
    return argv, loop.ingest, loop.mode, (loop.schema_name or loop.ingest) if loop.ingest else "", loop.timeout


def _validate_cron(raw_expr: str, label: str) -> str:
    expr = " ".join(models.clean(raw_expr.strip(), label).split())
    if cron.is_valid_cron(expr):
        return expr
    raise Error(f"{label}: must be an explicit duration (e.g. 1h, 1d), @macro, or 5-field cron expression")


def _check_ident(value: str, pat: re.Pattern[str], label: str, msg: str) -> str:
    if "\n" in value or "\r" in value or not pat.fullmatch(value):
        raise Error(f"{label}: {msg}")
    return value


def _check_where(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        raise Error(f"{label}: where must be a non-empty mapping")
    out: dict[str, Any] = {}
    for path_key, pred in raw.items():
        if not isinstance(path_key, str) or "\n" in path_key or "\x00" in path_key:
            raise Error(f"{label}: invalid where path key")
        if isinstance(pred, str):
            models.clean_text(pred, f"{label}.{path_key}")
        elif isinstance(pred, dict):
            if "prefix" in pred and isinstance(pred["prefix"], str):
                models.clean_text(pred["prefix"], f"{label}.{path_key}.prefix")
            elif "in" in pred and isinstance(pred["in"], list):
                for item in pred["in"]:
                    if isinstance(item, str):
                        models.clean_text(item, f"{label}.{path_key}.in")
        out[path_key] = pred
    return out


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
        if isinstance(item, dict) and "event" in item:
            ev = item["event"]
            deb_s = (
                models.parse_duration(item["debounce"], f"{label}.debounce", maximum=3600) if "debounce" in item else 0
            )
            if isinstance(ev, str):
                ev_kind = _check_ident(ev, models.EVENT_KIND_RE, f"{label}.event", "invalid event kind")
                events.append(EventMatch(kind=ev_kind, debounce=deb_s))
            elif isinstance(ev, dict):
                kind = str(ev.get("kind", ""))
                if kind:
                    _check_ident(kind, models.EVENT_KIND_RE, f"{label}.event.kind", "invalid event kind")
                src = str(ev.get("source", ""))
                if src:
                    _check_ident(src, models.NAME_RE, f"{label}.event.source", "invalid event source")
                where = _check_where(ev["where"], f"{label}.event.where") if "where" in ev else {}
                events.append(EventMatch(kind, src, where, deb_s))
        elif isinstance(item, dict) and ("every" in item or "cron" in item):
            bf_s = models.parse_duration(item["backfill"], f"{label}.backfill") if "backfill" in item else None
            ov_s = (
                models.parse_duration(item["overlap"], f"{label}.overlap", allow_zero=True)
                if "overlap" in item
                else None
            )
            if "every" in item:
                secs = models.parse_duration(item["every"], f"{label}.every")
                add_sched(ScheduleSpec(f"every:{secs}", every=secs, backfill=bf_s, overlap=ov_s))
            else:
                expr = _validate_cron(str(item["cron"]), f"{label}.cron")
                add_sched(ScheduleSpec(f"cron:{expr}", cron=expr, backfill=bf_s, overlap=ov_s))
        elif isinstance(item, str) and bool(models.DURATION_RE.fullmatch(item.strip())) and "\n" not in item:
            secs = models.parse_duration(item, label)
            add_sched(ScheduleSpec(f"every:{secs}", every=secs))
        else:
            expr = _validate_cron(str(item), label)
            add_sched(ScheduleSpec(f"cron:{expr}", cron=expr))
    return False, tuple(schedules), tuple(events)


def _parse_loop(name: str, raw: dict[str, object], *, reserved: frozenset[str]) -> LoopSpec:
    label = f"bf.yaml loops.{name}"
    _check_ident(name, models.NAME_RE, label, "loop name must be lowercase letters, digits, or '-'")
    daemon, schedules, events = _parse_when(raw.get("when"), f"{label}.when")
    emit = str(raw.get("emit") or "")
    if emit:
        _check_ident(emit, models.EVENT_KIND_RE, f"{label}.emit", "invalid event kind")
    raw_ingest = raw.get("ingest")
    schema_name = ""
    if isinstance(raw_ingest, str):
        if raw_ingest == "snapshot":
            ingest, mode = name, "snapshot"
        elif raw_ingest == "upsert":
            ingest, mode = name, "upsert"
        else:
            raise Error(f"{label}.ingest: must be 'upsert' or 'snapshot'")
    elif isinstance(raw_ingest, dict):
        ingest = str(raw_ingest.get("source") or name)
        _check_ident(ingest, models.NAME_RE, f"{label}.ingest.source", "invalid source name")
        mode = "snapshot" if raw_ingest.get("mode") == "snapshot" else "upsert"
        if raw_schema := raw_ingest.get("schema"):
            schema_name = _check_ident(str(raw_schema), models.NAME_RE, f"{label}.ingest.schema", "invalid schema name")
            if schema_name in models.RESERVED_SOURCES:
                raise Error(f"{label}.ingest.schema: schema name {schema_name!r} is reserved")
    else:
        ingest, mode = "", "upsert"
    if ingest and ingest in reserved:
        raise Error(f"{label}.ingest: source {ingest!r} is reserved")

    raw_run = raw.get("run")
    run = tuple(models.clean_env(str(item), f"{label}.run") for item in raw_run) if isinstance(raw_run, list) else ()
    for idx, arg in enumerate(run):
        for match in models.ANY_PLACEHOLDER_RE.finditer(arg):
            if idx == 0:
                raise Error(f"{label}.run[0]: placeholders are not allowed in executable name; use a relative path")
            inner = match[0][2:-2].strip()
            if match[0] != f"{{{{{inner}}}}}" or inner not in models.PLACEHOLDERS:
                allowed = ", ".join(f"{{{{{k}}}}}" for k in sorted(models.PLACEHOLDERS))
                raise Error(f"{label}.run: unknown placeholder {match[0]}; allowed: {allowed}")
    own_win = rw if isinstance(rw := raw.get("window"), dict) else {}
    bf_s = models.parse_duration(own_win.get("backfill", "1d"), f"{label}.window.backfill")
    ov_s = models.parse_duration(own_win.get("overlap", "0s"), f"{label}.window.overlap", allow_zero=True)
    to_s = models.parse_duration(raw.get("timeout", "5m"), f"{label}.timeout", maximum=86_400)
    desc = models.clean_text(str(raw.get("description") or ""), f"{label}.description")
    env_m = _parse_env_map(raw.get("env"), f"{label}.env")
    env_from = _parse_env_from(raw.get("env_from"), f"{label}.env_from")
    raw_pool = raw.get("pool")
    pool = (
        _check_ident(str(raw_pool), models.NAME_RE, f"{label}.pool", "invalid pool name")
        if raw_pool is not None
        else "default"
    )
    return LoopSpec(
        name=name,
        run=run,
        description=desc,
        daemon=daemon,
        ingest=ingest,
        mode=mode,
        schema_name=schema_name,
        emit=emit,
        schedules=schedules,
        events=events,
        backfill=bf_s,
        overlap=ov_s,
        timeout=to_s,
        enabled=bool(raw.get("enabled", True)),
        env=env_m,
        env_from=env_from,
        pool=pool,
    )


def _parse_concurrency(raw: object) -> tuple[int, dict[str, int]]:
    if raw is None:
        return 4, {}
    if type(raw) is int:
        return raw, {}
    if isinstance(raw, dict):
        pools = {
            _check_ident(str(k), models.NAME_RE, "bf.yaml concurrency", "invalid pool name"): int(v)
            for k, v in raw.items()
        }
        default_cap = pools.get("default", 4)
        full_pools = {"default": default_cap, **pools}
        return default_cap, full_pools
    return 4, {}


def _parse_config(raw: dict[str, object]) -> Config:
    if problems := validate_instance(_bf_validator(), raw, "bf.yaml"):
        raise Error("; ".join(problems))
    brain_name = _check_ident(str(raw["name"]), models.NAME_RE, "bf.yaml name", "invalid brain name")
    description = models.clean_text(str(raw.get("description") or ""), "bf.yaml description")
    raw_notes = raw.get("notes")
    if isinstance(raw_notes, list):
        notes = tuple(
            _check_ident(str(folder), models.NAME_RE, "bf.yaml notes", "invalid note folder name")
            for folder in raw_notes
        )
        for folder in notes:
            if folder in RESERVED_SOURCES:
                raise Error(f"bf.yaml notes: folder {folder!r} is reserved")
    else:
        notes = DEFAULT_NOTES
    raw_brains = rb if isinstance(rb := raw.get("brains"), dict) else {}
    brains = {
        _check_ident(str(alias), models.NAME_RE, "bf.yaml brains", "invalid brain alias"): models.clean(
            str(target), f"bf.yaml brains.{alias}"
        )
        for alias, target in raw_brains.items()
    }
    reserved = RESERVED_SOURCES | frozenset(notes)
    raw_search = rs if isinstance(rs := raw.get("search"), dict) else {}
    raw_excl = raw_search.get("exclude")
    search_exclude: tuple[str, ...] = ()
    if isinstance(raw_excl, list):
        excl_list: list[str] = []
        for item in raw_excl:
            src = _check_ident(str(item), models.NAME_RE, "bf.yaml search.exclude", "invalid source name")
            if src in reserved:
                raise Error(f"bf.yaml search.exclude: source {src!r} is reserved")
            excl_list.append(src)
        search_exclude = tuple(dict.fromkeys(excl_list))
    tz = _check_tz(str(raw.get("timezone") or "UTC"), "bf.yaml timezone")
    concurrency, pools = _parse_concurrency(raw.get("concurrency"))
    valid_pools = pools or {"default": concurrency}
    base_env = _parse_env_map(raw.get("env"), "bf.yaml env")
    raw_loops = rl if isinstance(rl := raw.get("loops"), dict) else {}
    loops = {
        str(k): _parse_loop(str(k), v if isinstance(v, dict) else {}, reserved=reserved) for k, v in raw_loops.items()
    }
    seen_schemas: dict[str, tuple[str, str]] = {}
    for lp_name, lp in loops.items():
        if not lp.daemon and lp.pool not in valid_pools:
            raise Error(
                f"bf.yaml loops.{lp_name}.pool: unknown concurrency pool {lp.pool!r}; declare it under concurrency:"
            )
        if lp.ingest:
            eff_schema = lp.schema_name or lp.ingest
            if (prev := seen_schemas.get(lp.ingest)) is not None and prev[1] != eff_schema:
                raise Error(
                    f"bf.yaml loops.{lp_name}: ingest.schema {eff_schema!r} conflicts with loops.{prev[0]} ({prev[1]!r}) for source {lp.ingest!r}"
                )
            seen_schemas[lp.ingest] = (lp_name, eff_schema)
    return Config(brain_name, description, notes, brains, search_exclude, tz, concurrency, base_env, loops, pools)


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


def inside_brain() -> Store | None:
    """Return the nearest enclosing user-owned brain directory, skipping foreign/non-regular `bf.yaml` files."""
    here, owner = Path.cwd(), os.geteuid()
    for candidate in (here, *here.parents):
        try:
            info = (candidate / "bf.yaml").lstat()
            dir_info = candidate.stat()
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid == owner and dir_info.st_uid == owner:
            return Store(candidate)
    return None


def _open_brain(chosen: str) -> Store:
    store = Store(expand(Path(chosen)))
    try:
        info = store.stat("bf.yaml").st_mode
    except FileNotFoundError:
        raise Error(f"{chosen} has no bf.yaml; pass the brain's root directory or run bf init") from None
    if not stat.S_ISREG(info):
        raise Error(f"{chosen}/bf.yaml must be a regular file")
    return store


def _owned_brain(path: Path, owner: int) -> bool:
    ds, ys = path.stat(), (path / "bf.yaml").lstat()
    return stat.S_ISDIR(ds.st_mode) and ds.st_uid == owner and stat.S_ISREG(ys.st_mode) and ys.st_uid == owner


def select(value: str = "") -> Store:
    """Resolve `--brain PATH` > `BF_BRAIN` > enclosing `bf.yaml` > `~/brain`."""
    if chosen := value.strip():
        return _open_brain(chosen)
    if chosen := os.environ.get("BF_BRAIN", "").strip():
        return _open_brain(chosen)
    if nearest := inside_brain():
        return nearest
    with suppress(RuntimeError, OSError):
        default_dir = Path.home() / "brain"
        if _owned_brain(default_dir, os.geteuid()):
            return Store(default_dir)
    raise Error("no brain selected; pass --brain PATH, set BF_BRAIN, run inside a brain, or run bf init ~/brain")


def resolve_brain(current: Store, cfg: Config, host: str) -> Store:
    """Resolve a `bf://<host>/...` brain target strictly from self or `bf.yaml` `brains:`."""
    if host in {"", cfg.name}:
        return current
    if host not in cfg.brains:
        raise InputError(f"brain {host!r} is not declared in bf.yaml brains:")
    raw_t = expand(Path(cfg.brains[host]))
    cand = raw_t if raw_t.is_absolute() else (current.root / raw_t)
    with suppress(OSError, ValueError):
        res = cand.resolve(strict=True)
        if _owned_brain(res, os.geteuid()):
            target = Store(res)
            load(target)
            return target
    raise Error(f"unknown or inaccessible brain {host!r}; check brains.{host} in bf.yaml")
