"""Copied examples run with the host's python3, not the package interpreter: Python 3.11 and its standard library.

The 3.11 grammar check rejects later statements such as PEP 758 unparenthesized except clauses. It accepts PEP 701
f-strings that reuse their quotes; ruff's py311 per-file target in pyproject.toml reports those. Neither sees a newer
library call, such as itertools.batched in Python 3.12, so ty checks the scripts against Python 3.11 too.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

SCRIPTS = sorted((ROOT / "examples").rglob("*.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_example_scripts_are_standalone_python311_scripts(script: Path) -> None:
    source = script.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(script), feature_version=(3, 11))
    if source.startswith("#!/usr/bin/env python3\n"):
        assert os.access(script, os.X_OK), "a script run through its shebang needs its executable bit"
    # The package's environment has PyYAML and pydantic; the python3 a user copies the script to has neither.
    modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    # A relative import keeps its dots, so it names no standard-library module: a copied script has no siblings.
    modules |= {"." * node.level + (node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert {module.partition(".")[0] for module in modules} <= sys.stdlib_module_names | {"__future__"}


def test_example_scripts_use_only_the_python311_standard_library() -> None:
    result = subprocess.run(  # noqa: S603 - the locked type checker on the copied examples only
        [Path(sys.executable).with_name("ty"), "check", "--python-version", "3.11", *SCRIPTS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_example_script_is_checked() -> None:
    assert len(SCRIPTS) >= 10
