#!/usr/bin/env python3
"""Print one tool's normalized fictional record from fixtures/TOOL.json; never contact a provider.

Usage: demo.py TOOL END. Like a real sensor, a fixture keeps its event `time` and dates an upstream change in
`attributes.updated`; an `updated` of "now" changed at collection, so it takes END, the requested window's end:
fixed fixtures cannot know when you run the demo.
"""

import argparse
import json
import sys
from pathlib import Path

SCHEMAS = {
    "workspace": ("Brief", "brief", "project", "state"),
    "jira": ("Issue", "issue", "workstream", "status"),
    "github": ("PullRequest", "pull-request", "repository_project", "state"),
    "gcloud": ("Deployment", "deployment", "service_project", "health"),
}

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("tool", choices=SCHEMAS)
parser.add_argument("end", help="the requested window's end, from {{end}}")
args = parser.parse_args()
raw = json.loads(Path("fixtures", f"{args.tool}.json").read_text(encoding="utf-8"))
attrs = dict(raw.get("attributes", {}))
if attrs.get("updated") == "now":
    attrs["updated"] = args.end
record_type, kind, project_key, status_key = SCHEMAS[args.tool]
project = attrs.pop(project_key)
status = attrs.pop(status_key)
record = {
    "id": raw["id"],
    "@type": record_type,
    "title": raw["title"],
    "text": raw["text"],
    "time": raw["time"],
    "url": raw["url"],
    "relations": {"project": [project]},
    "attributes": {"kind": kind, "status": status, **({"updated": attrs["updated"]} if "updated" in attrs else {})},
}
json.dump([record], sys.stdout)
