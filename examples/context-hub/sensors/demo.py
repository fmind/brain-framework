#!/usr/bin/env python3
"""Print one tool's fictional record from fixtures/TOOL.json; never contact a provider.

Usage: demo.py TOOL END. A fixture whose time is "now" happened at collection, so it takes END, the requested
window's end: fixed fixtures cannot know when you run the demo.
"""

import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("tool", choices=["workspace", "jira", "github", "gcloud"])
parser.add_argument("end", help="the requested window's end, from {{end}}")
args = parser.parse_args()
record = json.loads(Path("fixtures", f"{args.tool}.json").read_text(encoding="utf-8"))
if record.get("time") == "now":
    record["time"] = args.end
json.dump([record], sys.stdout)
