"""The README is also the PyPI project page, where relative links do not resolve."""

from __future__ import annotations

import html
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).parent.parent
DOCS = "https://fmind.github.io/brain-framework/"
HOME = "https://github.com/fmind/brain-framework"
REPOSITORY = re.compile(r"https://github\.com/fmind/brain-framework/(?:blob|tree)/main/(?P<path>[^#)]+)")
RAW = re.compile(r"https://raw\.githubusercontent\.com/fmind/brain-framework/main/(?P<path>[^?#]+)")
WORKFLOW = re.compile(r"https://github\.com/fmind/brain-framework/actions/workflows/(?P<file>[^/?#]+)(?:/badge\.svg)?")


def github_slug(title: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation other than `-` and `_` dropped, each space a hyphen."""
    return re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")


def anchors(path: Path, *, site: bool) -> set[str]:
    """HTML ids and heading anchors as the documentation site (Python-Markdown `toc`) or GitHub renders them."""
    text = re.sub(r"\A---\n.*?\n---\n", "", path.read_text(), flags=re.DOTALL)
    text = re.sub(
        r'^--8<--\s+"([^"]+)"\s*$',
        lambda match: (ROOT / match[1]).read_text(),
        text,
        flags=re.MULTILINE,
    )
    if site:
        rendered = markdown.Markdown(extensions=["extra", "toc"]).convert(text)
        return set(re.findall(r'\bid="([^"]+)"', rendered))
    found = set(re.findall(r'\bid="([^"]+)"', text))
    rendered = markdown.Markdown(extensions=["fenced_code", "tables"]).convert(text)
    for heading_html in re.findall(r"<h[1-6][^>]*>(.*?)</h[1-6]>", rendered, flags=re.DOTALL):
        plain = html.unescape(re.sub(r"<[^>]+>", "", heading_html)).strip()
        found.add(github_slug(plain))
    return found


def test_anchor_rules_follow_each_renderer(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("# Diátaxis - A `b`\n\n## Named {#chosen}\n")
    assert anchors(page, site=True) == {"diataxis-a-b", "chosen"}
    assert anchors(page, site=False) == {"diátaxis---a-b", "named-chosen"}


def test_readme_links_are_absolute_and_resolve_to_this_checkout() -> None:
    rendered = markdown.Markdown(extensions=["extra"]).convert((ROOT / "README.md").read_text())
    links = [html.unescape(url) for url in re.findall(r'<(?:a|img)\b[^>]*?\b(?:href|src)="([^"]+)"', rendered)]
    assert links
    for link in links:
        assert link.startswith("https://"), f"PyPI cannot resolve relative README link {link}"
        address, _, fragment = link.partition("#")
        bare = address.partition("?")[0]
        target = None
        if match := RAW.fullmatch(bare):
            assert (ROOT / match["path"]).is_file(), link
        elif match := WORKFLOW.fullmatch(bare):
            assert (ROOT / ".github/workflows" / match["file"]).is_file(), link
        elif link.startswith(DOCS):
            page = address.removeprefix(DOCS).strip("/") or "index"
            target = ROOT / "docs" / f"{page}.md"
            assert target.is_file(), link
        elif address == HOME:
            target = ROOT / "README.md"
        elif match := REPOSITORY.fullmatch(address):
            target = ROOT / match["path"]
            assert target.exists(), link
        # Deep links must survive heading renames on the documentation site and in the repository.
        if fragment and target is not None and target.suffix == ".md":
            assert fragment in anchors(target, site=link.startswith(DOCS)), link
