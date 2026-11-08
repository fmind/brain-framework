"""The assistant harness keeps prompt expansion, execution, recording and retrieval boundaries."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue, ValidationError
from typer.testing import CliRunner

from bf import assistants, collect, pages, records
from bf.cli import app
from bf.collect import parse_reply, reply_title, structured_reply
from bf.health import report
from bf.history import ASSISTANTS, log_path, state
from bf.messages import Message
from bf.models import Assistant, AssistantSchedule, Config, Error, Invocation, Program, Record
from bf.retrieve import read
from bf.runtime import Queue
from bf.schemas import document
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
    reply: {path: /response}
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
REPLY = json.dumps({"response": {"say": ANSWER + "\n", "wake": None}, "usage": {"total_tokens": 42}}).encode()


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
    assert (default.schedules, default.timeout, default.max_bytes, default.reply) == ({}, 900, 1 << 20, None)
    for path in ("notes/a.md", "prompts/../a.md", "prompts/a.txt"):
        with pytest.raises(ValidationError):
            Assistant(prompt=path, command=["agy"])
    with pytest.raises(ValidationError, match="pattern"):
        Assistant.model_validate({"prompt": "prompts/a.md", "command": ["agy"], "reply": {"path": "response"}})
    with pytest.raises(ValidationError, match="wake requires reply"):
        Assistant.model_validate({"prompt": "prompts/a.md", "command": ["agy"], "wake": {}})
    # Only an opening {{ starts a placeholder: inline JSON may end in }}.
    assert (
        Assistant(prompt="prompts/a.md", command=["agy", '{"a":{"b":1}}', "{{schema}}"]).command[1] == '{"a":{"b":1}}'
    )
    with pytest.raises(ValidationError, match="unknown placeholder"):
        Assistant(prompt="prompts/a.md", command=["agy", "{{schema_file}}"])
    with pytest.raises(ValidationError, match="unknown placeholder; use brain, home, start, end or a whole-argument"):
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


@pytest.mark.parametrize(
    ("schedule", "reason"),
    [
        ({}, "schedule requires every or at"),
        ({"lookback": 5}, "schedule requires every or at"),
        ({"at": "09:00", "timezone": "Mars/Base"}, "invalid IANA timezone"),
        ({"every": 60, "days": ["mon", "mon"]}, "days must be distinct"),
    ],
)
def test_a_schedule_needs_every_or_at_a_known_timezone_and_distinct_days(schedule: dict, reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        AssistantSchedule.model_validate(schedule)


def test_days_filter_an_interval_schedule_and_every_beside_at_never_repeats_it() -> None:
    sunday = datetime(2026, 9, 27, 9, tzinfo=UTC)
    weekdays = AssistantSchedule.model_validate({"every": 3600, "days": ["mon"], "timezone": "Europe/Paris"})
    # An interval due on Sunday moves to Monday's local midnight.
    assert weekdays.next_due(sunday, "2026-09-27T08:00:00.000000Z") == datetime(2026, 9, 27, 22, tzinfo=UTC)
    assert weekdays.next_due(sunday + timedelta(days=1), "2026-09-28T08:00:00.000000Z") == datetime(
        2026, 9, 28, 9, tzinfo=UTC
    )
    # With at, every is only the overdue grace: the next run is the next day's slot, not an hour later.
    daily = AssistantSchedule.model_validate({"every": 3600, "at": "08:00", "timezone": "UTC"})
    assert daily.next_due(sunday, "2026-09-27T08:00:05.000000Z") == datetime(2026, 9, 28, 8, tzinfo=UTC)


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
        (b"not json", "must print one JSON object when reply is set"),
        (b"[1]", "must print one JSON object when reply is set"),
        (b"\xff", "must print one JSON object when reply is set"),
    ],
)
def test_invalid_reply_keeps_raw_answer_without_publishing(configured: Store, output: bytes, message: str) -> None:
    with pytest.raises(Error, match=message):
        ask(configured, output=output)
    assert stored(configured) == []
    queue = Queue(configured)
    job = cast("list[dict]", queue.report()["jobs"])[0]
    assert job["state"] == "uncertain"
    assert not queue.get(job["id"])["raw"]


def test_json_pointer_and_plain_reply_contracts() -> None:
    assert structured_reply(b'{"a/b":{"~x":{"say":"hi"}},"usage":1}', "/a~1b/~0x") == (
        {"say": "hi"},
        {"a/b": {}, "usage": 1},
    )
    assert structured_reply(b'{"answers":[{"say":null},"context"]}', "/answers/0") == (
        {"say": None},
        {"answers": [None, "context"]},
    )
    assert structured_reply(b'{"say":"root"}', "") == ({"say": "root"}, {})
    assert parse_reply(b" Plain text \n") == "Plain text"
    with pytest.raises(Error, match="must print UTF-8"):
        parse_reply(b"\xff")
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
    configured.write(
        "bf.yaml", CONFIG.replace(b"    reply: {path: /response}\n", b"    reply: {path: /response}\n    keep: 1\n")
    )
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
    monkeypatch.setattr(records, "prune", lambda *_, **__: (_ for _ in ()).throw(Error("unreadable old run")))
    result = ask(configured, end=(NOW + timedelta(minutes=4)).isoformat())
    assert result["record"]
    # The run is recorded: the pruning failure is logged and never fails it.
    assert "kept older runs: unreadable old run" in configured.read(log_path("assistant")).decode()


def test_keep_never_removes_the_run_it_just_recorded(configured: Store) -> None:
    configured.write(
        "bf.yaml", CONFIG.replace(b"    reply: {path: /response}\n", b"    reply: {path: /response}\n    keep: 2\n", 1)
    )
    # Two runs recorded while the clock ran a day ahead.
    ahead = [
        Record(id=f"20260926T0{hour}0000Z-0000000{hour}", title="Ahead", time=f"2026-09-26T0{hour}:00:00Z")
        for hour in (1, 2)
    ]
    records.upsert(configured, "assistant", ahead, snapshot=False)
    result = ask(configured)
    # Before, its earlier time made the run's own record the oldest: it was removed while the reply named it.
    kept = sorted(str(record.id) for record in stored(configured))
    assert kept == sorted([result["record"].removeprefix("assistant:"), "20260926T020000Z-00000002"])


def test_wall_clock_schedules_context_schedule_silent_noop_and_idempotent_retry(configured: Store) -> None:
    configured.write(
        "bf.yaml",
        b"version: 8\nname: fixture\nassistants:\n"
        b"  brief:\n"
        b"    prompt: prompts/brief.md\n"
        b"    command: [agent]\n"
        b"    reply: {path: /response}\n"
        b"    idempotent: true\n"
        b"    schedules:\n"
        b"      morning:\n"
        b'        at: "08:00"\n'
        b"        timezone: UTC\n"
        b"        days: [mon, tue, wed, thu, fri]\n",
    )
    configured.write("prompts/brief.md", b"Context: {{context}}\n")
    # Sunday at 09:00 UTC is not in days; Monday at 07:30 UTC is before 08:00; Monday at 08:05 UTC is due.
    sunday = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    monday_early = datetime(2026, 8, 31, 7, 30, tzinfo=UTC)
    monday_due = datetime(2026, 8, 31, 8, 5, tzinfo=UTC)
    assert assistants.due_schedules(configured, {"brief"}, sunday) == []
    assert assistants.due_schedules(configured, {"brief"}, monday_early) == []
    due_list = assistants.due_schedules(configured, {"brief"}, monday_due)
    assert len(due_list) == 1
    assert due_list[0][1] == "schedule:morning"
    keys, _ = assistants.schedule(configured, {"brief"}, monday_due)
    job_id = keys[0]
    work = Queue(configured).work(job_id)
    _, stdin = assistants.expanded(configured, work)
    assert '"schedule":"morning"' in stdin.decode()
    # A pre-answer harness failure on an idempotent assistant retries (pending) instead of wedging in uncertain.
    with pytest.raises(Error, match="network down"):
        assistants.execute(configured, job_id, runner=lambda *_: (_ for _ in ()).throw(Error("network down")))
    assert Queue(configured).get(job_id)["state"] == "pending"
    # A silent no-op scheduled reply (no message and no metadata) completes and advances the checkpoint without writing a record.
    noop = assistants.execute(configured, job_id, runner=lambda *_: b'{"response": {"say": null, "wake": null}}')
    assert "record" not in noop
    assert records.files(configured, "brief") == []
    assert Queue(configured).progress("schedule:brief:schedule:morning")["end"] == "2026-08-31T08:05:00.000000Z"


TOLD = b"""version: 8
name: fixture
channels:
  chat:
    send: {command: [send-chat]}
    stream: {command: [receive-chat]}
assistants:
  told:
    prompt: prompts/assistant.md
    command: [told]
    inputs:
      - channel: {name: chat, mode: stream}
    schedules:
      review: {every: 1800}
"""


def test_harness_learns_its_trigger_schedule_and_window(
    configured: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured.write("bf.yaml", TOLD)
    folder = tmp_path / "told-bin"
    folder.mkdir()
    script = folder / "told"
    script.write_text(
        "#!/bin/sh\ncat > /dev/null\n"
        'printf "trigger=%s schedule=%s start=%s end=%s\\n" "$BF_TRIGGER" "$BF_SCHEDULE" "$BF_START" "$BF_END"\n'
    )
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{folder}:/usr/bin:/bin")
    # Values inherited from a calling program never reach a run that does not set them.
    monkeypatch.setenv("BF_SCHEDULE", "inherited")
    monkeypatch.setenv("BF_TRIGGER", "inherited")
    queue = Queue(configured)
    keys, failures = assistants.schedule(configured, {"told"}, datetime.now(UTC))
    assert failures == []
    work = queue.work(keys[0])
    scheduled = cast("dict", assistants.execute(configured, keys[0]))["text"]
    assert scheduled == f"trigger=schedule schedule=review start={work.start} end={work.end}"
    message = Message(id="m-1", sender="owner", conversation="c-1", time=START, text="Hello")
    assistants.accept(configured, "chat", [message], {"told"})
    key = queue.next(True, {"told"})
    assert key is not None
    work = queue.work(key)
    answered = cast("dict", assistants.execute(configured, key))["text"]
    assert answered == f"trigger=input schedule= start={START} end={work.end}"
    for dry_run in (False, True):
        reply = run_assistant(configured, "told", dry_run=dry_run)
        text = cast("list[dict]", reply["assistants"])[0]["text"]
        assert re.fullmatch(r"trigger=manual schedule= start=\S+Z end=\S+Z", text), text


CHECKED = b"""version: 8
name: fixture
assistants:
  checked:
    prompt: prompts/assistant.md
    command: [agent, --json-schema, "{{schema}}"]
    reply: {path: /structured_output}
    wake: {min: 600, max: 3600}
  retried:
    prompt: prompts/assistant.md
    command: [agent]
    reply: {path: /structured_output}
    idempotent: true
"""


def test_structured_reply_records_its_message_and_metadata(configured: Store) -> None:
    configured.write("bf.yaml", CHECKED)
    calls: list[list[str]] = []

    def runner(argv, *_):
        calls.append(argv)
        return b'{"structured_output":{"say":"Done","wake":null},"status":"SUCCESS"}'

    work = assistants.prepare(configured, "checked", "manual", START, END)
    Queue(configured).enqueue(work)
    result = cast("dict", assistants.execute(configured, work.id, runner=runner))
    assert result["text"] == "Done"
    # {{schema}} passes the published reply schema, exactly as bf schema prints it.
    assert json.loads(calls[0][2]) == {
        key: value for key, value in document("assistant-reply").items() if key != "$schema"
    }
    record = stored(configured, "checked")[0]
    assert record.attributes["output"] == {"status": "SUCCESS"}
    assert "wake" not in record.attributes


@pytest.mark.parametrize(
    "output",
    [
        b'{"status":"RESOURCE_EXHAUSTED","response":"secret quota"}',
        b'{"structured_output":"secret"}',
        b'{"structured_output":{"say":"secret"}}',
        b'{"structured_output":{"say":"secret","wake":null,"extra":"secret"}}',
        b'{"structured_output":{"say":1,"wake":null}}',
        b'{"structured_output":{"say":"secret","wake":{"in":0,"reason":"secret"}}}',
        b'{"structured_output":{"say":"secret","wake":{"in":"60","reason":"secret"}}}',
        b'{"structured_output":{"say":"secret","wake":{"in":60,"reason":"' + b"x" * 281 + b'"}}}',
    ],
)
def test_invalid_structured_reply_fails_the_attempt_without_quoting_it(configured: Store, output: bytes) -> None:
    configured.write("bf.yaml", CHECKED)
    message = "assistant reply at /structured_output is missing or does not match bf schema --kind assistant-reply"
    with pytest.raises(Error, match=re.escape(message)) as raised:
        ask(configured, name="checked", output=output)
    assert "secret" not in str(raised.value)
    assert str(raised.value).endswith(f"see {log_path('checked')}")
    queue = Queue(configured)
    job = cast("list[dict]", queue.report()["jobs"])[0]
    # Like a nonzero exit: the harness may have acted, so the job waits for inspection and saves no answer.
    assert job["state"] == "uncertain"
    assert "secret" not in job["error"]
    assert not queue.get(job["id"])["raw"]
    assert stored(configured, "checked") == []
    assert "secret" not in configured.read(log_path("checked")).decode()


@pytest.mark.parametrize(
    ("pointer", "output"),
    [("/result/say", b'{"result":"secret"}'), ("/result/01", b'{"result":["secret"]}')],
    ids=["into-a-string", "non-decimal-index"],
)
def test_an_untraversable_reply_path_reports_the_reply_error(configured: Store, pointer: str, output: bytes) -> None:
    configured.write("bf.yaml", CHECKED.replace(b"/structured_output}\n    wake", f"{pointer}}}\n    wake".encode()))
    message = f"assistant reply at {pointer} is missing or does not match bf schema --kind assistant-reply"
    with pytest.raises(Error, match=re.escape(message)) as raised:
        ask(configured, name="checked", output=output)
    assert "secret" not in str(raised.value)
    assert str(raised.value).endswith(f"see {log_path('checked')}")


@pytest.mark.parametrize(
    "output",
    [
        b'{"structured_output":{"say":"secret \\ud83d","wake":null}}',
        b'{"structured_output":{"say":"secret","wake":null},"status":"secret \\ud83d"}',
    ],
)
def test_structured_reply_a_record_cannot_hold_fails_the_attempt(configured: Store, output: bytes) -> None:
    configured.write("bf.yaml", CHECKED)
    with pytest.raises(Error, match="cannot become a record") as raised:
        ask(configured, name="checked", output=output)
    assert "secret" not in str(raised.value)
    queue = Queue(configured)
    job = cast("list[dict]", queue.report()["jobs"])[0]
    # Like an invalid reply: the harness may have acted, but no answer is saved that could never be recorded.
    assert job["state"] == "uncertain"
    assert not queue.get(job["id"])["raw"]
    assert stored(configured, "checked") == []


def test_oversized_structured_reply_metadata_retries_an_idempotent_assistant(configured: Store) -> None:
    configured.write("bf.yaml", CHECKED.replace(b"idempotent: true", b"idempotent: true\n    max_bytes: 4194304"))
    output = b'{"structured_output":{"say":"Done","wake":null},"result":"' + b"x" * (3 << 20) + b'"}'
    with pytest.raises(Error, match="more than 2 MiB outside its message"):
        ask(configured, name="retried", output=output)
    queue = Queue(configured)
    job = cast("list[dict]", queue.report()["jobs"])[0]
    assert job["state"] == "pending"
    assert not queue.get(job["id"])["raw"]


def test_invalid_structured_reply_retries_an_idempotent_assistant_and_fails_dry_runs(configured: Store) -> None:
    configured.write("bf.yaml", CHECKED)
    with pytest.raises(Error, match="does not match"):
        ask(configured, name="retried", output=b'{"ok":false,"response":"secret"}')
    job = cast("list[dict]", Queue(configured).report()["jobs"])[0]
    assert job["state"] == "pending"
    preview = assistants.manual(configured, "checked", dry_run=True, runner=lambda *_: b'{"response":"secret"}')
    assert preview["assistants"] == [
        {
            "assistant": "checked",
            "status": "failed",
            "error": "assistant reply at /structured_output is missing or does not match bf schema --kind "
            f"assistant-reply; see {log_path('checked')}",
        }
    ]


def test_idempotent_run_kinds_retry_only_runs_of_those_kinds(configured: Store) -> None:
    configured.write("bf.yaml", CHECKED.replace(b"idempotent: true", b"idempotent: [schedule, wake]"))
    queue = Queue(configured)

    def failed(kind: Invocation, *, interrupted: bool = False) -> str:
        work = assistants.prepare(configured, "retried", kind, START, END, slot=kind)
        queue.enqueue(work)
        if interrupted:
            assert queue.claim(work.id)
            queue.interrupted(job=work.id)
        else:
            with pytest.raises(Error, match="does not match"):
                assistants.execute(configured, work.id, runner=lambda *_: b'{"response":"secret"}')
        return str(queue.get(work.id)["state"])

    # A listed kind retries by itself; any other run, such as an answer to a message, may have acted on its request.
    assert failed("schedule") == "pending"
    assert failed("manual") == "uncertain"
    assert failed("wake", interrupted=True) == "pending"
    assert failed("input", interrupted=True) == "uncertain"
    assert Assistant.model_validate({"prompt": "prompts/a.md", "command": ["a"], "idempotent": []}).idempotent == []
    with pytest.raises(ValidationError, match="idempotent"):
        Assistant.model_validate({"prompt": "prompts/a.md", "command": ["a"], "idempotent": ["scheduled"]})


def wake_reply(seconds: int, reason: str = "Recheck CI", say: str | None = None) -> bytes:
    return json.dumps({"structured_output": {"say": say, "wake": {"in": seconds, "reason": reason}}}).encode()


def test_a_wake_is_clamped_runs_once_with_its_reason_and_is_replaced_by_a_newer_one(configured: Store) -> None:
    configured.write("bf.yaml", CHECKED)
    configured.write("prompts/assistant.md", b"Why: {{context}}\n")
    queue = Queue(configured)
    before = datetime.now(UTC)
    ask(configured, name="checked", output=wake_reply(5, say="Waiting for CI"))
    wake = queue.progress("wake:checked")
    # Five seconds is below the 600-second minimum.
    assert datetime.fromisoformat(str(wake["due"])) >= before + timedelta(seconds=600)
    assert stored(configured, "checked")[0].attributes["wake"] == {"due": wake["due"], "reason": "Recheck CI"}
    # The listing names when, never the model's reason.
    assert queue.report()["wakes"] == [{"assistant": "checked", "due": wake["due"], "job": None}]
    assert assistants.due_schedules(configured, {"checked"}, before + timedelta(seconds=300)) == []
    # Derived from the stored wake, so a slow ask() cannot push the due time past it.
    later = datetime.fromisoformat(str(wake["due"])) + timedelta(seconds=1)
    [due] = assistants.due_schedules(configured, {"checked"}, later)
    assert due[:2] == ("checked", "wake")
    [key], failures = assistants.schedule(configured, {"checked"}, later)
    assert failures == []
    # Taken by its job, the wake never starts twice.
    assert assistants.due_schedules(configured, {"checked"}, later) == []
    seen: list[str] = []

    def runner(_argv, _program, _store, _name, stdin):
        seen.append(stdin.decode())
        return wake_reply(99_999, reason="Again tomorrow")

    assistants.execute(configured, key, runner=runner)
    assert '"kind":"wake"' in seen[0]
    assert '"wake":{"reason":"Recheck CI"}' in seen[0]
    # Its own new wake replaces the one it consumed, clamped to the 3600-second maximum.
    renewed = queue.progress("wake:checked")
    assert renewed["reason"] == "Again tomorrow"
    assert datetime.fromisoformat(str(renewed["due"])) <= datetime.now(UTC) + timedelta(seconds=3600)
    assert renewed["job"] == ""


def test_a_wake_needs_bounds_and_a_failed_run_asks_for_none(configured: Store) -> None:
    configured.write("bf.yaml", CHECKED)
    queue = Queue(configured)
    ask(configured, name="retried", output=wake_reply(900, say="Noted"))
    # Without wake bounds, the request stays in the record's output only.
    assert queue.progress("wake:retried") == {}
    with pytest.raises(Error):
        ask(configured, name="checked", output=b'{"structured_output":{"say":"x","wake":{"in":900}}}')
    assert queue.progress("wake:checked") == {}


def test_harness_learns_it_runs_for_a_wake(configured: Store) -> None:
    configured.write("bf.yaml", CHECKED)
    queue = Queue(configured)
    queue.set_progress("wake:checked", {"due": START, "reason": "Recheck", "set": START, "job": ""})
    [key], _ = assistants.schedule(configured, {"checked"}, NOW)
    told: list[str] = []

    def runner(*_: object) -> bytes:
        # What run would pass the harness as BF_TRIGGER.
        told.append(collect._EXPOSED.get()["BF_TRIGGER"])  # noqa: SLF001
        return b'{"structured_output":{"say":null,"wake":null}}'

    assistants.execute(configured, key, runner=runner)
    assert told == ["wake"]
    # A silent wake run completes, records nothing and clears the wake it took.
    assert queue.get(key)["state"] == "complete"
    assert queue.progress("wake:checked") == {}
    assert stored(configured, "checked") == []


GATED = b"""version: 8
name: fixture
assistants:
  dev:
    prompt: prompts/assistant.md
    command: [agent]
    schedules:
      events:
        every: 600
        gate: {command: [routines/gate.py, "{{start}}"]}
"""


def gated(store: Store, gate: Callable[[], bytes], answer: bytes = b"Answer") -> tuple[str, list[str], dict]:
    """Queue the gated schedule and execute it once; returns its key, the programs started and the result."""
    store.write("bf.yaml", GATED)
    store.write("prompts/assistant.md", b"Events: {{context}}\n")
    [key], _ = assistants.schedule(store, {"dev"}, NOW)
    return key, *run_gated(store, key, gate, answer)


def run_gated(store: Store, key: str, gate: Callable[[], bytes], answer: bytes = b"Answer") -> tuple[list[str], dict]:
    started: list[str] = []
    prompts: list[str] = []

    def runner(argv, _program, _store, name, stdin):
        started.append(name)
        if name == "assistant.dev.gate.events":
            assert argv == ["routines/gate.py", Queue(store).work(key).start]
            return gate()
        prompts.append(stdin.decode())
        return answer

    try:
        result = cast("dict", assistants.execute(store, key, runner=runner))
    except Error as error:
        result = {"error": str(error)}
    return started, {**result, "prompts": prompts}


def test_a_gate_without_events_skips_the_harness_and_advances_the_schedule(configured: Store) -> None:
    key, started, result = gated(configured, lambda: b'{"events":[]}')
    assert started == ["assistant.dev.gate.events"]
    assert result["text"] == ""
    assert Queue(configured).get(key)["state"] == "complete"
    assert Queue(configured).progress("schedule:dev:schedule:events")["end"] == "2026-09-25T08:00:00.000000Z"
    assert stored(configured, "dev") == []


def test_gate_events_reach_the_prompt_and_are_reused_on_retry(configured: Store) -> None:
    events = b'{"events":[{"key":"ci:main","text":"CI failed","ref":"memories/ci#1"}]}'
    key, started, result = gated(configured, lambda: events, answer=b"CI failed on main.")
    assert started == ["assistant.dev.gate.events", "dev"]
    assert '"events":[{"key":"ci:main","ref":"memories/ci#1","text":"CI failed"}]' in result["prompts"][0]
    assert result["text"] == "CI failed on main."
    queue = Queue(configured)
    [key], _ = assistants.schedule(configured, {"dev"}, datetime.now(UTC) + timedelta(hours=1))

    def crashed() -> bytes:
        raise Error("harness crashed")

    started: list[str] = []

    def runner(_argv, _program, _store, name, _stdin):
        started.append(name)
        return events if name.startswith("assistant.") else crashed()

    with pytest.raises(Error, match="harness crashed"):
        assistants.execute(configured, key, runner=runner)
    assert queue.get(key)["state"] == "uncertain"
    # The retry reuses the events the gate already reported: a gate that remembers what it sent loses nothing.
    queue.retry(key)
    started, result = run_gated(configured, key, crashed, answer=b"Retried")
    assert started == ["dev"]
    assert '"key":"ci:main"' in result["prompts"][0]
    assert queue.progress(f"gate:{key}") == {}


@pytest.mark.parametrize(
    "output",
    [b"secret", b'{"events":"secret"}', b'{"events":[{"text":"secret"}]}', b'{"events":[], "secret": 1}'],
)
def test_a_failed_gate_retries_by_itself_without_quoting_its_output(configured: Store, output: bytes) -> None:
    key, started, result = gated(configured, lambda: output)
    assert started == ["assistant.dev.gate.events"]
    assert result["error"] == (
        'gate of schedule events must print {"events": [...]} with at most 64 events; '
        "see logs/assistant.dev.gate.events.log"
    )
    # No harness ran, so nothing acted: the job retries with backoff instead of waiting for review.
    job = Queue(configured).get(key)
    assert job["state"] == "pending"
    assert "secret" not in str(job["error"])
    assert Queue(configured).progress(f"retry:{key}")["attempts"] == 1


def test_oversized_gate_events_fail_the_gate_instead_of_every_prompt(configured: Store) -> None:
    # 20 events of 4,000 characters fit the gate's 256 KiB output but not one prompt: saved for retries, they would
    # fail each attempt the same way and stall the schedule.
    events = {"events": [{"key": f"k{index}", "text": "x" * 4000} for index in range(20)]}
    key, started, result = gated(configured, lambda: json.dumps(events).encode())
    assert started == ["assistant.dev.gate.events"]
    assert result["error"] == (
        "gate of schedule events printed more than 65536 bytes of events; shorten their text or send fewer"
    )
    queue = Queue(configured)
    assert queue.get(key)["state"] == "pending"
    assert queue.progress(f"retry:{key}")["attempts"] == 1
    assert queue.progress(f"gate:{key}") == {}


@pytest.mark.parametrize("events", [b'{"events":[]}', b'{"events":[{"key":"ci:main"}]}'])
def test_a_job_cancelled_while_its_gate_runs_stays_cancelled(configured: Store, events: bytes) -> None:
    configured.write("bf.yaml", GATED)
    configured.write("prompts/assistant.md", b"Events: {{context}}\n")
    [key], _ = assistants.schedule(configured, {"dev"}, NOW)
    queue = Queue(configured)

    def cancelled() -> bytes:
        queue.retry(key, cancel=True)
        return events

    started, _ = run_gated(configured, key, cancelled)
    assert started == ["assistant.dev.gate.events"]
    assert queue.get(key)["state"] == "cancelled"
    assert queue.progress(f"gate:{key}") == {}


def test_a_wake_replaced_while_it_was_read_is_not_queued(configured: Store) -> None:
    configured.write("bf.yaml", CHECKED)
    queue = Queue(configured)
    queue.set_progress("wake:checked", {"due": START, "reason": "Old", "set": START, "job": ""})
    stale = queue.progress("wake:checked")
    # A run that finished between the read and the enqueue asked for a later wake.
    newer: dict[str, JsonValue] = {"due": END, "reason": "Newer", "set": START, "job": ""}
    queue.set_progress("wake:checked", newer)
    work = assistants.prepare(configured, "checked", "wake", START, END, slot="wake", reason="Old")
    assert not queue.enqueue_wake(work, stale)
    assert queue.progress("wake:checked") == newer
    assert queue.report()["jobs"] == []
    assert queue.enqueue_wake(work, newer)
    assert queue.progress("wake:checked")["job"] == work.id


def test_cancelling_a_wake_run_drops_its_wake(configured: Store) -> None:
    configured.write("bf.yaml", CHECKED)
    queue = Queue(configured)
    queue.set_progress("wake:checked", {"due": START, "reason": "Recheck", "set": START, "job": ""})
    [key], _ = assistants.schedule(configured, {"checked"}, NOW)
    queue.retry(key, cancel=True)
    assert queue.progress("wake:checked") == {}
    assert queue.report()["wakes"] == []


def test_bf_schema_prints_the_assistant_reply_schema() -> None:
    result = CliRunner().invoke(app, ["schema", "--kind", "assistant-reply"])
    assert result.exit_code == 0
    schema = json.loads(result.stdout)
    assert schema["required"] == ["say", "wake"]
    assert schema["additionalProperties"] is False
