#!/usr/bin/env python3
"""Capture the current Drive folder catalog, linking each folder to its parents, through gws."""

import json
import subprocess
import sys
import tempfile
from urllib.parse import quote

MAX_BYTES = 16 << 20
FOLDER = "application/vnd.google-apps.folder"


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
            "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType,parents,createdTime,modifiedTime,webViewLink)",
        }
        if token:
            params["pageToken"] = token
        with tempfile.TemporaryFile() as output:
            subprocess.run(
                ["gws", "drive", "files", "list", "--params", json.dumps(params)],
                check=True,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            output.seek(0)
            data = output.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("folder page exceeds limit")
        page = json.loads(data)
        if (
            not isinstance(page, dict)
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
