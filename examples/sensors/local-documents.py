#!/usr/bin/env python3
"""Snapshot a selected document folder: text, saved HTML, Office XML and PDF via pdftotext."""

import argparse
import fnmatch
import io
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import tempfile
import time

# UTF-8-only XML is rejected before parsing if it contains a DTD/entity; depth/nodes are bounded below.
import xml.etree.ElementTree as ET  # nosemgrep: opengrep.rules.python.lang.security.use-defused-xml
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

FILE_BYTES = 16 << 20
TEXT_BYTES = 64 << 10
TOTAL_BYTES = 64 << 20
PDF_TIMEOUT = 60
EXTENSIONS = {".txt", ".md", ".rst", ".org", ".csv", ".tsv", ".html", ".htm", ".docx", ".pptx", ".xlsx", ".pdf"}
SKIP = {"node_modules", "__pycache__", "venv"}


class Page(HTMLParser):
    """Visible saved-page text, excluding executable and style content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def office(data: bytes, suffix: str) -> str:
    """Read document text, slide paragraphs, or every worksheet's labelled cells without extracting files."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(item.file_size for item in entries) > TOTAL_BYTES:
            raise ValueError("Office archive exceeds its expanded limit")
        if len({item.filename for item in entries}) != len(entries):
            raise ValueError("duplicate Office archive member")

        def xml(name: str) -> ET.Element:
            raw = archive.read(name)
            text = raw.decode("utf-8-sig")
            if len(raw) > FILE_BYTES or "\x00" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
                raise ValueError("unsafe or oversized document XML")
            events: tuple[Literal["start", "end"], ...] = ("start", "end")
            parser: ET.XMLPullParser[ET.Element] = ET.XMLPullParser(events=events)
            root: ET.Element | None = None
            count = depth = 0
            for offset in range(0, len(text), 65536):
                parser.feed(text[offset : offset + 65536])
                # The stdlib stub includes namespace events, which this parser never requests.
                parsed = cast("Iterator[tuple[Literal['start', 'end'], ET.Element]]", parser.read_events())
                for event, element in parsed:
                    if event == "start":
                        if root is None:
                            root = element
                        count += 1
                        depth += 1
                        if count > 100_000 or depth > 64:
                            raise ValueError("document XML exceeds its structure limit")
                    else:
                        depth -= 1
            parser.close()
            if root is None:
                raise ValueError("document XML is empty")
            return root

        def text(element: ET.Element) -> str:
            return "".join(child.text or "" for child in element.iter() if tag(child) == "t")

        if suffix == ".docx":
            names = ["word/document.xml"]
            names.extend(
                sorted(name for name in archive.namelist() if re.fullmatch(r"word/(header|footer)\d+\.xml", name))
            )
            return "\n".join(
                text(paragraph) for name in names for paragraph in xml(name).iter() if tag(paragraph) == "p"
            )
        if suffix == ".pptx":
            names = [
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml", name)
            ]
            names.sort(key=lambda name: ("notesSlides" in name, int(re.findall(r"\d+", name)[-1])))
            return "\n\n".join(
                name + "\n" + "\n".join(text(paragraph) for paragraph in xml(name).iter() if tag(paragraph) == "p")
                for name in names
            )
        strings = (
            [text(item) for item in xml("xl/sharedStrings.xml")] if "xl/sharedStrings.xml" in archive.namelist() else []
        )
        names = [name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)]
        names.sort(key=lambda name: int(re.findall(r"\d+", name)[-1]))
        parts = []
        for name in names:
            parts.append(name)
            for cell in xml(name).iter():
                if tag(cell) != "c":
                    continue
                value = next((child.text or "" for child in cell if tag(child) == "v"), "")
                if cell.get("t") == "s":
                    position = int(value)
                    if not 0 <= position < len(strings):
                        raise ValueError("worksheet shared string index is invalid")
                    value = strings[position]
                elif cell.get("t") == "inlineStr":
                    value = text(cell)
                formula = next((child.text or "" for child in cell if tag(child) == "f"), "")
                parts.append(f"{cell.get('r', '?')}: {value}" + (f" [formula: {formula}]" if formula else ""))
        return "\n".join(parts)


def pdf(path: Path) -> bytes:
    """Bound converter output while it runs; inherit the collector group so Brain Framework can cancel the whole tree."""
    child = subprocess.Popen(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    output = bytearray()
    deadline = time.monotonic() + PDF_TIMEOUT
    try:
        if child.stdout is None:
            raise ValueError("PDF converter pipe is missing")
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise ValueError("PDF conversion timed out")
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    output.extend(chunk)
                    if len(output) > FILE_BYTES:
                        raise ValueError("PDF text exceeds its limit")
        if child.wait():
            raise ValueError("PDF conversion failed")
        return bytes(output)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


def extract(data: bytes, suffix: str) -> str:
    if suffix in {".docx", ".pptx", ".xlsx"}:
        return office(data, suffix)
    if suffix == ".pdf":
        # Use a private copy of the checked bytes so the converter never reopens an untrusted path.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.pdf"
            path.write_bytes(data)
            return pdf(path).decode("utf-8")
    value = data.decode("utf-8-sig")
    if suffix in {".html", ".htm"}:
        page = Page()
        page.feed(value)
        page.close()
        value = "".join(page.parts)
    return value


def collect(label: str, root: Path, excluded: list[str]) -> list[dict[str, object]]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", label):
        raise ValueError("scope label must be a lowercase name")
    root = root.expanduser().absolute()
    records: list[dict[str, object]] = []
    visited = total = 0
    projected = 2

    def visit(descriptor: int, prefix: str = "") -> None:
        nonlocal visited, total, projected
        if prefix.count("/") > 20:
            raise ValueError("document tree exceeds its depth limit")
        with os.scandir(descriptor) as entries:
            selected = []
            for entry in entries:
                visited += 1
                if visited > 10000:
                    raise ValueError("selected document tree exceeds 10000 entries")
                selected.append(entry)
            for entry in sorted(selected, key=lambda item: item.name):
                relative = prefix + entry.name
                if (
                    entry.name.startswith(".")
                    or entry.name in SKIP
                    or any(fnmatch.fnmatchcase(relative, pattern) for pattern in excluded)
                ):
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise ValueError("selected document tree contains redirected or special files")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    try:
                        visit(child, relative + "/")
                    finally:
                        os.close(child)
                    continue
                suffix = Path(entry.name).suffix.lower()
                if suffix not in EXTENSIONS:
                    continue
                fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("document is not a regular file")
                    data = stream.read(FILE_BYTES + 1)
                    after = os.fstat(stream.fileno())
                if len(data) > FILE_BYTES or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError("document is oversized or changed during collection")
                total += len(data)
                if total > TOTAL_BYTES:
                    raise ValueError("selected documents exceed 64 MiB; narrow the root")
                content = extract(data, suffix).encode()
                identifier = label + "/" + relative
                when = datetime.fromtimestamp(info.st_mtime, UTC).isoformat()
                record: dict[str, object] = {
                    "id": identifier,
                    "title": " ".join(relative.split()),
                    "text": content[:TEXT_BYTES].decode(errors="ignore"),
                    "time": when,
                    "url": (root / relative).as_uri(),
                    "aliases": ["file:" + quote(identifier, safe="/")],
                    "attributes": {
                        "updated": when,
                        "scope": label,
                        "path": relative,
                        "bytes": len(data),
                        "partial": len(content) > TEXT_BYTES,
                    },
                }
                projected += len(json.dumps(record, ensure_ascii=False).encode()) + 2
                if projected > TOTAL_BYTES:
                    raise ValueError("normalized document snapshot exceeds 64 MiB")
                records.append(record)

    # Walk root ancestors through descriptors too: checking is_symlink then reopening by path races.
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(root.anchor, directory_flags)
    try:
        for part in root.parts[1:]:
            if part == "..":
                raise ValueError("select a normalized document root")
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        visit(descriptor)
    finally:
        os.close(descriptor)
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="stable personal/team scope name")
    parser.add_argument("root", type=Path, help="explicit allowlisted directory; use snapshot mode")
    parser.add_argument(
        "--exclude", action="append", default=[], help="selected-root-relative glob to skip (repeatable)"
    )
    args = parser.parse_args()
    try:
        payload = json.dumps(collect(args.label, args.root, args.exclude), ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > TOTAL_BYTES:
            raise ValueError("normalized document snapshot exceeds 64 MiB")
        print(payload)
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        UnicodeError,
        ET.ParseError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ):
        print(
            "Local document collection failed; check the selected root, limits, document integrity and pdftotext for PDFs.",
            file=sys.stderr,
        )
        sys.exit(1)
