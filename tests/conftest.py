"""Hermetic state, a synthetic decision-recovery corpus, and fake provider executables for adapters."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from fkf.models import Collection, Record, encode
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
            [sys.executable, str(ROOT / "adapters" / adapter), *arguments],
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
    monkeypatch.delenv("FKF_BASE", raising=False)
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)


@pytest.fixture
def base(tmp_path: Path) -> Store:
    root = tmp_path / "base"
    root.mkdir()
    store = Store(root)
    store.write("fkf.yaml", b"version: 1\nid: aabbccddeeff00112233445566778899\nname: fixture\nsources: {}\n")
    store.write(
        "wiki/project.md",
        b'---\ntitle: Offline retrieval decision\naliases: ["repo:example/project"]\n---\n# Retrieval\n\nKeep stored reads offline.\n\n## Reason\n\nProvider retention cannot guarantee historical evidence.\n',
    )
    capture = Collection(
        source="meetings",
        captured="2026-09-01T00:00:00Z",
        records=[
            Record(
                id="decision-1",
                title="Preserve durable evidence",
                text="The team chose offline retrieval.",
                time="2026-08-31T12:00:00Z",
                links=["wiki/project.md"],
                aliases=["meeting:decision-1"],
            ),
            Record(id="noise", title="Lunch plans", text="Meet for lunch on Tuesday."),
        ],
    )
    store.write("records/meetings/fixture.json", encode(capture.model_dump()))
    return store
