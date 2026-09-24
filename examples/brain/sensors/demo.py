#!/usr/bin/env python3
"""Emit one fictional event inside the requested window without accessing a provider."""

import json
import sys
from datetime import datetime

start, end = (datetime.fromisoformat(value) for value in sys.argv[1:])
if start.tzinfo is None or end.tzinfo is None or start >= end:
    raise SystemExit("expected a timezone-aware start before end")
sys.stdout.write(
    json.dumps(
        [
            {
                "id": "retention",
                "title": "Retention decision",
                "text": "Keep original evidence because upstream content can disappear.",
                "time": start.isoformat(),
                "links": ["repo:example/project"],
            }
        ]
    )
    + "\n"
)
