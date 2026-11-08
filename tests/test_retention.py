"""Retention, forgetting, retired sources, fixture checks and the environment BF gives its programs."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bf import records
from bf.cli import app
from bf.collect import collect, routine
from bf.config import load
from bf.models import Error, Record, digest
from bf.storage import Store
from conftest import records_file

START = "2026-09-01T00:00:00.000000Z"
END = "2026-09-02T00:00:00.000000Z"
NOW = datetime(2026, 9, 2, tzinfo=UTC)
YEAR = 31_536_000


def emit(*items: Mapping[str, object]) -> bytes:
    return json.dumps(list(items)).encode()


def configure(brain: Store, sensor: str) -> None:
    brain.write("bf.yaml", f"version: 8\nname: fixture\nsensors:\n  mail:\n{sensor}".encode())


def stored(brain: Store, source: str = "mail") -> list[str]:
    return sorted(records.load(brain, name).id for name in records.files(brain, source))


def invoke(*args: str, code: int = 0, stdin: str | None = None) -> dict:
    result = CliRunner().invoke(app, list(args), input=stdin)
    assert result.exit_code == code, result.output
    return json.loads(result.stdout) if code == 0 else {}


def test_forget_removes_records_and_keeps_collection_from_storing_them_again(brain: Store) -> None:
    configure(brain, "    command: [echo]\n    mode: snapshot\n")
    printed = emit({"id": "keep", "title": "Keep"}, {"id": "private", "title": "Private"})
    collect(brain, "mail", start=START, end=END, runner=lambda *_: printed, clock=lambda: NOW)
    assert stored(brain) == ["keep", "private"]
    preview = invoke("forget", "mail:private", "--dry-run", "--brain", "fixture")
    assert preview == {
        "ok": True,
        "dry_run": True,
        "records": [{"ref": "mail:private", "removed": True, "listed": False}],
    }
    assert stored(brain) == ["keep", "private"]
    result = invoke("forget", "mail:private", "--brain", "fixture")
    assert result["records"] == [{"ref": "mail:private", "removed": True, "listed": False}]
    assert result["index"]["skipped"] == 0
    assert stored(brain) == ["keep"]
    # The list holds only the record file's own digest, never its id.
    listed = (brain.root / "memories/mail/.forgotten").read_text()
    assert listed == digest(b"private") + "\n"
    assert "private" not in listed
    # The snapshot prints it again: collection skips it without --allow-removal and counts it.
    result_run = collect(brain, "mail", start=START, end=END, runner=lambda *_: printed, clock=lambda: NOW)
    assert result_run["forgotten"] == 1
    assert stored(brain) == ["keep"]
    assert invoke("search", "private", "--brain", "fixture")["items"] == []
    assert invoke("validate", "--brain", "fixture")["valid"] is True


def test_forget_keeps_the_owner_comments_in_the_forgotten_list(brain: Store) -> None:
    configure(brain, "    command: [echo]\n")
    records_file(brain, "mail", [Record(id="m1", title="One"), Record(id="m2", title="Two")])
    first = digest(b"m1")
    brain.write("memories/mail/.forgotten", f"# private thread, 2026-09\n{first}".encode())
    invoke("forget", "mail:m1", "mail:m2", "--brain", str(brain.root))
    listed = brain.read("memories/mail/.forgotten").decode()
    assert listed == f"# private thread, 2026-09\n{first}\n{digest(b'm2')}\n"
    assert stored(brain) == []


def test_forget_accepts_record_paths_and_ids_not_collected_yet(brain: Store) -> None:
    configure(brain, "    command: [echo]\n")
    records_file(brain, "mail", [Record(id="m1", title="One")])
    path = records.path("mail", "m1")
    result = invoke("forget", path, "mail:later", "--brain", "fixture")
    assert result["records"] == [
        {"ref": path, "removed": True, "listed": False},
        {"ref": "mail:later", "removed": False, "listed": False},
    ]
    collect(
        brain,
        "mail",
        start=START,
        end=END,
        runner=lambda *_: emit({"id": "later", "title": "Later"}, {"id": "m1", "title": "One"}),
        clock=lambda: NOW,
    )
    assert stored(brain) == []
    # Forgetting again reports the record as listed already.
    assert invoke("forget", "mail:later", "--brain", "fixture")["records"][0]["listed"] is True
    # Two spellings of one record in one call: neither was listed before it, and the list gains one line.
    records_file(brain, "mail", [Record(id="m2", title="Two")])
    both = invoke("forget", "mail:m2", records.path("mail", "m2"), "--brain", "fixture")["records"]
    assert [item["listed"] for item in both] == [False, False]
    assert brain.read("memories/mail/.forgotten").decode().count(digest(b"m2")) == 1


@pytest.mark.usefixtures("brain")
@pytest.mark.parametrize("ref", ["mail", "mail:", ":id", "memories/mail/x.json", "bf:x", "../mail:x"])
def test_forget_rejects_refs_that_name_no_record(ref: str) -> None:
    result = CliRunner().invoke(app, ["forget", ref, "--brain", "fixture"])
    assert result.exit_code == 2
    assert "invalid input: REF" in result.stderr


def test_a_corrupt_forgotten_list_fails_collection_without_changing_evidence(brain: Store) -> None:
    configure(brain, "    command: [echo]\n")
    records_file(brain, "mail", [Record(id="m1", title="One")])
    brain.write("memories/mail/.forgotten", b"# reviewed\nnot-a-digest\n")
    with pytest.raises(Error, match=r"\.forgotten:2: expected the SHA-256"):
        collect(brain, "mail", start=START, end=END, runner=lambda *_: emit(), clock=lambda: NOW)
    assert stored(brain) == ["m1"]


@pytest.mark.parametrize("mode", ["window", "snapshot"])
def test_expire_after_removes_and_skips_records_older_than_its_cutoff(brain: Store, mode: str) -> None:
    configure(brain, f"    command: [echo]\n    mode: {mode}\n    expire_after: {YEAR}\n")
    old = Record(id="old", title="Old", time="2024-01-01T00:00:00Z")
    timeless = Record(id="timeless", title="Timeless", attributes={"observed": "2024-06-01T00:00:00Z"})
    records_file(brain, "mail", [old, timeless, Record(id="recent", title="Recent", time="2026-08-01T00:00:00Z")])
    printed = emit(
        {"id": "ancient", "title": "Ancient", "time": "2020-01-01T00:00:00Z"},
        {"id": "recent", "title": "Recent", "time": "2026-08-01T00:00:00Z"},
        {"id": "new", "title": "New"},
    )
    preview = collect(brain, "mail", start=START, end=END, runner=lambda *_: printed, clock=lambda: NOW, dry_run=True)
    assert (preview["expired"], preview["removed"]) == (3, 0)
    result = collect(brain, "mail", start=START, end=END, runner=lambda *_: printed, clock=lambda: NOW)
    # The printed ancient record is skipped; the stored old and timeless ones are removed.
    assert result["expired"] == 3
    assert result["removed"] == 0
    assert stored(brain) == ["new", "recent"]


def test_a_failed_collection_keeps_its_expired_records(brain: Store) -> None:
    configure(brain, f"    command: [echo]\n    expire_after: {YEAR}\n")
    records_file(brain, "mail", [Record(id="old", title="Old", time="2024-01-01T00:00:00Z")])
    # A misnamed file the run would replace fails its commit after the expired removal was planned.
    brain.write(records.path("mail", "misnamed"), records.serialize(Record(id="other", title="Other")))
    printed = emit({"id": "misnamed", "title": "Misnamed"})
    with pytest.raises(Error, match="nothing was written"):
        collect(brain, "mail", start=START, end=END, runner=lambda *_: printed, clock=lambda: NOW)
    assert sorted(records.files(brain, "mail")) == sorted(records.path("mail", key) for key in ("old", "misnamed"))


def test_expire_after_is_bounded(brain: Store) -> None:
    configure(brain, "    command: [echo]\n    expire_after: 60\n")
    with pytest.raises(Error, match="expire_after"):
        load(brain)


def test_a_disabled_program_may_omit_its_command(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nsensors:\n  retired:\n    enabled: false\nroutines:\n  old:\n    enabled: false\n",
    )
    config = load(brain)
    assert config.sensors["retired"].command == []
    assert invoke("validate", "--brain", "fixture")["valid"] is True
    for sensor in ("retired:\n    refresh: 60\n", "retired:\n    enabled: true\n"):
        brain.write("bf.yaml", f"version: 8\nname: fixture\nsensors:\n  {sensor}".encode())
        with pytest.raises(Error, match="command is required unless enabled: false"):
            load(brain)


def test_collect_input_checks_saved_output_without_running_or_saving(brain: Store, tmp_path: Path) -> None:
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nfields:\n  sender:\n    description: Sender.\n    type: identity\n    cardinality: many\n"
        b"    relation: true\n    path: /attributes/sender_refs\nsensors:\n  mail:\n    enabled: false\n",
    )
    fixture = tmp_path / "mail.json"
    fixture.write_text(
        json.dumps([{"id": "m1", "title": "Hi", "attributes": {"sender_refs": ["person:email/alice@example.test"]}}])
    )
    result = invoke("collect", "mail", "--input", str(fixture), "--brain", "fixture")
    assert result["added"] == 1
    assert result["samples"][0]["fields"] == {"sender": ["person:email/alice@example.test"]}
    assert stored(brain) == []
    piped = invoke("collect", "mail", "--input", "-", "--brain", "fixture", stdin=fixture.read_text())
    assert piped["samples"][0]["fields"] == result["samples"][0]["fields"]
    # Invalid output fails like a run, naming the record, and still saves nothing.
    fixture.write_text(json.dumps([{"id": "m1"}]))
    with pytest.raises(Error, match=r"0\.title: Field required; nothing was written"):
        collect(brain, "mail", start=START, end=END, clock=lambda: NOW, fixture=fixture.read_bytes())
    missing = CliRunner().invoke(app, ["collect", "mail", "--input", str(tmp_path / "none"), "--brain", "fixture"])
    assert missing.exit_code == 2
    assert "invalid input: --input" in missing.stderr
    unknown = CliRunner().invoke(app, ["collect", "nope", "--input", str(fixture), "--brain", "fixture"])
    assert unknown.exit_code == 1


def test_collect_input_ignores_the_command_placeholders(
    brain: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fixture check runs nothing, so it needs neither this machine's line files nor HOME.
    configure(brain, '    command: [echo, "{{lines:settings/missing.txt}}", "{{home}}"]\n')
    monkeypatch.delenv("HOME", raising=False)
    fixture = tmp_path / "mail.json"
    fixture.write_text(json.dumps([{"id": "m1", "title": "Hi"}]))
    assert invoke("collect", "mail", "--input", str(fixture), "--brain", "fixture")["added"] == 1
    assert not (brain.root / "logs/mail.log").exists()


def test_collect_input_and_window_keep_their_bounds(brain: Store, tmp_path: Path) -> None:
    output = emit({"id": "m1", "title": "Hi"})
    configure(brain, f"    enabled: false\n    max_bytes: {len(output)}\n")
    fixture = tmp_path / "mail.json"
    fixture.write_bytes(output)
    assert invoke("collect", "mail", "--input", str(fixture), "--brain", "fixture")["added"] == 1
    # One more byte of valid JSON exceeds the sensor's bound, as its own run would.
    fixture.write_bytes(output + b" ")
    over = CliRunner().invoke(app, ["collect", "mail", "--input", str(fixture), "--brain", "fixture"])
    assert over.exit_code == 2
    assert "invalid input: --input: exceeds the sensor's max_bytes" in over.stderr
    assert stored(brain) == []
    # Without --since, the window starts one lookback before now: an older end names no window.
    early = CliRunner().invoke(app, ["collect", "mail", "--until", "2020-01-01", "--brain", "fixture"])
    assert early.exit_code == 2
    assert "invalid input: --until: must be later than the start; set --since for a historical window" in early.stderr


def test_programs_receive_their_name_window_and_private_state_directory(brain: Store) -> None:
    script = (
        "import json, os, stat, sys\n"
        "state = os.environ['BF_STATE_DIR']\n"
        "info = os.stat(state)\n"
        "names = ('BF_BRAIN', 'BF_PROGRAM', 'BF_START', 'BF_END', 'BF_TRIGGER', 'BF_SCHEDULE')\n"
        "seen = {name: os.environ.get(name, '') for name in names}\n"
        "seen['mode'] = oct(stat.S_IMODE(info.st_mode))\n"
        "seen['inside'] = os.path.realpath(state).startswith(os.path.realpath(os.environ['BF_BRAIN']))\n"
        "print(json.dumps([{'id': 'env', 'title': 'Env', 'attributes': seen}]))\n"
    )
    brain.write("sensors/env.py", script.encode())
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nsensors:\n  env:\n    command: [python3, sensors/env.py]\n",
    )
    os.environ["BF_TRIGGER"] = "inherited"
    try:
        collect(brain, "env", start=START, end=END, clock=lambda: NOW)
    finally:
        del os.environ["BF_TRIGGER"]
    seen = records.load(brain, records.path("env", "env")).attributes
    assert seen["BF_PROGRAM"] == "env"
    assert (seen["BF_START"], seen["BF_END"]) == (START, END)
    # Inherited values of the variables BF owns never reach a program.
    assert (seen["BF_TRIGGER"], seen["BF_SCHEDULE"]) == ("", "")
    assert seen["mode"] == "0o700"
    assert seen["inside"] is False


def test_routines_receive_their_window(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nroutines:\n  check:\n    command: [python3, routines/check.py]\n",
    )
    brain.write(
        "routines/check.py",
        b"import os, sys\nsys.exit(0 if os.environ['BF_START'] and os.environ['BF_PROGRAM'] == 'check' else 3)\n",
    )
    # A missing variable makes the routine exit 3, which fails the run.
    assert routine(brain, "check", start=START, end=END, clock=lambda: NOW)["routine"] == "check"


def test_expired_records_never_trip_the_snapshot_removal_guard(brain: Store) -> None:
    configure(brain, f"    command: [echo]\n    mode: snapshot\n    expire_after: {YEAR}\n")
    old = [Record(id=f"old-{n}", title="Old", time="2024-01-01T00:00:00Z") for n in range(20)]
    records_file(brain, "mail", [*old, *(Record(id=f"new-{n}", title="New") for n in range(6))])
    printed = emit(*({"id": f"new-{n}", "title": "New"} for n in range(6)))
    preview = collect(brain, "mail", start=START, end=END, runner=lambda *_: printed, clock=lambda: NOW, dry_run=True)
    assert "removal_refused" not in preview
    assert (preview["expired"], preview["removed"]) == (20, 0)
    result = collect(brain, "mail", start=START, end=END, runner=lambda *_: printed, clock=lambda: NOW)
    assert (result["expired"], result["removed"], result["unchanged"]) == (20, 0, 6)
    assert len(stored(brain)) == 6


def test_a_record_both_stored_and_printed_counts_once(brain: Store) -> None:
    configure(brain, f"    command: [echo]\n    expire_after: {YEAR}\n")
    records_file(brain, "mail", [Record(id="old", title="Old", time="2024-01-01T00:00:00Z")])
    printed = emit({"id": "old", "title": "Old", "time": "2024-01-01T00:00:00Z"})
    result = collect(brain, "mail", start=START, end=END, runner=lambda *_: printed, clock=lambda: NOW)
    assert result["expired"] == 1
    assert stored(brain) == []


def test_an_undated_record_the_sensor_still_prints_never_expires(brain: Store) -> None:
    configure(brain, f"    command: [echo]\n    mode: snapshot\n    expire_after: {YEAR}\n")
    records_file(brain, "mail", [Record(id="page", title="Page", attributes={"observed": "2020-01-01T00:00:00Z"})])
    printed = emit({"id": "page", "title": "Page"})
    result = collect(brain, "mail", start=START, end=END, runner=lambda *_: printed, clock=lambda: NOW)
    assert "expired" not in result
    assert result["unchanged"] == 1
    assert stored(brain) == ["page"]


def test_status_reports_the_last_run_retention_counts(brain: Store) -> None:
    configure(brain, "    command: [echo]\n")
    brain.write("memories/mail/.forgotten", digest(b"hidden").encode() + b"\n")
    printed = emit({"id": "hidden", "title": "Hidden"}, {"id": "shown", "title": "Shown"})
    collect(brain, "mail", start=START, end=END, runner=lambda *_: printed, clock=lambda: NOW)
    status = invoke("status", "--brain", "fixture")
    assert status["brains"][0]["sources"]["mail"]["last_run"]["forgotten"] == 1
    # A later run without forgotten records resets the count, which status then leaves out.
    shown = emit({"id": "shown", "title": "Shown"})
    collect(brain, "mail", start=START, end=END, runner=lambda *_: shown, clock=lambda: NOW)
    assert "forgotten" not in invoke("status", "--brain", "fixture")["brains"][0]["sources"]["mail"]["last_run"]


def test_forget_rejects_an_unknown_source_and_validate_checks_the_list(brain: Store) -> None:
    configure(brain, "    command: [echo]\n")
    result = CliRunner().invoke(app, ["forget", "gmail:x", "--brain", "fixture"])
    assert result.exit_code == 1
    assert "unknown source gmail" in str(result.exception)
    assert not (brain.root / "memories/gmail").exists()
    brain.write("memories/mail/.forgotten", b"not-a-digest\n")
    reply = CliRunner().invoke(app, ["validate", "--brain", "fixture"])
    assert reply.exit_code == 1
    assert json.loads(reply.stdout)["problems"][0]["file"] == "memories/mail/.forgotten"


def test_forget_exits_1_when_the_refreshed_cache_skips_a_file(brain: Store) -> None:
    configure(brain, "    command: [echo]\n")
    records_file(brain, "mail", [Record(id="m1", title="One")])
    brain.write("projects/broken.md", b"# Broken\n\n\xff\xfe\n")
    result = CliRunner().invoke(app, ["forget", "mail:m1", "--brain", str(brain.root)])
    # The records are forgotten, but the skipped note hides evidence, as for build and update.
    assert result.exit_code == 1, result.output
    reply = json.loads(result.stdout)
    assert reply["records"] == [{"ref": "mail:m1", "removed": True, "listed": False}]
    assert reply["index"]["skipped"] == 1
    assert stored(brain) == []
