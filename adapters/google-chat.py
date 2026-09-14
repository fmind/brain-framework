#!/usr/bin/env python3
"""Collect Google Chat messages posted in a window across the spaces you belong to, through the owner's gws CLI."""

import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta

PAGE_BYTES = 16 << 20
MAX_PAGES = 20
MAX_SPACES = 200


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


def pages(command: list[str], params: Mapping[str, object], collection: str) -> list[dict[str, object]]:
    """Follow a finite, non-repeating token chain and return every item of one collection."""
    items: list[dict[str, object]] = []
    token = ""
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        request = dict(params)
        if token:
            request["pageToken"] = token
        page = call([*command, "--params", json.dumps(request)])
        values = page.get(collection, [])
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise ValueError("unexpected Chat listing response")
        items.extend(values)
        token = page.get("nextPageToken", "")
        if not token:
            return items
        if not isinstance(token, str) or token in seen:
            raise ValueError("Chat pagination repeated a token")
        seen.add(token)
    raise ValueError("Chat pagination exceeded 20 pages")


def instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed


def visible(value: object, fallback: str, limit: int = 512) -> str:
    """One line without control characters, as the record contract requires."""
    if not isinstance(value, str):
        return fallback
    line = " ".join("".join(c if ord(c) >= 32 and not 127 <= ord(c) < 160 else " " for c in value).split())
    return line[:limit] or fallback


def collect(start: str, end: str) -> list[dict[str, object]]:
    begin, finish = instant(start), instant(end)
    if begin >= finish:
        raise ValueError("start must be before end")
    # The API filter is exclusive on both ends; one second earlier keeps the window half-open at start.
    earlier = (begin - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    query = f'createTime > "{earlier}" AND createTime < "{end}"'
    # https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces/list
    spaces = pages(["chat", "spaces", "list"], {"pageSize": 1000}, "spaces")
    if len(spaces) > MAX_SPACES:
        raise ValueError("space count exceeds 200")
    records: dict[str, dict[str, object]] = {}
    for space in sorted(spaces, key=lambda item: str(item.get("name", ""))):
        name = space.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("space without name")
        kind = str(space.get("spaceType", "")).replace("_", " ").capitalize()
        label = visible(space.get("displayName"), kind or name)
        # https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/list
        params = {"parent": name, "pageSize": 1000, "filter": query, "showDeleted": True}
        for message in pages(["chat", "spaces", "messages", "list"], params, "messages"):
            identifier, created = message.get("name"), message.get("createTime")
            if not isinstance(identifier, str) or not identifier or not isinstance(created, str):
                raise ValueError("message without name or time")
            if not begin <= instant(created) < finish:
                continue
            sender = message.get("sender") or {}
            thread = message.get("thread") or {}
            if not isinstance(sender, dict) or not isinstance(thread, dict):
                raise ValueError("unexpected message shape")
            author = visible(sender.get("displayName"), str(sender.get("name") or "unknown sender"))
            body = message.get("text") if isinstance(message.get("text"), str) else message.get("formattedText")
            body = body if isinstance(body, str) else ""
            if "deleteTime" in message and not body:
                body = "[deleted message]"
            fragment = visible(body, "", 80)
            lines = [f"Space: {label} ({name})", f"Sender: {author}", f"Created: {created}"]
            if isinstance(thread.get("name"), str):
                lines.append(f"Thread: {thread['name']}")
            if isinstance(message.get("deleteTime"), str):
                lines.append(f"Deleted: {message['deleteTime']}")
            lines.extend(["", body])
            links = [f"chat:{name}"]
            if isinstance(sender.get("email"), str) and sender["email"]:
                links.append(f"person:email/{sender['email'].lower()}")
            records[identifier] = {
                "id": identifier,
                "title": f"{label}: {author}" + (f" - {fragment}" if fragment else ""),
                "time": created,
                "text": "\n".join(lines),
                "url": "",
                "links": sorted(links),
                "aliases": [],
                "attributes": {
                    "space": name,
                    "thread": thread.get("name"),
                    "sender": sender.get("name"),
                    "deleted": "deleteTime" in message,
                },
            }
    return list(records.values())


if __name__ == "__main__":
    try:
        value = collect(sys.argv[1], sys.argv[2])
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > PAGE_BYTES:
            raise ValueError("normalized chat messages exceed 16 MiB")
        print(payload)
    except OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.SubprocessError, IndexError:
        print("Chat collection failed; check gws authentication and the requested window.", file=sys.stderr)
        sys.exit(1)
