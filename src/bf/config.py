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


# libyaml is about nine times faster than PyYAML's pure-Python parser, which also rejects some YAML that libyaml
# accepts: BF reads YAML only through libyaml, so one file loads alike on every installation.
LIBYAML = (
    "reading YAML needs PyYAML built with libyaml, which this installation lacks; reinstall bf with a PyYAML wheel "
    "that bundles it: uv tool install --reinstall --python 3.14 brain-framework, or 'brain-framework[mcp]' with MCP"
)
# One command reads bf.yaml many times: reuse validated configurations by content digest.
_LOADED: dict[str, Config] = {}
# The YAML 1.2 core schema, as in editor tooling: `on`, `no` and `off` stay strings, `017` is decimal and `1:30` is
# text. libyaml parses; this Python table resolves the plain scalars it reports.
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
    """YAML 1.2 integers: a leading zero is decimal, never YAML 1.1 octal.

    Every base keeps Python's digit limit for decimal text: replies, the cache and records print integers in decimal.
    """
    value = str(loader.construct_scalar(node))
    base = {"0o": 8, "0x": 16}.get(value[:2])
    try:
        number = int(value[2:], base) if base else int(value, 10)
        str(number)
    except ValueError:
        # Located like a syntax error; the reason is never shown, so neither is the value.
        raise yaml.constructor.ConstructorError(None, None, "invalid integer", node.start_mark) from None
    return number


class _KeyError(yaml.constructor.ConstructorError):
    """A duplicate or non-string mapping key: its position and fixed reason name it without quoting it."""


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
    """libyaml's safe loader with the core schema; it rejects duplicate mappings rather than accept a hidden override."""
    # A subclass, so the resolvers and constructors below leave PyYAML's own loader unchanged.
    loader = cast("type[yaml.CSafeLoader]", type("StrictLoader", (base,), {}))
    loader.yaml_implicit_resolvers = {}
    for tag, pattern, first in _CORE:
        loader.add_implicit_resolver(f"tag:yaml.org,2002:{tag}", re.compile(pattern), first)
    loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    loader.add_constructor("tag:yaml.org,2002:int", _integer)
    # Dates are validated by their owning models; even an explicit tag must not turn them into other types.
    loader.add_constructor("tag:yaml.org,2002:timestamp", lambda constructor, node: constructor.construct_scalar(node))
    return loader


def _loaders() -> tuple[type[yaml.CSafeLoader], type[yaml.CSafeLoader]]:
    """libyaml's safe loader, to scan events, and its strict subclass, to build values; never the pure-Python ones.

    PyYAML built from source without libyaml, as on a Python it publishes no wheel for, lacks `CSafeLoader`.
    """
    base = getattr(yaml, "CSafeLoader", None)
    if base is None:
        raise Error(LIBYAML)
    return base, _strict(base)


def yaml_object(data: bytes, name: str = "", *, line: int = 1) -> dict[str, object]:
    """No aliases, anchors, duplicate keys, deep documents or large node graphs.

    Errors name the file and, for syntax errors, the position: `line` is the file line of the first
    YAML line, such as 2 for Markdown frontmatter. They never quote the document's content. A PyYAML without
    libyaml fails every document alike, without naming one.
    """
    loaders = _loaders()
    try:
        return _yaml_object(data, line, *loaders)
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
        # PyYAML also reads UTF-16 and UTF-32 with a byte-order mark; brain files are UTF-8 only.
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise Error("YAML is not UTF-8") from None
    try:
        depth = 0
        for count, event in enumerate(yaml.parse(data, Loader=scanner), 1):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise Error("YAML anchors and aliases are not supported")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
            if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
            if depth > 32 or count > 20_000:
                raise Error("YAML structure exceeds its limit")
        value = yaml.load(data, Loader=loader)  # noqa: S506 - restricted CSafeLoader subclass
    except (yaml.YAMLError, ValueError, OverflowError, LookupError, TypeError) as error:
        # The safe constructor's explicit tags can raise conversion, indexing or node-shape errors.
        # Keep their possibly sensitive values behind the same private diagnostic boundary.
        mark = getattr(error, "problem_mark", None)
        reason = "YAML mapping keys must be unique strings" if isinstance(error, _KeyError) else "invalid YAML"
        if mark is None:
            raise Error(reason) from error
        raise Error(f"{reason} at line {mark.line + line}, column {mark.column + 1}") from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Error("YAML must contain one mapping")
    return cast(dict[str, object], value)


def load(store: Store) -> Config:
    """The brain configuration; each call rereads the file and shares one frozen result per content.

    Callers must not change the returned dictionaries, such as `sensors`: other loads share them.
    """
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
        # The optional registry needs the same bounded, regular-file reads as brain files:
        # opening a FIFO with Path.open() would wait indefinitely for another process.
        data = Store(path.parent).read(path.name, 1 << 20)
    except FileNotFoundError:
        return UserConfig(), ""
    try:
        registry = UserConfig.model_validate(yaml_object(data, str(path)))
    except ValidationError as error:
        raise Error(f"invalid {path}: " + explain(error)) from error
    # yaml_object has already rejected invalid UTF-8. Blank lines may separate the comments' paragraphs.
    lines = data.decode().splitlines(keepends=True)
    block = list(takewhile(lambda line: line.startswith("#") or not line.strip(), lines))
    while block and not block[-1].startswith("#"):
        block.pop()
    header = "".join(block)
    return registry, header if not header or header.endswith("\n") else header + "\n"


def user_config() -> UserConfig:
    return _registry()[0]


def _identity(entry: Registration) -> str | None:
    """The physical directory a registry entry names; None when it cannot be reached on this machine."""
    try:
        return Store(Path(entry.path).expanduser()).identity
    except Error:
        return None


def _location(entry: Registration) -> Path:
    """The directory an entry names, resolving only its leading `~`: a home reached through a link, such as /home ->
    /var/home, is a spelling, while a link the entry names itself is another path to the brain."""
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
    document: dict[str, object] = {}
    if registry.default is not None:
        document["default"] = registry.default
    document["brains"] = {key: value.model_dump() for key, value in sorted(registry.brains.items())}
    registry_store.write(
        path.name,
        # Keep the owner's leading comments; inline comments do not survive the rewrite.
        ((header or _HEADER) + yaml.safe_dump(document, sort_keys=False)).encode(),
    )


def register(store: Store, *, name: str = "", default: bool = False) -> dict[str, object]:
    """Add or update this brain in the user registry, optionally under an alias and/or as default.

    The entry of that name moves here when its directory no longer exists or holds no bf.yaml, as after moving the
    brain, or is this physical brain through another path; the reply names the path it replaced. An entry that names
    this brain's directory, even spelled with `~`, stays as written. A path that exists but cannot be reached, such as
    a locked folder or a disconnected network mount, keeps its entry; a share that is not mounted at all looks moved.
    """
    configured = load(store)
    alias = name or configured.name
    if name and not re.fullmatch(NAME, alias):
        raise InputError(
            "brain name must start with a lowercase letter and use only lowercase letters, digits and hyphens",
            "name",
        )
    path = user_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    registry_store = Store(path.parent)
    # Registration is one shared read/modify/write transaction, independent of each brain's lock.
    with writer(registry_store, wait=30, busy="another bf register is updating the registry; retry after it finishes"):
        registry, header = _registry()
        for other, entry in registry.brains.items():
            if other != alias and _identity(entry) == store.identity:
                raise Error(
                    f"this brain is already registered as {other}; unregister {other} from {path}, then run "
                    "bf register again"
                )
        replaced = ""
        # Locations, not spellings: a `~` path, which a dotfiles-managed registry may need, stays as written.
        if (existing := registry.brains.get(alias)) and _location(existing) != store.root:
            unreachable = (
                f"cannot reach the brain registered as {alias} at {existing.path}; restore access to it, or remove "
                f"that entry from {path}"
            )
            try:
                registered = _registered(existing)
            except Error as error:
                raise Error(unreachable) from error
            if registered is not None:
                try:
                    registered.mode("bf.yaml")
                except FileNotFoundError:
                    # A folder without bf.yaml holds no brain, such as one a move left or an empty mount point.
                    registered = None
                except OSError as error:
                    raise Error(unreachable) from error
            if registered is not None and registered.identity != store.identity:
                raise Error(
                    f"another brain is already registered as {alias} at {existing.path}; remove that entry from "
                    f"{path}, or pass a different --name"
                )
            replaced = existing.path
        default_changed = default and registry.default != alias
        # An unchanged registration leaves the file, and the owner's inline comments, as they are.
        if existing is None or replaced or default_changed:
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
        matched = ""
        if isinstance(target, Store):
            for name, entry in sorted(registry.brains.items()):
                if _identity(entry) == target.identity or _location(entry) == target.root:
                    matched = name
                    break
            if not matched:
                raise Error(f"this brain is not registered in {path}")
        elif "/" in target or target in {".", "..", "~"}:
            expanded = expand(Path(target))
            identity: str | None = None
            with suppress(Error):
                identity = Store(expanded).identity
            for name, entry in sorted(registry.brains.items()):
                if (identity is not None and _identity(entry) == identity) or _location(entry) == expanded:
                    matched = name
                    break
            if not matched:
                raise Error(f"{target} is not registered in {path}")
        else:
            if target not in registry.brains:
                raise Error(f"brain {target} is not registered{suggest(target, registry.brains)}")
            matched = target
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
    """A registered brain, or None when its directory is absent: missing, or no longer a directory.

    One that exists but cannot be reached, such as a locked folder or a disconnected mount, may still hold that
    brain and return: it fails with UNREACHABLE, so neither selection nor bf register treats it as moved.
    """
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
        # An invalid enclosing brain cannot be selected by name, so it claims none: the registry decides.
        return None
    return nearest if config.name == value else None


def _located(value: str, *, execute: bool = False) -> Store:
    """A path, or a name from the owner's registry, else the enclosing brain.

    A working directory can be an untrusted checkout: its brain may not replace a registered name, and a brain
    you do not own claims no name at all. To `execute` programs, a name must be registered or be the enclosing
    brain's own: a directory below the working directory needs a deliberate path.
    """
    if "/" in value or value in {".", "..", "~"}:
        return _configured(Store(expand(Path(value))), value)
    entry = user_config().brains.get(value)
    local = _enclosing_named(value, registered=entry is not None)
    if entry is not None:
        try:
            store = _registered(entry)
        except Error:
            raise Error(f"cannot reach the brain registered as {value}; restore access to its directory") from None
        if store is None:
            raise Error(f"{value}: {ABSENT}")
        if local is not None and local.identity != store.identity:
            raise Error(
                f"ambiguous brain name {value}: the registry and the enclosing brain name different directories; "
                "pass --brain PATH"
            )
        return _configured(store, f"registered brain {value}")
    if local is not None:
        return local
    if execute:
        raise Error(
            f"brain {value} is neither registered nor the enclosing brain; pass --brain PATH{suggest(value, _names())}"
        )
    # An unregistered name can still be a directory below the working directory.
    path = expand(Path(value))
    if not path.exists():
        raise Error(f"brain directory does not exist; pass an existing --brain or BF_BRAIN{suggest(value, _names())}")
    return _configured(Store(path), value)


def _names() -> set[str]:
    """Brain names a mistyped selection may have meant: registered ones and the enclosing brain's."""
    names = set(user_config().brains)
    with suppress(Error, OSError, UnicodeError):
        if (nearest := _nearest(required=False)) is not None:
            names.add(load(nearest).name)
    return names


def _configured(store: Store, given: str) -> Store:
    """A selected brain holds bf.yaml: a subfolder or parent fails here, before a command reads or caches it.

    The failure names the selection as given, never the directory it resolved to: MCP errors must not reveal it.
    """
    try:
        store.mode("bf.yaml")
    except FileNotFoundError:
        raise Error(
            f"{given} has no bf.yaml; pass the brain's root directory, or create a brain with bf init"
        ) from None
    return store


def _nearest(*, required: bool = True) -> Store | None:
    """The enclosing brain, trusted only when you own its directory and bf.yaml, as Git trusts repositories.

    An untrusted one fails discovery; when a name is given instead, it is ignored.
    """
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
            # Named from the working directory, never as an absolute path: MCP errors must not reveal it.
            location = "../" * up or "./"
            raise Error(f"{location}bf.yaml is not a regular file owned by you; pass --brain PATH to select a brain")
        return Store(candidate)
    return None


def select(value: str = "") -> Store:
    """Single-brain selection: --brain NAME|PATH, then BF_BRAIN, then the enclosing brain, then default."""
    chosen = value or os.environ.get("BF_BRAIN", "")
    if chosen:
        return _located(chosen)
    if nearest := _nearest():
        return nearest
    registry = user_config()
    if registry.default is None:
        if registry.brains:
            raise Error(
                "no default brain configured; pass --brain NAME, run inside a brain, or register one with --default"
            )
        raise Error("no brain selected; pass --brain, run inside a brain, or register one with --default")
    entry = registry.brains[registry.default]
    try:
        store = _registered(entry)
    except Error:
        raise Error(f"cannot reach default brain {registry.default}; restore access to its directory") from None
    if store is None:
        raise Error(f"default brain {registry.default}: {ABSENT}")
    return _configured(store, f"default brain {registry.default}")


def brain_name(store: Store) -> str:
    """How to name a brain whose bf.yaml cannot load: its registered name, else its directory name."""
    if match := registration(store):
        return match[0]
    return store.root.name


def execution(value: str = "") -> Store:
    """The one brain whose programs a command runs: --brain, BF_BRAIN or the enclosing brain.

    Registration selects brains for retrieval only: a registered brain never runs its programs implicitly.
    A name selects a registered brain or the owned enclosing brain, never an unchecked directory.
    """
    chosen = value or os.environ.get("BF_BRAIN", "")
    if chosen:
        return _located(chosen, execute=True)
    if nearest := _nearest():
        return nearest
    raise Error("pass --brain PATH or run inside the brain; registered brains are selected for retrieval only")


def one(value: str = "") -> Store:
    return select(value)
