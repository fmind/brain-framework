"""Fictional offline records: one changes every 30 seconds, one stays unchanged, one fails."""

from __future__ import annotations

import argparse
import json
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", choices=("calendar", "git", "unavailable"))
source = parser.parse_args().source
if source == "unavailable":
    sys.exit(1)
revision = int(time.time()) // 30 if source == "calendar" else 1
sys.stdout.write(
    json.dumps(
        [
            {
                "id": source,
                "title": f"Fictional {source} record",
                "text": f"Demo revision {revision}",
                "time": "2026-09-01T09:00:00Z",
            },
        ]
    )
)
