"""Strict YAML, one base configuration, and the user's base registry."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import yaml
import yaml.resolver
from pydantic import ValidationError

from fkf.models import Config, Error, Registration, UserConfig, explain
from fkf.storage import Store


class _Loader(yaml.SafeLoader):
    """Reject duplicate mappings rather than accepting a hidden override."""


def _mapping(loader: _Loader, node: yaml.MappingNode) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise Error("YAML mapping keys must be unique strings")
        result[key] = loader.construct_object(value_node)
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
# Dates are validated by their owning models; YAML must not turn them into other types.
_Loader.add_constructor("tag:yaml.org,2002:timestamp", lambda loader, node: loader.construct_scalar(node))


def yaml_object(data: bytes) -> dict[str, object]:
    """No aliases, anchors, duplicate keys, deep documents or large node graphs."""
    if len(data) > 1 << 20:
        raise Error("YAML exceeds 1 MiB")
    try:
        depth = 0
        for count, event in enumerate(yaml.parse(data), 1):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise Error("YAML anchors and aliases are not supported")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
            if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
            if depth > 32 or count > 20_000:
                raise Error("YAML structure exceeds its limit")
        value = yaml.load(data, Loader=_Loader)  # noqa: S506 - restricted SafeLoader subclass
    except (yaml.YAMLError, UnicodeError) as error:
        raise Error("invalid YAML") from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Error("YAML must contain one mapping")
    return cast(dict[str, object], value)


def load(store: Store) -> Config:
    value = yaml_object(store.read("fkf.yaml", 1 << 20))
    if value.get("version") == 1:
        raise Error("fkf.yaml version 1 is an FKF 7 base; see the FKF 8 upgrade notes in the changelog")
    try:
        return Config.model_validate(value)
    except ValidationError as error:
        raise Error("invalid fkf.yaml: " + explain(error)) from error


def user_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root).expanduser() / "fkf" / "config.yaml"


def user_config() -> UserConfig:
    """The optional user registry; a missing file means no registered bases."""
    path = user_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return UserConfig()
    try:
        return UserConfig.model_validate(yaml_object(data))
    except ValidationError as error:
        raise Error(f"invalid {path}: " + explain(error)) from error


def register(store: Store, *, collect: bool) -> dict[str, object]:
    """Add or update this base in the user registry, keyed by its configured name."""
    name = load(store).name
    registry = user_config()
    for other, entry in registry.bases.items():
        if other != name and Path(entry.path).expanduser().resolve() == store.root:
            raise Error(f"this directory is already registered as {other}")
    existing = registry.bases.get(name)
    if existing and Path(existing.path).expanduser().resolve() != store.root:
        raise Error(f"another base is already registered as {name}; rename one of them in fkf.yaml")
    registry.bases[name] = Registration(path=str(store.root), collect=collect)
    path = user_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"bases": {key: value.model_dump() for key, value in sorted(registry.bases.items())}}
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text("# https://fmind.github.io/fkf/\n" + yaml.safe_dump(document, sort_keys=True))
    temporary.replace(path)
    return {"base": name, "path": str(store.root), "collect": collect, "config": str(path)}


def _located(value: str) -> Store:
    registry = user_config()
    if value in registry.bases:
        return Store(Path(registry.bases[value].path).expanduser())
    return Store(Path(value).expanduser())


def _nearest() -> Store | None:
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / "fkf.yaml").is_file():
            return Store(candidate)
    return None


def select(value: str = "") -> list[Store]:
    """--base NAME|PATH, then FKF_BASE, then the enclosing base, then every registered base."""
    chosen = value or os.environ.get("FKF_BASE", "")
    if chosen:
        return [_located(chosen)]
    if nearest := _nearest():
        return [nearest]
    # A shared user configuration may list bases that exist only on some machines; skip absent ones.
    paths = [Path(entry.path).expanduser() for _, entry in sorted(user_config().bases.items())]
    stores = [Store(path) for path in paths if path.is_dir()]
    if not stores:
        raise Error("no base selected; pass --base, run inside a base, or register one with fkf register")
    return stores


def one(value: str = "") -> Store:
    stores = select(value)
    if len(stores) != 1:
        raise Error("several bases are registered; pass --base NAME")
    return stores[0]


def may_collect(store: Store) -> bool:
    return any(
        entry.collect and Path(entry.path).expanduser().resolve() == store.root
        for entry in user_config().bases.values()
    )
