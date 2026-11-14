"""Packaged Markdown guides bundled with Brain Framework."""

from __future__ import annotations

import re
from importlib.resources import files
from importlib.resources.abc import Traversable

from bf.models import InputError

_ORDER = (
    "index",
    "getting-started",
    "authoring",
    "retrieval",
    "jobs",
    "assist",
    "operations",
    "reference",
)
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
_DESCRIPTION = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*(?:\{#[^}\s]+\})?\s*$", re.MULTILINE)


def _guide_nodes() -> dict[str, Traversable]:
    """Map packaged guide slugs to their Markdown resources in reading order."""
    root = files("bf") / "guides"
    nodes = {
        child.name.removesuffix(".md"): child
        for child in root.iterdir()
        if child.is_file() and child.name.endswith(".md") and not child.name.startswith(".")
    }
    rank = {slug: index for index, slug in enumerate(_ORDER)}
    return dict(sorted(nodes.items(), key=lambda item: (rank.get(item[0], len(_ORDER)), item[0])))


def _summary(slug: str, text: str) -> str:
    """Extract a one-line summary from frontmatter `description` or the first Markdown heading."""
    body = text
    if frontmatter := _FRONTMATTER.match(text):
        if match := _DESCRIPTION.search(frontmatter.group(1)):
            value = match.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1].strip()
            if value:
                return value
        body = text[frontmatter.end() :]
    if heading := _HEADING.search(body):
        return heading.group(1).strip()
    return slug


def topics() -> list[tuple[str, str]]:
    """Return `(slug, summary)` pairs for all packaged guides."""
    return [(slug, _summary(slug, node.read_text(encoding="utf-8"))) for slug, node in _guide_nodes().items()]


def read_guide(topic: str) -> str:
    """Return the Markdown guide for `topic`, or raise `InputError` naming valid topics."""
    nodes = _guide_nodes()
    if topic in nodes:
        return nodes[topic].read_text(encoding="utf-8")
    available = ", ".join(nodes)
    if not topic.strip():
        raise InputError(f"give a guide topic; available: {available}", "topic")
    label = topic[:64] + "..." if len(topic) > 64 else topic
    raise InputError(f"unknown guide {label!r}; available: {available}", "topic")


def render_index() -> str:
    """Render a concise Markdown listing of all available guide topics."""
    lines = ["# Brain Framework guides", "", "Run `bf guide TOPIC` to read a guide:", ""]
    lines.extend(f"- `{slug}`: {summary}" for slug, summary in topics())
    return "\n".join(lines) + "\n"
