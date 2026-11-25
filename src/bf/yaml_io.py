"""Single-pass strict YAML 1.2 mapping parser without anchors, aliases, duplicate keys, or deep nesting."""

from __future__ import annotations

import re
from functools import cache
from typing import TYPE_CHECKING, Any, cast

from bf.models import Error

if TYPE_CHECKING:
    from yaml import MappingNode, Node, ScalarNode
    from yaml.constructor import SafeConstructor

_FLOAT_RE = r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
_CORE = (
    ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", list("-+0123456789")),
    ("float", _FLOAT_RE, list("-+.0123456789")),
)


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


class _YamlLimitError(Error):
    """Single-pass YAML anchor, depth, or node-count violation."""


def _mapping(loader: SafeConstructor, node: MappingNode) -> dict[str, object]:
    out: dict[str, object] = {}
    for key_node, val_node in node.value:
        if not isinstance(key := loader.construct_object(key_node), str) or key in out:
            raise _KeyError(key_node.start_mark)
        out[key] = loader.construct_object(val_node)
    return out


@cache
def _strict_loader() -> type[Any]:
    import yaml
    import yaml.composer
    import yaml.resolver

    base: type[Any] = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader

    class _StrictLoader(base):  # type: ignore[valid-type, misc]
        def compose_strict_document(self) -> Node | None:
            """Iteratively compose one YAML document from libyaml/Python events without anchors, aliases, or deep nesting."""
            ev = self.get_event()  # StreamStartEvent
            if isinstance(ev, yaml.StreamEndEvent):
                return None
            ev = self.get_event()
            if isinstance(ev, yaml.StreamEndEvent):
                return None
            if not isinstance(ev, yaml.DocumentStartEvent):
                raise yaml.composer.ComposerError(None, None, "expected document start", ev.start_mark)

            root: Node | None = None
            stack: list[list[Any]] = []
            nodes = 0

            def attach(node: Node) -> None:
                nonlocal root
                if not stack:
                    root = node
                    return
                frame = stack[-1]
                if frame[0] == "seq":
                    frame[1].value.append(node)
                elif frame[2] is None:
                    frame[2] = node
                else:
                    frame[1].value.append((frame[2], node))
                    frame[2] = None

            while True:
                ev = self.get_event()
                if isinstance(ev, yaml.DocumentEndEvent):
                    break
                if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None):
                    raise _YamlLimitError("YAML anchors and aliases are not supported")
                if isinstance(ev, yaml.ScalarEvent):
                    nodes += 1
                    if nodes > 20_000:
                        raise _YamlLimitError("YAML structure exceeds its limit")
                    tag = ev.tag
                    if tag is None or tag == "!":
                        tag = self.resolve(yaml.ScalarNode, ev.value, ev.implicit)
                    attach(yaml.ScalarNode(tag, ev.value, ev.start_mark, ev.end_mark, style=ev.style))
                elif isinstance(ev, yaml.SequenceStartEvent):
                    nodes += 1
                    if len(stack) + 1 > 32 or nodes > 20_000:
                        raise _YamlLimitError("YAML structure exceeds its limit")
                    tag = ev.tag
                    if tag is None or tag == "!":
                        tag = self.resolve(yaml.SequenceNode, None, ev.implicit)
                    stack.append(["seq", yaml.SequenceNode(tag, [], ev.start_mark, None, flow_style=ev.flow_style)])
                elif isinstance(ev, yaml.MappingStartEvent):
                    nodes += 1
                    if len(stack) + 1 > 32 or nodes > 20_000:
                        raise _YamlLimitError("YAML structure exceeds its limit")
                    tag = ev.tag
                    if tag is None or tag == "!":
                        tag = self.resolve(yaml.MappingNode, None, ev.implicit)
                    stack.append(
                        ["map", yaml.MappingNode(tag, [], ev.start_mark, None, flow_style=ev.flow_style), None]
                    )
                elif isinstance(ev, (yaml.SequenceEndEvent, yaml.MappingEndEvent)):
                    frame = stack.pop()
                    frame[1].end_mark = ev.end_mark
                    attach(frame[1])
                else:
                    raise yaml.composer.ComposerError(None, None, "unexpected YAML event", ev.start_mark)

            tail = self.get_event()
            if not isinstance(tail, yaml.StreamEndEvent):
                raise yaml.composer.ComposerError(
                    None, None, "expected a single document in the stream", tail.start_mark
                )
            return root

    _StrictLoader.yaml_implicit_resolvers = {}
    for tag, pat, first in _CORE:
        _StrictLoader.add_implicit_resolver(f"tag:yaml.org,2002:{tag}", re.compile(pat), first)
    _StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    _StrictLoader.add_constructor("tag:yaml.org,2002:int", _integer)
    _StrictLoader.add_constructor("tag:yaml.org,2002:timestamp", lambda c, n: c.construct_scalar(n))
    return _StrictLoader


def yaml_object(data: bytes, name: str = "", *, line: int = 1) -> dict[str, object]:
    """Parse one UTF-8 YAML 1.2 mapping in a single pass without anchors, aliases, duplicate keys, or deep nesting."""
    import yaml

    prefix = f"{name}: " if name else ""
    try:
        if len(data) > 1 << 20:
            raise Error("YAML exceeds 1 MiB")
        data.decode("utf-8")
        loader = _strict_loader()(data)
        try:
            node = loader.compose_strict_document()
            val = loader.construct_document(node) if node is not None else None
        finally:
            loader.dispose()
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
        if name:
            raise Error(f"{prefix}{error}") from error
        raise
