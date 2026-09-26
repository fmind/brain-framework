"""Discovery summaries minimize disclosure and never execute found tools."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "skills/bf-scan/scripts/inventory.py"


def run(*args: str, raw: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed helper with synthetic input
        [sys.executable, str(HELPER), *args], input=raw, capture_output=True, timeout=10, check=False
    )


def export(*urls: str) -> bytes:
    return json.dumps(
        {
            "roots": {
                "bar": {
                    "type": "folder",
                    "name": "PRIVATE_FOLDER",
                    "children": [{"type": "url", "name": "PRIVATE_TITLE", "url": url} for url in urls],
                }
            }
        }
    ).encode()


@pytest.mark.parametrize("fmt", ["chromium", "html"])
def test_bookmarks_retain_only_hosts_and_counts(fmt: str) -> None:
    urls = [
        "https://user:SECRET@EXAMPLE.org/private?token=SECRET#PRIVATE",
        "https://example.org/other",
        "javascript:SECRET",
    ]
    raw = (
        export(*urls)
        if fmt == "chromium"
        else (
            "<!DOCTYPE NETSCAPE-Bookmark-file-1><TITLE>PRIVATE_TITLE</TITLE><DL>"
            + "".join(f'<DT><A HREF="{url}">PRIVATE_TITLE</A>' for url in urls)
            + "</DL>"
        ).encode()
    )
    result = run("bookmarks", "--format", fmt, raw=raw)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"hosts": [{"host": "example.org", "count": 2}], "skipped_urls": 1}
    assert not result.stderr


@pytest.mark.parametrize(
    "raw",
    [
        b"SECRET",
        b'{"roots":{"x":{"type":"url","url":123}}}',
        b"\xff",
        b"x" * (4 * 1024 * 1024 + 1),
        export(*(f"https://h{i}.example.org" for i in range(201))),
        export(*(["https://example.org"] * 20_001)),
        export("https://example.org")[:-1],
    ],
    ids=["text", "bad-node", "encoding", "bytes", "hosts", "entries", "truncated"],
)
def test_invalid_or_excessive_input_releases_no_partial_summary(raw: bytes) -> None:
    result = run("bookmarks", "--format", "chromium", raw=raw)
    assert result.returncode == 1
    assert not result.stdout
    assert b"select a smaller supported export" in result.stderr
    assert b"SECRET" not in result.stderr
    assert b"Traceback" not in result.stderr


def test_html_requires_an_export_and_rejects_malformed_links() -> None:
    for raw in (
        b'<a href="https://example.org">secret</a>',
        b"<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><A>secret</A></DL>",
        b'<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><A HREF="https://example.org">secret</A>',
        b'<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><A HREF="https://example.org" HREF="https://other.org">secret</A></DL>',
        b'<!DOCTYPE NETSCAPE-Bookmark-file-1><A HREF="https://example.org">secret</A>',
        b"<!DOCTYPE NETSCAPE-Bookmark-file-1><DL></DL></DL>",
    ):
        result = run("bookmarks", "--format", "html", raw=raw)
        assert result.returncode == 1
        assert not result.stdout


def test_html_nested_folders_and_empty_exports() -> None:
    for body, count in (
        ("<DL></DL>", 0),
        ('<DL><DT><H3>PRIVATE</H3><DL><A HREF="https://example.org">PRIVATE</A></DL></DL>', 1),
    ):
        result = run("bookmarks", "--format", "html", raw=("<!DOCTYPE NETSCAPE-Bookmark-file-1>" + body).encode())
        assert result.returncode == 0
        assert json.loads(result.stdout) == {
            "hosts": [{"host": "example.org", "count": 1}] if count else [],
            "skipped_urls": 0,
        }


def test_empty_export_is_supported_and_invalid_urls_are_visible() -> None:
    empty = run("bookmarks", "--format", "chromium", raw=export())
    assert json.loads(empty.stdout) == {"hosts": [], "skipped_urls": 0}
    result = run(
        "bookmarks",
        "--format",
        "chromium",
        raw=export("file:///SECRET", "https://[broken", "https://", "https://a%20b.org"),
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"hosts": [], "skipped_urls": 4}


def test_tool_checks_never_execute_or_reveal_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    tool = tmp_path / "available"
    tool.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = run("tools", "available", "absent", "available", raw=b"IGNORED")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "tools": [{"name": "available", "available": True}, {"name": "absent", "available": False}]
    }
    assert not marker.exists()
    assert str(tmp_path).encode() not in result.stdout


@pytest.mark.parametrize(
    "args", [(), ("tools",), ("tools", "../secret"), ("tools", "a;id"), ("bookmarks",), ("tools", *(["git"] * 33))]
)
def test_no_implicit_scan_or_arbitrary_executable_path(args: tuple[str, ...]) -> None:
    result = run(*args)
    assert result.returncode == 2
    assert not result.stdout


def test_helper_supports_the_agent_interpreter() -> None:
    ast.parse(HELPER.read_text(encoding="utf-8"), feature_version=(3, 11))
    assert os.access(HELPER, os.X_OK)
