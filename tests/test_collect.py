"""Collection is explicit, trusted per machine, bounded, and never writes a partial result."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from fkf import records
from fkf.collect import collect, due, log_path, run, state
from fkf.config import register
from fkf.models import Error, Source
from fkf.storage import Store
from fkf.update import update

START = "2026-09-01T00:00:00.000000Z"
END = "2026-09-02T00:00:00.000000Z"
NOW = datetime(2026, 9, 2, tzinfo=UTC)
CONFIG = b"""version: 2
name: fixture
sources:
  sample:
    command: [echo, "{{start}}", "{{end}}", "{{base}}", "{{home}}"]
    refresh: 3600
  folders:
    command: [echo]
    mode: snapshot
    refresh: 86400
  manual:
    command: [echo]
  disabled:
    command: [echo]
    enabled: false
    refresh: 60
"""


def emit(*items: dict[str, object]) -> bytes:
    return json.dumps(list(items)).encode()


@pytest.fixture
def configured(base: Store) -> Store:
    base.write("fkf.yaml", CONFIG)
    return base


def test_collect_dry_run_then_upsert(configured: Store) -> None:
    def fake(argv: list[str], source: Source, store: Store, log: Path) -> bytes:
        assert argv[1:] == [START, END, str(configured.root), str(Path.home())]
        assert source.refresh == 3600
        assert store.root == configured.root
        assert log == log_path(configured, "sample")
        return emit({"id": "x", "title": "Decision", "text": "Keep evidence", "time": "2026-09-01T10:00:00Z"})

    preview = collect(configured, "sample", start=START, end=END, runner=fake, dry_run=True)
    samples = cast("list[dict[str, object]]", preview["samples"])
    assert preview == {"records": 1, "samples": samples, "source": "sample"}
    assert samples[0]["id"] == "x"
    assert records.partitions(configured, "sample") == []
    assert state(configured) == {}
    result = collect(configured, "sample", start=START, end=END, runner=fake, clock=lambda: NOW)
    assert result == {"source": "sample", "records": 1, "added": 1, "updated": 0, "unchanged": 0, "removed": 0}
    assert records.partitions(configured, "sample") == ["records/sample/2026-09.jsonl"]
    assert state(configured)["sample"] == {"run": NOW.isoformat(), "success": NOW.isoformat(), "end": END, "error": ""}


@pytest.mark.parametrize(
    ("output", "match"),
    [
        (b"not json", "invalid JSON"),
        (b'{"id":"x"}', "one JSON array"),
        (b'[{"id":"x"}]', "title"),
        (b'[{"id":"x","title":"A"},{"id":"x","title":"B"}]', "duplicate record ids"),
    ],
)
def test_invalid_output_writes_nothing_and_is_remembered(configured: Store, output: bytes, match: str) -> None:
    with pytest.raises(Error, match=match) as failure:
        collect(configured, "sample", start=START, end=END, runner=lambda *_: output)
    assert str(log_path(configured, "sample")) in str(failure.value)
    assert records.partitions(configured, "sample") == []
    assert match.split()[0] in str(state(configured)["sample"]["error"])


def test_collection_requires_a_known_enabled_trusted_source_and_a_window(configured: Store, tmp_path: Path) -> None:
    for name in ["absent", "disabled"]:
        with pytest.raises(Error, match="unknown or disabled"):
            collect(configured, name, start=START, end=END, runner=lambda *_: b"[]")
    for start, end in [(END, START), ("2026-09-01", END)]:
        with pytest.raises(Error, match="start"):
            collect(configured, "sample", start=start, end=end, runner=lambda *_: b"[]")
    clone = tmp_path / "clone"
    clone.mkdir()
    shared = Store(clone)
    shared.write("fkf.yaml", CONFIG.replace(b"name: fixture", b"name: shared"))
    with pytest.raises(Error, match="register --collect"):
        collect(shared, "sample", start=START, end=END, runner=lambda *_: b"[]")
    register(shared, collect=False)
    assert update([shared])["bases"] == [{"base": "shared", "skipped": "not trusted to collect on this machine"}]


def test_due_windows_resume_with_overlap_and_catch_up_at_most_30_days(configured: Store) -> None:
    assert due(configured, NOW) == [
        ("folders", "2026-09-01T00:00:00.000000Z", "2026-09-02T00:00:00.000000Z"),
        ("sample", "2026-09-01T00:00:00.000000Z", "2026-09-02T00:00:00.000000Z"),
    ]
    collect(configured, "sample", start=START, end=END, runner=lambda *_: b"[]", clock=lambda: NOW)
    collect(configured, "folders", start=START, end=END, runner=lambda *_: b"[]", clock=lambda: NOW)
    assert due(configured, NOW + timedelta(minutes=30)) == []
    later = NOW + timedelta(hours=2)
    assert due(configured, later) == [("sample", "2026-09-01T23:55:00.000000Z", "2026-09-02T02:00:00.000000Z")]
    far = NOW + timedelta(days=90)
    windows = {name: start for name, start, _ in due(configured, far)}
    assert windows == {"folders": "2026-11-30T00:00:00.000000Z", "sample": "2026-11-01T00:00:00.000000Z"}


def test_update_isolates_failures_and_refreshes_the_cache(configured: Store) -> None:
    def fake(_argv: list[str], source: Source, *_: object) -> bytes:
        if source.mode == "snapshot":
            raise Error("provider unavailable")
        return emit({"id": "x", "title": "Collected zirconium", "time": "2026-09-01T10:00:00Z"})

    planned = cast("Any", update([configured], dry_run=True, now=NOW))
    assert [s["status"] for s in planned["bases"][0]["sources"]] == ["due", "due"]
    assert "index" not in planned["bases"][0]
    report = cast("Any", update([configured], now=NOW, runner=fake))
    assert not report["ok"]
    sources = report["bases"][0]["sources"]
    assert [s["status"] for s in sources] == ["failed", "collected"]
    assert "provider unavailable" in sources[0]["error"]
    assert report["bases"][0]["index"]["changed"] >= 1
    # The failed snapshot stays due; the collected window waits for its refresh interval.
    assert [name for name, *_ in due(configured, NOW + timedelta(minutes=1))] == ["folders"]


def test_real_process_boundary(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("LD_PRELOAD", "/bad.so")
    log = log_path(configured, "sample")
    source = Source(command=["sh"], timeout=1, max_bytes=128)
    assert run(["sh", "-c", 'printf "[]"; printf "$LD_PRELOAD note" >&2'], source, configured, log) == b"[]"
    assert log.read_text() == " note"
    assert log.stat().st_mode & 0o077 == 0
    for script, match in [
        ("printf private-secret >&2; exit 7", "status 7"),
        ("sleep 5", "timed out"),
        ("while :; do printf abc; done", "max_bytes"),
    ]:
        with pytest.raises(Error, match=match) as failure:
            run(["sh", "-c", script], source, configured, log)
        assert "private-secret" not in str(failure.value)
    assert log.read_text() == ""
    run(["sh", "-c", "head -c 400000 /dev/zero >&2; printf '[]'"], source, configured, log)
    assert log.stat().st_size == 256 << 10
    with pytest.raises(Error, match="not on PATH"):
        run(["no-such-fkf-command"], source, configured, log)
    with pytest.raises(Error, match="bare command"):
        run(["/bin/sh"], source, configured, log)


def test_base_collectors_run_from_the_base_root(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    log = log_path(configured, "sample")
    configured.write("sources/where.sh", b"#!/bin/sh\npwd\n")
    (configured.root / "sources/where.sh").chmod(0o700)
    assert (
        run(["sources/where.sh"], Source(command=["sources/where.sh"]), configured, log)
        == f"{configured.root}\n".encode()
    )
    configured.write("sources/bad.sh", b"#!/nonexistent/interpreter\n")
    (configured.root / "sources/bad.sh").chmod(0o700)
    with pytest.raises(Error, match="interpreter"):
        run(["sources/bad.sh"], Source(command=["sources/bad.sh"]), configured, log)
    with pytest.raises(Error):
        run(["sources/../fkf.yaml"], Source(command=["x"]), configured, log)


def test_placeholders_are_expanded_once(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    home = configured.root.parent / "literal-{{start}}"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    configured.write("fkf.yaml", CONFIG.replace(b'"{{start}}", "{{end}}", "{{base}}", ', b""))

    def fake(argv: list[str], *_: object) -> bytes:
        assert argv == ["echo", str(home)]
        return b"[]"

    assert collect(configured, "sample", start=START, end=END, dry_run=True, runner=fake)["records"] == 0
