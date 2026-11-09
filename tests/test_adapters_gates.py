"""The copy-in records gate reports each new or changed record once, within BF's gate bounds, and fails closed."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from typer.testing import CliRunner

from bf.assistants import GATE_EVENT_BYTES
from bf.cli import app
from bf.models import GateReply, encode
from bf.storage import Store
from conftest import ROOT, Provider

GATE = "records-gate.py"
START, END = "2026-09-29T10:00:00Z", "2026-09-29T11:00:00Z"
# Never quoted by a diagnostic, whichever line or stream of the export carries it.
PRIVATE = "Quarterly figures for the board"


def item(ref: str, observed: str | None, **extra: object) -> dict:
    """One `bf export --kind items` record line as BF prints it: times with the local offset."""
    return {
        "brain": "fixture",
        "kind": "record",
        "ref": ref,
        "source": ref.partition(":")[0],
        "title": f"Title of {ref}",
        "text": PRIVATE,
        **({"observed": observed} if observed else {}),
        **extra,
    }


def lines(*items: dict) -> str:
    return "".join(json.dumps(value) + "\n" for value in items)


def gate(provider: Provider, state: Path, *arguments: str, environment: dict[str, str] | None = None):
    state.mkdir(exist_ok=True)
    window = {"BF_START": START, "BF_END": END, "BF_STATE_DIR": str(state)}
    return provider.run(GATE, *arguments, folder="routines", environment=environment or window)


def events(result) -> list[dict]:
    assert result.returncode == 0, result.stderr
    reply = json.loads(result.stdout)
    # What BF accepts from a gate: at most 64 events within 65,536 encoded bytes.
    events = [event.model_dump(exclude_defaults=True) for event in GateReply.model_validate(reply).events]
    assert len(encode(events)) <= GATE_EVENT_BYTES
    return reply["events"]


def test_reports_records_observed_in_the_window_once(provider: Provider, tmp_path: Path) -> None:
    export = lines(
        # Collected by a run that started 5 minutes before the window, so the previous check could not see it.
        item("mail:late", "2026-09-29T11:55:00+02:00"),
        item("mail:old", "2026-09-29T09:49:00Z"),
        item("mail:new", "2026-09-29T10:30:00.250000Z"),
        item("mail:future", END),
        # Placed by hand: nothing says when it arrived.
        item("mail:manual", None),
        {"brain": "fixture", "kind": "note", "ref": "projects/a.md", "title": "A", "text": "x"},
    )
    provider.install("bf", [{"match": ["export"], "stdout": export, "repeat": True}])
    state = tmp_path / "state"
    assert events(gate(provider, state, "mail")) == [
        {
            "key": "bf://fixture/mail:new@2026-09-29T10:30:00.250000Z",
            "text": "Title of mail:new",
            "ref": "bf://fixture/mail:new",
        },
        {
            "key": "bf://fixture/mail:late@2026-09-29T09:55:00.000000Z",
            "text": "Title of mail:late",
            "ref": "bf://fixture/mail:late",
        },
    ]
    assert provider.calls("bf") == [["export", "--kind", "items", "--scope", "memories/mail"]]
    # A second check of the same window finds nothing new: the harness does not start again.
    result = gate(provider, state, "mail")
    assert (result.returncode, result.stdout, result.stderr) == (0, '{"events": []}\n', "")
    # A changed record is a new version, reported once more.
    provider.install("bf", [{"match": ["export"], "stdout": lines(item("mail:new", "2026-09-29T10:45:00Z"))}])
    assert [event["key"] for event in events(gate(provider, state, "mail"))] == [
        "bf://fixture/mail:new@2026-09-29T10:45:00.000000Z"
    ]


def test_a_long_record_address_gets_a_hashed_key_and_is_still_reported_once(provider: Provider, tmp_path: Path) -> None:
    ref = "mail:" + "x" * 300
    export = lines(item(ref, "2026-09-29T10:30:00Z", title="Long address"))
    provider.install("bf", [{"match": ["export"], "stdout": export, "repeat": True}])
    state = tmp_path / "state"
    version = f"bf://fixture/{ref}@2026-09-29T10:30:00.000000Z"
    # BF accepts event keys of at most 256 characters: a longer one becomes the source and the version's digest.
    assert events(gate(provider, state, "mail")) == [
        {
            "key": f"mail:sha256:{hashlib.sha256(version.encode()).hexdigest()}",
            "text": "Long address",
            "ref": f"bf://fixture/{ref}",
        }
    ]
    result = gate(provider, state, "mail")
    assert (result.returncode, result.stdout, result.stderr) == (0, '{"events": []}\n', "")


def test_records_of_referenced_brains_are_named_by_address(provider: Provider, tmp_path: Path) -> None:
    # `bf export` also lists the brains this brain references, where the same plain ref can name another record.
    observed = "2026-09-29T10:30:00Z"
    export = lines(item("mail:m1", observed), {**item("mail:m1", observed), "brain": "team"})
    provider.install("bf", [{"match": ["export"], "stdout": export}])
    state = tmp_path / "state"
    assert [(event["key"], event["ref"]) for event in events(gate(provider, state, "mail"))] == [
        ("bf://team/mail:m1@2026-09-29T10:30:00.000000Z", "bf://team/mail:m1"),
        ("bf://fixture/mail:m1@2026-09-29T10:30:00.000000Z", "bf://fixture/mail:m1"),
    ]
    # A change in one brain is reported alone; the other brain's unchanged record stays remembered.
    export = lines(item("mail:m1", observed), {**item("mail:m1", "2026-09-29T10:50:00Z"), "brain": "team"})
    provider.install("bf", [{"match": ["export"], "stdout": export}])
    assert [event["ref"] for event in events(gate(provider, state, "mail"))] == ["bf://team/mail:m1"]
    # An address percent-encodes what a record id may hold, as a search result's uri does.
    provider.install("bf", [{"match": ["export"], "stdout": lines(item("mail:a b#c?d%", observed))}])
    assert [event["ref"] for event in events(gate(provider, state, "mail"))] == ["bf://fixture/mail:a%20b%23c%3Fd%25"]


def test_where_keeps_matching_single_value_fields_and_conditions_combine(provider: Provider, tmp_path: Path) -> None:
    observed = "2026-09-29T10:30:00Z"
    export = lines(
        item("mail:a", observed, fields={"kind": "message", "status": "open", "priority": 1, "flagged": True}),
        item("mail:b", observed, fields={"kind": "message", "status": "closed"}),
        item("mail:c", observed, fields={"kind": "reply", "status": "open"}),
        item("mail:d", observed),
    )
    provider.install("bf", [{"match": ["export"], "stdout": export, "repeat": True}])

    def refs(*where: str) -> list[str]:
        state = tmp_path / ("state" + "".join(where))
        arguments = [argument for condition in where for argument in ("--where", condition)]
        return sorted(event["ref"] for event in events(gate(provider, state, "mail", *arguments)))

    assert refs("kind=message") == ["bf://fixture/mail:a", "bf://fixture/mail:b"]
    assert refs("kind=message", "status=open") == ["bf://fixture/mail:a"]
    # Numbers and booleans read as JSON writes them.
    assert refs("priority=1", "flagged=true") == ["bf://fixture/mail:a"]
    assert refs("status=") == []


def test_overflow_keeps_the_newest_63_events_and_counts_the_rest(provider: Provider, tmp_path: Path) -> None:
    export = lines(*(item(f"mail:m{n:02d}", f"2026-09-29T10:{n % 60:02d}:{n // 60:02d}Z") for n in range(70)))
    provider.install("bf", [{"match": ["export"], "stdout": export}])
    state = tmp_path / "state"
    reported = events(gate(provider, state, "mail"))
    assert len(reported) == 64
    assert reported[-1] == {
        "key": "mail:more",
        "text": "7 more new or changed records are not listed: mail 7. Read their source pages.",
        "ref": "memories/mail",
    }
    # Every record is remembered, listed or counted: the next check reports none of them.
    assert len(json.loads((state / "records-gate.json").read_text())["seen"]) == 70


def test_overflow_stays_within_the_event_bytes_bf_accepts(provider: Provider, tmp_path: Path) -> None:
    # Refs near their 4,096-character bound and long titles of characters JSON escapes.
    long = [
        item(f"mail:{n:02d}{'x' * 4000}", "2026-09-29T10:30:00Z", title="\u202e" * 200 + "t" * 5000) for n in range(40)
    ]
    provider.install("bf", [{"match": ["export"], "stdout": lines(*long)}])
    reported = events(gate(provider, tmp_path / "state", "mail", "mail"))
    assert reported[-1]["key"] == "mail:more"
    assert all(len(event["key"]) <= 256 and len(event["text"]) <= 4096 for event in reported)
    # Hidden characters stay visible to whoever reads the event; the title is cut to 280 characters first.
    assert reported[0]["text"].startswith("\\u202e")
    assert reported[0]["text"].endswith("t…")


def test_period_and_several_sources_each_export_one_scope(provider: Provider, tmp_path: Path) -> None:
    provider.install(
        "bf",
        [
            {"match": ["memories/mail/7d"], "stdout": lines(item("mail:a", "2026-09-29T10:30:00Z"))},
            {"match": ["memories/jira/7d"], "stdout": lines(item("jira:b", "2026-09-29T10:40:00Z"))},
        ],
    )
    reported = events(gate(provider, tmp_path / "state", "mail", "jira", "mail", "--period", "7d"))
    assert [event["ref"] for event in reported] == ["bf://fixture/jira:b", "bf://fixture/mail:a"]
    assert [call[-1] for call in provider.calls("bf")] == ["memories/mail/7d", "memories/jira/7d"]


def test_remembered_keys_are_forgotten_after_35_days(provider: Provider, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    old, recent = "bf://fixture/mail:a@2026-08-24T10:00:00.000000Z", "bf://fixture/mail:b@2026-09-28T10:00:00.000000Z"
    (state / "records-gate.json").write_text(json.dumps({"seen": [old, recent]}))
    provider.install("bf", [{"match": ["export"], "stdout": ""}])
    assert events(gate(provider, state, "mail")) == []
    assert json.loads((state / "records-gate.json").read_text()) == {"seen": [recent]}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            {"stdout": lines(item("mail:a", "2026-09-29T10:30:00Z")), "stderr": f"{PRIVATE}\n", "code": 1},
            "bf export of memories/mail exited with status 1; run bf export --kind items --scope memories/mail to see why",
        ),
        ({"stdout": f"{PRIVATE}\n"}, "bf export of memories/mail printed an invalid line 1"),
        ({"stdout": '["mail:a"]\n'}, "bf export of memories/mail printed an invalid line 1"),
        # Without its brain, a ref cannot name its record: a referenced brain may hold the same ref.
        ({"stdout": '{"kind": "record", "ref": "mail:a"}\n'}, "bf export of memories/mail printed an invalid line 1"),
        (
            {"stdout": lines(item("mail:a", PRIVATE))},
            "bf export of memories/mail printed an invalid observed time on line 1",
        ),
        (
            # bf notes a busy cache on stderr and still exits 0: what it printed may miss records.
            {"stdout": "", "stderr": "bf: fixture: a writer kept the cache from refreshing; retry for newer claims\n"},
            (
                "bf export of memories/mail reported incomplete evidence; "
                "run bf export --kind items --scope memories/mail to see why"
            ),
        ),
    ],
)
def test_export_failures_exit_1_without_quoting_output(
    provider: Provider, tmp_path: Path, response: dict, message: str
) -> None:
    provider.install("bf", [{"match": ["export"], **response}])
    state = tmp_path / "state"
    result = gate(provider, state, "mail")
    assert (result.returncode, result.stdout, result.stderr) == (1, "", f"records-gate: {message}\n")
    # Nothing was reported, so nothing is remembered: BF retries the gate.
    assert not (state / "records-gate.json").exists()


def test_an_older_bf_without_export_scope_is_named(provider: Provider, tmp_path: Path) -> None:
    provider.install(
        "bf",
        [
            {"match": ["export"], "stderr": "No such option: --scope\n", "code": 2},
            {"match": ["--version"], "stdout": "21.2.0\n"},
        ],
    )
    result = gate(provider, tmp_path / "state", "mail")
    assert (result.returncode, result.stderr) == (
        1,
        "records-gate: bf 21.2.0 cannot export a scope; install bf 21.3.0 or later\n",
    )


def test_missing_bf_or_window_fails_closed(provider: Provider, tmp_path: Path) -> None:
    result = gate(provider, tmp_path / "state", "mail")
    assert (result.returncode, result.stderr) == (1, "records-gate: bf is not on PATH; install bf 21.3.0 or later\n")
    provider.install("bf", [])
    for missing in ("BF_START", "BF_END", "BF_STATE_DIR"):
        environment = {"BF_START": START, "BF_END": END, "BF_STATE_DIR": str(tmp_path)}
        environment.pop(missing)
        result = gate(provider, tmp_path / "state", "mail", environment=environment)
        assert result.returncode == 2
        assert result.stderr.startswith("records-gate: BF_START, BF_END and BF_STATE_DIR must be set")
    assert provider.calls("bf") == []
    # Arguments are checked before anything runs.
    for arguments in (["Mail"], ["mail", "--where", "kind"], ["mail", "--period", "a/b"]):
        assert gate(provider, tmp_path / "state", *arguments).returncode == 2


def test_a_damaged_state_file_stops_the_gate(provider: Provider, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "records-gate.json").write_text('{"seen": "x"}')
    provider.install("bf", [{"match": ["export"], "stdout": lines(item("mail:a", "2026-09-29T10:30:00Z"))}])
    result = gate(provider, state, "mail")
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == (
        "records-gate: cannot read records-gate.json in BF_STATE_DIR; delete it to report this window's records again\n"
    )
    assert (state / "records-gate.json").read_text() == '{"seen": "x"}'


def load() -> ModuleType:
    """The gate as a module, to drive its export with small bounds."""
    spec = importlib.util.spec_from_file_location("records_gate", ROOT / "examples/routines" / GATE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_export_is_bounded_in_time_and_line_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load()
    slow = tmp_path / "slow-bf"
    # Prints one valid line, then never finishes: the gate stops it at its deadline.
    slow.write_text(
        f'#!{sys.executable}\nimport sys, time\nprint(\'{{"brain": "b", "ref": "mail:a"}}\', flush=True)\ntime.sleep(60)\n'
    )
    slow.chmod(0o700)
    started = time.monotonic()
    with pytest.raises(SystemExit) as stopped:
        list(module.export(str(slow), "memories/mail", time.monotonic() + 1))
    assert stopped.value.code == 1
    assert time.monotonic() - started < 30
    assert "bf export of memories/mail ran past the gate's 100-second limit" in capsys.readouterr().err
    # A line beyond the bound stops the gate before it is held whole.
    long = tmp_path / "long-bf"
    long.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write('x' * 300000)\n")
    long.chmod(0o700)
    monkeypatch.setattr(module, "LINE_BYTES", 1000)
    with pytest.raises(SystemExit):
        list(module.export(str(long), "memories/mail", time.monotonic() + 30))
    assert "printed line 1 longer than 1000 bytes" in capsys.readouterr().err


def test_stopping_the_gate_stops_its_export(provider: Provider, tmp_path: Path) -> None:
    # BF stops a gate by signalling its process group, on a timeout, a cancellation or watch's shutdown. The gate then
    # dies at once, so its nested export must share that group to stop with it.
    lock, ready = tmp_path / "bf.lock", tmp_path / "bf.ready"
    fake = provider.bin / "bf"
    fake.write_text(
        f"#!{sys.executable}\nimport fcntl, os, pathlib, time\nlock = open({str(lock)!r}, 'w')\n"
        f"fcntl.flock(lock, fcntl.LOCK_EX)\npathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n"
    )
    fake.chmod(0o700)
    state = tmp_path / "state"
    state.mkdir()
    environment = {
        "PATH": f"{provider.bin}:/usr/bin:/bin",
        "BF_START": START,
        "BF_END": END,
        "BF_STATE_DIR": str(state),
    }
    # Its own session, as BF starts a gate.
    child = subprocess.Popen(  # noqa: S603 - the gate on a fake bf
        [sys.executable, str(ROOT / "examples/routines" / GATE), "mail"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    stopped = False
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert child.poll() is None, "the gate stopped before its export started"
            assert time.monotonic() < deadline, "the export never started"
            time.sleep(0.05)
        os.killpg(child.pid, signal.SIGTERM)
        assert child.wait(timeout=30) == -signal.SIGTERM
        # The fake bf holds the lock until it exits, reaped or not.
        with lock.open("w") as stream:
            deadline = time.monotonic() + 10
            while not stopped:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    stopped = True
                except BlockingIOError:
                    assert time.monotonic() < deadline, "the export outlived its gate"
                    time.sleep(0.05)
    finally:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
        if not stopped and ready.exists():
            with suppress(ProcessLookupError, ValueError):
                os.kill(int(ready.read_text()), signal.SIGKILL)


def test_the_state_file_keeps_the_newest_keys_within_its_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load()
    keys = {f"bf://fixture/mail:m{n}@2026-09-29T10:{n:02d}:00.000000Z" for n in range(10)}
    bound = len(module.encoded(keys)) - 1
    monkeypatch.setattr(module, "STATE_BYTES", bound)
    path = tmp_path / "records-gate.json"
    module.save(path, keys)
    # The oldest key goes: later checks cover only newer records.
    assert json.loads(path.read_text())["seen"] == sorted(keys - {"bf://fixture/mail:m0@2026-09-29T10:00:00.000000Z"})
    assert len(path.read_bytes()) <= bound
    assert module.load(path) == keys - {"bf://fixture/mail:m0@2026-09-29T10:00:00.000000Z"}
    assert capsys.readouterr().err == (
        f"records-gate: forgot the oldest reported records, 1 of 10, to keep records-gate.json within {bound} bytes\n"
    )


def test_the_gate_reads_records_collected_by_the_same_update(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # The gate's own `bf export` runs inside `bf update`, which has just collected the source: no lock blocks it.
    brain.write("routines/records-gate.py", (ROOT / "examples/routines" / GATE).read_bytes())
    brain.write("prompts/watcher.md", b"Events: {{context}}\n")
    record = {"id": "m1 #launch", "title": "Launch moved", "time": "2026-09-29T12:00:00Z"}
    config = {
        "version": 8,
        "name": "fixture",
        "sensors": {"mail": {"command": ["printf", json.dumps([record])], "refresh": 3600}},
        "assistants": {
            "watcher": {
                "prompt": "prompts/watcher.md",
                # `cat` answers with the prompt it received, events included.
                "command": ["cat"],
                "schedules": {
                    "changes": {"every": 1, "gate": {"command": ["python3", "routines/records-gate.py", "mail"]}}
                },
            }
        },
    }
    brain.write("bf.yaml", yaml.safe_dump(config).encode())
    # The checkout's bf and python3 stand first on PATH, as an installed bf would.
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}:/usr/bin:/bin")
    runner = CliRunner()

    def update() -> dict:
        result = runner.invoke(app, ["update", "--brain", str(brain.root)])
        assert result.exit_code == 0, result.output
        [row] = json.loads(result.stdout)["assistants"]
        assert row["status"] == "ran"
        return row

    first = update()
    answer = runner.invoke(app, ["read", first["record"], "--brain", str(brain.root)])
    uri = "bf://fixture/mail:m1%20%23launch"
    assert f'"ref":"{uri}","text":"Launch moved"' in json.loads(answer.stdout)["record"]["text"]
    # The event's address reads the record, as the harness would.
    evidence = runner.invoke(app, ["read", uri, "--brain", str(brain.root)])
    assert evidence.exit_code == 0, evidence.output
    assert (json.loads(evidence.stdout)["brain"], json.loads(evidence.stdout)["ref"]) == ("fixture", "mail:m1 #launch")
    # The schedule is due again a second after its last success; its gate then finds nothing new.
    time.sleep(1.1)
    second = update()
    assert second["job"] != first["job"]
    assert "record" not in second
    assert len(list((brain.root / "memories/watcher").glob("*.json"))) == 1
