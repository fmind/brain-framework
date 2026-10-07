"""Strict YAML, one brain configuration, and the user's brain registry."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from contextlib import suppress
from itertools import takewhile
from pathlib import Path
from typing import cast

import yaml
import yaml.constructor
import yaml.resolver
from pydantic import ValidationError

from bf.models import Config, Error, FormatError, Registration, UserConfig, check_version, digest, explain, suggest
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


def _identity(entry: Registration) -> str | None:
    """The physical directory a registry entry names; None when it cannot be reached on this machine."""
    try:
        return Store(Path(entry.path).expanduser()).identity
    except Error:
        return None


def register(store: Store) -> dict[str, object]:
    """Add or update this brain in the user registry, keyed by its configured name.

    The entry of that name moves here when its directory no longer exists, as after moving the brain, or is this
    physical brain through another path; the reply names the path it replaced. A path that exists but cannot be
    reached, such as a locked folder or a disconnected network mount, keeps its entry; a share that is not mounted
    at all looks moved.
    """
    name = load(store).name
    path = user_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    registry_store = Store(path.parent)
    # Registration is one shared read/modify/write transaction, independent of each brain's lock.
    with writer(registry_store, wait=30):
        registry, header = _registry()
        for other, entry in registry.brains.items():
            if other != name and _identity(entry) == store.identity:
                raise Error(
                    f"this brain is already registered as {other}; remove that entry from {path}, then run "
                    "bf register again"
                )
        replaced = ""
        if (existing := registry.brains.get(name)) and existing.path != str(store.root):
            try:
                registered = _registered(existing)
            except Error as error:
                raise Error(
                    f"cannot reach the brain registered as {name} at {existing.path}; restore access to it, or "
                    f"remove that entry from {path}"
                ) from error
            if registered is not None and registered.identity != store.identity:
                raise Error(
                    f"another brain is already registered as {name} at {existing.path}; remove that entry from "
                    f"{path}, or rename one of the brains in its bf.yaml"
                )
            replaced = existing.path
        registry.brains[name] = Registration(path=str(store.root))
        document = {"brains": {key: value.model_dump() for key, value in sorted(registry.brains.items())}}
        registry_store.write(
            path.name,
            # Keep the owner's leading comments; inline comments do not survive the rewrite.
            ((header or _HEADER) + yaml.safe_dump(document, sort_keys=True)).encode(),
        )
    return {"brain": name, "path": str(store.root), "config": str(path), **({"replaced": replaced} if replaced else {})}


def _reference(store: Store, name: str, path: str) -> Store:
    try:
        destination = Path(path).expanduser()
    except RuntimeError as error:
        raise Error("referenced brain home directory cannot be resolved") from error
    target = Store(destination if destination.is_absolute() else store.root / destination)
    if load(target).name != name:
        raise Error(f"referenced brain {name} has a different configured name")
    return target


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


class Selection(list[Store]):
    """Selected roots, plus each registered brain this machine cannot use, by name, with its error."""

    def __init__(self, stores: Iterable[Store] = (), unavailable: dict[str, str] | None = None) -> None:
        super().__init__(stores)
        self.unavailable = unavailable or {}


def related(roots: list[Store]) -> tuple[list[Store], list[dict[str, object]]]:
    """Expand direct declarations once; a reference never grants execution authority.

    Brains are compared by physical directory, so two paths to one brain are searched once.
    """
    candidates: dict[str, Store] = {}
    for store in roots:
        candidates.setdefault(store.identity, store)
    names: dict[str, set[str]] = {}
    problems: list[dict[str, object]] = [
        {"brain": name, "error": error}
        for name, error in (roots.unavailable.items() if isinstance(roots, Selection) else ())
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
        return _configured(Store(expand(Path(value))), value)
    entry = user_config().brains.get(value)
    local, referenced = _claim(value, registered=entry is not None)
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
    if execute and (local is None or referenced):
        raise Error(
            f"brain {value} is neither registered nor the enclosing brain; pass --brain PATH{suggest(value, _names())}"
        )
    if local is not None:
        return local
    # An unregistered name can still be a directory below the working directory.
    path = expand(Path(value))
    if not path.exists():
        raise Error(f"brain directory does not exist; pass an existing --brain or BF_BRAIN{suggest(value, _names())}")
    return _configured(Store(path), value)


def _names() -> set[str]:
    """Brain names a mistyped selection may have meant: registered ones, the enclosing brain's and its references."""
    names = set(user_config().brains)
    with suppress(Error, OSError, UnicodeError):
        if (nearest := _nearest(required=False)) is not None:
            config = load(nearest)
            names |= {config.name, *config.brains}
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


def select(value: str = "") -> list[Store]:
    """--brain NAME|PATH, then BF_BRAIN, then the enclosing brain, then every registered brain."""
    chosen = value or os.environ.get("BF_BRAIN", "")
    if chosen:
        return [_located(chosen)]
    if nearest := _nearest():
        return [nearest]
    # Retrieval reports registered brains this machine cannot use instead of silently answering without them.
    stores: list[Store] = []
    names: list[str] = []
    unavailable: dict[str, str] = {}
    for name, entry in sorted(user_config().brains.items()):
        try:
            store = _registered(entry)
        except Error as error:
            unavailable[name] = str(error)
            continue
        if store is None:
            unavailable[name] = ABSENT
        else:
            stores.append(store)
            names.append(name)
    if unavailable and not stores:
        raise Error(
            f"registered brains are unavailable on this machine: {', '.join(unavailable)}; restore them or access to "
            "them, run bf register at their new place, or remove their registry entries"
        )
    if not stores:
        raise Error("no brain selected; pass --brain, run inside a brain, or register one with bf register")
    # The only registered brain may have lost its bf.yaml: name that before a command reads or caches it. Of several,
    # retrieval reports each one whose bf.yaml does not load.
    only = [_configured(stores[0], f"registered brain {names[0]}")] if len(stores) == 1 else stores
    return Selection(only, unavailable)


def brain_name(store: Store) -> str:
    """How to name a brain whose bf.yaml cannot load: its registered name, else its directory name."""
    try:
        for name, entry in user_config().brains.items():
            if _identity(entry) == store.identity:
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
