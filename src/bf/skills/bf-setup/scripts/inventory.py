#!/usr/bin/env python3
"""Summarize explicitly selected discovery inputs without executing tools or fetching URLs."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urlsplit

MAX_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 20_000
MAX_HOSTS = 200


class LimitError(ValueError):
    """An exceeded inventory limit: its fixed message names the limit, never the input."""


class Inventory:
    """Keep only hostname counts, with bounded work and output."""

    def __init__(self) -> None:
        self.hosts: Counter[str] = Counter()
        self.entries = 0
        self.skipped = 0

    def visit(self) -> None:
        self.entries += 1
        if self.entries > MAX_ENTRIES:
            raise LimitError(f"{MAX_ENTRIES:,}-entry")

    def url(self, value: str) -> None:
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
            if parsed.scheme not in {"http", "https"} or not host or len(host) > 253:
                raise ValueError("unsupported URL")
            if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
                raise ValueError("invalid hostname")
        except (ValueError, UnicodeError):
            self.skipped += 1
            return
        self.hosts[host] += 1
        if len(self.hosts) > MAX_HOSTS:
            raise LimitError(f"{MAX_HOSTS}-host")

    def report(self) -> dict[str, object]:
        return {
            "hosts": [{"host": host, "count": count} for host, count in sorted(self.hosts.items())],
            "skipped_urls": self.skipped,
        }


class BookmarksHTML(HTMLParser):
    """Read exported bookmark links, never markup instructions or resources."""

    def __init__(self, inventory: Inventory) -> None:
        super().__init__(convert_charrefs=True)
        self.inventory = inventory
        self.bookmark_export = False
        self.list_depth = 0
        self.saw_list = False

    def handle_decl(self, decl: str) -> None:
        if decl.lower() == "doctype netscape-bookmark-file-1":
            self.bookmark_export = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "dl":
            self.list_depth += 1
            self.saw_list = True
        if tag == "a":
            if not self.list_depth:
                raise ValueError("bookmark outside list")
            self.inventory.visit()
            hrefs = [value for name, value in attrs if name == "href"]
            if len(hrefs) != 1 or hrefs[0] is None:
                raise ValueError("invalid bookmark")
            self.inventory.url(hrefs[0])

    def handle_endtag(self, tag: str) -> None:
        if tag == "dl":
            if not self.list_depth:
                raise ValueError("unbalanced bookmark list")
            self.list_depth -= 1


def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject hidden overrides instead of silently dropping selected evidence."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def chromium(raw: str, inventory: Inventory) -> None:
    """Validate the bookmark tree before releasing any summary."""
    data = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(data, dict) or not isinstance(data.get("roots"), dict):
        raise ValueError("unsupported bookmark data")
    nodes = list(data["roots"].values())
    while nodes:
        node = nodes.pop()
        inventory.visit()
        if not isinstance(node, dict):
            raise ValueError("invalid node")
        if node.get("type") == "url" and isinstance(node.get("url"), str):
            inventory.url(node["url"])
        elif node.get("type") == "folder" and isinstance(node.get("children"), list):
            if len(nodes) + len(node["children"]) + inventory.entries > MAX_ENTRIES:
                raise LimitError(f"{MAX_ENTRIES:,}-entry")
            nodes.extend(node["children"])
        else:
            raise ValueError("invalid node")


def tool_name(value: str) -> str:
    """Accept bare names only, never an arbitrary path or command line."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", value):
        raise argparse.ArgumentTypeError("expected a bare executable name")
    return value


def main() -> int:
    """Select one explicit input category; do no default discovery."""
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    tools = modes.add_parser("tools", help="check named executables without running them")
    tools.add_argument("names", nargs="+", type=tool_name)
    bookmarks = modes.add_parser("bookmarks", help="summarize a selected export from stdin")
    bookmarks.add_argument("--format", choices=("html", "chromium"), required=True)
    args = parser.parse_args()
    if args.mode == "tools":
        if len(args.names) > 32:
            parser.error("at most 32 tool names are allowed")
        report: dict[str, object] = {
            "tools": [{"name": name, "available": shutil.which(name) is not None} for name in dict.fromkeys(args.names)]
        }
    else:
        try:
            data = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise LimitError("4 MiB")
            raw = data.decode("utf-8-sig")
            inventory = Inventory()
            if args.format == "chromium":
                chromium(raw, inventory)
            else:
                reader = BookmarksHTML(inventory)
                reader.feed(raw)
                reader.close()
                if not reader.bookmark_export or not reader.saw_list or reader.list_depth:
                    raise ValueError("not a complete bookmark export")
            report = inventory.report()
        except LimitError as error:
            sys.stderr.write(
                f"Cannot summarize bookmarks: the export exceeds the {error} limit; select a smaller supported "
                "export, such as one exported folder or a copy without some folders.\n"
            )
            return 1
        # Older html.parser releases raise AssertionError, quoting the input, on malformed declarations.
        except (ValueError, RecursionError, OSError, AssertionError):
            sys.stderr.write(
                "Cannot summarize bookmarks: not a complete, supported export; select a supported export.\n"
            )
            return 1
    sys.stdout.write(json.dumps(report, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
