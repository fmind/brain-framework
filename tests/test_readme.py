"""The README is also the PyPI project page, where relative links do not resolve."""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

from markdown.extensions.toc import slugify  # Zensical renders headings with Python-Markdown's toc extension
from markdown_it import MarkdownIt

ROOT = Path(__file__).parent.parent
DOCS = "https://fmind.github.io/brain-framework/docs/"
HOME = "https://github.com/fmind/brain-framework"
REPOSITORY = re.compile(r"https://github\.com/fmind/brain-framework/(?:blob|tree)/main/(?P<path>[^#)]+)")


def github_slug(title: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation other than `-` and `_` dropped, each space a hyphen."""
    return re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")


def anchors(path: Path, *, site: bool) -> set[str]:
    """HTML ids and heading anchors as the documentation site (Python-Markdown `toc`) or GitHub renders them."""
    text = re.sub(r"\A---\n.*?\n---\n", "", path.read_text(), flags=re.DOTALL)  # front matter is no heading
    found = set(re.findall(r'\bid="([^"]+)"', text))
    tokens = MarkdownIt().parse(text)
    for token, inline in pairwise(tokens):
        if token.type == "heading_open":
            title = "".join(child.content for child in inline.children or [] if child.type in {"text", "code_inline"})
            if not site:
                found.add(github_slug(title))
            elif explicit := re.search(r"\s*\{#([^}\s]+)\}\s*$", title):  # attr_list; GitHub shows it as text
                found.add(explicit[1])
            else:
                found.add(slugify(title, "-"))
    return found


def test_anchor_rules_follow_each_renderer(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("# Diátaxis - A `b`\n\n## Named {#chosen}\n")
    assert anchors(page, site=True) == {"diataxis-a-b", "chosen"}
    assert anchors(page, site=False) == {"diátaxis---a-b", "named-chosen"}


def test_readme_links_are_absolute_and_resolve_to_this_checkout() -> None:
    links = [
        str(child.attrGet("href"))
        for token in MarkdownIt().parse((ROOT / "README.md").read_text())
        for child in token.children or []
        if child.type == "link_open"
    ]
    assert links
    for link in links:
        assert link.startswith("https://"), f"PyPI cannot resolve relative README link {link}"
        address, _, fragment = link.partition("#")
        target = None
        if link.startswith(DOCS):
            page = address.removeprefix(DOCS).strip("/") or "index"
            target = ROOT / "docs/docs" / f"{page}.md"
            assert target.is_file(), link
        elif address == HOME:
            target = ROOT / "README.md"
        elif match := REPOSITORY.fullmatch(address):
            target = ROOT / match["path"]
            assert target.exists(), link
        # Deep links must survive heading renames on the documentation site and in the repository.
        if fragment and target is not None and target.suffix == ".md":
            assert fragment in anchors(target, site=link.startswith(DOCS)), link
