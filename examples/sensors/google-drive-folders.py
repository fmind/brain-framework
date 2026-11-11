#!/usr/bin/env python3
"""Capture the Drive folders of My Drive and shared-with-me items, linking each to its parents, through gws.

Shared drives are outside this catalog (`corpora: user`). Each folder links its parents and lists them in
`attributes.parent_refs`, which the standard `part-of` relation of a new brain maps. A brain whose bf.yaml lacks that
field declares it under `fields:` first:

  part-of:
    description: Containing thread, folder, list, epic, project or other parent item.
    type: identity
    cardinality: many
    relation: true
    path: /attributes/parent_refs
"""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import quote

import _sensor

MAX_BYTES = 16 << 20
MAX_FOLDERS = 10000
MAX_PAGES = 20
FOLDER = "application/vnd.google-apps.folder"
InvalidError = _sensor.InvalidError


def optional_instant(value: object) -> str:
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
        data = _sensor.run(
            ["gws", "drive", "files", "list", "--params", json.dumps(params)], timeout=120, max_bytes=MAX_BYTES
        )
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
                or _sensor.CONTROL.search(identifier)
                or len(identifier) > _sensor.MAX_ID
                or len(quote(identifier, safe="/@:")) > _sensor.MAX_ENCODED_ID
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
            created = optional_instant(folder.get("createdTime"))
            modified = optional_instant(folder.get("modifiedTime"))
            parent_refs = list(
                filter(None, (_sensor.reference("drive:" + quote(parent, safe="")) for parent in parents))
            )
            alias = _sensor.reference("drive:" + quote(identifier, safe=""))
            records.append(
                _sensor.record(
                    identifier,
                    _sensor.line(name, "Untitled Drive folder"),
                    text="Drive folder: " + name,
                    # The folder's creation is its event; modification is the revision BF compares.
                    time=created or modified,
                    url=str(folder.get("webViewLink", "")),
                    aliases=[alias] if alias else (),
                    links=parent_refs,
                    # Selected fields only: `updated` is BF's reserved provider-modification time.
                    attributes={
                        "name": name,
                        "parents": parents,
                        "parent_refs": parent_refs,
                        **({"created": created} if created else {}),
                        **({"updated": modified} if modified else {}),
                    },
                )
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
    _sensor.cli(
        lambda: _sensor.emit(collect(), max_bytes=MAX_BYTES),
        "Drive folder collection failed",
        "check gws access and folder listing completeness",
    )
