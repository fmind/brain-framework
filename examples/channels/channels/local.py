#!/usr/bin/env python3
"""Offline channel fixture: finite history, one pushed message, and recorded outgoing deliveries.

While logs/offline exists inside the brain, send fails before recording anything, standing in for a provider outage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Event

MESSAGE = {
    "id": "hello-1",
    "sender": "fictional-owner",
    "conversation": "thread-1",
    "time": "2026-10-16T09:00:00Z",
    "text": "Can you help?",
}


def main(argv: list[str]) -> None:
    operation = argv[0]
    if operation == "read":
        start, end, *allow = argv[1:]
        in_window = start <= MESSAGE["time"] < end
        permitted = not allow or MESSAGE["sender"] in allow
        sys.stdout.write(json.dumps({"messages": [MESSAGE] if in_window and permitted else []}) + "\n")
    elif operation == "send":
        message = json.load(sys.stdin)
        if Path("logs/offline").exists():
            sys.exit("local channel is offline; nothing was sent")
        destination = message.get("conversation") or "proactive-thread"
        path = Path("logs/deliveries.jsonl")
        path.parent.mkdir(exist_ok=True)
        with path.open("a") as output:
            output.write(json.dumps(message) + "\n")
        sys.stdout.write(json.dumps({"id": "sent-" + message["id"], "conversation": destination}) + "\n")
    elif operation == "stream":
        sys.stdout.write(json.dumps(MESSAGE) + "\n")
        sys.stdout.flush()
        # A real adapter blocks reading provider events until the supervisor stops it.
        Event().wait()
    else:
        raise ValueError("unknown operation")


if __name__ == "__main__":
    main(sys.argv[1:])
