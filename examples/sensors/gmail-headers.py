#!/usr/bin/env python3
"""Collect Gmail message headers (sender, recipients, subject and date) through the owner's gws CLI; never bodies."""

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import getaddresses
from urllib.parse import quote

MAX_MESSAGES = 1000
MAX_PAGES = 10
MAX_BYTES = 16 << 20
# One message's metadata: its headers are bounded by Gmail, far below this.
MAX_MESSAGE_BYTES = 1 << 20
# BF record bounds: an id fits a BF address once percent-encoded; links and URLs are bounded single lines.
MAX_ID, MAX_ENCODED, MAX_REF, MAX_TITLE = 4096, 7988, 8192, 4096
# A record holds at most 1,000 links.
MAX_LINKS = 1000
# C0 and C1 control characters, which BF rejects in ids, titles, URLs and identities.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
HEADERS = ("From", "To", "Cc", "Subject", "Date", "Message-ID")


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
                        raise InvalidError("a gws response exceeds its byte limit")
        if code := child.wait():
            raise subprocess.CalledProcessError(code, argv)
        return bytes(output)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


def line(value: object, fallback: str) -> str:
    """One bounded title line: control characters become spaces and whitespace collapses."""
    return " ".join(CONTROL.sub(" ", str(value)).split())[:MAX_TITLE].rstrip() or fallback


def reference(value: object) -> str:
    """A URL or identity BF accepts, or nothing: an over-long or control-character value is dropped."""
    if isinstance(value, str) and value.strip() and len(value) <= MAX_REF and not CONTROL.search(value):
        return value
    return ""


def person(address: str) -> str:
    return reference("person:email/" + quote(address, safe="/:@+").replace("~", "%7E"))


def identifier(value: object) -> str:
    """A Gmail message or thread id: a short hexadecimal string."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{1,64}", value):
        raise InvalidError("gws returned an invalid message or thread id")
    return value


def gws(method: str, params: dict[str, object], limit: int) -> dict:
    # https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages
    payload = json.loads(run(["gws", "gmail", "users", "messages", method, "--params", json.dumps(params)], limit, 60))
    if not isinstance(payload, dict):
        raise InvalidError("gws returned an unexpected Gmail response")
    return payload


def listed(start: datetime, end: datetime, query: str) -> list[str]:
    """Every message id in [start, end): Gmail's after/before take seconds since the epoch."""
    search = f"after:{int(start.timestamp())} before:{int(end.timestamp())}" + (f" {query}" if query else "")
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
    """One message's headers as a record; None when its date falls outside the window, as `after:` rounds."""
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
    when = datetime.fromtimestamp(int(received) / 1000, UTC)
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
    subject = line(values.get("subject", ""), "")
    lines = [f"Subject: {subject or '(no subject)'}"]
    lines += [f"{key.capitalize()}: {', '.join(people[key])}" for key in ("from", "to", "cc") if people[key]]
    if labels:
        lines.append("Labels: " + ", ".join(sorted(labels)))
    # RFC 2392 names a message by its Message-ID across mail tools: other records can link the same message.
    message_header = values.get("message-id", "").strip().removeprefix("<").removesuffix(">")
    alias = reference("mid:" + quote(message_header, safe="@!$&'*+.^_`{|}~-")) if message_header else ""
    linked = [ref for ref in [*map(person, everyone), reference(f"gmail-thread:{thread}")] if ref]
    return {
        "id": message_id,
        "title": subject or "(no subject)",
        "time": when.isoformat(),
        "text": "\n".join(lines),
        "url": f"https://mail.google.com/mail/#all/{thread}",
        "links": sorted(set(linked))[:MAX_LINKS],
        "aliases": [alias] if alias else [],
        "attributes": {
            "thread": thread,
            "labels": sorted(labels),
            "from_refs": [ref for ref in map(person, people["from"]) if ref],
            "to_refs": [ref for ref in map(person, people["to"]) if ref][:MAX_LINKS],
            "cc_refs": [ref for ref in map(person, people["cc"]) if ref][:MAX_LINKS],
        },
    }


def collect(start: str, end: str, query: str = "") -> list[dict[str, object]]:
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if begin.tzinfo is None or finish.tzinfo is None or begin >= finish:
        raise InvalidError("START and END need timezones, with START before END")
    if CONTROL.search(query) or len(query) > 1024:
        raise InvalidError("the query must be one line of at most 1024 characters")
    found = (record(message, begin, finish) for message in listed(begin, finish, query))
    return [item for item in found if item is not None]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("--query", default="", help="A Gmail search, such as '-category:promotions'.")
    arguments = parser.parse_args()
    try:
        payload = json.dumps(collect(arguments.start, arguments.end, arguments.query), ensure_ascii=False)
        if len(payload.encode()) > MAX_BYTES:
            raise InvalidError("normalized messages exceed 16 MiB; narrow the window")
        print(payload)
    except InvalidError as error:
        print(f"Gmail collection failed: {error}.", file=sys.stderr)
        sys.exit(1)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("Gmail collection failed; check gws authentication and the requested window.", file=sys.stderr)
        sys.exit(1)
