"""Hermetic test environment, fake CLI provider harness, and starter brain fixture."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from bf import engine, records
from bf.storage import Store

ROOT = Path(__file__).resolve().parents[1]
OBJ_SCHEMA = (
    json.dumps(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": True}
    ).encode()
    + b"\n"
)

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


def pytest_configure() -> None:
    """Run every test in UTC on any host."""
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    elif datetime.now().astimezone().utcoffset():
        pytest.exit("run the tests with TZ=UTC: this Python cannot change its timezone at runtime", returncode=4)


@dataclass
class Provider:
    """Fake provider executables on a private PATH plus an adapter runner."""

    bin: Path
    state: Path

    def install(self, name: str, responses: Sequence[Mapping[str, object]]) -> Path:
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
        self,
        adapter: str,
        *arguments: str,
        home: Path | None = None,
        folder: str = "sensors",
        stdin: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **(environment or {}),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(home or self.state / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
            "TZ": os.environ.get("TZ", "UTC"),
        }
        return subprocess.run(  # noqa: S603
            [sys.executable, str(ROOT / "examples" / folder / adapter), *arguments],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def records(self, adapter: str, *arguments: str, home: Path | None = None) -> list[dict[str, Any]]:
        result = self.run(adapter, *arguments, home=home)
        assert result.returncode == 0, result.stderr
        return records.parse_batch_envelope(result.stdout.encode())[0]


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
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)


@pytest.fixture
def brain(tmp_path: Path) -> Store:
    """An initialized brain with one concept, one project note, and two sensor records."""
    root = tmp_path / "brain"
    engine.init_brain(root, name="fixture")
    store = Store(root)
    store.write("schemas/meetings.json", OBJ_SCHEMA)
    records.ingest(
        store,
        "meetings",
        json.dumps(
            [
                {
                    "id": "decision-1",
                    "title": "Preserve durable evidence",
                    "text": "The team chose offline JSONL retrieval.",
                    "created": "2026-08-31T12:00:00Z",
                    "updated": "2026-08-31T12:00:00Z",
                    "links": ["bf:///projects/offline.md"],
                },
                {
                    "id": "lunch",
                    "title": "Lunch plans",
                    "text": "Meet for lunch on Tuesday.",
                    "created": "2026-08-30T12:00:00Z",
                    "updated": "2026-08-30T12:00:00Z",
                },
            ]
        ).encode(),
    )
    store.write(
        "projects/offline.md",
        b"---\ntype: project\nstatus: active\ncreated: 2026-09-01\nupdated: 2026-09-01\ntags: [retention]\n---\n\n"
        b"# Offline retrieval\n\n"
        b"Keep stored reads offline. Decided in [the meeting](meetings:decision-1){rel=cites}.\n"
        b"{: #summary confidence=high}\n\n"
        b"## Decision {#decision owner=alice}\n\n"
        b"Provider retention cannot guarantee historical evidence, so the team keeps durable JSONL records.\n\n"
        b"## Next actions\n\n"
        b"- [ ] Publish the retention guide.\n",
    )
    engine.build(store)
    return store
