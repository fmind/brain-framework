"""Prompts run an agent harness over the brain on a schedule or now, recording each run in memories/NAME/."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import bf.collect as collector
from bf import pages, records
from bf.cli import app
from bf.collect import Runner, due_prompts, prompt
from bf.health import attention, report
from bf.history import PROMPTS, log_path, state
from bf.models import Config, Error, Program, Prompt
from bf.retrieve import read
from bf.storage import Store, collecting
from bf.update import run_prompt, update
from bf.validate import validate
from bf.watch import Job, _prompt_due, snapshot

NOW = datetime(2026, 9, 25, 8, tzinfo=UTC)
START = "2026-09-24T08:00:00.000000Z"
END = "2026-09-25T08:00:00.000000Z"
RECORD = "20260925T080000Z-00000001"
CONFIG = b"""version: 7
name: fixture
prompts:
  assistant:
    prompt: prompts/assistant.md
    command: [agent, --print, "{{prompt}}"]
    text: /response
  piped:
    prompt: prompts/assistant.md
    command: [agent]
    refresh: 0
  paused:
    prompt: prompts/assistant.md
    command: [agent]
    enabled: false
"""
TEMPLATE = (
    b"# Assistant\n\nWindow {{start}} to {{end}} in {{brain}}.\n\nOwner: {{input}}\nKeep {{prompt}} and {{other}}.\n"
)
ANSWER = "## Two replies owed\n\nAnswer the Monday thread."
REPLY = {"response": ANSWER + "\n", "usage": {"total_tokens": 42}}


@pytest.fixture(autouse=True)
def fixed_record_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "uuid4", lambda: UUID(int=1))


@pytest.fixture
def configured(brain: Store) -> Store:
    brain.write("bf.yaml", CONFIG)
    brain.write("prompts/assistant.md", TEMPLATE)
    return brain


def replying(output: bytes, calls: list[tuple[list[str], bytes]] | None = None) -> Runner:
    def runner(argv: list[str], program: Program, _store: Store, _name: str, stdin: bytes) -> bytes:
        assert isinstance(program, Prompt)
        if calls is not None:
            calls.append((argv, stdin))
        return output

    return runner


def failing(_argv: list[str], _program: Program, _store: Store, _name: str, _stdin: bytes) -> bytes:
    raise Error("program exited with status 3")


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A harness on PATH that saves its arguments and input, then answers."""
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "agent"
    seen = tmp_path / "seen"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {seen}.argv\ncat > {seen}.stdin\nprintf "Answer: done\\n"\n')
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{folder}:/usr/bin:/bin")
    return seen


def stored(store: Store, source: str = "assistant") -> list[dict[str, object]]:
    return [records.load(store, file).model_dump(exclude_defaults=True) for file in records.files(store, source)]


def test_configuration_validates_prompts() -> None:
    config = Config.model_validate(json.loads(json.dumps({"version": 7, "name": "x", "prompts": None})))
    assert config.prompts == {}
    default = Prompt.model_validate({"prompt": "prompts/a.md", "command": ["agy", "-p={{prompt}}"]})
    assert (default.refresh, default.timeout, default.max_bytes, default.text) == (1800, 900, 1 << 20, None)
    invalid = [
        ({"prompt": "notes/a.md", "command": ["agy"]}, "normalized Markdown path in prompts/"),
        ({"prompt": "prompts/../a.md", "command": ["agy"]}, "normalized Markdown path in prompts/"),
        ({"prompt": "prompts/a.txt", "command": ["agy"]}, "normalized Markdown path in prompts/"),
        ({"prompt": "prompts/a.md", "command": ["agy", "{{input}}"]}, "use brain, home, start, end or prompt"),
        ({"prompt": "prompts/a.md", "command": ["agy"], "text": "response"}, "JSON Pointer starting with /"),
    ]
    for settings, message in invalid:
        with pytest.raises(ValidationError, match=message):
            Prompt.model_validate(settings)
    # Only a prompt's command takes the expanded prompt.
    with pytest.raises(ValidationError, match="use brain, home, start or end"):
        Program.model_validate({"command": ["echo", "{{prompt}}"]})
    shared = {"command": ["echo"]}
    for kind in ("sensors", "routines"):
        with pytest.raises(ValidationError, match=f"{kind}.assistant and prompts.assistant: program names must be"):
            Config.model_validate(
                {
                    "version": 7,
                    "name": "x",
                    kind: {"assistant": shared},
                    "prompts": {"assistant": {**shared, "prompt": "prompts/a.md"}},
                }
            )


def test_a_scheduled_run_expands_the_prompt_and_records_the_reply(configured: Store) -> None:
    calls: list[tuple[list[str], bytes]] = []
    result = prompt(
        configured,
        "assistant",
        start=START,
        end=END,
        scheduled=True,
        runner=replying(json.dumps(REPLY).encode(), calls),
        clock=lambda: NOW,
    )
    assert result == {"prompt": "assistant", "record": f"assistant:{RECORD}", "text": ANSWER}
    [(argv, stdin)] = calls
    assert argv[:2] == ["agent", "--print"]
    assert stdin == b""
    # One expansion pass: only template placeholders change, and the expanded text replaces {{prompt}}.
    assert argv[2] == (
        f"# Assistant\n\nWindow {START} to {END} in {configured.root}.\n\nOwner: \nKeep {{{{prompt}}}} and {{{{other}}}}.\n"
    )
    [record] = stored(configured)
    attributes = cast("dict[str, object]", record["attributes"])
    assert record["id"] == RECORD
    assert record["title"] == "Two replies owed"
    assert record["text"] == ANSWER
    assert record["time"] == END
    # The reply is the text: the other members of the harness JSON stay as output, without a copy of it.
    assert attributes["output"] == {"usage": {"total_tokens": 42}}
    assert attributes["trigger"] == "schedule"
    assert attributes["window"] == {"start": START, "end": END}
    assert "input" not in attributes
    history = state(configured, PROMPTS)["assistant"]
    assert {
        key: history[key] for key in ("run", "success", "start", "end", "runs", "record", "records", "failures")
    } == {
        "run": END,
        "success": END,
        "start": START,
        "end": END,
        "runs": 1,
        "record": f"assistant:{RECORD}",
        "records": 1,
        "failures": 0,
    }
    # The run is evidence the brain can read like any record.
    found = read([configured], f"assistant:{RECORD}")
    assert cast("dict[str, object]", found["record"])["title"] == "Two replies owed"
    assert cast("dict[str, object]", found["collection"])["state"] == "active"
    # The next scheduled window starts where this one ended.
    assert due_prompts(configured, NOW + timedelta(minutes=31)) == [("assistant", END, "2026-09-25T08:31:00.000000Z")]


def test_a_direct_run_answers_its_input_without_moving_the_schedule(configured: Store, agent: Path) -> None:
    prompt(
        configured,
        "assistant",
        start=START,
        end=END,
        scheduled=True,
        runner=replying(json.dumps(REPLY).encode()),
        clock=lambda: NOW,
    )
    reply = run_prompt(configured, "piped", text="Can you check my inbox?")
    [answer] = cast("list[dict[str, object]]", reply["prompts"])
    assert (reply["ok"], answer["status"], answer["text"]) == (True, "ran", "Answer: done")
    # A direct run records when it actually ran.
    assert re.fullmatch(r"piped:\d{8}T\d{6}Z-00000001", str(answer["record"]))
    # Without {{prompt}} in its command, the expanded prompt is the harness's standard input.
    assert "Owner: Can you check my inbox?\n" in agent.with_suffix(".stdin").read_text()
    assert agent.with_suffix(".argv").read_text() == "\n"
    [record] = stored(configured, "piped")
    attributes = cast("dict[str, object]", record["attributes"])
    assert (attributes["trigger"], attributes["input"]) == ("run", "Can you check my inbox?")
    assert "output" not in attributes
    history = state(configured, PROMPTS)["piped"]
    assert history["runs"] == 1
    assert "success" not in history
    # Answering a message never postpones the scheduled review of another prompt either.
    assert state(configured, PROMPTS)["assistant"]["success"] == END
    preview = run_prompt(configured, "piped", text="again", dry_run=True)
    assert preview["prompts"] == [{"prompt": "piped", "text": "Answer: done", "status": "ran"}]
    assert len(stored(configured, "piped")) == 1
    assert state(configured, PROMPTS)["piped"]["runs"] == 1


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"not json", "must print one JSON object when text is set"),
        (b"[1]", "must print one JSON object when text is set"),
        (b'{"result": "x"}', "prompt reply has no text string at /response"),
        (b"\xff", "must print UTF-8"),
    ],
)
def test_an_unreadable_reply_writes_no_record(configured: Store, output: bytes, message: str) -> None:
    with pytest.raises(Error, match=message):
        prompt(configured, "assistant", start=START, end=END, runner=replying(output), clock=lambda: NOW)
    assert stored(configured) == []
    assert state(configured, PROMPTS)["assistant"]["failures"] == 1


def test_failures_write_no_record_and_back_off(configured: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Error, match=rf"exited with status 3; no record was written; see {log_path('assistant')}"):
        prompt(configured, "assistant", start=START, end=END, scheduled=True, runner=failing, clock=lambda: NOW)
    history = state(configured, PROMPTS)["assistant"]
    assert (history["failures"], "success" in history) == (1, False)
    assert due_prompts(configured, NOW + timedelta(seconds=30)) == []
    assert [item[0] for item in due_prompts(configured, NOW + timedelta(minutes=1))] == ["assistant"]
    assert attention(configured, NOW) == [{"prompt": "assistant", "freshness": "never", "failed": True}]
    with pytest.raises(Error, match="prompt paused is disabled"):
        prompt(configured, "paused", start=START, end=END, runner=failing)
    with pytest.raises(Error, match="unknown prompt absent"):
        prompt(configured, "absent", start=START, end=END, runner=failing)
    monkeypatch.setattr(collector, "MAX_PROMPT", 64)
    with pytest.raises(Error, match="file exceeds 65 bytes"):
        prompt(configured, "assistant", start=START, end=END, runner=failing)
    configured.root.joinpath("prompts/assistant.md").unlink()
    with pytest.raises(Error, match="no record was written"):
        prompt(configured, "assistant", start=START, end=END, runner=failing)
    assert stored(configured) == []


def test_update_runs_prompts_last_and_outside_the_collection_lock(configured: Store) -> None:
    reply = json.dumps(REPLY).encode()
    result = update(configured, now=NOW, runner=replying(reply))
    assert result["ok"], result
    assert [(item["prompt"], item["status"]) for item in cast("list[dict[str, object]]", result["prompts"])] == [
        ("assistant", "ran")
    ]
    assert result["manual"] == ["piped"]
    # A prompt-only cycle neither waits for nor takes the update lock collection cycles hold.
    later = NOW + timedelta(hours=1)
    with collecting(configured, ".update"):
        prompted = update(configured, now=later, runner=replying(reply), kinds=frozenset({"prompt"}))
    assert prompted["ok"], prompted
    assert (prompted["sensors"], prompted["routines"]) == ([], [])
    # A scheduled run still active keeps its window for the next cycle.
    with collecting(configured, "assistant"):
        busy = update(configured, now=later + timedelta(hours=1), runner=replying(reply), kinds=frozenset({"prompt"}))
    assert cast("list[dict[str, object]]", busy["prompts"])[0]["status"] == "skipped"
    assert "index" not in busy
    preview = update(configured, now=later + timedelta(hours=2), dry_run=True)
    assert [item["status"] for item in cast("list[dict[str, object]]", preview["prompts"])] == ["due"]


def test_prompt_records_rank_below_the_evidence_they_summarize(configured: Store) -> None:
    prompt(
        configured, "assistant", start=START, end=END, runner=replying(json.dumps(REPLY).encode()), clock=lambda: NOW
    )
    assert pages.low(configured) == ["assistant", "paused", "piped"]
    page = read([configured], "memories/assistant")
    assert [item["ref"] for item in cast("list[dict[str, object]]", page["items"])] == [f"assistant:{RECORD}"]


def test_status_reports_prompts_and_their_records(configured: Store) -> None:
    prompt(
        configured,
        "assistant",
        start=START,
        end=END,
        scheduled=True,
        runner=replying(json.dumps(REPLY).encode()),
        clock=lambda: NOW,
    )
    [brain] = cast("list[dict[str, object]]", report([configured], now=NOW)["brains"])
    prompts = cast("dict[str, dict[str, object]]", brain["prompts"])
    last = cast("dict[str, object]", prompts["assistant"].pop("last_run"))
    assert prompts["assistant"] == {
        "state": "active",
        "freshness": "fresh",
        "runs": 1,
        "last_success": END,
        "record": f"assistant:{RECORD}",
    }
    assert (last["at"], last["output_bytes"], type(last["elapsed_seconds"])) == (END, len(json.dumps(REPLY)), float)
    assert prompts["piped"]["freshness"] == "manual"
    assert prompts["paused"] == {"state": "disabled", "freshness": "unknown", "runs": 0}
    source = cast("dict[str, dict[str, object]]", brain["sources"])["assistant"]
    assert (source["state"], source["records"], "last_run" in source) == ("active", 1, False)
    assert brain["attention"] == []


def test_cli_runs_a_prompt_with_piped_input(configured: Store, agent: Path) -> None:
    result = CliRunner().invoke(app, ["run", "piped", "--stdin", "--brain", str(configured.root)], input=b"Hello")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["prompts"][0]["text"] == "Answer: done"
    assert "Owner: Hello\n" in agent.with_suffix(".stdin").read_text()
    extra = CliRunner().invoke(app, ["run", "piped", "extra", "--brain", str(configured.root)])
    assert extra.exit_code == 2
    assert "a prompt takes no arguments" in extra.output
    kind = CliRunner().invoke(app, ["update", "--kind", "agent", "--brain", str(configured.root)])
    assert kind.exit_code == 2
    assert "agent is not sensor, routine or prompt" in kind.output
    # Like a routine, a disabled prompt fails before running anything.
    disabled = CliRunner().invoke(app, ["run", "paused", "--brain", str(configured.root)])
    assert disabled.exit_code == 1
    assert "prompt paused is disabled in bf.yaml" in disabled.output + str(disabled.exception)


def test_validate_checks_prompt_files_and_their_record_refs(configured: Store) -> None:
    prompt(
        configured, "assistant", start=START, end=END, runner=replying(json.dumps(REPLY).encode()), clock=lambda: NOW
    )
    configured.write(
        "concepts/assistant.md",
        f"---\ntype: concept\nstatus: draft\n---\n\n# Assistant\n\nSee [the run](assistant:{RECORD}), "
        "[a missing run](assistant:absent) and [its runs](../memories/assistant).\n".encode(),
    )
    configured.root.joinpath("prompts/assistant.md").unlink()
    problems = [problem["error"] for problem in cast("list[dict[str, str]]", validate(configured)["problems"])]
    assert "prompts.assistant.prompt: prompts/assistant.md is not a regular file" in problems
    assert "missing record assistant:absent" in problems
    assert not [problem for problem in problems if RECORD in problem or "memories/assistant" in problem], problems


def test_watch_lists_prompts_and_runs_them_in_their_own_job(configured: Store) -> None:
    rows = {row.name: row for row in snapshot(configured)}
    assert (rows["assistant"].kind, rows["assistant"].runs, rows["assistant"].status) == ("prompt", 0, "never")
    assert _prompt_due(list(rows.values()), datetime.now(UTC))
    prompt(
        configured,
        "assistant",
        start=START,
        end=END,
        scheduled=True,
        runner=replying(json.dumps(REPLY).encode()),
        clock=lambda: datetime.now(UTC),
    )
    rows = {row.name: row for row in snapshot(configured)}
    assert (rows["assistant"].runs, rows["assistant"].record.endswith("Z-00000001")) == (1, True)
    assert not _prompt_due(list(rows.values()), datetime.now(UTC))
    lane = Job(configured, (), (), ("assistant",), kinds=frozenset({"prompt"}))
    assert lane.argv[-4:] == ["--prompt", "assistant", "--kind", "prompt"]
    assert "--kind" not in Job(configured, (), ()).argv
