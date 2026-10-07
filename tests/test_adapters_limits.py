"""Provider limits apply while a producer runs, including after it closes stdout."""

from __future__ import annotations

import ast
import os
import re
import runpy
from pathlib import Path

import pytest

from conftest import ROOT

ADAPTERS = ["google-calendar.py", "google-drive-folders.py", "gmail-headers.py", "git-history.py", "github-history.py"]


def test_provider_run_copies_stay_identical() -> None:
    # Each sensor carries its own `run` so an owner can copy one file; only the overflow sentence differs.
    copies = set()
    for adapter in ADAPTERS:
        source = (ROOT / "examples/sensors" / adapter).read_text()
        [node] = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "run"]
        segment = ast.get_source_segment(source, node) or ""
        copies.add(re.sub(r'raise InvalidError\("[^"]*"\)', "raise InvalidError(MESSAGE)", segment))
    assert len(copies) == 1


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("mode", ["overflow", "timeout"])
def test_provider_output_is_bounded_and_child_is_reaped(adapter: str, mode: str, tmp_path: Path) -> None:
    run = runpy.run_path(str(ROOT / "examples/sensors" / adapter))["run"]
    pid = tmp_path / "pid"
    # A shell starts within the one-second timeout even on a loaded runner, where a Python producer once did not
    # write its pid in time; `exec` keeps that pid for the sleep the adapter must kill.
    producer = tmp_path / "producer.sh"
    producer.write_text(
        f'echo $$ > "{pid}"\n'
        + ("head -c 8192 /dev/zero\n" if mode == "overflow" else "exec 1>&-\n")
        + "exec sleep 60\n"
    )
    expected = ValueError if mode == "overflow" else TimeoutError
    with pytest.raises(expected):
        run(["/bin/sh", str(producer)], limit=1024, timeout=1)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)
