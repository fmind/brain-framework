#!/usr/bin/env python3
"""Discover explicitly selected writing files and index metadata once per package."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import NoReturn

MAX_DOCUMENTS = 10_000


class Arguments(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # A malformed path must not be repeated in public diagnostics.
        self.exit(2, "writing-source-json.py: invalid arguments; see --help\n")


def discover(arguments: argparse.Namespace) -> list[str]:
    files = list(arguments.files)

    def append(path: Path) -> None:
        if len(files) >= MAX_DOCUMENTS:
            raise ValueError
        files.append(os.fspath(path))

    for value in arguments.articles_root:
        root = Path(value).expanduser()
        if not root.is_dir():
            raise ValueError
        for path in root.glob("*.md"):
            if not path.name.startswith(".") and path.is_file():
                append(path)
    for value in arguments.packages_root:
        root = Path(value).expanduser()
        if not root.is_dir():
            raise ValueError
        for count, package in enumerate(root.iterdir(), start=1):
            if count > MAX_DOCUMENTS:
                raise ValueError
            if package.name.startswith("."):
                continue
            article, draft = package / "article.md", package / "draft.txt"
            if article.is_file():
                append(article)
            elif draft.is_file():
                append(draft)
    if not files or len(files) > MAX_DOCUMENTS:
        raise ValueError
    # Stable discovery order makes equal-date ordering reproducible across filesystems.
    return sorted(os.fspath(Path(value).expanduser()) for value in files)


def main(arguments: list[str]) -> int:
    parser = Arguments(description=__doc__)
    parser.add_argument("--version", "-v", action="version", version="writing-source-json.py (fkf preset helper)")
    parser.add_argument("--articles-root", action="append", default=[], help="directory of Markdown articles; repeatable")
    parser.add_argument("--packages-root", action="append", default=[], help="directory of packages containing article.md or draft.txt; repeatable")
    parser.add_argument("files", nargs="*", help="explicit authored files")
    selected = parser.parse_args(arguments)
    try:
        files = discover(selected)
        os.execvp("writing-index.py", ["writing-index.py", *files])
    except (OSError, ValueError):
        sys.stderr.write("writing-source-json.py: cannot index the selected documents; check roots, file limit, and indexer availability\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
