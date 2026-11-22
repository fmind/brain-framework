#!/usr/bin/env python3
"""Emit one fictional FinOps/prompt usage record inside BF_START..BF_END for `usages/usage-demo.jsonl`."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

start_raw = os.environ.get("BF_START") or (sys.argv[1] if len(sys.argv) > 1 else "")
end_raw = os.environ.get("BF_END") or (sys.argv[2] if len(sys.argv) > 2 else "")
start, end = datetime.fromisoformat(start_raw), datetime.fromisoformat(end_raw)
if start.tzinfo is None or end.tzinfo is None or start >= end:
    raise SystemExit("expected a timezone-aware BF_START before BF_END")

record = {
    "id": "demo-session-1",
    "title": "Example coding harness session",
    "type": "session",
    "description": "Synthetic FinOps telemetry record demonstrating usages/*.jsonl.",
    "created": start.isoformat(),
    "updated": start.isoformat(),
    "harness": "agy",
    "model": "demo-model",
    "session_id": "demo-session-1",
    "input_tokens": 1200,
    "output_tokens": 350,
    "cached_tokens": 800,
    "cost_usd": 0.012,
    "duration_ms": 4200,
    "prompts": 2,
}
sys.stdout.write(json.dumps([record], ensure_ascii=False) + "\n")
