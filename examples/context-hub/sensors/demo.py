#!/usr/bin/env python3
"""Normalize one tool's fictional record from fixtures/TOOL.json without network calls."""

import argparse
import json
import sys
from pathlib import Path

SCHEMAS = {
    "workspace": ("brief", "project", "state"),
    "jira": ("issue", "workstream", "status"),
    "github": ("pull-request", "repository_project", "state"),
    "gcloud": ("deployment", "service_project", "health"),
}

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("tool", choices=SCHEMAS)
parser.add_argument("end", help="UTC collection timestamp (YYYY-MM-DDTHH:MM:SSZ)")
args = parser.parse_args()

raw = json.loads(Path("fixtures", f"{args.tool}.json").read_text(encoding="utf-8"))
attrs = dict(raw.get("attributes", {}))
record_type, project_key, status_key = SCHEMAS[args.tool]
raw_project = str(attrs.pop(project_key))
project_note = f"projects/{raw_project.removeprefix('project:')}.md"
status = str(attrs.pop(status_key))
updated = args.end if attrs.get("updated") == "now" else raw["time"]

record = {
    "id": raw["id"],
    "title": raw["title"],
    "type": record_type,
    "status": status,
    "project": project_note,
    "links": [project_note],
    "text": raw["text"],
    "time": raw["time"],
    "updated": updated,
    "url": raw["url"],
}
json.dump([record], sys.stdout)
sys.stdout.write("\n")
