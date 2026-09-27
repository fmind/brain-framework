#!/usr/bin/env python3
"""Emit fictional adapter output for four tools; never contact a provider."""

import argparse
import json
import sys

RECORDS = {
    "workspace": {
        "id": "brief",
        "title": "New website product brief",
        "text": "Start with one product page: visitors need a clear explanation before signing up.",
        "url": "https://example.test/workspace/website-brief",
        "attributes": {"project": "project:new-website"},
    },
    "jira": {
        "id": "review",
        "title": "New website launch review",
        "text": "Accessibility review is still open. Launch is blocked until the keyboard navigation check passes.",
        "url": "https://example.test/jira/WEB-7",
        "attributes": {"workstream": "project:new-website"},
    },
    "github": {
        "id": "implementation",
        "title": "New website implementation",
        "text": "The product-page implementation was merged as revision demo-42. This does not close the launch review.",
        "url": "https://example.test/github/new-website/pull/42",
        "attributes": {"repository_project": "project:new-website"},
    },
    "gcloud": {
        "id": "deployment",
        "title": "New website preview deployment",
        "text": "Revision demo-42 is deployed to the preview service and its health check passes. "
        "This is a preview deployment, not a public launch or an accessibility result.",
        "url": "https://example.test/gcloud/new-website/preview",
        "attributes": {"service_project": "project:new-website"},
    },
}

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", choices=RECORDS)
args = parser.parse_args()
json.dump([RECORDS[args.source]], sys.stdout)
