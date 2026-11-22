#!/usr/bin/env python3
"""Starter retrieval evaluation script (run with `python3 evals/smoke.py` or `pytest evals/`)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

BRAIN = Path(__file__).resolve().parents[1]


def _bf(*args: str) -> dict[str, object]:
    proc = subprocess.run(["bf", *args, "--brain", str(BRAIN)], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def test_welcome_concept_is_searchable() -> None:
    res = _bf("query", "Conventions", "--kind", "note")
    items = res["items"]
    assert isinstance(items, list)
    assert any(isinstance(it, dict) and it.get("ref") == "concepts/welcome.md" for it in items)


def test_welcome_section_is_readable() -> None:
    res = _bf("read", "concepts/welcome.md#conventions")
    assert res["kind"] == "section"
    assert "attr_list" in str(res.get("text", ""))


if __name__ == "__main__":
    test_welcome_concept_is_searchable()
    test_welcome_section_is_readable()
