#!/usr/bin/env python3
"""Validate and project the complete bounded Google Drive file page chain."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any
from urllib.parse import quote

MAX_PAGES = 100
MAX_PROVIDER_BYTES = 64 << 20
MAX_OUTPUT_BYTES = 64 << 20


def compact(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def identity(value: str) -> str:
    return quote(value, safe=":/@+").replace("~", "%7E")


def documents(raw: bytes) -> list[Any]:
    text = raw.decode("utf-8")
    def reject_constant(_value: str) -> None:
        raise ValueError

    decoder = json.JSONDecoder(parse_constant=reject_constant)
    offset = 0
    values: list[Any] = []
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset == len(text):
            break
        value, offset = decoder.raw_decode(text, offset)
        values.append(value)
    return values


def validated_pages(raw: bytes) -> list[dict[str, Any]]:
    values = documents(raw)
    if not 1 <= len(values) <= MAX_PAGES or any(not isinstance(value, dict) for value in values):
        raise ValueError
    pages = [value for value in values if isinstance(value, dict)]
    tokens: list[str] = []
    for index, page in enumerate(pages):
        files = page.get("files")
        token = page.get("nextPageToken")
        if files is not None and (
            not isinstance(files, list) or any(not isinstance(item, dict) for item in files)
        ):
            raise ValueError
        if token is not None and (not isinstance(token, str) or not token):
            raise ValueError
        if index < len(pages) - 1 and token is None:
            raise ValueError
        if token is not None:
            tokens.append(token)
    if pages[-1].get("nextPageToken") is not None or len(tokens) != len(set(tokens)):
        raise ValueError
    return pages


def person(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError
    email = value.get("emailAddress")
    return compact(
        {
            "displayName": value.get("displayName"),
            "emailAddress": email,
            "uri": f"person:email/{identity(email.lower())}" if isinstance(email, str) and email else None,
        }
    )


def projected_file(value: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "name",
        "mimeType",
        "webViewLink",
        "createdTime",
        "modifiedTime",
        "modifiedByMeTime",
        "viewedByMeTime",
        "size",
        "version",
        "starred",
        "trashed",
        "shared",
        "ownedByMe",
        "fileExtension",
        "parents",
    )
    owners = value.get("owners") or []
    modifier = value.get("lastModifyingUser")
    if not isinstance(owners, list):
        raise TypeError
    return compact(
        {
            **{field: value.get(field) for field in fields},
            "owners": [person(owner) for owner in owners],
            "lastModifyingUser": person(modifier) if modifier is not None else None,
        }
    )


def read_provider(arguments: list[str]) -> bytes:
    if arguments[:1] != ["gws"]:
        raise ValueError
    with subprocess.Popen(["gws", *arguments[1:]], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        if process.stdout is None:  # pragma: no cover
            raise RuntimeError
        raw = process.stdout.read(MAX_PROVIDER_BYTES + 1)
        if len(raw) > MAX_PROVIDER_BYTES:
            process.kill()
            process.wait()
            raise ValueError
        status = process.wait()
        if status != 0:
            raise RuntimeError(status)
    return raw


def main(arguments: list[str]) -> int:
    if arguments[:1] in (["--version"], ["-v"]):
        sys.stdout.write("gws-drive-files-json.py (fkf preset helper)\n")
        return 0
    try:
        raw = read_provider(arguments) if arguments else sys.stdin.buffer.read(MAX_PROVIDER_BYTES + 1)
        if len(raw) > MAX_PROVIDER_BYTES:
            raise ValueError
        pages = validated_pages(raw)
        output = "".join(
            json.dumps(
                {"files": [projected_file(item) for item in page.get("files") or []]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
            for page in pages
        )
        if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError
    except RuntimeError as error:
        return int(error.args[0]) if error.args and isinstance(error.args[0], int) else 1
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        sys.stderr.write(
            "gws-drive-files-json.py: invalid token chain or page limit reached; cannot prove completeness\n"
        )
        return 1
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
