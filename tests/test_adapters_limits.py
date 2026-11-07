"""Provider limits apply while a producer runs, including after it closes stdout."""

from __future__ import annotations

import ast
import os
import re
import runpy
from pathlib import Path

import pytest

from conftest import ROOT, Provider

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
    # write its pid in time; `exec` keeps that pid for the sleep the adapter must kill. Overflow ends at the byte
    # limit, so only the timeout case keeps the one-second budget it must wait out.
    producer = tmp_path / "producer.sh"
    producer.write_text(
        f'echo $$ > "{pid}"\n'
        + ("head -c 8192 /dev/zero\n" if mode == "overflow" else "exec 1>&-\n")
        + "exec sleep 60\n"
    )
    expected = ValueError if mode == "overflow" else TimeoutError
    with pytest.raises(expected):
        run(["/bin/sh", str(producer)], limit=1024, timeout=30 if mode == "overflow" else 1)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_providers_start_outside_the_brain(adapter: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # BF runs a sensor from the brain folder, and gws loads a `.env` file from its working directory or any parent:
    # one in the brain, or above it, could switch the account the sensor collects from.
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.chdir(brain)
    run = runpy.run_path(str(ROOT / "examples/sensors" / adapter))["run"]
    assert run(["/bin/pwd"], limit=1024, timeout=30) == b"/\n"


# A JSON reply nested deeper than Python's recursion limit makes json.loads raise RecursionError, not ValueError.
NESTED = "[" * 100_000
WINDOW = ("2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00")


@pytest.mark.parametrize(
    ("adapter", "provider_name", "stdout", "arguments"),
    [
        ("gmail-headers.py", "gws", NESTED, WINDOW),
        ("google-calendar.py", "gws", NESTED, ("primary", *WINDOW)),
        ("google-drive-folders.py", "gws", NESTED, ()),
        ("github-history.py", "gh", "HTTP/2.0 200 OK\r\n\r\n" + NESTED, ("owner/repo", "commits", *WINDOW)),
    ],
)
def test_deeply_nested_provider_json_fails_with_one_line(
    provider: Provider, adapter: str, provider_name: str, stdout: str, arguments: tuple[str, ...]
) -> None:
    provider.install(provider_name, [{"match": [], "stdout": stdout, "repeat": True}])
    result = provider.run(adapter, *arguments)
    assert result.returncode == 1
    assert not result.stdout
    assert len(result.stderr.splitlines()) == 1, result.stderr
    assert "Traceback" not in result.stderr
