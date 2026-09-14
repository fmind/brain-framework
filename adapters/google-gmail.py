#!/usr/bin/env python3
"""Collect Gmail message headers and snippets in a half-open window through the owner's gws CLI."""

import email.utils
import json
import subprocess
import sys
import tempfile
import unicodedata
from datetime import UTC, datetime
from email.header import decode_header, make_header
from urllib.parse import quote

PAGE_BYTES = 16 << 20
PAGE_LIMIT = 20
HEADERS = ("from", "to", "cc", "subject", "date", "list-id")


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("window bounds require a timezone")
    return parsed.astimezone(UTC)


def call(arguments: list[str]) -> dict[str, object]:
    with tempfile.TemporaryFile() as output:
        subprocess.run(["gws", *arguments], check=True, stdout=output, stderr=subprocess.DEVNULL, timeout=120)
        output.seek(0)
        payload = output.read(PAGE_BYTES + 1)
    if len(payload) > PAGE_BYTES:
        raise ValueError("provider page exceeds 16 MiB")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("unexpected provider response")
    return value


def tidy(value: object) -> str:
    """One visible line: decode encoded words, drop format/control characters, collapse whitespace."""
    if not isinstance(value, str):
        return ""
    try:
        decoded = str(make_header(decode_header(value)))
    except UnicodeError, ValueError, LookupError:
        decoded = value
    visible = "".join(
        " " if unicodedata.category(c) == "Cc" else "" if unicodedata.category(c) == "Cf" else c for c in decoded
    )
    return " ".join(visible.split())


def addresses(values: list[str]) -> list[str]:
    found = {address.lower() for _, address in email.utils.getaddresses(values) if address and "@" in address}
    return sorted("person:email/" + quote(address, safe="/:@+").replace("~", "%7E") for address in found)


def identifiers(query: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    tokens: set[str] = set()
    token = ""
    for _ in range(PAGE_LIMIT):
        # https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
        params: dict[str, object] = {"userId": "me", "q": query, "maxResults": 500}
        if token:
            params["pageToken"] = token
        page = call(["gmail", "users", "messages", "list", "--params", json.dumps(params)])
        messages = page.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("unexpected message listing")
        for item in messages:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("unexpected message identifier")
            if item["id"] in seen:
                raise ValueError("message listing repeated an identifier")
            seen.add(item["id"])
            found.append(item["id"])
        token = str(page.get("nextPageToken") or "")
        if not token:
            return found
        if token in tokens:
            raise ValueError("message pagination repeated a token")
        tokens.add(token)
    raise ValueError("message pagination exceeded its page limit")


def collect(start: str, end: str, extra: str) -> list[dict[str, object]]:
    begin, finish = instant(start), instant(end)
    if begin >= finish:
        raise ValueError("start must be earlier than end")
    # Gmail's after: operator is exclusive at second precision; widen by one second and filter exactly.
    query = f"after:{int(begin.timestamp()) - 1} before:{int(finish.timestamp())}"
    if extra:
        query += " " + extra
    start_ms, end_ms = int(begin.timestamp()) * 1000, int(finish.timestamp()) * 1000
    records: list[dict[str, object]] = []
    for identifier in identifiers(query):
        # https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get
        params = {"userId": "me", "id": identifier, "format": "metadata"}
        message = call(["gmail", "users", "messages", "get", "--params", json.dumps(params)])
        internal = int(str(message.get("internalDate", "")))
        if not start_ms <= internal < end_ms:
            continue
        payload = message.get("payload")
        raw = payload.get("headers") if isinstance(payload, dict) else None
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise ValueError("message headers must be an array")
        headers = {
            name: tidy(item.get("value")) for item in raw if (name := str(item.get("name", "")).lower()) in HEADERS
        }
        subject = headers.get("subject") or "(no subject)"
        raw_labels = message.get("labelIds")
        labels = [str(label) for label in raw_labels] if isinstance(raw_labels, list) else []
        lines = ["Subject: " + subject]
        lines.extend(
            f"{name.capitalize()}: {headers[name]}" for name in ("from", "to", "cc", "date") if headers.get(name)
        )
        if labels:
            lines.append("Labels: " + ", ".join(labels))
        snippet = tidy(message.get("snippet"))
        if snippet:
            lines.append("")
            lines.append(snippet)
        when = datetime.fromtimestamp(internal / 1000, UTC).isoformat()
        records.append(
            {
                "id": identifier,
                "title": subject,
                "time": when,
                "text": "\n".join(lines),
                "url": f"https://mail.google.com/mail/u/0/#all/{identifier}",
                "links": addresses([headers[name] for name in ("from", "to", "cc") if headers.get(name)]),
                "aliases": [f"mail:{identifier}"],
                "parents": ["gmail-label:" + quote(label, safe="") for label in labels],
                "attributes": {
                    "threadId": message.get("threadId", ""),
                    "labelIds": labels,
                    "headers": headers,
                    "sizeEstimate": message.get("sizeEstimate", 0),
                },
            }
        )
    return records


if __name__ == "__main__":
    try:
        value = collect(sys.argv[1], sys.argv[2], " ".join(sys.argv[3:]))
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > PAGE_BYTES:
            raise ValueError("normalized mail exceeds 16 MiB")
        print(payload)
    except OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.SubprocessError, IndexError:
        print("Gmail collection failed; check gws authentication and the requested window.", file=sys.stderr)
        sys.exit(1)
