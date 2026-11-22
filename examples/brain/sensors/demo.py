#!/usr/bin/env python3
"""Emit one fictional event inside the BF_START..BF_END window without accessing a provider."""

import json
import os
import sys
from datetime import datetime

start_raw = os.environ.get("BF_START") or (sys.argv[1] if len(sys.argv) > 1 else "")
end_raw = os.environ.get("BF_END") or (sys.argv[2] if len(sys.argv) > 2 else "")
start, end = datetime.fromisoformat(start_raw), datetime.fromisoformat(end_raw)
if start.tzinfo is None or end.tzinfo is None or start >= end:
    raise SystemExit("expected a timezone-aware BF_START before BF_END")
sys.stdout.write(
    json.dumps(
        [
            {
                "id": "retention",
                "title": "Retention decision",
                "type": "decision",
                "text": "Keep original evidence because upstream content can disappear.",
                "created": start.isoformat(),
                "updated": start.isoformat(),
                "links": ["bf:///projects/example.md"],
            }
        ]
    )
    + "\n"
)
