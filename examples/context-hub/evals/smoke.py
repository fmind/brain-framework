#!/usr/bin/env python3
"""Retrieval evaluation script for the four-tool context-hub walkthrough."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

BRAIN = Path(__file__).resolve().parents[1]


def _bf(*args: str) -> dict[str, object]:
    proc = subprocess.run(["bf", *args, "--brain", str(BRAIN)], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def test_project_and_workspace_brief() -> None:
    res = _bf("query", "product")
    items = res["items"]
    assert isinstance(items, list)
    refs = {it.get("ref") for it in items if isinstance(it, dict)}
    assert {"projects/new-website.md", "workspace:brief"} <= refs


def test_jira_record_readable() -> None:
    res = _bf("read", "jira:review")
    assert res["kind"] == "memory"
    assert res["ref"] == "jira:review"


if __name__ == "__main__":
    test_project_and_workspace_brief()
    test_jira_record_readable()
