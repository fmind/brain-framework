"""The README is also the PyPI project page, where relative links do not resolve."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
DOCS = "https://fmind.github.io/brain-framework/docs/"
REPOSITORY = re.compile(r"https://github\.com/fmind/brain-framework/(?:blob|tree)/main/(?P<path>[^#)]+)")


def test_readme_links_are_absolute_and_resolve_to_this_checkout() -> None:
    links = re.findall(r"\]\(([^)]+)\)", (ROOT / "README.md").read_text())
    assert links
    for link in links:
        assert link.startswith("https://"), f"PyPI cannot resolve relative README link {link}"
        if link.startswith(DOCS):
            page = link.removeprefix(DOCS).partition("#")[0].strip("/") or "index"
            assert (ROOT / "docs/docs" / f"{page}.md").is_file(), link
        elif match := REPOSITORY.fullmatch(link.partition("#")[0]):
            assert (ROOT / match["path"]).exists(), link
