#!/usr/bin/env python3
"""Capture the current Drive folder catalog, linking each folder to its parents, through gws."""

import json
import os
import selectors
import subprocess
import sys
import time
from contextlib import suppress
from urllib.parse import quote

MAX_BYTES = 16 << 20
FOLDER = "application/vnd.google-apps.folder"


def run(argv: list[str], limit: int, timeout: int) -> bytes:
    """Bound provider output while it runs; inherit Brain Framework's cancellable process group."""
    # Only literal provider commands reach this helper; no shell interprets argv.
    child = subprocess.Popen(  # nosemgrep: dangerous-subprocess-use-audit
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
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
                        raise ValueError("provider output exceeds its byte limit")
        if code := child.wait():
            raise subprocess.CalledProcessError(code, argv)
        return bytes(output)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


def collect() -> list[dict[str, object]]:
    # https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list
    token = ""
    tokens: set[str] = set()
    seen: set[str] = set()
    records = []
    for _ in range(20):
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
            raise ValueError("incomplete or invalid folder listing")
        for folder in page.get("files", []):
            if not isinstance(folder, dict):
                raise ValueError("invalid folder")
            identifier, name, parents = folder.get("id"), folder.get("name"), folder.get("parents", [])
            if (
                not isinstance(identifier, str)
                or not identifier
                or identifier in seen
                or not isinstance(name, str)
                or not name.strip()
                or folder.get("mimeType") != FOLDER
                or not isinstance(parents, list)
                or any(not isinstance(p, str) or not p for p in parents)
            ):
                raise ValueError("invalid folder identity")
            seen.add(identifier)
            if len(seen) > 10000:
                raise ValueError("folder count exceeds limit")
            records.append(
                {
                    "id": identifier,
                    "title": " ".join(name.split()),
                    "text": "Drive folder: " + name,
                    "time": folder.get("modifiedTime", ""),
                    "url": folder.get("webViewLink", ""),
                    "aliases": ["drive:" + quote(identifier, safe="")],
                    "links": ["drive:" + quote(parent, safe="") for parent in parents],
                    "attributes": folder,
                }
            )
        token = page.get("nextPageToken", "")
        if not token:
            return records
        if not isinstance(token, str) or token in tokens:
            raise ValueError("folder pagination repeated a token")
        tokens.add(token)
    raise ValueError("folder pagination exceeded limit")


if __name__ == "__main__":
    try:
        payload = json.dumps(collect(), ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > MAX_BYTES:
            raise ValueError("normalized folders exceed limit")
        print(payload)
    except OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError:
        print("Drive folder collection failed; check gws access and folder listing completeness.", file=sys.stderr)
        sys.exit(1)
