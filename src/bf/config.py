"""Strict YAML, one brain configuration, and the user's brain registry."""

from __future__ import annotations

import os
import re
import stat
from contextlib import suppress
from functools import cache
from itertools import takewhile
from pathlib import Path
from typing import cast

import yaml
import yaml.constructor
import yaml.resolver
from pydantic import ValidationError

from bf.models import NAME, Config, Error, InputError, Registration, UserConfig, check_version, digest, explain, suggest
from bf.storage import Store, expand, writer, xdg

_HEADER = "# https://fmind.github.io/brain-framework/docs/reference/\n"
# libyaml parses (~9x faster than pure Python); BF requires CSafeLoader so YAML loads identically everywhere.
LIBYAML = (
    "reading YAML needs PyYAML built with libyaml, which this installation lacks; reinstall bf with a PyYAML wheel "
    "that bundles it: uv tool install --reinstall --python 3.14 brain-framework, or 'brain-framework[mcp]' with MCP"
)
_LOADED: dict[str, Config] = {}
ABSENT = "registered brain directory is absent on this machine; restore it, run bf register at its new place, or remove its registry entry"
# YAML 1.2 core schema: `on`/`no`/`off` stay strings, `017` is decimal, `1:30` is text.
_FLOAT_RE = r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
_CORE = (
    ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", list("-+0123456789")),
    ("float", _FLOAT_RE, list("-+.0123456789")),
)


def _integer(loader: yaml.constructor.SafeConstructor, node: yaml.ScalarNode) -> int:
    """YAML 1.2 decimal/octal/hex integer preserving Python's decimal digit limit."""
    value = str(loader.construct_scalar(node))
    base = {"0o": 8, "0x": 16}.get(value[:2])
    try:
        number = int(value[2:], base) if base else int(value, 10)
        str(number)
    except ValueError:
        raise yaml.constructor.ConstructorError(None, None, "invalid integer", node.start_mark) from None
    return number


class _KeyError(yaml.constructor.ConstructorError):
    """A duplicate or non-string mapping key, reported without quoting its text."""


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
    """libyaml's safe loader restricted to YAML 1.2 scalars and unique string mapping keys."""
    loader = cast("type[yaml.CSafeLoader]", type("StrictLoader", (base,), {}))
    loader.yaml_implicit_resolvers = {}
    for tag, pattern, first in _CORE:
        loader.add_implicit_resolver(f"tag:yaml.org,2002:{tag}", re.compile(pattern), first)
    loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    loader.add_constructor("tag:yaml.org,2002:int", _integer)
    loader.add_constructor("tag:yaml.org,2002:timestamp", lambda constructor, node: constructor.construct_scalar(node))
    return loader


def yaml_object(data: bytes, name: str = "", *, line: int = 1) -> dict[str, object]:
    """Parse one UTF-8 YAML mapping without anchors, aliases, duplicate keys or deep nesting."""
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
            starts = isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent))
            depth += starts - isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent))
            if depth > 32 or count > 20_000:
                raise Error("YAML structure exceeds its limit")
        value = yaml.load(data, Loader=loader)  # noqa: S506 - restricted CSafeLoader subclass
    except (yaml.YAMLError, ValueError, OverflowError, LookupError, TypeError) as error:
        mark = getattr(error, "problem_mark", None)
        reason = "YAML mapping keys must be unique strings" if isinstance(error, _KeyError) else "invalid YAML"
        loc = f" at line {mark.line + line}, column {mark.column + 1}" if mark else ""
        raise Error(f"{reason}{loc}") from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Error("YAML must contain one mapping")
    return cast(dict[str, object], value)


def load(store: Store) -> Config:
    """Validate and cache `bf.yaml` per content digest; callers must treat nested dicts as read-only."""
    try:
        data = store.read("bf.yaml", 1 << 20)
    except FileNotFoundError:
        raise Error("bf.yaml is missing; restore it, or select the brain's root directory") from None
    if (config := _LOADED.get(key := digest(data))) is None:
        value = yaml_object(data, "bf.yaml")
        check_version(value, "bf.yaml")
        try:
            config = Config.model_validate(value)
        except ValidationError as error:
            raise Error("invalid bf.yaml: " + explain(error)) from error
        if len(_LOADED) >= 64:
            _LOADED.clear()
        _LOADED[key] = config
    return config


def user_path() -> Path:
    return xdg("XDG_CONFIG_HOME", ".config") / "bf" / "config.yaml"


def _registry() -> tuple[UserConfig, str]:
    path = user_path()
    try:
        if not path.parent.exists():
            return UserConfig(), ""
        data = Store(path.parent).read(path.name, 1 << 20)
    except FileNotFoundError:
        return UserConfig(), ""
    try:
        registry = UserConfig.model_validate(yaml_object(data, str(path)))
    except ValidationError as error:
        raise Error(f"invalid {path}: " + explain(error)) from error
    block = list(takewhile(lambda ln: ln.startswith("#") or not ln.strip(), data.decode().splitlines(keepends=True)))
    while block and not block[-1].startswith("#"):
        block.pop()
    header = "".join(block)
    return registry, header if not header or header.endswith("\n") else header + "\n"


def user_config() -> UserConfig:
    return _registry()[0]


def _identity(entry: Registration) -> str | None:
    try:
        return Store(Path(entry.path).expanduser()).identity
    except Error:
        return None


def _location(entry: Registration) -> Path:
    if not entry.path.startswith("~"):
        return Path(entry.path)
    head, *rest = Path(entry.path).parts
    home = Path(head).expanduser()
    try:
        return home.resolve(strict=True).joinpath(*rest)
    except OSError, RuntimeError:
        return home.joinpath(*rest)


def _write_registry(registry_store: Store, path: Path, header: str, registry: UserConfig) -> None:
    doc = {
        **({"default": registry.default} if registry.default is not None else {}),
        "brains": {k: v.model_dump() for k, v in sorted(registry.brains.items())},
    }
    registry_store.write(path.name, ((header or _HEADER) + yaml.safe_dump(doc, sort_keys=False)).encode())


def _match_registered(brains: dict[str, Registration], target: str | Store, path: Path) -> str:
    if isinstance(target, Store):
        identity, root, missing = target.identity, target.root, f"this brain is not registered in {path}"
    elif "/" in target or target in {".", "..", "~"}:
        expanded = expand(Path(target))
        identity, root, missing = None, expanded, f"{target} is not registered in {path}"
        with suppress(Error):
            identity = Store(expanded).identity
    elif target not in brains:
        raise Error(f"brain {target} is not registered{suggest(target, brains)}")
    else:
        return target
    for name, entry in sorted(brains.items()):
        if (identity is not None and _identity(entry) == identity) or _location(entry) == root:
            return name
    raise Error(missing)


def register(store: Store, *, name: str = "", default: bool = False) -> dict[str, object]:
    """Add or update this brain in the user registry, optionally under an alias and/or as default."""
    alias = name or load(store).name
    if name and not re.fullmatch(NAME, alias):
        raise InputError(
            "brain name must start with a lowercase letter and use only lowercase letters, digits and hyphens", "name"
        )
    path = user_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    busy = "another bf register is updating the registry; retry after it finishes"
    with writer(Store(path.parent), wait=30, busy=busy):
        registry, header = _registry()
        for other, entry in registry.brains.items():
            if other != alias and _identity(entry) == store.identity:
                raise Error(
                    f"this brain is already registered as {other}; unregister {other} from {path}, then run bf register again"
                )
        replaced = ""
        if (existing := registry.brains.get(alias)) and _location(existing) != store.root:
            try:
                if (reg := _registered(existing)) is not None:
                    reg.mode("bf.yaml")
            except FileNotFoundError:
                reg = None
            except (Error, OSError) as error:
                raise Error(
                    f"cannot reach the brain registered as {alias} at {existing.path}; restore access to it, or remove that entry from {path}"
                ) from error
            if reg is not None and reg.identity != store.identity:
                raise Error(
                    f"another brain is already registered as {alias} at {existing.path}; remove that entry from {path}, or pass a different --name"
                )
            replaced = existing.path
        if existing is None or replaced or (default and registry.default != alias):
            if existing is None or replaced:
                registry.brains[alias] = Registration(path=str(store.root))
            if default:
                registry.default = alias
            _write_registry(Store(path.parent), path, header, registry)
    out: dict[str, object] = {"brain": alias, "path": str(store.root), "config": str(path)}
    if registry.default == alias:
        out["default"] = True
    if replaced:
        out["replaced"] = replaced
    return out


def unregister(target: str | Store) -> dict[str, object]:
    """Remove a registered brain by name, path or Store, clearing `default` when removing the default brain."""
    path = user_path()
    missing = "this brain is not registered" if isinstance(target, Store) else f"brain {target} is not registered"
    try:
        if not path.parent.exists():
            raise FileNotFoundError
        (registry_store := Store(path.parent)).mode(path.name)
    except FileNotFoundError:
        raise Error(missing) from None
    busy = "another bf unregister is updating the registry; retry after it finishes"
    with writer(registry_store, wait=30, busy=busy):
        registry, header = _registry()
        matched = _match_registered(registry.brains, target, path)
        removed = registry.brains.pop(matched)
        if cleared := (registry.default == matched):
            registry.default = None
        _write_registry(registry_store, path, header, registry)
    out: dict[str, object] = {"brain": matched, "path": removed.path, "config": str(path)}
    if cleared:
        out["cleared_default"] = True
    return out


def registration(store: Store) -> tuple[str, Registration] | None:
    """This brain's `(name, Registration)` entry in the user registry by physical identity, or None."""
    try:
        return next(((n, e) for n, e in sorted(user_config().brains.items()) if _identity(e) == store.identity), None)
    except Error, OSError, RuntimeError, UnicodeError:
        return None


def is_registered(store: Store) -> bool:
    return registration(store) is not None


def _registered(entry: Registration) -> Store | None:
    try:
        return Store(Path(entry.path).expanduser())
    except Error as error:
        cause = error.__cause__
        if isinstance(cause, OSError) and not isinstance(cause, FileNotFoundError | NotADirectoryError):
            raise Error("registered brain directory exists but cannot be reached; restore access to it") from cause
        return None


def _open_registered(entry: Registration, unreachable_label: str, label: str) -> Store:
    try:
        store = _registered(entry)
    except Error:
        raise Error(f"cannot reach {unreachable_label}; restore access to its directory") from None
    if store is None:
        raise Error(f"{label}: {ABSENT}")
    return _configured(store, label)


def _located(value: str, *, execute: bool = False) -> Store:
    if "/" in value or value in {".", "..", "~"}:
        return _configured(Store(expand(Path(value))), value)
    brains = user_config().brains
    try:
        near = _nearest(required=False)
        enclosing = (near, load(near)) if near is not None else None
    except Error, OSError, UnicodeError:
        if value not in brains:
            raise
        enclosing = None
    if (entry := brains.get(value)) is not None:
        store = _open_registered(entry, f"the brain registered as {value}", f"registered brain {value}")
        if enclosing and enclosing[1].name == value and enclosing[0].identity != store.identity:
            raise Error(
                f"ambiguous brain name {value}: the registry and the enclosing brain name different directories; pass --brain PATH"
            )
        return store
    if enclosing and enclosing[1].name == value:
        return enclosing[0]
    names = {*brains, *({enclosing[1].name} if enclosing else ())}
    if execute:
        raise Error(
            f"brain {value} is neither registered nor the enclosing brain; pass --brain PATH{suggest(value, names)}"
        )
    if not (path := expand(Path(value))).exists():
        raise Error(f"brain directory does not exist; pass an existing --brain or BF_BRAIN{suggest(value, names)}")
    return _configured(Store(path), value)


def _configured(store: Store, given: str) -> Store:
    try:
        store.mode("bf.yaml")
    except FileNotFoundError:
        raise Error(
            f"{given} has no bf.yaml; pass the brain's root directory, or create a brain with bf init"
        ) from None
    return store


def _nearest(*, required: bool = True) -> Store | None:
    here, owner = Path.cwd(), os.geteuid()
    for up, candidate in enumerate((here, *here.parents)):
        try:
            info = (candidate / "bf.yaml").lstat()
        except FileNotFoundError, NotADirectoryError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or candidate.stat().st_uid != owner:
            if not required:
                return None
            raise Error(
                f"{'../' * up or './'}bf.yaml is not a regular file owned by you; pass --brain PATH to select a brain"
            )
        return Store(candidate)
    return None


def select(value: str = "", *, execute: bool = False) -> Store:
    """Single-brain selection: --brain NAME|PATH, then BF_BRAIN, then the enclosing brain, then default."""
    if chosen := value or os.environ.get("BF_BRAIN", ""):
        return _located(chosen, execute=execute)
    if nearest := _nearest():
        return nearest
    registry = user_config()
    if registry.default is None:
        hint = (
            "no default brain configured; pass --brain NAME" if registry.brains else "no brain selected; pass --brain"
        )
        raise Error(f"{hint}, run inside a brain, or register one with --default")
    label = f"default brain {registry.default}"
    return _open_registered(registry.brains[registry.default], label, label)


def brain_name(store: Store) -> str:
    """How to name a brain whose bf.yaml cannot load: its registered name, else its directory name."""
    return match[0] if (match := registration(store)) else store.root.name


def execution(value: str = "") -> Store:
    return select(value, execute=True)


def one(value: str = "") -> Store:
    return select(value)
