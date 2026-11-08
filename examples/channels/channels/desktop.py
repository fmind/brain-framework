#!/usr/bin/env python3
"""Desktop channel: show each outgoing message as a desktop notification; send-only, no network or model.

Usage: desktop.py send [--title TITLE] [--notifier notify-send|osascript]

BF pipes one outgoing message to `send`. The script shows its text with notify-send on Linux or osascript on macOS,
passing the text as an argument, never as script code, and prints the receipt BF expects. A notification is a
pointer, not the answer: text beyond 1,000 characters ends with an ellipsis, and the assistant's record keeps it all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import unicodedata
from typing import NoReturn

# Outgoing text holds up to 4 MiB; its JSON escapes can take six bytes per character.
INPUT_BYTES = 32 << 20
BODY_CHARS = 1000
TITLE_CHARS = 100
TIMEOUT = 10
# The text travels only as argv: `on run argv` hands it to the script as data, never as code.
APPLESCRIPT = ("on run argv", "display notification (item 1 of argv) with title (item 2 of argv)", "end run")


def fail(message: str, code: int = 1) -> NoReturn:
    """Stop with one diagnostic that names the failure and never quotes the message."""
    sys.stderr.write(f"desktop: {message}\n")
    raise SystemExit(code)


def shown(text: str, limit: int) -> str:
    """Text a notification can show: bounded, hidden characters made visible, never read as an option."""
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    text = "".join(
        char.encode("unicode_escape").decode()
        if char != "\n" and unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
        else char
        for char in text
    )
    # osascript stops reading options at its first other argument; a leading dash would make the text one.
    return f" {text}" if text.startswith("-") else text


def outgoing() -> dict:
    """The message BF pipes on stdin: a nonempty `id` and `text`, and an optional `conversation`."""
    data = sys.stdin.buffer.read(INPUT_BYTES + 1)
    try:
        message = json.loads(data) if len(data) <= INPUT_BYTES else None
    except (ValueError, RecursionError):
        message = None
    if (
        not isinstance(message, dict)
        or not all(isinstance(message.get(key), str) and message[key] for key in ("id", "text"))
        or not isinstance(message.get("conversation", ""), str)
    ):
        fail("expected one outgoing message with a nonempty id and text on stdin", 2)
    return message


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("operation", choices=["send"], help="desktop notifications are send-only")
    parser.add_argument("--title", default="Brain", help="the notification's title")
    parser.add_argument(
        "--notifier",
        choices=["notify-send", "osascript"],
        default="osascript" if sys.platform == "darwin" else "notify-send",
        help="osascript on macOS, notify-send elsewhere",
    )
    args = parser.parse_args(argv)
    message = outgoing()
    executable = shutil.which(args.notifier)
    if executable is None:
        fail(f"{args.notifier} is not on PATH; install it or pass --notifier; nothing was shown")
    title = shown(args.title, TITLE_CHARS)
    body = shown(message["text"], BODY_CHARS)
    if args.notifier == "notify-send":
        # Notification servers read the body as markup: escape it so no text becomes a link or tag.
        body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        command = [executable, "--", title, body]
    else:
        command = [executable, *(part for line in APPLESCRIPT for part in ("-e", line)), body, title]
    try:
        result = subprocess.run(  # noqa: S603 - argv only: no shell or script code ever holds the text
            command, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        fail(f"{args.notifier} did not finish within {TIMEOUT} seconds or could not start; nothing was confirmed")
    if result.returncode:
        fail(f"{args.notifier} exited with status {result.returncode}; nothing was confirmed")
    # The notification has no provider id: name it by its message and the moment it was shown.
    digest = hashlib.sha256(f"{message['id']}\n{time.time_ns()}\n{message['text']}".encode()).hexdigest()
    receipt = {"id": f"desktop-{digest[:32]}", "conversation": message.get("conversation") or "desktop"}
    sys.stdout.write(json.dumps(receipt) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
