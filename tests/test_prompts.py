"""The assistant harness keeps prompt expansion, execution, recording and retrieval boundaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bf import assistants, pages, records
from bf.cli import app
from bf.collect import parse_reply, reply_title
from bf.health import report
from bf.history import ASSISTANTS, state
from bf.models import Assistant, Config, Error, Program, Record
from bf.retrieve import read
from bf.runtime import Queue
from bf.storage import Store, collecting
from bf.update import run_assistant, update
from bf.validate import validate
from bf.watch import Job, snapshot

NOW = datetime(2026, 9, 25, 8, tzinfo=UTC)
START = "2026-09-24T08:00:00.000000Z"
END = "2026-09-25T08:00:00.000000Z"
CONFIG = b"""version: 8
name: fixture
assistants:
  assistant:
    prompt: prompts/assistant.md
    command: [agent, --print, "{{prompt}}"]
    text: /response
    schedules:
      review: {every: 1800}
  piped:
    prompt: prompts/assistant.md
    command: [agent]
  paused:
    prompt: prompts/assistant.md
    command: [agent]
    enabled: false
"""
TEMPLATE = b"# Assistant\nWindow {{start}} to {{end}} in {{brain}}.\nOwner: {{input}}\nKeep {{prompt}} and {{other}}.\n"
ANSWER = "## Two replies owed\n\nAnswer the Monday thread."
REPLY = json.dumps({"response": ANSWER + "\n", "usage": {"total_tokens": 42}}).encode()


@pytest.fixture
def configured(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    brain.write("prompts/assistant.md", TEMPLATE)
    return brain


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir()
    script, seen = folder / "agent", tmp_path / "seen"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {seen}.argv\ncat > {seen}.stdin\nprintf "Answer: done\\n"\n')
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{folder}:/usr/bin:/bin")
    return seen


def ask(store: Store, *, name: str = "assistant", output: bytes = REPLY, text: str = "", end: str = END):
    work = assistants.prepare(store, name, "manual", START, end, text=text)
    Queue(store).enqueue(work)
    return cast("dict", assistants.execute(store, work.id, runner=lambda *_: output))


def stored(store: Store, source: str = "assistant") -> list[Record]:
    return [records.load(store, file) for file in records.files(store, source)]


def test_configuration_validates_assistants() -> None:
    assert Config.model_validate({"version": 8, "name": "x", "assistants": None}).assistants == {}
    default = Assistant.model_validate({"prompt": "prompts/a.md", "command": ["agy", "-p={{prompt}}"]})
    assert (default.schedules, default.timeout, default.max_bytes, default.text) == ({}, 900, 1 << 20, None)
    for path in ("notes/a.md", "prompts/../a.md", "prompts/a.txt"):
        with pytest.raises(ValidationError):
            Assistant(prompt=path, command=["agy"])
    with pytest.raises(ValidationError, match="pattern"):
        Assistant(prompt="prompts/a.md", command=["agy"], text="response")
    with pytest.raises(ValidationError, match="unknown placeholder"):
        Program(command=["echo", "{{prompt}}"])
    for kind in ("sensors", "routines"):
        with pytest.raises(ValidationError, match="program names must be distinct"):
            Config.model_validate(
                {
                    "version": 8,
                    "name": "x",
                    kind: {"assistant": {"command": ["echo"]}},
                    "assistants": {"assistant": {"command": ["echo"], "prompt": "prompts/a.md"}},
                }
            )


def test_expansion_and_record_preserve_harness_contract(configured: Store) -> None:
    work = assistants.prepare(configured, "assistant", "schedule", START, END, slot="schedule:review")
    queue = Queue(configured)
    queue.enqueue(work)
    calls = []

    def runner(argv, _program, _store, _name, stdin):
        calls.append((argv, stdin))
        return REPLY

    result = cast("dict", assistants.execute(configured, work.id, runner=runner))
    argv, stdin = calls[0]
    assert stdin == b""
    assert argv[:2] == ["agent", "--print"]
    assert f"Window {START} to {END} in {configured.root}" in argv[2]
    assert "Keep {{prompt}} and {{other}}." in argv[2]
    record = stored(configured)[0]
    assert (record.title, record.text, record.time) == ("Two replies owed", ANSWER, END)
    assert record.attributes["output"] == {"usage": {"total_tokens": 42}}
    assert record.attributes["trigger"] == "schedule"
    assert record.attributes["window"] == {"start": START, "end": END}
    assert state(configured, ASSISTANTS)["assistant"]["runs"] == 1
    assert cast("dict", read([configured], result["record"])["record"])["title"] == "Two replies owed"


def test_direct_and_preview_runs_leave_named_schedule_progress_alone(configured: Store, agent: Path) -> None:
    queue = Queue(configured)
    reply = run_assistant(configured, "piped", text="Can you check my inbox?")
    assert reply["ok"]
    assert cast("list[dict]", reply["assistants"])[0]["text"] == "Answer: done"
    assert "Owner: Can you check my inbox?\n" in agent.with_suffix(".stdin").read_text()
    assert stored(configured, "piped")[0].attributes["trigger"] == "manual"
    assert queue.progress("schedule:assistant:schedule:review") == {}
    preview = run_assistant(configured, "piped", text="again", dry_run=True)
    assert preview["assistants"] == [{"assistant": "piped", "text": "Answer: done", "status": "ran"}]
    assert len(stored(configured, "piped")) == 1
    assert state(configured, ASSISTANTS)["piped"]["runs"] == 1


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"not json", "must print one JSON object when text is set"),
        (b"[1]", "must print one JSON object when text is set"),
        (b'{"result":"x"}', "reply has no text string"),
        (b"\xff", "must print UTF-8"),
    ],
)
def test_invalid_reply_keeps_raw_answer_without_publishing(configured: Store, output: bytes, message: str) -> None:
    with pytest.raises(Error, match=message):
        ask(configured, output=output)
    assert stored(configured) == []
    queue = Queue(configured)
    job = cast("list[dict]", queue.report()["jobs"])[0]
    assert job["state"] == "failed"
    assert queue.get(job["id"])["raw"] == output


def test_json_pointer_and_plain_reply_contracts() -> None:
    assert parse_reply(b'{"a/b":{"~x":" answer "},"usage":1}', "/a~1b/~0x")[0] == "answer"
    assert parse_reply(b" Plain text \n", None) == ("Plain text", {})
    assert parse_reply(b'{"answers":["Answer","context"]}', "/answers/0") == (
        "Answer",
        {"answers": [None, "context"]},
    )
    assert reply_title("personal", "# Summary:\tTwo replies") == "Summary: Two replies"
    assert reply_title("personal", "\n\n") == "personal run without a reply"
    assert len(reply_title("personal", "x" * 500)) == 120


def test_input_is_expanded_once_and_arguments_are_bounded(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    work = assistants.prepare(configured, "assistant", "manual", START, END, text="literal {{brain}}")
    argv, _ = assistants.expanded(configured, work)
    assert "Owner: literal {{brain}}" in argv[-1]
    work.text = "a\x00b"
    with pytest.raises(Error, match="NUL"):
        assistants.expanded(configured, work)
    work.text = "x" * 120_000
    with pytest.raises(Error, match="exceeds 120000"):
        assistants.expanded(configured, work)
    work.text = ""
    monkeypatch.setattr(assistants, "MAX_ARGUMENT", 64)
    with pytest.raises(Error, match="128 KiB"):
        assistants.expanded(configured, work)


def test_unreadable_prompt_and_disabled_assistant_do_not_launch(configured: Store) -> None:
    for name in ("paused", "absent"):
        with pytest.raises(Error):
            assistants.prepare(configured, name, "manual", START, END)
    configured.write("prompts/assistant.md", b"\xff")
    with pytest.raises(Error, match="UTF-8"):
        assistants.prepare(configured, "assistant", "manual", START, END)


def test_update_keeps_assistants_outside_collection_lock(configured: Store) -> None:
    at = datetime.now(UTC)
    with collecting(configured, ".update"):
        result = update(configured, now=at, runner=lambda *_: REPLY, kinds=frozenset({"assistant"}))
    assert result["ok"]
    assert cast("list[dict]", result["assistants"])[0]["status"] == "ran"
    assert (result["sensors"], result["routines"]) == ([], [])
    with collecting(configured, ".dispatch"):
        busy = update(
            configured, runner=lambda *_: pytest.fail("dispatcher already active"), kinds=frozenset({"assistant"})
        )
    assert all(item["status"] == "skipped" for item in cast("list[dict]", busy["assistants"]))
    preview = update(configured, now=at + timedelta(hours=2), dry_run=True)
    assert cast("list[dict]", preview["assistants"])[0]["status"] == "due"


def test_assistant_records_are_retrievable_but_lower_priority(configured: Store) -> None:
    result = ask(configured)
    assert pages.low(configured) == ["assistant", "paused", "piped"]
    page = read([configured], "memories/assistant")
    assert cast("list[dict]", page["items"])[0]["ref"] == result["record"]


def test_status_and_watch_show_assistant_schedules_and_records(configured: Store) -> None:
    result = update(configured, runner=lambda *_: REPLY, kinds=frozenset({"assistant"}))
    brain = cast("list[dict]", report([configured])["brains"])[0]
    assert brain["assistants"]["assistant"]["schedules"]["review"]["freshness"] == "fresh"
    assert brain["assistants"]["assistant"]["record"] == cast("list[dict]", result["assistants"])[0]["record"]
    assert brain["assistants"]["piped"]["freshness"] == "manual"
    assert brain["assistants"]["paused"]["state"] == "disabled"
    rows = {row.name: row for row in snapshot(configured)}
    assert rows["assistant"].kind == "assistant"
    assert rows["assistant"].runs == 1
    lane = Job(configured, (), (), ("assistant",), kinds=frozenset({"assistant"}))
    assert lane.argv[-4:] == ["--assistant", "assistant", "--kind", "assistant"]


def test_cli_runs_assistant_with_piped_input(configured: Store, agent: Path) -> None:
    result = CliRunner().invoke(app, ["run", "piped", "--stdin", "--brain", str(configured.root)], input=b"Hello")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["assistants"][0]["text"] == "Answer: done"
    assert "Owner: Hello\n" in agent.with_suffix(".stdin").read_text()
    extra = CliRunner().invoke(app, ["run", "piped", "extra", "--brain", str(configured.root)])
    assert extra.exit_code == 2
    assert "assistant takes no arguments" in extra.output
    binary = CliRunner().invoke(app, ["run", "piped", "--stdin", "--brain", str(configured.root)], input=b"\xff")
    assert binary.exit_code == 1
    assert "UTF-8" in str(binary.exception)


def test_validation_checks_prompt_files_and_record_refs(configured: Store) -> None:
    result = ask(configured)
    configured.write(
        "concepts/assistant.md",
        (
            "---\ntype: concept\nstatus: draft\n---\n\n# Assistant\n\n"
            f"See [run]({result['record']}), [missing](assistant:absent), [history](../memories/assistant).\n"
        ).encode(),
    )
    configured.root.joinpath("prompts/assistant.md").unlink()
    errors = [item["error"] for item in cast("list[dict]", validate(configured)["problems"])]
    assert "assistants.assistant.prompt: prompts/assistant.md is not a regular file" in errors
    assert "missing record assistant:absent" in errors
    assert not any(result["record"] in error for error in errors)


def test_retention_keeps_foreign_records_and_does_not_fail_saved_runs(
    configured: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured.write("bf.yaml", CONFIG.replace(b"    text: /response\n", b"    text: /response\n    keep: 1\n"))
    configured.write("memories/assistant/stray.json", b"not a record")
    records.upsert(configured, "assistant", [Record(id="legacy", title="Kept")], snapshot=False)
    for index in range(3):
        ask(configured, end=(NOW + timedelta(minutes=index)).isoformat())
    names = [
        records.load(configured, path).id
        for path in records.files(configured, "assistant")
        if not path.endswith("stray.json")
    ]
    assert len(names) == 2
    assert "legacy" in names
    assert configured.read("memories/assistant/stray.json") == b"not a record"
    monkeypatch.setattr(records, "prune", lambda *_: (_ for _ in ()).throw(Error("unreadable old run")))
    result = ask(configured, end=(NOW + timedelta(minutes=4)).isoformat())
    assert result["record"]
