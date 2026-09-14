#!/usr/bin/env python3
"""Collect Google Docs named like meeting notes, with their exported text, through the owner's gws CLI."""

import json
import subprocess
import sys
import tempfile
from datetime import datetime
from urllib.parse import quote

DOCUMENT = "application/vnd.google-apps.document"
FIELDS = "nextPageToken,incompleteSearch,files(id,name,mimeType,size,createdTime,modifiedTime,webViewLink,parents,owners(emailAddress))"
PAGE_BYTES = 16 << 20
TEXT_BYTES = 64 << 10
MAX_PAGES = 20
MAX_DOCUMENTS = 200


def call(arguments: list[str]) -> dict[str, object]:
    """Run one bounded gws command and decode its JSON object; provider diagnostics stay private."""
    with tempfile.TemporaryFile() as output:
        subprocess.run(["gws", *arguments], check=True, stdout=output, stderr=subprocess.DEVNULL, timeout=60)
        output.seek(0)
        payload = output.read(PAGE_BYTES + 1)
    if len(payload) > PAGE_BYTES:
        raise ValueError("provider page exceeds 16 MiB")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("unexpected provider response")
    return value


def instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed


def title(value: object, fallback: str) -> str:
    """One visible line without control characters, as the record contract requires."""
    if not isinstance(value, str):
        return fallback
    visible = " ".join("".join(c if ord(c) >= 32 and not 127 <= ord(c) < 160 else " " for c in value).split())
    return visible[:512] or fallback


def runs(value: object) -> list[str]:
    """Text runs in response traversal order, as the Docs API nests them."""
    parts: list[str] = []
    if isinstance(value, dict):
        run = value.get("textRun")
        if isinstance(run, dict) and isinstance(run.get("content"), str):
            parts.append(run["content"])
        for child in value.values():
            parts.extend(runs(child))
    elif isinstance(value, list):
        for child in value:
            parts.extend(runs(child))
    return parts


def export(identifier: str) -> str:
    # https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/get
    document = call(["docs", "documents", "get", "--params", json.dumps({"documentId": identifier})])
    text = "".join(runs(document))
    encoded = text.encode()
    if len(encoded) > TEXT_BYTES:
        text = encoded[:TEXT_BYTES].decode(errors="ignore") + "\n[truncated: document text exceeds 64 KiB]"
    return text


def listing(query: str) -> list[dict[str, object]]:
    files: dict[str, dict[str, object]] = {}
    token = ""
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        # https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list
        params: dict[str, object] = {"q": query, "pageSize": 1000, "orderBy": "createdTime asc", "fields": FIELDS}
        if token:
            params["pageToken"] = token
        page = call(["drive", "files", "list", "--params", json.dumps(params)])
        if page.get("incompleteSearch", False) is not False:
            raise ValueError("Drive search is incomplete")
        items = page.get("files", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("unexpected Drive listing response")
        for item in items:
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("Drive file without id")
            files.setdefault(identifier, item)
            if len(files) > MAX_DOCUMENTS:
                raise ValueError("document count exceeds 200")
        token = page.get("nextPageToken", "")
        if not token:
            return list(files.values())
        if not isinstance(token, str) or token in seen:
            raise ValueError("Drive pagination repeated a token")
        seen.add(token)
    raise ValueError("Drive pagination exceeded 20 pages")


def collect(start: str, end: str, name: str) -> list[dict[str, object]]:
    if "'" in name or "\\" in name or not name.strip():
        raise ValueError("name may not be empty or contain a quote or backslash")
    begin, finish = instant(start), instant(end)
    if begin >= finish:
        raise ValueError("start must be before end")
    window = f"(createdTime >= '{start}' and createdTime < '{end}') or (modifiedTime >= '{start}' and modifiedTime < '{end}')"
    query = f"mimeType='{DOCUMENT}' and trashed=false and name contains '{name}' and ({window})"
    records: list[dict[str, object]] = []
    for item in listing(query):
        created, modified = instant(item.get("createdTime")), instant(item.get("modifiedTime"))
        if not (begin <= created < finish or begin <= modified < finish) or item.get("mimeType") != DOCUMENT:
            continue
        identifier = str(item["id"])
        owners = item.get("owners") or []
        if not isinstance(owners, list) or any(not isinstance(owner, dict) for owner in owners):
            raise ValueError("unexpected owners")
        emails = sorted(
            {
                str(owner["emailAddress"]).lower()
                for owner in owners
                if isinstance(owner.get("emailAddress"), str) and owner["emailAddress"]
            }
        )
        lines = [f"Created: {item['createdTime']}", f"Modified: {item['modifiedTime']}"]
        if emails:
            lines.append("Owners: " + ", ".join(emails))
        lines.extend(["", export(identifier)])
        parents = item.get("parents", [])
        if not isinstance(parents, list) or any(not isinstance(parent, str) or not parent for parent in parents):
            raise ValueError("unexpected parent identifiers")
        records.append(
            {
                "id": identifier,
                "title": title(item.get("name"), "Untitled document"),
                "time": str(item["createdTime"]),
                "text": "\n".join(lines),
                "url": str(item.get("webViewLink") or ""),
                "links": [f"person:email/{email}" for email in emails],
                "aliases": [f"drive:{identifier}"],
                "parents": ["drive:" + quote(parent, safe="") for parent in parents],
                "attributes": {
                    "mimeType": item.get("mimeType"),
                    "size": item.get("size"),
                    "modifiedTime": item.get("modifiedTime"),
                    "parents": item.get("parents", []),
                },
            }
        )
    return records


if __name__ == "__main__":
    try:
        value = collect(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "Notes by Gemini")
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > PAGE_BYTES:
            raise ValueError("normalized meeting notes exceed 16 MiB")
        print(payload)
    except (
        OSError,
        UnicodeError,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
        subprocess.SubprocessError,
        IndexError,
    ):
        print("Meeting notes collection failed; check gws authentication and the requested window.", file=sys.stderr)
        sys.exit(1)
