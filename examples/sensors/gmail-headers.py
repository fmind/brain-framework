#!/usr/bin/env python3
"""Collect Gmail message headers (sender, recipients, subject and date) through the owner's gws CLI; never bodies.

Each message lists its thread in `attributes.parent_refs` and its label ids in `attributes.label_refs`, which the
standard `part-of` and `label` relations of a new brain map. A brain whose bf.yaml lacks those fields declares them
under `fields:` first:

  part-of:
    description: Containing thread, folder, list, epic, project or other parent item.
    type: identity
    cardinality: many
    relation: true
    path: /attributes/parent_refs
  label:
    description: Label the provider attaches to the item.
    type: identity
    cardinality: many
    relation: true
    path: /attributes/label_refs
"""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from urllib.parse import quote

import _sensor

MAX_MESSAGES = 1000
MAX_PAGES = 10
MAX_BYTES = 16 << 20
# One message's metadata: its headers are bounded by Gmail, far below this.
MAX_MESSAGE_BYTES = 1 << 20
HEADERS = ("From", "To", "Cc", "Subject", "Date", "Message-ID")
InvalidError = _sensor.InvalidError


def identifier(value: object) -> str:
    """A Gmail message or thread id: a short hexadecimal string."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{1,64}", value):
        raise InvalidError("gws returned an invalid message or thread id")
    return value


def gws(method: str, params: dict[str, object], limit: int) -> dict[str, object]:
    # https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages
    raw = _sensor.run(
        ["gws", "gmail", "users", "messages", method, "--params", json.dumps(params)], timeout=60, max_bytes=limit
    )
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise InvalidError("gws returned an unexpected Gmail response")
    return payload


def listed(start: datetime, end: datetime, query: str) -> list[str]:
    """Every message id received in [start, end), and possibly a few around it, which record() drops.

    Gmail's after: and before: take whole seconds, and after: excludes its own second: widen the search to whole
    seconds outward, so a narrow window or a message near a bound is never missed, then filter exactly.
    """
    search = f"after:{math.floor(start.timestamp()) - 1} before:{math.ceil(end.timestamp())}"
    search += f" {query}" if query else ""
    ids: list[str] = []
    token, seen = "", set()
    for _ in range(MAX_PAGES):
        params: dict[str, object] = {
            "userId": "me",
            "q": search,
            "maxResults": 500,
            "fields": "nextPageToken,messages(id)",
        }
        if token:
            params["pageToken"] = token
        page = gws("list", params, MAX_BYTES)
        messages = page.get("messages", [])
        if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
            raise InvalidError("gws returned an invalid message list")
        ids.extend(identifier(item.get("id")) for item in messages)
        if len(ids) > MAX_MESSAGES:
            raise InvalidError(f"the window has more than {MAX_MESSAGES} messages; narrow it or the query")
        token = page.get("nextPageToken", "")
        if not isinstance(token, str):
            raise InvalidError("gws returned an invalid continuation token")
        if not token:
            if len(set(ids)) != len(ids):
                raise InvalidError("gws listed a message twice")
            return ids
        if token in seen:
            raise InvalidError("gws repeated a continuation token")
        seen.add(token)
    raise InvalidError(f"the window needs more than {MAX_PAGES} pages; narrow it or the query")


def record(message_id: str, start: datetime, end: datetime) -> dict[str, object] | None:
    """One message's headers as a record; None when it was received outside the window the search widened."""
    params: dict[str, object] = {
        "userId": "me",
        "id": message_id,
        "format": "metadata",
        "metadataHeaders": list(HEADERS),
        "fields": "id,threadId,labelIds,internalDate,payload/headers",
    }
    message = gws("get", params, MAX_MESSAGE_BYTES)
    if identifier(message.get("id")) != message_id:
        raise InvalidError("gws returned another message than the one requested")
    thread = identifier(message.get("threadId"))
    labels = message.get("labelIds", [])
    payload = message.get("payload", {})
    headers = payload.get("headers", []) if isinstance(payload, dict) else None
    received = message.get("internalDate")
    if (
        not isinstance(labels, list)
        or any(not isinstance(label, str) for label in labels)
        or not isinstance(headers, list)
        or any(not isinstance(h, dict) or not isinstance(h.get("name"), str) for h in headers)
        or not isinstance(received, str)
        or not received.isdigit()
    ):
        raise InvalidError("gws returned invalid message metadata")
    # Integer milliseconds: a float division could round a message across a window bound.
    when = datetime.fromtimestamp(int(received) // 1000, UTC) + timedelta(milliseconds=int(received) % 1000)
    if not start <= when < end:
        return None
    # Header names are case-insensitive; the first occurrence of each counts.
    values: dict[str, str] = {}
    for header in headers:
        values.setdefault(header["name"].lower(), str(header.get("value", "")))
    people = {
        key: sorted({address.strip().lower() for _, address in getaddresses([values.get(key, "")]) if "@" in address})
        for key in ("from", "to", "cc")
    }
    everyone = sorted({address for addresses in people.values() for address in addresses})
    subject = _sensor.line(values.get("subject", ""), "")
    lines = [f"Subject: {subject or '(no subject)'}"]
    lines += [f"{key.capitalize()}: {', '.join(people[key])}" for key in ("from", "to", "cc") if people[key]]
    if labels:
        lines.append("Labels: " + ", ".join(sorted(labels)))
    # RFC 2392 names a message by its Message-ID across mail tools: other records can link the same message.
    message_header = values.get("message-id", "").strip().removeprefix("<").removesuffix(">")
    alias = _sensor.reference("mid:" + quote(message_header, safe="@!$&'*+.^_`{|}~-")) if message_header else ""
    thread_ref = f"gmail-thread:{thread}"
    # Label ids, such as INBOX or Label_12, are opaque and case-sensitive: keep their case, encode anything else.
    label_refs = sorted(
        {ref for label in labels if label and (ref := _sensor.reference("gmail-label:" + quote(label, safe="")))}
    )
    return _sensor.record(
        message_id,
        subject or "(no subject)",
        time=when,
        text="\n".join(lines),
        url=f"https://mail.google.com/mail/#all/{thread}",
        links=[thread_ref, *map(_sensor.person, everyone)],
        aliases=[alias] if alias else (),
        attributes={
            "thread": thread,
            "labels": sorted(labels),
            "parent_refs": [thread_ref],
            "label_refs": label_refs,
            "sender_refs": list(map(_sensor.person, people["from"])),
            "recipient_refs": list(map(_sensor.person, people["to"])),
            "cc_refs": list(map(_sensor.person, people["cc"])),
        },
    )


def collect(start: str, end: str, query: str = "") -> list[dict[str, object]]:
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if begin.tzinfo is None or finish.tzinfo is None or begin >= finish:
        raise InvalidError("START and END need timezones, with START before END")
    if _sensor.CONTROL.search(query) or len(query) > 1024:
        raise InvalidError("the query must be one line of at most 1024 characters")
    found = (record(message, begin, finish) for message in listed(begin, finish, query))
    return [item for item in found if item is not None]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("--query", default="", help="A Gmail search, such as '-category:promotions'.")
    arguments = parser.parse_args()
    _sensor.cli(
        lambda: _sensor.emit(collect(arguments.start, arguments.end, arguments.query), max_bytes=MAX_BYTES),
        "Gmail collection failed",
        "check gws authentication and the requested window",
    )


if __name__ == "__main__":
    main()
