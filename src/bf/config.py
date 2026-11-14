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

_HEADER = "# https://fmind.github.io/brain-framework/docs/configuration/\n"


# libyaml parses (~9x faster than pure Python); BF requires CSafeLoader so YAML loads identically everywhere.
LIBYAML = (
    "reading YAML needs PyYAML built with libyaml, which this installation lacks; reinstall bf with a PyYAML wheel "
    "that bundles it: uv tool install --reinstall --python 3.14 brain-framework, or 'brain-framework[mcp]' with MCP"
)
_LOADED: dict[str, Config] = {}
# YAML 1.2 core schema: `on`/`no`/`off` stay strings, `017` is decimal, `1:30` is text.
_CORE = (
    ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", list("-+0123456789")),
    (
        "float",
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        list("-+.0123456789"),
    ),
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


def _loaders() -> tuple[type[yaml.CSafeLoader], type[yaml.CSafeLoader]]:
    base = getattr(yaml, "CSafeLoader", None)
    if base is None:
        raise Error(LIBYAML)
    return base, _strict(base)


def yaml_object(data: bytes, name: str = "", *, line: int = 1) -> dict[str, object]:
    """Parse one UTF-8 YAML mapping without anchors, aliases, duplicate keys or deep nesting."""
    try:
        return _yaml_object(data, line, *_loaders())
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
    key = digest(data)
    if (config := _LOADED.get(key)) is None:
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
    """The optional user registry and its leading comment block; a missing file registers no brain."""
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
    lines = data.decode().splitlines(keepends=True)
    block = list(takewhile(lambda line: line.startswith("#") or not line.strip(), lines))
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
    """Resolve only a leading `~` so symlinked paths remain distinct unless pointing to the same directory."""
    path = Path(entry.path)
    if not entry.path.startswith("~"):
        return path
    head, *rest = path.parts
    home = Path(head).expanduser()
    try:
        return home.resolve(strict=True).joinpath(*rest)
    except OSError, RuntimeError:
        return home.joinpath(*rest)


def _write_registry(registry_store: Store, path: Path, header: str, registry: UserConfig) -> None:
    document = {**({"default": registry.default} if registry.default is not None else {})}
    document["brains"] = {key: value.model_dump() for key, value in sorted(registry.brains.items())}
    registry_store.write(path.name, ((header or _HEADER) + yaml.safe_dump(document, sort_keys=False)).encode())


def _is_path(value: str) -> bool:
    return "/" in value or value in {".", "..", "~"}


def _match_registered(brains: dict[str, Registration], target: str | Store, path: Path) -> str:
    if isinstance(target, Store):
        identity: str | None = target.identity
        root, missing = target.root, f"this brain is not registered in {path}"
    elif _is_path(target):
        expanded = expand(Path(target))
        identity, root, missing = None, expanded, f"{target} is not registered in {path}"
        with suppress(Error):
            identity = Store(expanded).identity
    else:
        if target not in brains:
            raise Error(f"brain {target} is not registered{suggest(target, brains)}")
        return target
    for name, entry in sorted(brains.items()):
        if (identity is not None and _identity(entry) == identity) or _location(entry) == root:
            return name
    raise Error(missing)


def _existing_brain(entry: Registration, alias: str, path: Path) -> Store | None:
    unreachable = f"cannot reach the brain registered as {alias} at {entry.path}; restore access to it, or remove that entry from {path}"
    try:
        if (registered := _registered(entry)) is None:
            return None
        registered.mode("bf.yaml")
        return registered
    except FileNotFoundError:
        return None
    except (Error, OSError) as error:
        raise Error(unreachable) from error


def register(store: Store, *, name: str = "", default: bool = False) -> dict[str, object]:
    """Add or update this brain in the user registry, optionally under an alias and/or as default."""
    alias = name or load(store).name
    if name and not re.fullmatch(NAME, alias):
        raise InputError(
            "brain name must start with a lowercase letter and use only lowercase letters, digits and hyphens", "name"
        )
    path = user_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    registry_store = Store(path.parent)
    with writer(registry_store, wait=30, busy="another bf register is updating the registry; retry after it finishes"):
        registry, header = _registry()
        for other, entry in registry.brains.items():
            if other != alias and _identity(entry) == store.identity:
                raise Error(
                    f"this brain is already registered as {other}; unregister {other} from {path}, then run bf register again"
                )
        replaced = ""
        if (existing := registry.brains.get(alias)) and _location(existing) != store.root:
            registered = _existing_brain(existing, alias, path)
            if registered is not None and registered.identity != store.identity:
                raise Error(
                    f"another brain is already registered as {alias} at {existing.path}; remove that entry from {path}, or pass a different --name"
                )
            replaced = existing.path
        if existing is None or replaced or (default and registry.default != alias):
            if existing is None or replaced:
                registry.brains[alias] = Registration(path=str(store.root))
            if default:
                registry.default = alias
            _write_registry(registry_store, path, header, registry)
    return {
        "brain": alias,
        "path": str(store.root),
        "config": str(path),
        **({"default": True} if registry.default == alias else {}),
        **({"replaced": replaced} if replaced else {}),
    }


def unregister(target: str | Store) -> dict[str, object]:
    """Remove a registered brain by name, path or Store, clearing `default` when removing the default brain."""
    path = user_path()
    missing = "this brain is not registered" if isinstance(target, Store) else f"brain {target} is not registered"
    if not path.parent.exists():
        raise Error(missing)
    registry_store = Store(path.parent)
    try:
        registry_store.mode(path.name)
    except FileNotFoundError:
        raise Error(missing) from None
    with writer(
        registry_store, wait=30, busy="another bf unregister is updating the registry; retry after it finishes"
    ):
        registry, header = _registry()
        matched = _match_registered(registry.brains, target, path)
        removed = registry.brains.pop(matched)
        cleared_default = registry.default == matched
        if cleared_default:
            registry.default = None
        _write_registry(registry_store, path, header, registry)
    return {
        "brain": matched,
        "path": removed.path,
        "config": str(path),
        **({"cleared_default": True} if cleared_default else {}),
    }


def registration(store: Store) -> tuple[str, Registration] | None:
    """This brain's `(name, Registration)` entry in the user registry by physical identity, or None."""
    try:
        brains = user_config().brains
    except Error, OSError, RuntimeError, UnicodeError:
        return None
    for name, entry in sorted(brains.items()):
        if _identity(entry) == store.identity:
            return name, entry
    return None


def is_registered(store: Store) -> bool:
    """Whether this physical brain directory is registered in the user registry."""
    return registration(store) is not None


ABSENT = (
    "registered brain directory is absent on this machine; restore it, run bf register at its new place, or remove "
    "its registry entry"
)
UNREACHABLE = "registered brain directory exists but cannot be reached; restore access to it"


def _registered(entry: Registration) -> Store | None:
    """A registered brain, or None when its directory is absent; raises UNREACHABLE on locked/disconnected paths."""
    try:
        return Store(Path(entry.path).expanduser())
    except Error as error:
        cause = error.__cause__
        if isinstance(cause, OSError) and not isinstance(cause, FileNotFoundError | NotADirectoryError):
            raise Error(UNREACHABLE) from cause
        return None


def _enclosing_named(value: str, *, registered: bool) -> Store | None:
    """The owned enclosing brain if its configured name matches `value`."""
    if (nearest := _nearest(required=False)) is None:
        return None
    try:
        config = load(nearest)
    except Error, OSError, UnicodeError:
        if not registered:
            raise
        return None
    return nearest if config.name == value else None


def _open_registered(entry: Registration, unreachable_label: str, label: str) -> Store:
    try:
        store = _registered(entry)
    except Error:
        raise Error(f"cannot reach {unreachable_label}; restore access to its directory") from None
    if store is None:
        raise Error(f"{label}: {ABSENT}")
    return _configured(store, label)


def _located(value: str, *, execute: bool = False) -> Store:
    """Resolve explicit paths, registered names, enclosing brain name, or (for retrieval only) relative dirs."""
    if _is_path(value):
        return _configured(Store(expand(Path(value))), value)
    entry = user_config().brains.get(value)
    local = _enclosing_named(value, registered=entry is not None)
    if entry is not None:
        store = _open_registered(entry, f"the brain registered as {value}", f"registered brain {value}")
        if local is not None and local.identity != store.identity:
            raise Error(
                f"ambiguous brain name {value}: the registry and the enclosing brain name different directories; "
                "pass --brain PATH"
            )
        return store
    if local is not None:
        return local
    if execute:
        raise Error(
            f"brain {value} is neither registered nor the enclosing brain; pass --brain PATH{suggest(value, _names())}"
        )
    path = expand(Path(value))
    if not path.exists():
        raise Error(f"brain directory does not exist; pass an existing --brain or BF_BRAIN{suggest(value, _names())}")
    return _configured(Store(path), value)


def _names() -> set[str]:
    names = set(user_config().brains)
    with suppress(Error, OSError, UnicodeError):
        if (nearest := _nearest(required=False)) is not None:
            names.add(load(nearest).name)
    return names


def _configured(store: Store, given: str) -> Store:
    """Require `bf.yaml` at the selected brain root without revealing resolved filesystem paths."""
    try:
        store.mode("bf.yaml")
    except FileNotFoundError:
        raise Error(
            f"{given} has no bf.yaml; pass the brain's root directory, or create a brain with bf init"
        ) from None
    return store


def _nearest(*, required: bool = True) -> Store | None:
    """The enclosing brain, trusted only when you own its directory and bf.yaml."""
    here = Path.cwd()
    for up, candidate in enumerate((here, *here.parents)):
        try:
            info = (candidate / "bf.yaml").lstat()
        except FileNotFoundError, NotADirectoryError:
            continue
        owner = os.geteuid()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or candidate.stat().st_uid != owner:
            if not required:
                return None
            location = "../" * up or "./"
            raise Error(f"{location}bf.yaml is not a regular file owned by you; pass --brain PATH to select a brain")
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
    """Select one brain for commands or execution: --brain, BF_BRAIN, enclosing, then default."""
    return select(value, execute=True)


def one(value: str = "") -> Store:
    return select(value)
