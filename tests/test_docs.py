"""The documentation site's navigation and llms.txt index stay complete, local and in sync with page metadata."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
PROJECT = tomllib.loads((ROOT / "zensical.toml").read_text())["project"]


def pages(entries: list[Any]) -> list[str]:
    """Nav targets in reading order; groups are flattened."""
    found = []
    for entry in entries:
        for target in entry.values():
            found.extend(pages(target) if isinstance(target, list) else [target])
    return found


def description(page: str) -> str:
    match = re.search(r'^description: "?(.*?)"?$', (DOCS / page).read_text(), re.MULTILINE)
    assert match, f"{page} has no description"
    return match[1]


def test_nav_lists_every_page_once_and_only_site_pages() -> None:
    nav = pages(PROJECT["nav"])
    assert len(nav) == len(set(nav))
    # External URLs render like site pages, with no outbound marker; the header and footer own them.
    assert all(not page.startswith(("http:", "https:")) for page in nav)
    assert sorted(nav) == sorted(str(path.relative_to(DOCS)) for path in DOCS.rglob("*.md"))


def test_llms_index_lists_every_guide_in_nav_order_with_its_description() -> None:
    sections = PROJECT["plugins"]["llmstxt"]["sections"]
    listed = [entry for entries in sections.values() for entry in entries]
    guides = [page for page in pages(PROJECT["nav"]) if page.startswith("docs/")]
    assert [next(iter(entry)) for entry in listed] == guides
    assert [next(iter(entry.values())) for entry in listed] == [description(page) for page in guides]
