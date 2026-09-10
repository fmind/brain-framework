#!/usr/bin/env python3
"""Collect dated posts from the official Google Developers feed and articles."""

from __future__ import annotations

import json
import pyexpat
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

FEED = "https://developers.googleblog.com/feed/"
HOST = "developers.googleblog.com"
MAX_DOWNLOAD_BYTES = 8 << 20
MAX_ITEMS = 256
MAX_OUTPUT_BYTES = 64 << 20
DATE_PUBLISHED = re.compile(rb'"datePublished"\s*:\s*"([0-9]{4}-[0-9]{2}-[0-9]{2})"')
RFC3339 = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})$")
USER_AGENT = "Mozilla/5.0 (compatible; fkf-google-developers-json/1.0; +https://fmind.github.io/fkf)"


@dataclass
class Element:
    tag: str
    content: list[str | Element]

    def children(self, name: str) -> list[Element]:
        wanted = name.casefold()
        return [
            item
            for item in self.content
            if isinstance(item, Element) and local_name(item.tag).casefold() == wanted
        ]

    def text(self) -> str:
        return "".join(item.text() if isinstance(item, Element) else item for item in self.content).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1]


class XMLParseError(ValueError):
    """The bounded feed bytes are not safe, well-formed XML."""


class XMLTreeParser:
    """Build the small RSS subset with Expat while rejecting every DTD path."""

    def __init__(self) -> None:
        self.root: Element | None = None
        self.stack: list[Element] = []

    def start_element(self, name: str, attributes: dict[str, str]) -> None:
        del attributes
        element = Element(name, [])
        if self.stack:
            self.stack[-1].content.append(element)
        elif self.root is None:
            self.root = element
        else:
            raise XMLParseError("multiple XML roots")
        self.stack.append(element)

    def end_element(self, name: str) -> None:
        if not self.stack or self.stack[-1].tag != name:
            raise XMLParseError(f"mismatched closing tag {name}")
        self.stack.pop()

    def character_data(self, data: str) -> None:
        if self.stack:
            self.stack[-1].content.append(data)
        elif data.strip():
            raise XMLParseError("text outside the XML root")

    @staticmethod
    def reject_processing_instruction(target: str, data: str) -> None:
        del target, data
        raise XMLParseError("processing instructions are forbidden")

    @staticmethod
    def reject_doctype(
        name: str,
        system_id: str | None,
        public_id: str | None,
        has_internal_subset: int,
    ) -> None:
        del name, system_id, public_id, has_internal_subset
        raise XMLParseError("DTD declarations are forbidden")

    @staticmethod
    def reject_entity(
        name: str,
        is_parameter: int,
        value: str | None,
        base: str | None,
        system_id: str | None,
        public_id: str | None,
        notation_name: str | None,
    ) -> None:
        del name, is_parameter, value, base, system_id, public_id, notation_name
        raise XMLParseError("DTD entity declarations are forbidden")

    @staticmethod
    def reject_external_entity(
        context: str | None,
        base: str | None,
        system_id: str | None,
        public_id: str | None,
    ) -> int:
        del context, base, system_id, public_id
        raise XMLParseError("external XML entities are forbidden")

    def parse(self, source: bytes) -> Element:
        # The direct binding exposes DTD hooks without adding a parser dependency.
        parser = pyexpat.ParserCreate(namespace_separator="}")
        parser.buffer_text = True
        parser.StartElementHandler = self.start_element
        parser.EndElementHandler = self.end_element
        parser.CharacterDataHandler = self.character_data
        parser.ProcessingInstructionHandler = self.reject_processing_instruction
        parser.StartDoctypeDeclHandler = self.reject_doctype
        parser.EntityDeclHandler = self.reject_entity
        parser.ExternalEntityRefHandler = self.reject_external_entity
        if not parser.SetParamEntityParsing(pyexpat.XML_PARAM_ENTITY_PARSING_NEVER):
            raise XMLParseError("cannot disable XML parameter entities")
        try:
            parser.Parse(source, True)
        except XMLParseError:
            raise
        except (LookupError, ValueError, pyexpat.ExpatError) as error:
            raise XMLParseError("invalid XML") from error
        if self.stack:
            raise XMLParseError("unclosed XML element")
        if self.root is None:
            raise XMLParseError("empty XML document")
        return self.root


def clean_title(value: str) -> str:
    visible = "".join(
        "" if unicodedata.category(character) == "Cf" else " " if unicodedata.category(character) == "Cc" else character
        for character in value
    )
    return " ".join(visible.split())


def same_origin_article(url: str) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == HOST
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and not parsed.netloc.endswith(":")
    )


def encode_output(records: list[dict[str, Any]]) -> bytes:
    """Encode one bounded JSON document before writing any stdout bytes."""
    output = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    for chunk in encoder.iterencode(records):
        encoded = chunk.encode("utf-8")
        if len(output) + len(encoded) + 1 > MAX_OUTPUT_BYTES:
            raise ValueError("output exceeds 64 MiB")
        output.extend(encoded)
    output.extend(b"\n")
    return bytes(output)


def parse_rfc3339(value: str) -> datetime:
    if RFC3339.fullmatch(value) is None:
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def download(url: str, *, article: bool) -> bytes:
    arguments = [
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "60",
        "--retry",
        "2",
        "--retry-delay",
        "2",
        "--retry-all-errors",
        "--proto",
        "=https",
    ]
    if article:
        arguments.extend(["--max-redirs", "0"])
    arguments.extend(["--user-agent", USER_AGENT, url])
    with subprocess.Popen(["curl", *arguments], stdout=subprocess.PIPE) as process:
        if process.stdout is None:  # pragma: no cover
            raise RuntimeError
        raw = process.stdout.read(MAX_DOWNLOAD_BYTES + 1)
        if len(raw) > MAX_DOWNLOAD_BYTES:
            process.kill()
            process.wait()
            raise RuntimeError
        if process.wait() != 0:
            raise RuntimeError
    return raw


def feed_items(raw: bytes) -> list[dict[str, str]]:
    root = XMLTreeParser().parse(raw)
    if local_name(root.tag).casefold() != "rss":
        raise ValueError
    channels = root.children("channel")
    if len(channels) != 1:
        raise ValueError
    items = channels[0].children("item")
    if len(items) > MAX_ITEMS:
        raise ValueError
    records: list[dict[str, str]] = []
    for item in items:
        titles = item.children("title")
        links = item.children("link")
        guids = item.children("guid")
        title = clean_title(titles[0].text()) if titles else ""
        url = links[0].text() if links else ""
        identity = guids[0].text() if guids else url
        if not identity or not title or not same_origin_article(url):
            raise ValueError
        records.append({"id": identity, "title": title, "url": url})
    if not records:
        raise ValueError
    return records


def publication(raw: bytes) -> tuple[str, datetime]:
    matches = {match.decode("ascii") for match in DATE_PUBLISHED.findall(raw)}
    if len(matches) != 1:
        raise ValueError
    published_date = matches.pop()
    parsed = datetime.strptime(published_date, "%Y-%m-%d").replace(hour=12, tzinfo=UTC)
    return published_date, parsed


def main(arguments: list[str]) -> int:
    if arguments[:1] in (["--version"], ["-v"]):
        sys.stdout.write("google-developers-json.py (fkf base helper)\n")
        return 0
    if len(arguments) != 2:
        sys.stderr.write("usage: google-developers-json.py <start> <end>\n")
        return 2
    start_text, end_text = arguments
    try:
        start = parse_rfc3339(start_text)
    except ValueError:
        sys.stderr.write(f"google-developers-json.py: start is not an RFC3339 timestamp: {start_text}\n")
        return 2
    try:
        end = parse_rfc3339(end_text)
    except ValueError:
        sys.stderr.write(f"google-developers-json.py: end is not an RFC3339 timestamp: {end_text}\n")
        return 2
    if start >= end:
        sys.stderr.write("google-developers-json.py: start must be before end\n")
        return 2

    try:
        raw_feed = download(FEED, article=False)
    except (OSError, RuntimeError):
        sys.stderr.write(f"google-developers-json.py: cannot download {FEED}\n")
        return 1
    try:
        items = feed_items(raw_feed)
    except (UnicodeError, ValueError):
        sys.stderr.write(
            "google-developers-json.py: the official feed has an unexpected shape or external item URL\n"
        )
        return 1

    records: list[dict[str, Any]] = []
    for item in items:
        try:
            raw_article = download(item["url"], article=True)
        except (OSError, RuntimeError):
            sys.stderr.write(
                f"google-developers-json.py: cannot download same-origin article {item['url']}\n"
            )
            return 1
        try:
            published_date, published = publication(raw_article)
        except (UnicodeError, ValueError):
            sys.stderr.write(
                "google-developers-json.py: article has no unambiguous datePublished value: "
                f"{item['url']}\n"
            )
            return 1
        records.append(
            {
                **item,
                "time": published.isoformat().replace("+00:00", "Z"),
                "published_date": published_date,
                "time_precision": "date",
                "feed": FEED,
                "folder": "Google",
                "__published": published,
            }
        )

    identities = [record["id"] for record in records]
    if len(identities) != len(set(identities)) or any(
        records[index - 1]["__published"] < records[index]["__published"]
        for index in range(1, len(records))
    ):
        sys.stderr.write(
            "google-developers-json.py: feed item ids must be unique and publication dates newest-first\n"
        )
        return 1
    oldest = min(record["__published"] for record in records)
    if oldest >= start:
        oldest_text = oldest.isoformat().replace("+00:00", "Z")
        sys.stderr.write(
            "google-developers-json.py: the bounded feed cuts off at "
            f"{oldest_text}, at or after requested start {start_text}; completeness cannot be proved\n"
        )
        return 1
    selected = []
    for record in records:
        published = record.pop("__published")
        if start <= published < end:
            selected.append(record)
    selected.sort(key=lambda record: (record["time"], record["id"]))
    try:
        output = encode_output(selected)
    except ValueError as error:
        sys.stderr.write(f"google-developers-json.py: {error}\n")
        return 1
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
