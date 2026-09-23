"""Hermetic state, a synthetic decision-recovery corpus, and fake provider executables for adapters."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from fkf.config import register
from fkf.models import Record, encode
from fkf.storage import Store

ROOT = Path(__file__).resolve().parents[1]
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

    def run(self, adapter: str, *arguments: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(home or self.state / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
        }
        return subprocess.run(  # noqa: S603 - the subject is the adapter's own process boundary
            [sys.executable, str(ROOT / "examples" / "sources" / adapter), *arguments],
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
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.delenv("FKF_BASE", raising=False)
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    previous_timezone = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        yield
    finally:
        if previous_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_timezone
        time.tzset()


PROJECT = b"""---
type: project
status: active
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


def records_file(store: Store, source: str, month: str, records: list[Record]) -> None:
    store.write(
        f"records/{source}/{month}.jsonl", b"".join(encode(r.model_dump(exclude_defaults=True)) for r in records)
    )


@pytest.fixture
def base(tmp_path: Path) -> Store:
    """A registered, collect-trusted base with one project note, one wiki concept and two records."""
    root = tmp_path / "base"
    root.mkdir()
    store = Store(root)
    store.write("fkf.yaml", b"version: 2\nname: fixture\nsources: {}\n")
    store.write("projects/offline.md", PROJECT)
    store.write(
        "wiki/evidence.md",
        b"---\ntype: concept\nstatus: stable\n---\n\n# Durable evidence\n\nOriginals outlive providers.\n",
    )
    records_file(
        store,
        "meetings",
        "2026-08",
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
    register(store, collect=True)
    return store
