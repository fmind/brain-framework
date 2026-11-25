#!/usr/bin/env python3
"""Retrieval evaluation script for the example brain (`python3 evals/smoke.py` or `pytest evals/`)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

BRAIN = Path(__file__).resolve().parents[1]


def _bf(*args: str) -> dict[str, object]:
    proc = subprocess.run(["bf", *args, "--brain", str(BRAIN)], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def test_retention_record_is_searchable() -> None:
    res = _bf("query", "upstream content can disappear", "--scope", "demo")
    items = res["items"]
    assert isinstance(items, list)
    assert any(isinstance(it, dict) and it.get("ref") == "demo:retention" for it in items)


def test_example_project_decision_section() -> None:
    res = _bf("read", "projects/example.md#decision")
    assert res["kind"] == "section"
    assert "demo:retention" in str(res.get("text", ""))


def test_cites_links_on_compiled_note() -> None:
    res = _bf("read", "projects/example.md")
    rec = res["record"]
    assert isinstance(rec, dict)
    links = rec.get("links")
    assert isinstance(links, list)
    assert any(
        isinstance(lk, dict)
        and lk.get("to") == "demo:retention"
        and isinstance(lk.get("meta"), dict)
        and lk["meta"].get("rel") == "cites"
        for lk in links
    )


if __name__ == "__main__":
    test_retention_record_is_searchable()
    test_example_project_decision_section()
    test_cites_links_on_compiled_note()
