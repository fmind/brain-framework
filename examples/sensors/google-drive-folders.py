#!/usr/bin/env python3
"""Capture the Drive folders of My Drive and shared-with-me items, linking each to its parents, through gws.

Shared drives are outside this catalog (`corpora: user`).
"""

import json
import os
import re
import selectors
import subprocess
import sys
import time
from contextlib import suppress
from datetime import datetime
from urllib.parse import quote

MAX_BYTES = 16 << 20
MAX_FOLDERS = 10000
MAX_PAGES = 20
FOLDER = "application/vnd.google-apps.folder"
# BF record bounds: an id fits a BF address once percent-encoded; links and URLs are bounded single lines.
MAX_ID, MAX_ENCODED, MAX_REF, MAX_TITLE = 4096, 7988, 8192, 4096
# C0 and C1 control characters, which BF rejects in ids, titles, URLs and identities.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class InvalidError(ValueError):
    """A content-free diagnostic safe to show on stderr."""


def run(argv: list[str], limit: int, timeout: int) -> bytes:
    """Bound provider output while it runs; inherit Brain Framework's cancellable process group."""
    # Only literal provider commands reach this helper; no shell interprets argv.
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        if child.stdout is None:
            raise ValueError("provider pipe is missing")
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map() or child.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("provider exceeded its timeout")
                if not selector.get_map():
                    with suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=0.05)
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    output.extend(chunk)
                    if len(output) > limit:
                        raise InvalidError("a gws page exceeds its byte limit")
        if code := child.wait():
            raise subprocess.CalledProcessError(code, argv)
        return bytes(output)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


def line(value: str, fallback: str) -> str:
    """One bounded title line: control characters become spaces and whitespace collapses."""
    return " ".join(CONTROL.sub(" ", value).split())[:MAX_TITLE].rstrip() or fallback


def reference(value: object) -> str:
    """A URL or identity BF accepts, or nothing: an over-long or control-character value is dropped."""
    if isinstance(value, str) and value.strip() and len(value) <= MAX_REF and not CONTROL.search(value):
        return value
    return ""


def instant(value: object) -> str:
    """A provider timestamp with a timezone, or nothing: BF validates `attributes.updated` strictly."""
    try:
        return value if isinstance(value, str) and datetime.fromisoformat(value).tzinfo else ""
    except ValueError:
        return ""


def collect() -> list[dict[str, object]]:
    # https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list
    token = ""
    tokens: set[str] = set()
    seen: set[str] = set()
    records = []
    for _ in range(MAX_PAGES):
        params = {
            "q": f"trashed=false and mimeType='{FOLDER}'",
            "corpora": "user",
            "pageSize": 1000,
            "fields": "kind,nextPageToken,incompleteSearch,files(id,name,mimeType,parents,createdTime,modifiedTime,webViewLink)",
        }
        if token:
            params["pageToken"] = token
        data = run(["gws", "drive", "files", "list", "--params", json.dumps(params)], MAX_BYTES, 120)
        page = json.loads(data)
        if (
            not isinstance(page, dict)
            or page.get("kind") != "drive#fileList"
            or page.get("incompleteSearch", False) is not False
            or not isinstance(page.get("files", []), list)
        ):
            raise InvalidError("gws returned an incomplete or invalid folder listing")
        for folder in page.get("files", []):
            if not isinstance(folder, dict):
                raise InvalidError("gws returned an invalid folder")
            identifier, name, parents = folder.get("id"), folder.get("name"), folder.get("parents", [])
            if (
                not isinstance(identifier, str)
                or not identifier.strip()
                or identifier in seen
                or CONTROL.search(identifier)
                or len(identifier) > MAX_ID
                or len(quote(identifier, safe="/@:")) > MAX_ENCODED
                or not isinstance(name, str)
                or not name.strip()
                or folder.get("mimeType") != FOLDER
                or not isinstance(parents, list)
                or any(not isinstance(p, str) or not p for p in parents)
            ):
                raise InvalidError("gws returned an invalid, duplicate or over-long folder identity")
            seen.add(identifier)
            if len(seen) > MAX_FOLDERS:
                raise InvalidError(f"the catalog has more than {MAX_FOLDERS} folders")
            created, modified = instant(folder.get("createdTime")), instant(folder.get("modifiedTime"))
            records.append(
                {
                    "id": identifier,
                    "title": line(name, "Untitled Drive folder"),
                    "text": "Drive folder: " + name,
                    # The folder's creation is its event; modification is the revision BF compares.
                    "time": created or modified,
                    "url": reference(folder.get("webViewLink", "")),
                    "aliases": list(filter(None, [reference("drive:" + quote(identifier, safe=""))])),
                    "links": list(filter(None, (reference("drive:" + quote(parent, safe="")) for parent in parents))),
                    # Selected fields only: `updated` is BF's reserved provider-modification time.
                    "attributes": {
                        "name": name,
                        "parents": parents,
                        **({"created": created} if created else {}),
                        **({"updated": modified} if modified else {}),
                    },
                }
            )
        token = page.get("nextPageToken", "")
        if not isinstance(token, str):
            raise InvalidError("gws returned an invalid continuation token")
        if not token:
            return records
        if token in tokens:
            raise InvalidError("gws repeated a continuation token")
        tokens.add(token)
    raise InvalidError(f"the catalog needs more than {MAX_PAGES} pages")


if __name__ == "__main__":
    try:
        payload = json.dumps(collect(), ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > MAX_BYTES:
            raise InvalidError("normalized folders exceed 16 MiB")
        print(payload)
    except InvalidError as error:
        print(f"Drive folder collection failed: {error}.", file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError):
        print("Drive folder collection failed; check gws access and folder listing completeness.", file=sys.stderr)
        sys.exit(1)
