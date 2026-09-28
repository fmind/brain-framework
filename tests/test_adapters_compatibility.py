"""Copied examples run with the host's python3, not the package interpreter, so they keep Python 3.11 syntax.

The 3.11 grammar check rejects later statements such as PEP 758 unparenthesized except clauses. It accepts PEP 701
f-strings that reuse their quotes; ruff's py311 per-file target in pyproject.toml reports those.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from conftest import ROOT

SCRIPTS = sorted((ROOT / "examples").rglob("*.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_example_scripts_parse_as_python_311(script: Path) -> None:
    source = script.read_text(encoding="utf-8")
    ast.parse(source, filename=str(script), feature_version=(3, 11))
    if source.startswith("#!/usr/bin/env python3\n"):
        assert os.access(script, os.X_OK), "a script run through its shebang needs its executable bit"


def test_every_example_script_is_checked() -> None:
    assert len(SCRIPTS) >= 10
