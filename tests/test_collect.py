"""Collection is explicit, trusted per machine, bounded, and never writes a partial result."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

from bf import records
from bf.collect import collect, due, log_path, run, state
from bf.config import register
from bf.models import Error, Sensor
from bf.storage import BusyError, Store, writer
from bf.update import update

START = "2026-09-01T00:00:00.000000Z"
END = "2026-09-02T00:00:00.000000Z"
NOW = datetime(2026, 9, 2, tzinfo=UTC)
CONFIG = b"""version: 3
name: fixture
sensors:
  sample:
    command: [echo, "{{start}}", "{{end}}", "{{brain}}", "{{home}}"]
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
def configured(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    return brain


def test_collect_dry_run_then_upsert(configured: Store) -> None:
    def fake(argv: list[str], source: Sensor, store: Store, log: Path) -> bytes:
        assert argv[1:] == [START, END, str(configured.root), str(Path.home())]
        assert source.refresh == 3600
        assert store.root == configured.root
        assert log == log_path(configured, "sample")
        return emit({"id": "x", "title": "Decision", "text": "Keep evidence", "time": "2026-09-01T10:00:00Z"})

    preview = collect(configured, "sample", start=START, end=END, runner=fake, dry_run=True)
    samples = cast("list[dict[str, object]]", preview["samples"])
    assert preview == {"records": 1, "samples": samples, "sensor": "sample"}
    assert samples[0]["id"] == "x"
    assert records.partitions(configured, "sample") == []
    assert state(configured) == {}
    result = collect(configured, "sample", start=START, end=END, runner=fake, clock=lambda: NOW)
    assert result == {"sensor": "sample", "records": 1, "added": 1, "updated": 0, "unchanged": 0, "removed": 0}
    assert records.partitions(configured, "sample") == ["memories/sample/2026-09.jsonl"]
    assert state(configured)["sample"] == {
        "run": NOW.isoformat(),
        "success": NOW.isoformat(),
        "start": START,
        "end": END,
        "error": "",
        **{key: value for key, value in result.items() if key != "sensor"},
    }


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
    shared.write("bf.yaml", CONFIG.replace(b"name: fixture", b"name: shared"))
    with pytest.raises(Error, match="register --collect"):
        collect(shared, "sample", start=START, end=END, runner=lambda *_: b"[]")
    register(shared, collect=False)
    assert update([shared])["brains"] == [{"brain": "shared", "skipped": "not trusted to collect on this machine"}]


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
    def fake(_argv: list[str], source: Sensor, *_: object) -> bytes:
        if source.mode == "snapshot":
            raise Error("provider unavailable")
        return emit({"id": "x", "title": "Collected zirconium", "time": "2026-09-01T10:00:00Z"})

    planned = cast("Any", update([configured], dry_run=True, now=NOW))
    assert [s["status"] for s in planned["brains"][0]["sensors"]] == ["due", "due"]
    assert "index" not in planned["brains"][0]
    report = cast("Any", update([configured], now=NOW, runner=fake))
    assert not report["ok"]
    sources = report["brains"][0]["sensors"]
    assert [s["status"] for s in sources] == ["failed", "collected"]
    assert "provider unavailable" in sources[0]["error"]
    assert report["brains"][0]["index"]["changed"] >= 1
    # The failed snapshot stays due; the collected window waits for its refresh interval.
    assert [name for name, *_ in due(configured, NOW + timedelta(minutes=1))] == ["folders"]


def test_real_process_boundary(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("LD_PRELOAD", "/bad.so")
    log = log_path(configured, "sample")
    source = Sensor(command=["sh"], timeout=1, max_bytes=128)
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
        run(["no-such-bf-command"], source, configured, log)
    with pytest.raises(Error, match="bare command"):
        run(["/bin/sh"], source, configured, log)


def test_brain_collectors_run_from_the_brain_root(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    log = log_path(configured, "sample")
    configured.write("sensors/where.sh", b"#!/bin/sh\npwd\n")
    (configured.root / "sensors/where.sh").chmod(0o700)
    assert (
        run(["sensors/where.sh"], Sensor(command=["sensors/where.sh"]), configured, log)
        == f"{configured.root}\n".encode()
    )
    configured.write("sensors/bad.sh", b"#!/nonexistent/interpreter\n")
    (configured.root / "sensors/bad.sh").chmod(0o700)
    with pytest.raises(Error, match="interpreter"):
        run(["sensors/bad.sh"], Sensor(command=["sensors/bad.sh"]), configured, log)
    with pytest.raises(Error):
        run(["sensors/../bf.yaml"], Sensor(command=["x"]), configured, log)


def test_placeholders_are_expanded_once(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    home = configured.root.parent / "literal-{{start}}"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    configured.write("bf.yaml", CONFIG.replace(b'"{{start}}", "{{end}}", "{{brain}}", ', b""))

    def fake(argv: list[str], *_: object) -> bytes:
        assert argv == ["echo", str(home)]
        return b"[]"

    assert collect(configured, "sample", start=START, end=END, dry_run=True, runner=fake)["records"] == 0


def test_missing_source_does_not_prevent_other_sources(configured: Store) -> None:
    configured.write(
        "bf.yaml",
        b"version: 3\nname: fixture\nsensors:\n"
        b"  a-missing:\n    command: [sensors/missing.py]\n    refresh: 3600\n"
        b'  b-good:\n    command: [echo, "[]"]\n    refresh: 3600\n',
    )
    report = cast("Any", update([configured], now=NOW))
    assert not report["ok"]
    assert [item["status"] for item in report["brains"][0]["sensors"]] == ["failed", "collected"]
    assert state(configured)["a-missing"]["error"]
    assert state(configured)["b-good"]["success"]


def test_success_and_failure_history_are_serialized(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Store.write
    writes = []

    def checked(self: Store, name: str, data: bytes) -> None:
        if name == "sensors.json":
            with pytest.raises(BusyError), writer(configured):
                pass
            writes.append(name)
        original(self, name, data)

    monkeypatch.setattr(Store, "write", checked)
    collect(configured, "sample", start=START, end=END, runner=lambda *_: b"[]")
    with pytest.raises(Error, match="JSON"):
        collect(configured, "folders", start=START, end=END, runner=lambda *_: b"broken")
    assert writes == ["sensors.json", "sensors.json"]
    assert set(state(configured)) == {"sample", "folders"}


def test_overlapping_source_run_cannot_overwrite_newer_snapshot(configured: Store) -> None:
    entered, release = Event(), Event()

    def slow(*_: object) -> bytes:
        entered.set()
        assert release.wait(5)
        return emit({"id": "one", "title": "First run"})

    with ThreadPoolExecutor(max_workers=1) as executor:
        active = executor.submit(collect, configured, "folders", start=START, end=END, runner=slow)
        try:
            assert entered.wait(5)
            with pytest.raises(BusyError):
                collect(configured, "folders", start=START, end=END, runner=lambda *_: b"[]")
        finally:
            release.set()
        assert active.result()["added"] == 1
    assert records.find(configured, "folders", "one") is not None


def test_interpreter_startup_injection_is_removed(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    for key in ("PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS", "RUBYOPT", "PERL5OPT", "DYLD_FALLBACK_LIBRARY_PATH"):
        monkeypatch.setenv(key, "startup-injection")
    source = Sensor(command=["sh"])
    output = run(
        ["sh", "-c", 'printf "%s" "$PYTHONPATH$PYTHONHOME$NODE_OPTIONS$RUBYOPT$PERL5OPT$DYLD_FALLBACK_LIBRARY_PATH"'],
        source,
        configured,
        log_path(configured, "sample"),
    )
    assert output == b""


def test_sigterm_cancels_collector_and_descendants(configured: Store, tmp_path: Path) -> None:
    marker = tmp_path / "children"
    configured.write("sensors/wait.sh", b'#!/bin/sh\nsleep 60 &\nprintf "%s %s\\n" "$$" "$!" > "$1"\nwait\n')
    (configured.root / "sensors/wait.sh").chmod(0o700)
    configured.write(
        "bf.yaml",
        json.dumps(
            {
                "version": 3,
                "name": "fixture",
                "sensors": {"sample": {"command": ["sensors/wait.sh", str(marker)]}},
            }
        ).encode(),
    )
    child = subprocess.Popen(  # noqa: S603 - exercise the CLI boundary with a temporary, synthetic collector
        [
            sys.executable,
            "-m",
            "bf",
            "collect",
            "sample",
            "--brain",
            str(configured.root),
            "--since",
            START,
            "--until",
            END,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    pids: list[int] = []
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.02)
        assert marker.exists(), "collector did not start"
        pids = [int(value) for value in marker.read_text().split()]
        child.send_signal(signal.SIGTERM)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 130, stderr.decode()
        assert stdout == b""
        for pid in pids:
            status = subprocess.run(  # noqa: S603 - inspect only process ids from the synthetic collector
                ["ps", "-o", "stat=", "-p", str(pid)],  # noqa: S607 - POSIX process-boundary check
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            ).stdout.strip()
            assert not status or status.startswith("Z"), f"collector process {pid} survived"
        assert records.partitions(configured, "sample") == []
    finally:
        if pids:
            with suppress(ProcessLookupError):
                os.killpg(pids[0], signal.SIGKILL)
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def test_invalid_history_and_other_brain_errors_do_not_stop_update(configured: Store, tmp_path: Path) -> None:
    from bf.storage import state_store

    state_store(configured.root).write("sensors.json", b'{"sample":{"success":"not-a-date"},"folders":[]}')
    assert state(configured) == {}
    state_store(configured.root).write("sensors.json", b"broken")
    assert state(configured) == {}
    other = tmp_path / "broken-brain"
    other.mkdir()
    broken = Store(other)
    broken.write("bf.yaml", b"invalid: true\n")
    configured.write(".bf", b"not a directory")
    report = cast("Any", update([broken, configured], now=NOW, runner=lambda *_: b"[]"))
    assert not report["ok"]
    assert report["brains"][0]["error"]
    assert report["brains"][1]["index"]["error"]
    assert [item["status"] for item in report["brains"][1]["sensors"]] == ["collected", "collected"]


def test_observation_and_coverage_describe_collected_evidence(configured: Store) -> None:
    incoming = emit({"id": "item", "title": "Evidence", "attributes": {"updated": "2026-08-30T00:00:00Z"}})
    collect(configured, "sample", start=START, end=END, runner=lambda *_: incoming, clock=lambda: NOW)
    found = records.find(configured, "sample", "item")
    assert found is not None
    assert found[1].updated == "2026-08-30T00:00:00.000000Z"
    assert found[1].observed == END
    collect(configured, "sample", start="2026-08-31T00:00:00Z", end=START, runner=lambda *_: b"[]", clock=lambda: NOW)
    assert state(configured)["sample"]["start"] == "2026-08-31T00:00:00.000000Z"
    assert state(configured)["sample"]["end"] == END
    collect(configured, "sample", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z", runner=lambda *_: b"[]")
    assert state(configured)["sample"]["start"] == "2026-01-01T00:00:00.000000Z"
    assert state(configured)["sample"]["end"] == "2026-01-02T00:00:00.000000Z"


def test_failed_run_history_reports_that_records_were_committed(
    configured: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Store.write

    def fail_history(self: Store, name: str, data: bytes) -> None:
        if name == "sensors.json":
            raise PermissionError("synthetic state failure")
        original(self, name, data)

    monkeypatch.setattr(Store, "write", fail_history)
    with pytest.raises(Error, match="records were committed"):
        collect(configured, "sample", start=START, end=END, runner=lambda *_: emit({"id": "one", "title": "Saved"}))
    assert records.find(configured, "sample", "one") is not None
