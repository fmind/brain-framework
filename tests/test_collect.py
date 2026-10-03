"""Collection is explicit, bounded, and never writes a partial result."""

from __future__ import annotations

import json
import os
import re
import shlex
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
from typer.testing import CliRunner

from bf import records
from bf.cli import app
from bf.collect import Runner, collect, due, run
from bf.health import source_health
from bf.history import LOG_LIMIT, SENSORS, environment, log_path, remember, state
from bf.models import MAX_FIELDS, Error, Program, Record, Sensor, encode, timestamp
from bf.storage import BusyError, Store, state_store, writer
from bf.update import update
from bf.watch import snapshot

START = "2026-09-01T00:00:00.000000Z"
END = "2026-09-02T00:00:00.000000Z"
NOW = datetime(2026, 9, 2, tzinfo=UTC)
CONFIG = b"""version: 7
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
    output = emit({"id": "x", "title": "Decision", "text": "Keep evidence", "time": "2026-09-01T10:00:00Z"})

    def fake(argv: list[str], source: Program, store: Store, name: str, stdin: bytes) -> bytes:
        assert argv[1:] == [START, END, str(configured.root), str(Path.home())]
        assert source.refresh == 3600
        assert store.root == configured.root
        assert (name, stdin) == ("sample", b"")
        return output

    preview = collect(configured, "sample", start=START, end=END, runner=fake, dry_run=True)
    samples = cast("list[dict[str, object]]", preview["samples"])
    assert preview["records"] == 1
    assert preview["sensor"] == "sample"
    assert preview["requested_start"] == START
    assert preview["requested_end"] == END
    assert preview["output_bytes"] == len(output)
    assert isinstance(preview["elapsed_seconds"], float)
    assert preview["elapsed_seconds"] >= 0
    assert preview["reconcile"] is False
    assert samples[0]["id"] == "x"
    assert records.files(configured, "sample") == []
    assert state(configured) == {}
    result = collect(configured, "sample", start=START, end=END, runner=fake, clock=lambda: NOW)
    assert {key: result[key] for key in ("sensor", "records", "added", "updated", "unchanged", "removed")} == {
        "sensor": "sample",
        "records": 1,
        "added": 1,
        "updated": 0,
        "unchanged": 0,
        "removed": 0,
    }
    assert result["output_bytes"] == preview["output_bytes"]
    assert records.files(configured, "sample") == [records.path("sample", "x")]
    # Run history keeps canonical UTC instants, like every other reply timestamp.
    assert state(configured)["sample"] == {
        "run": END,
        "success": END,
        "start": START,
        "end": END,
        "error": "",
        "failures": 0,
        **{key: value for key, value in result.items() if key != "sensor"},
    }


@pytest.mark.parametrize("dry_run", [False, True], ids=["collect", "preview"])
@pytest.mark.parametrize(
    ("output", "match"),
    [
        (b"not json", "invalid JSON"),
        (b'{"id":"x"}', "one JSON array"),
        (b'[{"id":"x"}]', "title"),
        (b'[{"id":"x","title":"A"},{"id":"x","title":"B"}]', "duplicate record ids"),
        # The JSON escape of a surrogate-escaped, non-UTF-8 filename.
        (b'[{"id":"x","title":"A","links":["file:caf\\udce9"]}]', "links: .*valid Unicode"),
    ],
)
def test_invalid_output_writes_nothing_and_is_remembered(
    configured: Store, output: bytes, match: str, dry_run: bool
) -> None:
    with pytest.raises(Error, match=match) as failure:
        collect(configured, "sample", start=START, end=END, runner=lambda *_: output, dry_run=dry_run)
    assert log_path("sample") in str(failure.value)
    assert records.files(configured, "sample") == []
    if dry_run:
        # A failed preview saves no run history either, so it cannot make a source failed or due.
        assert state(configured) == {}
    else:
        assert match.split()[0] in str(state(configured)["sample"]["error"])


def test_provider_keys_never_reach_errors_or_history(configured: Store) -> None:
    for output, match in [
        (b'[{"id":"x","title":"A","From: ceo@corp.example Subject: layoffs":1}]', "one JSON array.*<key>"),
        # Collection maps fields itself: printed ones fail before validation, whatever their keys.
        (b'[{"id":"x","title":"A","fields":{"From: ceo@corp.example":1}}]', "record 0: .* not precomputed fields"),
    ]:
        with pytest.raises(Error, match=match) as failure:
            collect(configured, "sample", start=START, end=END, runner=lambda *_, data=output: data)
        assert "ceo@corp" not in str(failure.value)
        assert "ceo@corp" not in str(state(configured)["sample"]["error"])


def test_provider_relation_names_never_reach_errors_history_or_status(configured: Store) -> None:
    # A sensor controls a link's `?rel=` value: an undeclared one fails the run without being quoted.
    output = emit({"id": "a", "title": "t", "links": ["bf://fixture/x?rel=ignore-previous-instructions"]})
    with pytest.raises(Error, match="link relation is undeclared") as failure:
        collect(configured, "sample", start=START, end=END, runner=lambda *_: output)
    assert "ignore-previous" not in str(failure.value)
    assert "ignore-previous" not in json.dumps(state(configured))
    status = CliRunner().invoke(app, ["status", "--brain", str(configured.root)])
    assert "ignore-previous" not in status.stdout
    assert "link relation is undeclared" in status.stdout


def test_large_invalid_output_reports_only_its_first_invalid_record(configured: Store) -> None:
    # Before, each invalid item added a diagnostic: megabytes of error text, log heading and run history.
    invalid: list[dict[str, object]] = [{"id": f"item-{n}"} for n in range(50_000)]
    output = emit({"id": "valid", "title": "Valid"}, *invalid)
    message = "collector must print one JSON array of records: 1.title: Field required; nothing was written"
    with pytest.raises(Error, match=re.escape(message)):
        collect(configured, "sample", start=START, end=END, runner=lambda *_: output)
    assert state(configured)["sample"]["error"] == message
    assert records.files(configured, "sample") == []


def test_printed_fields_fail_by_their_rule_whatever_their_keys(configured: Store) -> None:
    # Validation would name the 1,000-name bound or each invalid key instead of the rule the sensor broke.
    printed: dict[str, object] = {"id": "x", "title": "t", "fields": {f"Key {n}": n for n in range(100_000)}}
    message = "record 1: sensors must supply mapped output, not precomputed fields; nothing was written"
    with pytest.raises(Error, match=re.escape(message)):
        collect(
            configured, "sample", start=START, end=END, runner=lambda *_: emit({"id": "ok", "title": "OK"}, printed)
        )
    assert records.files(configured, "sample") == []


def test_the_observed_stamp_cannot_push_a_record_over_the_size_bound(configured: Store) -> None:
    # Fields other than text exactly at the bound: valid as printed, but not once collection stamps `observed`.
    padding = MAX_FIELDS - len("xt") - len(encode({"blob": ""})) - len(encode({}))
    printed: dict[str, object] = {"id": "x", "title": "t", "attributes": {"blob": "a" * padding}}
    assert Record.model_validate(printed)
    with pytest.raises(Error, match=r"sample: record 0: fields other than text exceed 2 MiB.*; nothing was written"):
        collect(configured, "sample", start=START, end=END, runner=lambda *_: emit(printed), clock=lambda: NOW)
    assert records.files(configured, "sample") == []


def test_collection_requires_a_known_enabled_source_and_a_window(configured: Store, tmp_path: Path) -> None:
    # An unknown name suggests only sensors that can run; a disabled one says how to enable it.
    for name, message in [
        ("sampel", "unknown sensor sampel; check names in bf.yaml; did you mean sample?"),
        ("absent", "unknown sensor absent; check names in bf.yaml"),
        ("disabled", "sensor disabled is disabled in bf.yaml; set enabled: true to run it"),
    ]:
        with pytest.raises(Error, match=f"^{re.escape(message)}$"):
            collect(configured, name, start=START, end=END, runner=lambda *_: b"[]")
    for start, end in [(END, START), ("2026-09-01", END)]:
        with pytest.raises(Error, match="start"):
            collect(configured, "sample", start=start, end=end, runner=lambda *_: b"[]")
    clone = tmp_path / "clone"
    clone.mkdir()
    shared = Store(clone)
    shared.write("bf.yaml", CONFIG.replace(b"name: fixture", b"name: shared"))

    assert collect(shared, "sample", start=START, end=END, runner=lambda *_: b"[]")["records"] == 0
    assert update(shared, now=NOW, runner=lambda *_: b"[]")["ok"]


def test_due_windows_resume_with_overlap_and_catch_up_at_most_30_days(configured: Store) -> None:
    assert due(configured, NOW) == [
        ("folders", "2026-09-01T00:00:00.000000Z", "2026-09-02T00:00:00.000000Z", False),
        ("sample", "2026-09-01T00:00:00.000000Z", "2026-09-02T00:00:00.000000Z", False),
    ]
    collect(configured, "sample", start=START, end=END, runner=lambda *_: b"[]", clock=lambda: NOW)
    collect(configured, "folders", start=START, end=END, runner=lambda *_: b"[]", clock=lambda: NOW)
    assert due(configured, NOW + timedelta(minutes=30)) == []
    later = NOW + timedelta(hours=2)
    assert due(configured, later) == [("sample", "2026-09-01T23:55:00.000000Z", "2026-09-02T02:00:00.000000Z", False)]
    far = NOW + timedelta(days=90)
    windows = {name: start for name, start, *_ in due(configured, far)}
    assert windows == {"folders": "2026-11-30T00:00:00.000000Z", "sample": "2026-11-01T00:00:00.000000Z"}


def test_update_isolates_failures_and_refreshes_the_cache(configured: Store) -> None:
    def fake(_argv: list[str], source: Program, *_: object) -> bytes:
        if isinstance(source, Sensor) and source.mode == "snapshot":
            raise Error("provider unavailable")
        return emit({"id": "x", "title": "Collected zirconium", "time": "2026-09-01T10:00:00Z"})

    planned = cast("Any", update(configured, dry_run=True, now=NOW))
    assert [s["status"] for s in planned["sensors"]] == ["due", "due"]
    assert "index" not in planned
    report = cast("Any", update(configured, now=NOW, runner=fake))
    assert not report["ok"]
    sources = report["sensors"]
    assert [s["status"] for s in sources] == ["failed", "collected"]
    assert "provider unavailable" in sources[0]["error"]
    assert report["index"]["changed"] >= 1
    # The failed snapshot retries after a minute; the collected window waits for its refresh interval.
    assert due(configured, NOW + timedelta(seconds=59)) == []
    assert [name for name, *_ in due(configured, NOW + timedelta(minutes=1))] == ["folders"]


def test_failed_programs_back_off_exponentially_up_to_their_refresh(configured: Store) -> None:
    def fail(*_: object) -> bytes:
        raise Error("provider unavailable")

    def pending(when: datetime) -> bool:
        return "folders" in [name for name, *_ in due(configured, when)]

    at = NOW
    for failures in range(1, 13):
        with pytest.raises(Error, match="provider unavailable"):
            collect(configured, "folders", start=START, end=END, runner=fail, clock=lambda at=at: at)
        # One minute, doubling per consecutive failure, but never longer than the daily refresh.
        delay = min(timedelta(minutes=2 ** (failures - 1)), timedelta(days=1))
        assert not pending(at + delay - timedelta(seconds=1))
        assert pending(at + delay)
        (row,) = [row for row in snapshot(configured) if row.name == "folders"]
        assert (row.status, row.next_due) == ("failed", timestamp((at + delay).isoformat()))
        at += delay
    assert state(configured)["folders"]["failures"] == 12
    # A success resets the count: the next failure retries after one minute again.
    collect(configured, "folders", start=START, end=END, runner=catalog(1), clock=lambda: at)
    assert not pending(at + timedelta(hours=23))
    with pytest.raises(Error):
        collect(configured, "folders", start=START, end=END, runner=fail, clock=lambda: at)
    assert pending(at + timedelta(minutes=1))


def background(pidfile: Path, output: str = ">/dev/null 2>&1") -> str:
    """A shell prefix that starts a descendant outliving its leader and records the descendant's pid."""
    return f"sleep 30 {output} & echo $! > {shlex.quote(str(pidfile))}; "


def ended(pidfile: Path) -> bool:
    """Whether a recorded descendant is gone, a zombie included, within two seconds; one that survived is killed."""
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 2
    while status := subprocess.run(  # noqa: S603 - inspect only a synthetic program's descendant
        ["ps", "-o", "stat=", "-p", str(pid)],  # noqa: S607 - POSIX process-boundary check
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    ).stdout.strip():
        if status.startswith("Z"):
            break
        if time.monotonic() > deadline:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            return False
        time.sleep(0.05)
    return True


def test_real_process_boundary(configured: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("LD_PRELOAD", "/bad.so")
    log = configured.root / log_path("sample")
    finished = tmp_path / "finished"
    source = Sensor(command=["sh"], timeout=1, max_bytes=128)
    script = background(finished) + 'printf "[]"; printf "$LD_PRELOAD note" >&2'
    assert run(["sh", "-c", script], source, configured, "sample") == b"[]"
    # A finished program's whole process group ends with it, as after any failure below.
    assert ended(finished)
    # One entry per run: a heading with the local time, outcome and duration, then the program's stderr.
    assert re.fullmatch(r"== \S+ exited with status 0 after [0-9.]+s ==\n note\n", log.read_text())
    assert log.stat().st_mode & 0o077 == 0
    assert log.parent.stat().st_mode & 0o077 == 0
    for number, (script, match) in enumerate(
        [
            ("printf private-secret >&2; exit 7", "status 7"),
            ("sleep 5", "timed out"),
            # Output beyond max_bytes from a program that already exited: a group that survives fails, never hangs.
            ("head -c 4096 /dev/zero; exit 0", "max_bytes"),
            ("while :; do printf abc; done", "max_bytes"),
        ]
    ):
        pidfile = tmp_path / f"descendant-{number}"
        started = time.monotonic()
        with pytest.raises(Error, match=match) as failure:
            run(["sh", "-c", background(pidfile) + script], source, configured, "sample")
        assert ended(pidfile), script
        assert time.monotonic() - started < 3, script
        # Provider output reaches the private log, never the error.
        assert "private-secret" not in str(failure.value)
    entries = log.read_text().split("== ")[1:]
    assert [entry.split(" ", 1)[1].split("==")[0].rsplit(" after ", 1)[0] for entry in entries] == [
        "exited with status 0",
        "program exited with status 7",
        "program timed out after 1s",
        "program output exceeded max_bytes",
        "program output exceeded max_bytes",
    ]
    assert "private-secret" in entries[1]
    run(["sh", "-c", "head -c 400000 /dev/zero >&2; printf '[]'"], source, configured, "sample")
    # Each entry keeps the last 256 KiB of stderr; the log keeps whole newer entries within 1 MiB.
    assert log.read_bytes().endswith(b"==\n" + b"\0" * (256 << 10) + b"\n")
    for _ in range(4):
        run(["sh", "-c", "head -c 400000 /dev/zero >&2; printf '[]'"], source, configured, "sample")
    assert log.stat().st_size <= LOG_LIMIT
    assert log.read_bytes().startswith(b"== ")
    with pytest.raises(Error, match="not on PATH"):
        run(["no-such-bf-command"], source, configured, "sample")
    with pytest.raises(Error, match="bare command"):
        run(["/bin/sh"], source, configured, "sample")


def test_programs_select_the_executing_brain(
    configured: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An inherited selection would make a nested `bf read` without --brain read another brain.
    monkeypatch.setenv("BF_BRAIN", str(tmp_path / "other"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    source = Sensor(command=["sh"])
    output = run(["sh", "-c", 'printf "%s" "$BF_BRAIN"'], source, configured, "sample")
    assert output == str(configured.root).encode()


def test_background_descendants_cannot_hold_a_finished_program(
    configured: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    log = configured.root / log_path("sample")
    pidfile = tmp_path / "descendant"
    source = Sensor(command=["sh"], timeout=30)
    started = time.monotonic()
    # A helper that inherits only stderr, such as an SSH master, holds diagnostics, not the result.
    script = background(pidfile, ">/dev/null") + 'printf "[]"; printf note >&2'
    assert run(["sh", "-c", script], source, configured, "sample") == b"[]"
    assert log.read_text().endswith("==\nnote\n")
    assert ended(pidfile)
    # Output still open after the program exited may be incomplete: fail quickly with the cause.
    holder = tmp_path / "holder"
    with pytest.raises(Error, match="kept its stdout open"):
        run(["sh", "-c", background(holder, "") + 'printf "[]"'], source, configured, "sample")
    assert ended(holder)
    assert time.monotonic() - started < 10


@pytest.mark.parametrize(
    ("signum", "name"),
    [
        (signal.SIGKILL, "SIGKILL"),
        # Real-time signals between SIGRTMIN and SIGRTMAX have no name in Python.
        pytest.param(40, "signal 40", marks=pytest.mark.skipif(sys.platform != "linux", reason="Linux signal")),
    ],
)
def test_a_program_killed_by_a_signal_names_it(
    configured: Store, monkeypatch: pytest.MonkeyPatch, signum: int, name: str
) -> None:
    # Before, the out-of-memory killer's SIGKILL read as `exited with status -9`.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    with pytest.raises(Error, match=f"^program was killed by {name}$"):
        run(["sh", "-c", f"kill -{int(signum)} $$"], Sensor(command=["sh"]), configured, "sample")
    assert f"program was killed by {name} after" in (configured.root / log_path("sample")).read_text()


def test_brain_collectors_run_from_the_brain_root(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    log = "sample"
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
        b"version: 7\nname: fixture\nsensors:\n"
        b"  a-missing:\n    command: [sensors/missing.py]\n    refresh: 3600\n"
        b'  b-good:\n    command: [echo, "[]"]\n    refresh: 3600\n',
    )
    report = cast("Any", update(configured, now=NOW))
    assert not report["ok"]
    assert [item["status"] for item in report["sensors"]] == ["failed", "collected"]
    assert state(configured)["a-missing"]["error"]
    assert state(configured)["b-good"]["success"]


def test_success_and_failure_history_are_serialized(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Store.write
    writes = []

    def checked(self: Store, name: str, data: bytes, *, durable: bool = True) -> None:
        if name == "sensors.json":
            with pytest.raises(BusyError), writer(configured):
                pass
            writes.append(name)
        original(self, name, data, durable=durable)

    monkeypatch.setattr(Store, "write", checked)
    collect(configured, "sample", start=START, end=END, runner=lambda *_: b"[]")
    with pytest.raises(Error, match="JSON"):
        collect(configured, "folders", start=START, end=END, runner=lambda *_: b"broken")
    assert writes == ["sensors.json", "sensors.json"]
    assert set(state(configured)) == {"sample", "folders"}


def catalog(count: int, first: int = 0) -> Runner:
    return lambda *_: json.dumps(
        [{"id": f"item-{n}", "title": f"Item {n}"} for n in range(first, first + count)]
    ).encode()


def test_a_shrinking_snapshot_never_erases_most_of_a_catalog(configured: Store) -> None:
    # A first empty catalog is a valid answer.
    assert collect(configured, "folders", start=START, end=END, runner=catalog(0))["records"] == 0
    collect(configured, "folders", start=START, end=END, runner=catalog(30))
    # A wrong account, a lost folder or a truncated listing also looks like a smaller catalog.
    for smaller, removed in ((0, 30), (14, 16)):
        with pytest.raises(Error, match=f"would remove {removed} of 30 records.*--allow-removal"):
            collect(configured, "folders", start=START, end=END, runner=catalog(smaller))
        assert len(records.files(configured, "folders")) == 30
    entry = state(configured)["folders"]
    assert "would remove 16 of 30 records" in str(entry["error"])
    assert entry["failures"] == 2
    # Half of a catalog, or at most ten records of a small one, can change like an agenda.
    for count, first, removed in ((30, 15, 15), (16, 29, 14), (6, 39, 10)):
        assert collect(configured, "folders", start=START, end=END, runner=catalog(count, first))["removed"] == removed
    assert (state(configured)["folders"]["error"], state(configured)["folders"]["failures"]) == ("", 0)


def test_allow_removal_accepts_one_deliberate_shrink(configured: Store) -> None:
    configured.write(
        "bf.yaml", CONFIG.replace(b"command: [echo]\n    mode: snapshot", b'command: [echo, "[]"]\n    mode: snapshot')
    )
    collect(configured, "folders", start=START, end=END, runner=catalog(30))
    command = ["collect", "folders", "--brain", str(configured.root)]
    refused = CliRunner().invoke(app, command)
    assert refused.exit_code == 1
    assert "--allow-removal" in str(refused.exception)
    assert len(records.files(configured, "folders")) == 30
    accepted = CliRunner().invoke(app, [*command, "--allow-removal"])
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["removed"] == 30
    assert records.files(configured, "folders") == []
    assert not state(configured)["folders"]["error"]


def test_the_shrink_guard_counts_the_catalog_an_interrupted_commit_restores(configured: Store) -> None:
    collect(configured, "folders", start=START, end=END, runner=catalog(40))
    # A snapshot keeping 24 of 40 records is killed after its 16 removals, before its completion marker.
    script = """
import os, sys
from pathlib import Path
from bf import records
from bf.models import Record
from bf.storage import Store, writer
store = Store(Path(sys.argv[1]))
delete, removed = Store.delete, []
def interrupted(self, name, *, durable=True):
    delete(self, name, durable=durable)
    removed.append(name)
    if len(removed) == 16:
        os._exit(73)
Store.delete = interrupted
with writer(store):
    records.upsert(store, "folders", [Record(id=f"item-{n}", title=f"Item {n}") for n in range(24)], snapshot=True)
"""
    child = subprocess.run(  # noqa: S603 - terminate only a synthetic transaction in its temporary brain
        [sys.executable, "-c", script, str(configured.root)],
        env=os.environ.copy(),
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert child.returncode == 73, child.stderr.decode()
    assert len(records.files(configured, "folders")) == 24
    # Against the 24 files left, a truncated listing of 12 removes only half; the rollback restores all 40.
    with pytest.raises(Error, match="would remove 28 of 40 records"):
        collect(configured, "folders", start=START, end=END, runner=catalog(12))
    assert len(records.files(configured, "folders")) == 40
    assert not (configured.root / "memories/.pending").exists()


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


def test_environment_removes_documented_startup_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    # The documented contract, not the implementation's list: removing an entry must fail here.
    startup = (
        "BASH_ENV",
        "BASHOPTS",
        "BASH_FUNC_x%%",
        "DYLD_INSERT_LIBRARIES",
        "ENV",
        "GCONV_PATH",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "_JAVA_OPTIONS",
        "LD_PRELOAD",
        "LUA_INIT_5_4",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PERL5LIB",
        "PERL5OPT",
        "PERLLIB",
        "PS4",
        "PYTHONSTARTUP",
        "RUBYLIB",
        "RUBYOPT",
        "SHELLOPTS",
    )
    for key in startup:
        monkeypatch.setenv(key, "startup-injection")
    monkeypatch.setenv("EXAMPLE_PROVIDER_ACCOUNT", "ordinary-value")
    env = environment()
    assert not set(startup) & env.keys()
    assert env["EXAMPLE_PROVIDER_ACCOUNT"] == "ordinary-value"


def test_interpreter_startup_injection_is_removed(
    configured: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variables = (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PYTHONWARNINGS",
        "NODE_OPTIONS",
        "RUBYOPT",
        "PERL5OPT",
        "JAVA_TOOL_OPTIONS",
        "DYLD_FALLBACK_LIBRARY_PATH",
    )
    for key in variables:
        monkeypatch.setenv(key, "startup-injection")
    # Bash would run each of these before the program: a startup file, xtrace with a command
    # substitution in its prompt, and an imported function replacing a builtin.
    marker = tmp_path / "injected"
    startup = tmp_path / "startup.sh"
    startup.write_text(f"touch '{marker}'\n")
    monkeypatch.setenv("BASH_ENV", str(startup))
    monkeypatch.setenv("SHELLOPTS", "xtrace")
    monkeypatch.setenv("PS4", f"$(touch '{marker}')")
    monkeypatch.setenv("BASH_FUNC_printf%%", f"() {{ touch '{marker}'; }}")
    # A relative PATH entry would resolve inside the brain, where only sensors/ and routines/ may run.
    configured.write("bin/tool", b"#!/bin/sh\necho injected\n")
    (configured.root / "bin/tool").chmod(0o700)
    monkeypatch.setenv("PATH", "bin:/usr/bin:/bin")
    source = Sensor(command=["bash"])
    log = "sample"
    output = run(
        ["bash", "-c", 'printf "%s|%s" "$PATH" "' + "".join(f"${key}" for key in variables) + '"'],
        source,
        configured,
        log,
    )
    assert output == b"/usr/bin:/bin|"
    assert not marker.exists()
    with pytest.raises(Error, match="not on PATH"):
        run(["tool"], source, configured, log)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP], ids=["stop", "hangup"])
def test_cancellation_kills_collector_and_descendants(configured: Store, tmp_path: Path, signum: int) -> None:
    marker = tmp_path / "children"
    # The marker appears complete (renamed into place), so the test never reads a half-written list of ids.
    configured.write(
        "sensors/wait.sh", b'#!/bin/sh\nsleep 60 &\nprintf "%s %s\\n" "$$" "$!" > "$1.tmp"\nmv "$1.tmp" "$1"\nwait\n'
    )
    (configured.root / "sensors/wait.sh").chmod(0o700)
    configured.write(
        "bf.yaml",
        json.dumps(
            {
                "version": 7,
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
        assert len(pids) == 2, pids
        child.send_signal(signum)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 130, stderr.decode()
        # Cancellation exits 130 without a message.
        assert (stdout, stderr) == (b"", b"")
        for pid in pids:
            status = subprocess.run(  # noqa: S603 - inspect only process ids from the synthetic collector
                ["ps", "-o", "stat=", "-p", str(pid)],  # noqa: S607 - POSIX process-boundary check
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            ).stdout.strip()
            assert not status or status.startswith("Z"), f"collector process {pid} survived"
        assert records.files(configured, "sample") == []
    finally:
        if pids:
            with suppress(ProcessLookupError):
                os.killpg(pids[0], signal.SIGKILL)
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def test_invalid_history_and_cache_errors_do_not_stop_update(configured: Store, tmp_path: Path) -> None:
    state_store(configured.root).write(
        "sensors.json",
        b'{"sample":{"success":"2026-09-01T02:00:00+02:00"},"manual":{"success":"not-a-date"},'
        b'"folders":[],"Bad Name":{}}',
    )
    # Each invalid entry is dropped alone, and instants read back in canonical UTC.
    assert state(configured) == {"sample": {"success": START}}
    state_store(configured.root).write("sensors.json", b"[]")
    assert state(configured) == {}
    state_store(configured.root).write("sensors.json", b"broken")
    assert state(configured) == {}
    configured.write(".bf", b"not a directory")
    report = cast("Any", update(configured, now=NOW, runner=lambda *_: b"[]"))
    assert not report["ok"]
    assert report["index"]["error"]
    assert [item["status"] for item in report["sensors"]] == ["collected", "collected"]
    # Like collect, a brain whose configuration cannot load fails before running anything.
    other = tmp_path / "broken-brain"
    other.mkdir()
    broken = Store(other)
    broken.write("bf.yaml", b"invalid: true\n")
    with pytest.raises(Error):
        update(broken, now=NOW, runner=lambda *_: pytest.fail("no program runs"))


def test_observation_and_coverage_describe_collected_evidence(configured: Store) -> None:
    incoming = emit({"id": "item", "title": "Evidence", "attributes": {"updated": "2026-08-30T00:00:00Z"}})
    collect(configured, "sample", start=START, end=END, runner=lambda *_: incoming, clock=lambda: NOW)
    found = records.find(configured, "sample", "item")
    assert found is not None
    assert found[1].updated == "2026-08-30T00:00:00.000000Z"
    assert found[1].observed == END
    later = NOW + timedelta(hours=1)
    # A contiguous backfill extends coverage backwards without claiming a fresher success.
    collect(configured, "sample", start="2026-08-31T00:00:00Z", end=START, runner=lambda *_: b"[]", clock=lambda: later)
    entry = state(configured)["sample"]
    assert (entry["start"], entry["end"], entry["success"], entry["run"]) == (
        "2026-08-31T00:00:00.000000Z",
        END,
        END,
        timestamp(later.isoformat()),
    )
    # An older, disjoint backfill keeps the recorded coverage, resume point and freshness.
    collect(
        configured,
        "sample",
        start="2026-01-01T00:00:00Z",
        end="2026-01-02T00:00:00Z",
        runner=lambda *_: b"[]",
        clock=lambda: later,
    )
    entry = state(configured)["sample"]
    assert (entry["start"], entry["end"], entry["success"]) == ("2026-08-31T00:00:00.000000Z", END, END)
    assert due(configured, NOW + timedelta(days=1)) == [
        (
            "folders",
            timestamp((NOW + timedelta(days=1) - timedelta(days=1)).isoformat()),
            timestamp((NOW + timedelta(days=1)).isoformat()),
            False,
        ),
        ("sample", "2026-09-01T23:55:00.000000Z", timestamp((NOW + timedelta(days=1)).isoformat()), False),
    ]
    # A newer window leaving a gap, such as `bf collect sample --since 1d` after a pause, keeps the
    # resume point and freshness, so the next update still fills the gap.
    newer = datetime(2026, 9, 11, 1, tzinfo=UTC)
    collect(
        configured,
        "sample",
        start="2026-09-10T00:00:00Z",
        end="2026-09-11T00:00:00Z",
        runner=lambda *_: b"[]",
        clock=lambda: newer,
    )
    entry = state(configured)["sample"]
    assert (entry["start"], entry["end"], entry["success"]) == ("2026-08-31T00:00:00.000000Z", END, END)
    assert due(configured, newer)[-1] == (
        "sample",
        "2026-09-01T23:55:00.000000Z",
        timestamp(newer.isoformat()),
        False,
    )
    # A window ending after its run covers only up to the run, so later scheduled runs stay the latest.
    ahead = newer + timedelta(hours=1)
    collect(configured, "sample", start=END, end="2026-12-31T00:00:00Z", runner=lambda *_: b"[]", clock=lambda: ahead)
    assert state(configured)["sample"]["end"] == timestamp(ahead.isoformat())
    after = ahead + timedelta(hours=2)
    window = due(configured, after)[-1]
    collect(configured, "sample", start=window[1], end=window[2], runner=lambda *_: b"[]", clock=lambda: after)
    assert state(configured)["sample"]["success"] == timestamp(after.isoformat())
    # A window entirely after its run claims no coverage and no success.
    collect(
        configured,
        "sample",
        start="2027-01-01T00:00:00Z",
        end="2027-01-02T00:00:00Z",
        runner=lambda *_: b"[]",
        clock=lambda: after,
    )
    assert state(configured)["sample"]["success"] == timestamp(after.isoformat())
    assert state(configured)["sample"]["end"] == window[2]


def test_scheduled_catch_up_after_a_long_pause_is_the_latest_window(configured: Store) -> None:
    collect(configured, "sample", start=START, end=END, runner=lambda *_: b"[]", clock=lambda: NOW)
    far = NOW + timedelta(days=60)
    # A manual day inside the catch-up horizon leaves the older gap due.
    day = far - timedelta(days=1)
    collect(
        configured,
        "sample",
        start=timestamp(day.isoformat()),
        end=timestamp(far.isoformat()),
        clock=lambda: far,
        runner=lambda *_: b"[]",
    )
    assert state(configured)["sample"]["success"] == END
    ((_, start, end, _),) = [window for window in due(configured, far) if window[0] == "sample"]
    assert start == timestamp((far - timedelta(days=30)).isoformat())
    # The update catches up the last 30 days only, and that window becomes the latest.
    collect(configured, "sample", start=start, end=end, runner=lambda *_: b"[]", clock=lambda: far)
    entry = state(configured)["sample"]
    assert (entry["start"], entry["end"], entry["success"]) == (start, end, end)
    assert not [window for window in due(configured, far) if window[0] == "sample"]


@pytest.mark.parametrize("days", [3, 45])
def test_manual_window_sensor_coverage_follows_its_latest_run(configured: Store, days: int) -> None:
    # No update revisits a manual sensor's gap, so each default `bf collect manual` window is the latest.
    def default_window(now: datetime) -> None:
        since = timestamp((now - timedelta(days=1)).isoformat())
        collect(
            configured,
            "manual",
            start=since,
            end=timestamp(now.isoformat()),
            runner=lambda *_: b"[]",
            clock=lambda: now,
        )

    default_window(NOW)
    later = NOW + timedelta(days=days)
    default_window(later)
    since, until = timestamp((later - timedelta(days=1)).isoformat()), timestamp(later.isoformat())
    report = source_health(configured, now=later)["manual"]
    assert (report["last_collected"], report["window"]) == (until, {"since": since, "until": until})
    # A contiguous window that stops before its run extends the coverage but is no collection, manual or not.
    chunk, ran = timestamp((later + timedelta(minutes=30)).isoformat()), later + timedelta(hours=1)
    collect(configured, "manual", start=since, end=chunk, runner=lambda *_: b"[]", clock=lambda: ran)
    report = source_health(configured, now=ran)["manual"]
    assert (report["last_collected"], report["window"]) == (until, {"since": since, "until": chunk})


def test_a_window_ending_before_its_run_extends_coverage_without_claiming_freshness(configured: Store) -> None:
    collect(configured, "sample", start=START, end=END, runner=lambda *_: b"[]", clock=lambda: NOW)
    # Ten days later, as after a pause, a backfill chunk adjacent to the coverage stops seven days short of now.
    later, chunk = NOW + timedelta(days=10), timestamp((NOW + timedelta(days=3)).isoformat())
    collect(configured, "sample", start=END, end=chunk, runner=lambda *_: b"[]", clock=lambda: later)
    entry = state(configured)["sample"]
    assert (entry["start"], entry["end"], entry["success"]) == (START, chunk, END)
    # Before, the chunk counted as a fresh collection, which also hid the source from search replies' coverage.
    health = source_health(configured, now=later)["sample"]
    assert (health["freshness"], health["last_collected"]) == ("overdue", END)
    assert health["window"] == {"since": START, "until": chunk}
    # The next update resumes from the chunk's end, with overlap, and brings the coverage up to date.
    report = cast("Any", update(configured, now=later, sensors=("sample",), runner=lambda *_: b"[]"))
    assert report["sensors"][0]["start"] == timestamp((NOW + timedelta(days=3, minutes=-5)).isoformat())
    entry, current = state(configured)["sample"], timestamp(later.isoformat())
    assert (entry["start"], entry["end"], entry["success"]) == (START, current, current)


def test_a_first_window_ending_in_the_past_is_coverage_not_a_collection(configured: Store) -> None:
    # Like the first monthly chunk of a history backfill.
    january = ("2020-01-01T00:00:00.000000Z", "2020-02-01T00:00:00.000000Z")
    collect(configured, "sample", start=january[0], end=january[1], runner=lambda *_: b"[]", clock=lambda: NOW)
    health = source_health(configured, now=NOW)["sample"]
    assert (health["freshness"], health["window"]) == ("never", {"since": january[0], "until": january[1]})
    assert "last_collected" not in health
    # Never collected up to now, the source is due at once and catches up the last 30 days.
    assert [window[1] for window in due(configured, NOW) if window[0] == "sample"] == [
        timestamp((NOW - timedelta(days=30)).isoformat())
    ]


def test_a_default_collect_covers_through_its_own_run(configured: Store) -> None:
    # The command and the collection share one clock: a run without --until reaches the instant it ran.
    configured.write(
        "bf.yaml", CONFIG.replace(b"  manual:\n    command: [echo]", b'  manual:\n    command: [echo, "[]"]')
    )
    result = CliRunner().invoke(app, ["collect", "manual", "--brain", str(configured.root)])
    assert result.exit_code == 0, result.output
    entry = state(configured)["manual"]
    assert entry["success"] == entry["end"] == entry["run"]


def test_invalid_run_history_fails_its_write_instead_of_erasing_the_entry(configured: Store) -> None:
    collect(configured, "sample", start=START, end=END, runner=lambda *_: b"[]", clock=lambda: NOW)
    saved = state(configured)
    # The next read would drop the whole entry: the program would show `never` and restart its window.
    with writer(configured):
        for values in ({"failures": -1}, {"unexpected": 1}, {"success": "yesterday"}):
            with pytest.raises(Error, match=r"^sample: invalid run history: "):
                remember(configured, "sample", SENSORS, **values)
    assert state(configured) == saved


def test_failed_run_history_reports_that_records_were_committed(
    configured: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Store.write

    def fail_history(self: Store, name: str, data: bytes, *, durable: bool = True) -> None:
        if name == "sensors.json":
            raise PermissionError("synthetic state failure")
        original(self, name, data, durable=durable)

    monkeypatch.setattr(Store, "write", fail_history)
    with pytest.raises(Error, match="records were committed"):
        collect(configured, "sample", start=START, end=END, runner=lambda *_: emit({"id": "one", "title": "Saved"}))
    assert records.find(configured, "sample", "one") is not None


@pytest.mark.parametrize("previous_start", ["2026-08-01T00:00:00.000000Z", "2026-12-01T00:00:00.000000Z"])
def test_future_run_history_cannot_block_current_collection(configured: Store, previous_start: str) -> None:
    state_store(configured.root).write(
        "sensors.json",
        json.dumps({"sample": {"start": previous_start, "end": "2027-01-01T00:00:00Z", "success": START}}).encode(),
    )
    report = cast("Any", update(configured, now=NOW, runner=lambda *_: b"[]"))
    assert report["ok"]
    entry = state(configured)["sample"]
    assert entry["success"] == END
    assert entry["end"] == END
    assert entry["start"] == min(previous_start, START)
    assert not due(configured, NOW)
