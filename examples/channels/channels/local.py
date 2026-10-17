#!/usr/bin/env python3
"""Offline channel fixture: finite history, one pushed message, and recorded outgoing deliveries.

While CHANNEL_DEMO_OFFLINE is set, send fails before recording anything, standing in for a provider outage.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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
        start, end = argv[1:]
        sys.stdout.write(json.dumps({"messages": [MESSAGE] if start <= MESSAGE["time"] < end else []}) + "\n")
    elif operation == "send":
        message = json.load(sys.stdin)
        if os.environ.get("CHANNEL_DEMO_OFFLINE"):
            sys.exit("local channel is offline; nothing was sent")
        destination = message.get("conversation") or "proactive-thread"
        path = Path("logs/deliveries.jsonl")
        path.parent.mkdir(exist_ok=True)
        with path.open("a") as output:
            output.write(json.dumps(message) + "\n")
        sys.stdout.write(json.dumps({"id": "sent-" + message["id"], "conversation": destination}) + "\n")
    elif operation == "stream":
        if json.loads(sys.stdin.readline())["type"] != "resume":
            raise ValueError("expected resume")
        sys.stdout.write(json.dumps(MESSAGE) + "\n")
        sys.stdout.flush()
        ack = json.loads(sys.stdin.readline())
        if (ack.get("type"), ack.get("id")) != ("ack", MESSAGE["id"]):
            raise ValueError("expected durable acknowledgement")
        # A real adapter waits for another provider message here. EOF means the supervisor stopped.
        for _line in sys.stdin:
            pass
    else:
        raise ValueError("unknown operation")


if __name__ == "__main__":
    main(sys.argv[1:])
