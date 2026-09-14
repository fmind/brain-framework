"""Strict authored configuration and source-only local overrides."""

from __future__ import annotations

from typing import cast

import yaml
import yaml.resolver
from pydantic import ValidationError

from fkf.models import Config, Error, explain
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
# Dates are validated by their owning models; YAML must not raise value-bearing constructor errors.
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
    if not isinstance(value, dict):
        raise Error("YAML must contain one mapping")
    return cast(dict[str, object], value)


def load(store: Store) -> Config:
    value = yaml_object(store.read("fkf.yaml", 1 << 20))
    try:
        overlay = yaml_object(store.read("fkf.local.yaml", 1 << 20))
    except FileNotFoundError:
        overlay = {}
    if overlay.keys() - {"sources"}:
        raise Error("fkf.local.yaml may override only sources")
    additions = overlay.get("sources", {})
    sources = value.get("sources", {})
    if not isinstance(sources, dict) or not isinstance(additions, dict):
        raise Error("sources must be a mapping")
    combined = dict(sources)
    for name, settings in additions.items():
        if not isinstance(settings, dict) or not isinstance(combined.get(name, {}), dict):
            raise Error("source settings must be a mapping")
        combined[name] = {**combined.get(name, {}), **settings}
    value["sources"] = combined
    try:
        return Config.model_validate(value)
    except ValidationError as error:
        raise Error("invalid configuration: " + explain(error)) from error
