"""Provider limits apply while a producer runs, including after it closes stdout."""

from __future__ import annotations

import ast
import os
import runpy
from pathlib import Path

import pytest

from conftest import ROOT, Provider

SENSORS = [
    "git-history.py",
    "github-history.py",
    "github-issues.py",
    "gmail-headers.py",
    "google-calendar.py",
    "google-drive-folders.py",
    "highlights.py",
    "local-documents.py",
]


def test_example_sensors_import_the_kit_without_local_run_copies() -> None:
    for sensor in SENSORS:
        tree = ast.parse((ROOT / "examples/sensors" / sensor).read_text())
        imports = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
        assert "_sensor" in imports, sensor
        assert not [node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"], sensor


@pytest.mark.parametrize("mode", ["overflow", "timeout"])
def test_provider_output_is_bounded_and_child_is_reaped(mode: str, tmp_path: Path) -> None:
    kit = runpy.run_path(str(ROOT / "examples/sensors/_sensor.py"))
    run = kit["run"]
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
    expected = kit["InvalidError"] if mode == "overflow" else kit["ProviderError"]
    with pytest.raises(expected):
        run(["/bin/sh", str(producer)], max_bytes=1024, timeout=30 if mode == "overflow" else 1)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


def test_providers_start_outside_the_brain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # BF runs a sensor from the brain folder, and gws loads a `.env` file from its working directory or any parent:
    # one in the brain, or above it, could switch the account the sensor collects from.
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.chdir(brain)
    run = runpy.run_path(str(ROOT / "examples/sensors/_sensor.py"))["run"]
    assert run(["/bin/pwd"], max_bytes=1024, timeout=30) == b"/\n"


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
