#!/usr/bin/env python3
"""Collect Gmail message headers (sender, recipients, subject and date) through the owner's gws CLI; never bodies."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses

MAX_MESSAGES = 1000
MAX_PAGES = 10
MAX_BYTES = 16 << 20
# One message's metadata: its headers are bounded by Gmail, far below this.
MAX_MESSAGE_BYTES = 1 << 20
MAX_REF = 8192
HEADERS = ("From", "To", "Cc", "Subject", "Date")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
IDENTITY = re.compile(r"[a-z][a-z0-9+.-]*:\S+")


class InvalidError(ValueError):
    """Provider output violated the expected schema or bounds."""


class ProviderError(Exception):
    """Provider subprocess failed, timed out, or could not start."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def run(argv: Sequence[str], *, timeout: float, max_bytes: int) -> bytes:
    """Execute one provider CLI from / with bounded output and process-group reaping."""
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise ProviderError(f"cannot start {argv[0]}") from error
    stdout, stderr = proc.stdout, proc.stderr
    if stdout is None or stderr is None:
        if stdout is not None:
            stdout.close()
        if stderr is not None:
            stderr.close()
        raise RuntimeError("subprocess pipes missing")
    out_fd, err_fd = stdout.fileno(), stderr.fileno()
    chunks: list[bytes] = []
    overflow = False

    def drain_out() -> None:
        nonlocal overflow
        total = 0
        while data := os.read(out_fd, 65536):
            total += len(data)
            if total > max_bytes:
                overflow = True
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                return
            chunks.append(data)

    def drain_err() -> None:
        while os.read(err_fd, 65536):
            pass

    t_out = threading.Thread(target=drain_out, daemon=True)
    t_err = threading.Thread(target=drain_err, daemon=True)
    t_out.start()
    t_err.start()
    deadline = time.monotonic() + timeout
    try:
        while t_out.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise ProviderError(f"{argv[0]} exceeded its {int(timeout)}s timeout")
            t_out.join(timeout=min(remaining, 0.05))
        try:
            remaining = max(0.0, deadline - time.monotonic())
            code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise ProviderError(f"{argv[0]} exceeded its {int(timeout)}s timeout") from None
    finally:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        stdout.close()
        stderr.close()
    if overflow:
        raise InvalidError(f"{argv[0]} output exceeds {max_bytes} bytes")
    if code != 0:
        raise ProviderError(f"{argv[0]} exited with status {code}", status=code)
    return b"".join(chunks)


def line(value: str, fallback: str = "") -> str:
    cleaned = " ".join(CONTROL.sub(" ", value).split())
    return (cleaned[:4096] or fallback)[:4096] if (cleaned or fallback) else ""


def reference(value: str) -> str:
    if (
        not value
        or len(value) > MAX_REF
        or any(c.isspace() for c in value)
        or CONTROL.search(value)
        or not IDENTITY.fullmatch(value)
    ):
        return ""
    return value


def emit(records: list[dict[str, object]], *, max_bytes: int = MAX_BYTES) -> None:
    payload = (json.dumps(records, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > max_bytes:
        raise InvalidError(f"output exceeds {max_bytes} bytes")
    sys.stdout.buffer.write(payload)


def cli(action: Callable[[], None], summary: str, hint: str) -> None:
    try:
        action()
    except (InvalidError, ProviderError) as error:
        sys.stderr.write(f"{summary}: {error}.\n")
        sys.exit(1)
    except (OSError, UnicodeError, ValueError, LookupError, ArithmeticError, RecursionError):
        sys.stderr.write(f"{summary}; {hint}.\n")
        sys.exit(1)


def make_record(
    identifier: str,
    title: str,
    *,
    item_type: str,
    when: datetime,
    text: str,
    url: str,
    attributes: dict[str, object],
) -> dict[str, object]:
    out: dict[str, object] = {
        "id": identifier,
        "title": line(title, identifier),
        "type": "email" if item_type == "EmailMessage" else item_type.lower(),
        "text": text,
        "time": when.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    if valid_url := reference(url):
        out["url"] = valid_url
    if attributes:
        out["attributes"] = attributes
    return out


def identifier(value: object) -> str:
    """A Gmail message or thread id: a short hexadecimal string."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{1,64}", value):
        raise InvalidError("gws returned an invalid message or thread id")
    return value


def gws(method: str, params: dict[str, object], limit: int) -> dict[str, object]:
    # https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages
    raw = run(
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
    subject = line(values.get("subject", ""), "")
    lines = [f"Subject: {subject or '(no subject)'}"]
    lines += [f"{key.capitalize()}: {', '.join(people[key])}" for key in ("from", "to", "cc") if people[key]]
    if labels:
        lines.append("Labels: " + ", ".join(sorted(labels)))
    return make_record(
        message_id,
        subject or "(no subject)",
        item_type="EmailMessage",
        when=when,
        text="\n".join(lines),
        url=f"https://mail.google.com/mail/#all/{thread}",
        attributes={
            "thread": thread,
            "labels": sorted(labels),
        },
    )


def collect(start: str, end: str, query: str = "") -> list[dict[str, object]]:
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if begin.tzinfo is None or finish.tzinfo is None or begin >= finish:
        raise InvalidError("START and END need timezones, with START before END")
    if CONTROL.search(query) or len(query) > 1024:
        raise InvalidError("the query must be one line of at most 1024 characters")
    found = (record(message, begin, finish) for message in listed(begin, finish, query))
    return [item for item in found if item is not None]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("start", nargs="?", default="", help="window start timestamp (default: BF_START)")
    parser.add_argument("end", nargs="?", default="", help="window end timestamp (default: BF_END)")
    parser.add_argument("--query", default="", help="A Gmail search, such as '-category:promotions'.")
    arguments = parser.parse_args()
    start_raw = arguments.start or os.environ.get("BF_START", "")
    end_raw = arguments.end or os.environ.get("BF_END", "")
    cli(
        lambda: emit(collect(start_raw, end_raw, arguments.query), max_bytes=MAX_BYTES),
        "Gmail collection failed",
        "check gws authentication and the requested window",
    )


if __name__ == "__main__":
    main()
