"""Strict YAML, one brain configuration, and the user's brain registry."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from itertools import takewhile
from pathlib import Path
from typing import cast

import yaml
import yaml.constructor
import yaml.resolver
from pydantic import ValidationError

from bf.models import Config, Error, FormatError, Registration, UserConfig, check_version, digest, explain
from bf.storage import Store, expand, writer, xdg

_HEADER = "# https://fmind.github.io/brain-framework/\n"


# libyaml parses bf.yaml about nine times faster; the pure-Python loader remains the fallback.
_SafeLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
# One command reads bf.yaml many times: reuse validated configurations by content digest.
_LOADED: dict[str, Config] = {}


class _Loader(_SafeLoader):
    """Reject duplicate mappings rather than accepting a hidden override."""


# The YAML 1.2 core schema, as in editor tooling: `on`, `no` and `off` stay strings, `017` is decimal
# and `1:30` is text. Both loaders resolve plain scalars through this Python table.
_Loader.yaml_implicit_resolvers = {}
for _tag, _pattern, _first in (
    ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", list("-+0123456789")),
    (
        "float",
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        list("-+.0123456789"),
    ),
):
    _Loader.add_implicit_resolver(f"tag:yaml.org,2002:{_tag}", re.compile(_pattern), _first)


def _integer(loader: _Loader, node: yaml.ScalarNode) -> int:
    """YAML 1.2 integers: a leading zero is decimal, never YAML 1.1 octal."""
    value = str(loader.construct_scalar(node))
    for prefix, base in (("0o", 8), ("0x", 16)):
        if value.startswith(prefix):
            return int(value.removeprefix(prefix), base)
    return int(value, 10)


class _KeyError(yaml.constructor.ConstructorError):
    """A duplicate or non-string mapping key: its position and fixed reason name it without quoting it."""


def _mapping(loader: _Loader, node: yaml.MappingNode) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise _KeyError(None, None, "YAML mapping keys must be unique strings", key_node.start_mark)
        result[key] = loader.construct_object(value_node)
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
_Loader.add_constructor("tag:yaml.org,2002:int", _integer)
# Dates are validated by their owning models; even an explicit tag must not turn them into other types.
_Loader.add_constructor("tag:yaml.org,2002:timestamp", lambda loader, node: loader.construct_scalar(node))


def yaml_object(data: bytes, name: str = "", *, line: int = 1) -> dict[str, object]:
    """No aliases, anchors, duplicate keys, deep documents or large node graphs.

    Errors name the file and, for syntax errors, the position: `line` is the file line of the first
    YAML line, such as 2 for Markdown frontmatter. They never quote the document's content.
    """
    try:
        return _yaml_object(data, line)
    except Error as error:
        if not name:
            raise
        raise Error(f"{name}: {error}") from error


def _yaml_object(data: bytes, line: int) -> dict[str, object]:
    if len(data) > 1 << 20:
        raise Error("YAML exceeds 1 MiB")
    try:
        # PyYAML also reads UTF-16 and UTF-32 with a byte-order mark; brain files are UTF-8 only.
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise Error("YAML is not UTF-8") from None
    try:
        depth = 0
        for count, event in enumerate(yaml.parse(data, Loader=_SafeLoader), 1):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise Error("YAML anchors and aliases are not supported")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
            if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
            if depth > 32 or count > 20_000:
                raise Error("YAML structure exceeds its limit")
        value = yaml.load(data, Loader=_Loader)  # noqa: S506 - restricted SafeLoader subclass
    except (yaml.YAMLError, ValueError, OverflowError, LookupError, TypeError) as error:
        # SafeLoader's explicit tags can raise conversion, indexing or node-shape errors.
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
    data = store.read("bf.yaml", 1 << 20)
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
    # yaml_object has already rejected invalid UTF-8.
    header = "".join(takewhile(lambda line: line.startswith("#"), data.decode().splitlines(keepends=True)))
    return registry, header if not header or header.endswith("\n") else header + "\n"


def user_config() -> UserConfig:
    return _registry()[0]


def register(store: Store) -> dict[str, object]:
    """Add or update this brain in the user registry, keyed by its configured name."""
    name = load(store).name
    path = user_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    registry_store = Store(path.parent)
    # Registration is one shared read/modify/write transaction, independent of each brain's lock.
    with writer(registry_store, wait=30):
        registry, header = _registry()
        for other, entry in registry.brains.items():
            if other != name and Path(entry.path).expanduser().resolve() == store.root:
                raise Error(f"this directory is already registered as {other}")
        existing = registry.brains.get(name)
        if existing and Path(existing.path).expanduser().resolve() != store.root:
            raise Error(f"another brain is already registered as {name}; rename one of them in bf.yaml")
        registry.brains[name] = Registration(path=str(store.root))
        document = {"brains": {key: value.model_dump() for key, value in sorted(registry.brains.items())}}
        registry_store.write(
            path.name,
            # Keep the owner's leading comments; inline comments do not survive the rewrite.
            ((header or _HEADER) + yaml.safe_dump(document, sort_keys=True)).encode(),
        )
    return {"brain": name, "path": str(store.root), "config": str(path)}


def _reference(store: Store, name: str, path: str) -> Store:
    try:
        destination = Path(path).expanduser()
    except RuntimeError as error:
        raise Error("referenced brain home directory cannot be resolved") from error
    target = Store(destination if destination.is_absolute() else store.root / destination)
    if load(target).name != name:
        raise Error(f"referenced brain {name} has a different configured name")
    return target


ABSENT = "registered brain directory is absent on this machine; fix or remove its registry entry"


class Selection(list[Store]):
    """Selected roots, plus the names of registered brains that are absent on this machine."""

    def __init__(self, stores: Iterable[Store] = (), absent: Iterable[str] = ()) -> None:
        super().__init__(stores)
        self.absent = tuple(absent)


def related(roots: list[Store]) -> tuple[list[Store], list[dict[str, object]]]:
    """Expand direct declarations once; a reference never grants execution authority.

    Brains are compared by physical directory, so two paths to one brain are searched once.
    """
    candidates: dict[str, Store] = {}
    for store in roots:
        candidates.setdefault(store.identity, store)
    names: dict[str, set[str]] = {}
    problems: list[dict[str, object]] = [
        {"brain": name, "error": ABSENT} for name in (roots.absent if isinstance(roots, Selection) else ())
    ]
    for store in roots:
        try:
            config = load(store)
        except Error, OSError, UnicodeError:
            # The owning operation reports root failures, retaining its normal failure semantics.
            continue
        names.setdefault(config.name, set()).add(store.identity)
        for name, reference in sorted(config.brains.items()):
            try:
                target = _reference(store, name, reference.path)
            except (Error, OSError, UnicodeError) as error:
                # A teammate's newer format is worth naming; other causes stay generic, without paths.
                message = (
                    str(error)
                    if isinstance(error, FormatError)
                    else "referenced brain is missing, inaccessible, invalid or has a different name; check bf.yaml"
                )
                # Problems carry only error, brain and file; the declaring key names the reference.
                problems.append({"brain": config.name, "file": "bf.yaml", "error": f"brains.{name}: {message}"})
                continue
            names.setdefault(name, set()).add(target.identity)
            candidates.setdefault(target.identity, target)
    excluded: set[str] = set()
    for name, identities in sorted(names.items()):
        if len(identities) > 1:
            excluded.update(identities)
            problems.append({"brain": name, "error": "ambiguous brain name; multiple directories claim this identity"})
    return [store for identity, store in candidates.items() if identity not in excluded], problems


def _claim(value: str, *, registered: bool) -> tuple[Store | None, bool]:
    """The owned enclosing brain claiming a name, and whether it claims it through a direct reference."""
    if (nearest := _nearest(required=False)) is None:
        return None, False
    try:
        config = load(nearest)
    except Error, OSError, UnicodeError:
        if not registered:
            raise
        # An invalid enclosing brain cannot be selected by name, so it claims none: the registry decides.
        return None, False
    if value == config.name:
        return nearest, False
    if value not in config.brains:
        return None, False
    try:
        return _reference(nearest, value, config.brains[value].path), True
    except (Error, OSError, UnicodeError) as error:
        # It may name another directory than the registry does: fail closed, naming the declaration to fix.
        raise Error(
            f"the enclosing bf.yaml declares brains.{value}, which is missing, inaccessible, invalid or has a "
            "different name; fix it or pass --brain PATH"
        ) from error


def _located(value: str, *, execute: bool = False) -> Store:
    """A path, or a name from the owner's registry, else from the enclosing brain and its direct references.

    A working directory can be an untrusted checkout: its brain may not replace a registered name, and a brain
    you do not own claims no name at all. To `execute` programs, a name must be registered or be the enclosing
    brain's own: a reference or a directory below the working directory needs a deliberate path.
    """
    if "/" in value or value in {".", "..", "~"}:
        return Store(expand(Path(value)))
    entry = user_config().brains.get(value)
    local, referenced = _claim(value, registered=entry is not None)
    if entry is not None:
        store = Store(Path(entry.path).expanduser())
        if local is not None and local.identity != store.identity:
            raise Error(
                f"ambiguous brain name {value}: the registry and the enclosing brain name different directories; "
                "pass --brain PATH"
            )
        return store
    if execute and (local is None or referenced):
        raise Error(f"brain {value} is neither registered nor the enclosing brain; pass --brain PATH")
    # An unregistered name can still be a directory below the working directory.
    return local or Store(expand(Path(value)))


def _nearest(*, required: bool = True) -> Store | None:
    """The enclosing brain, trusted only when you own its directory and bf.yaml, as Git trusts repositories.

    An untrusted one fails discovery; when a name is given instead, it is ignored.
    """
    for candidate in (Path.cwd(), *Path.cwd().parents):
        try:
            info = (candidate / "bf.yaml").lstat()
        except FileNotFoundError, NotADirectoryError:
            continue
        owner = os.geteuid()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or candidate.stat().st_uid != owner:
            if not required:
                return None
            raise Error(f"{candidate}: bf.yaml is not a regular file owned by you; pass --brain PATH to select a brain")
        return Store(candidate)
    return None


def select(value: str = "") -> list[Store]:
    """--brain NAME|PATH, then BF_BRAIN, then the enclosing brain, then every registered brain."""
    chosen = value or os.environ.get("BF_BRAIN", "")
    if chosen:
        return [_located(chosen)]
    if nearest := _nearest():
        return [nearest]
    # Retrieval reports registered brains absent on this machine instead of silently answering without them.
    stores, absent = [], []
    for name, entry in sorted(user_config().brains.items()):
        path = Path(entry.path).expanduser()
        if path.is_dir():
            stores.append(Store(path))
        else:
            absent.append(name)
    if absent and not stores:
        raise Error(
            f"registered brains are absent on this machine: {', '.join(absent)}; restore them or remove their "
            "registry entries"
        )
    if not stores:
        raise Error("no brain selected; pass --brain, run inside a brain, or register one with bf register")
    return Selection(stores, absent)


def brain_name(store: Store) -> str:
    """How to name a brain whose bf.yaml cannot load: its registered name, else its directory name."""
    try:
        for name, entry in user_config().brains.items():
            if Path(entry.path).expanduser().resolve() == store.root:
                return name
    except Error, OSError, RuntimeError, UnicodeError:
        pass
    return store.root.name


def execution(value: str = "") -> Store:
    """The one brain whose programs a command runs: --brain, BF_BRAIN or the enclosing brain.

    Registration selects brains for retrieval only: a registered clone never runs its programs implicitly.
    A name selects a registered brain or the owned enclosing brain, never a reference or an unchecked directory.
    """
    chosen = value or os.environ.get("BF_BRAIN", "")
    if chosen:
        return _located(chosen, execute=True)
    if nearest := _nearest():
        return nearest
    raise Error("pass --brain PATH or run inside the brain; registered brains are selected for retrieval only")


def one(value: str = "") -> Store:
    stores = select(value)
    if len(stores) != 1:
        raise Error("several brains are registered; pass --brain NAME")
    return stores[0]
