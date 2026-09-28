"""Hermetic state, a synthetic decision-recovery corpus, and fake provider executables for adapters."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from bf.config import register
from bf.models import Record, encode
from bf.records import path as record_path
from bf.storage import Store

ROOT = Path(__file__).resolve().parents[1]
# Host terminal settings that change Rich and Typer output: width, color and forced terminal behavior.
TERMINAL = ("COLUMNS", "LINES", "FORCE_COLOR", "NO_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE", "GITHUB_ACTIONS")


def pytest_configure() -> None:
    """Run every test in UTC on any host; tests of local dates choose their own zone in a subprocess."""
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    elif datetime.now().astimezone().utcoffset():
        # Some Intel macOS Python builds omit tzset, so TZ applies only from interpreter startup.
        pytest.exit("run the tests with TZ=UTC: this Python cannot change its timezone at runtime", returncode=4)


def plain(text: str) -> str:
    """Terminal text without ANSI styles, which Typer and Rich emit when a terminal or CI forces colors."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


_FAKE = '''#!{python}
"""Answer argv patterns from a JSON script once each unless repeated; record every call."""
import json, sys
argv = sys.argv[1:]
with open({calls!r}, "a") as stream:
    stream.write(json.dumps(argv) + "\\n")
responses = json.load(open({script!r}))
used = json.load(open({used!r})) if __import__("os").path.exists({used!r}) else []
for index, response in enumerate(responses):
    if index in used and not response.get("repeat"):
        continue
    if all(any(needle in item for item in argv) for needle in response["match"]):
        json.dump([*used, index], open({used!r}, "w"))
        out = response.get("stdout", "")
        sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
        sys.stderr.write(response.get("stderr", ""))
        sys.exit(response.get("code", 0))
sys.stderr.write("fake provider: no scripted response for " + json.dumps(argv) + "\\n")
sys.exit(97)
'''


@dataclass
class Provider:
    """Fake provider executables on a private PATH plus an adapter runner."""

    bin: Path
    state: Path

    def install(self, name: str, responses: Sequence[Mapping[str, object]]) -> Path:
        """Each response has `match` substrings (all must occur in argv) and optional stdout, stderr, code, repeat."""
        script, used, calls = (self.state / f"{name}.{suffix}" for suffix in ("responses.json", "used.json", "calls"))
        script.write_text(json.dumps(list(responses)))
        used.unlink(missing_ok=True)
        calls.write_text("")
        executable = self.bin / name
        executable.write_text(_FAKE.format(python=sys.executable, script=str(script), used=str(used), calls=str(calls)))
        executable.chmod(0o700)
        return calls

    def calls(self, name: str) -> list[list[str]]:
        return [json.loads(line) for line in (self.state / f"{name}.calls").read_text().splitlines()]

    def run(
        self, adapter: str, *arguments: str, home: Path | None = None, folder: str = "sensors"
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(home or self.state / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
            # Adapters that derive local dates follow the suite's pinned timezone unless a test chooses one.
            "TZ": os.environ.get("TZ", "UTC"),
        }
        return subprocess.run(  # noqa: S603 - the subject is the adapter's own process boundary
            [sys.executable, str(ROOT / "examples" / folder / adapter), *arguments],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def records(self, adapter: str, *arguments: str, home: Path | None = None) -> list[Record]:
        result = self.run(adapter, *arguments, home=home)
        assert result.returncode == 0, result.stderr
        return TypeAdapter(list[Record]).validate_python(json.loads(result.stdout))


@pytest.fixture
def provider(tmp_path: Path) -> Provider:
    state = tmp_path / "provider"
    (state / "home").mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    return Provider(tmp_path / "bin", state)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.delenv("BF_BRAIN", raising=False)
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith("GIT_") or key in TERMINAL:
            monkeypatch.delenv(key)


PROJECT = b"""---
type: project
status: draft
updated: 2026-09-01
tags: [retention]
aliases: ["repo:example/project"]
---

# Offline retrieval

Keep stored reads offline. Decided in [the meeting](meetings:decision-1).

## Decision

Provider retention cannot guarantee historical evidence, so the team keeps durable records.

## Next actions

- Publish the retention guide.
"""


def records_file(store: Store, source: str, records: list[Record]) -> None:
    """Write each record to its own SHA-256-named file, as collection does."""
    for record in records:
        store.write(record_path(source, record.id), encode(record.model_dump(exclude_defaults=True)))


@pytest.fixture
def brain(tmp_path: Path) -> Store:
    """A registered brain with one project note, one concept and two records."""
    root = tmp_path / "brain"
    root.mkdir()
    store = Store(root)
    store.write("bf.yaml", b"version: 6\nname: fixture\nsensors: {}\n")
    store.write("projects/offline.md", PROJECT)
    store.write(
        "concepts/evidence.md",
        b"---\ntype: concept\nstatus: stable\n---\n\n# Durable evidence\n\nOriginals outlive providers.\n",
    )
    records_file(
        store,
        "meetings",
        [
            Record(
                id="decision-1",
                title="Preserve durable evidence",
                text="The team chose offline retrieval.",
                time="2026-08-31T12:00:00Z",
                links=["repo:example/project"],
                aliases=["meeting:decision-1"],
            ),
            Record(id="lunch", title="Lunch plans", text="Meet for lunch on Tuesday.", time="2026-08-30T12:00:00Z"),
        ],
    )
    register(store)
    return store
