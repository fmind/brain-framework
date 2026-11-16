"""Tests for `jobs.py` and `watch.py`: schedules, KISS JSON events, hooks, timeouts, channels, servers, and watch."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bf import config, engine, jobs, records, watch
from bf.models import FORMAT, Error, InputError, decode
from bf.storage import Store


def test_jobs_schedules_events_hooks_input_channels_post_and_timeout(tmp_path: Path) -> None:
    root = tmp_path / "orch-brain"
    engine.init_brain(root, name="orch")
    store = Store(root)
    for sub in ("sensors", "channels", "routines", "prompts"):
        (root / sub).mkdir(exist_ok=True)

    (root / "sensors" / "feed.py").write_text(
        "import json, sys\nprint(json.dumps([{'id': 'f1', 'title': 'Feed item', 'text': sys.argv[1]}]))\n"
    )
    (root / "channels" / "mock_chat.py").write_text(
        "import json, sys\n"
        "mode = sys.argv[1]\n"
        "if mode == 'send':\n"
        "    msg = json.loads(sys.stdin.read())\n"
        "    open('channels/sent.jsonl', 'a').write(json.dumps(msg) + '\\n')\n"
        "    print(json.dumps({'id': 'sent-1', 'conversation': msg.get('conversation', 'main')}))\n"
        "elif mode == 'listen':\n"
        "    print(json.dumps({'id': 'in-live', 'text': 'ping from chat', 'direct': True}), flush=True)\n"
        "    import time; time.sleep(0.2)\n"
    )
    (root / "routines" / "fake_harness.py").write_text(
        "import json, pathlib, sys\n"
        "ctx = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "prompt_path = sys.argv[2]\n"
        "stdin_txt = sys.stdin.read()\n"
        "assert oct(pathlib.Path(sys.argv[1]).stat().st_mode & 0o777) == '0o600'\n"
        "assert ctx['stdin'] == '' and ctx['input'] == prompt_path\n"
        "assert stdin_txt.startswith('# ')\n"
        "print(f\"Summary for {ctx['kind']} ({len(ctx['events'])} ev) from {pathlib.Path(prompt_path).name}\")\n"
    )
    (root / "routines" / "emit_events.py").write_text(
        "import json\nprint(json.dumps({'kind': 'custom.ev', 'id': 'e1'}))\n"
    )
    (root / "routines" / "slow.py").write_text("import time\ntime.sleep(30)\n")
    (root / "routines" / "hook_guard.py").write_text(
        "import pathlib, sys\n"
        "pathlib.Path('routines/hook.args').write_text(' '.join(sys.argv[1:]) + '|' + sys.stdin.read())\n"
    )
    (root / "prompts" / "digest.md").write_text("# Digest prompt\nSummarize recent updates.\n")
    (root / "prompts" / "reply.md").write_text("# Reactive prompt\nAnswer direct chat messages.\n")

    store.write(
        "bf.yaml",
        f"version: {FORMAT}\n"
        "name: orch\n"
        "defaults:\n"
        "  timezone: UTC\n"
        "jobs:\n"
        "  feed:\n"
        f"    run: [{sys.executable!r}, sensors/feed.py, '{{{{start}}}}']\n"
        "    ingest:\n"
        "      source: feed\n"
        "      mode: upsert\n"
        "    every: 1h\n"
        "  rebuild:\n"
        "    builtin: build\n"
        "    events:\n"
        "      - kind: records.ingested\n"
        "        source: feed\n"
        "  brief:\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, '{{{{context}}}}', '{{{{input}}}}']\n"
        "    input: prompts/digest.md\n"
        "    at: '08:00'\n"
        "    days: [sun]\n"
        "    channel: chat\n"
        "  assistant:\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, '{{{{context}}}}', '{{{{input}}}}']\n"
        "    input: prompts/reply.md\n"
        "    events:\n"
        "      - kind: channel.message\n"
        "        source: chat\n"
        "        filter: '.data.direct == true'\n"
        "    channel: chat\n"
        "  ev-emitter:\n"
        f"    run: [{sys.executable!r}, routines/emit_events.py]\n"
        "    emit: events\n"
        "    enabled: false\n"
        "  slow:\n"
        f"    run: [{sys.executable!r}, routines/slow.py]\n"
        "    timeout: 1s\n"
        "    enabled: false\n"
        "  guard-commit:\n"
        "    builtin: validate\n"
        "    hook: pre-commit\n"
        "  guard-push:\n"
        f"    run: [{sys.executable!r}, routines/hook_guard.py]\n"
        "    hook: [pre-push]\n"
        "channels:\n"
        "  chat:\n"
        f"    listen: [{sys.executable!r}, channels/mock_chat.py, listen]\n"
        f"    send: [{sys.executable!r}, channels/mock_chat.py, send]\n".encode(),
    )

    # 2026-11-15 is a Sunday! At 09:00 UTC both `feed` (`every: 1h`) and `brief` (`at: "08:00"`, `days: [sun]`) are due.
    t0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    dry = jobs.run_jobs(store, ("feed",), dry_run=True, now=t0)
    assert dry["results"][0]["status"] == "dry_run"

    # Bare `bf run` skips pure event-only (`rebuild`, `assistant`) and hook-only (`guard-*`) jobs!
    bare = jobs.run_jobs(store, dry_run=True, now=t0)
    assert [r["job"] for r in bare["results"]] == ["feed", "brief"]

    due1 = jobs.run_jobs(store, due_only=True, now=t0)
    assert due1["ok"], due1
    assert [r["job"] for r in due1["results"]] == ["feed", "brief"]
    assert records.load_source(store, "feed")[0]["id"] == "f1"

    # Next tick 5 minutes later: `rebuild` triggers on `records.ingested`!
    t1 = t0 + timedelta(minutes=5)
    due2 = jobs.run_jobs(store, due_only=True, now=t1)
    assert [r["job"] for r in due2["results"]] == ["rebuild"]

    # Third tick 5 minutes later: cursor advanced, nothing due!
    due3 = jobs.run_jobs(store, due_only=True, now=t1 + timedelta(minutes=5))
    assert due3["results"] == []

    # `emit: events` job appends emitted JSON events to `logs/events.json`!
    ev_res = jobs.run_jobs(store, ("ev-emitter",), now=t1)
    assert ev_res["ok"]
    assert ev_res["results"][0]["summary"] == {"events": 1}

    # Git hook execution (`pre-commit` and `pre-push` with extra_args and stdin_bytes)!
    hk1 = jobs.run_jobs(store, hook="pre-commit", now=t1)
    assert hk1["ok"]
    assert [r["job"] for r in hk1["results"]] == ["guard-commit"]
    hk2 = jobs.run_jobs(
        store, hook="pre-push", extra_args=("--pre-push", "origin"), stdin_bytes=b"refs/heads/main", now=t1
    )
    assert hk2["ok"]
    assert (root / "routines" / "hook.args").read_text() == "--pre-push origin|refs/heads/main"

    # Explicit `bf post` auto-selects the single enabled channel (`chat`) + dry_run!
    p_dry = jobs.post_message(store, "Hello dry", dry_run=True, now=t1)
    assert (p_dry["brain"], p_dry["channel"], p_dry["status"], p_dry["message"]["text"]) == (
        "orch",
        "chat",
        "dry_run",
        "Hello dry",
    )
    posted = jobs.post_message(store, "Hello live", thread="space-1", reply_to="m-0", now=t1)
    assert posted["status"] == "ok"
    assert posted["channel"] == "chat"
    sent_lines = [json.loads(ln) for ln in (root / "channels" / "sent.jsonl").read_text().splitlines()]
    assert any(m["text"] == "Hello live" and m["conversation"] == "space-1" for m in sent_lines)

    # Reactive `jobs.assistant` triggered via `watch.tick` when a `channel.message` event arrives!
    records.append_event(store, "channel.message", "chat", {"text": "hi assistant", "direct": True}, now=t1)
    tick_out = watch.tick(store, now=t1 + timedelta(minutes=6))
    assert tick_out["jobs_ran"] == 1
    assert tick_out["ok"]
    sent_after = (root / "channels" / "sent.jsonl").read_text()
    assert "Summary for event (1 ev) from reply.md" in sent_after

    # `DaemonStream` captures JSON line output from `channels.chat.listen` into `logs/events.json`!
    cfg = config.load(store)
    stop_ev, wake_ev = threading.Event(), threading.Event()
    daemon = watch.DaemonStream(store, cfg.channels["chat"], stop_ev, wake_ev)
    daemon.start()
    assert wake_ev.wait(timeout=5.0)
    stop_ev.set()
    daemon.terminate()
    ev_doc = decode(store.read("logs/events.json"), "logs/events.json")
    assert isinstance(ev_doc, dict)
    assert any(ev["source"] == "chat" and ev["data"].get("text") == "ping from chat" for ev in ev_doc["events"])

    # Timeout reaps slow process group and records failure in `logs/slow.json`!
    slow_res = jobs.run_jobs(store, ("slow",), now=t1)
    assert not slow_res["ok"]
    assert slow_res["results"][0]["exit_code"] == 124
    assert "timed out" in slow_res["results"][0]["error"]
    assert jobs.status_summary(store, check=True)["ok"]

    # Render TUI dashboard table (`job` + `channel` rows) and supervise file changes via `watch.serve`!
    table = watch.render_dashboard(store)
    assert len(table.rows) >= 3

    serve_stop = threading.Event()
    store.write("bf.yaml", store.read("bf.yaml") + b"watch:\n  interval: 500ms\n  debounce: 50ms\n")
    serve_thread = threading.Thread(
        target=lambda: watch.serve(store, json_stream=True, stop_event=serve_stop), daemon=True
    )
    serve_thread.start()
    deadline = time.monotonic() + 8.0
    step = 0
    while time.monotonic() < deadline and not store.exists("memories/concepts/live-edit.json"):
        if step % 6 == 2:
            store.write("concepts/live-edit.md", b"---\ntype: guide\nstatus: stable\n---\n# Live Edit\n")
        step += 1
        time.sleep(0.05)
    serve_stop.set()
    serve_thread.join(timeout=5)
    assert store.exists("memories/concepts/live-edit.json")

    # Builtin `review`/`eval`, relative script executable, `requires`, missing input, failed channel post, multi-channel `--to`!
    rel_exec = root / "routines" / "exec_rel.py"
    rel_exec.write_text(f"#!{sys.executable}\nprint('---\\ntype: action\\nstatus: draft\\n---\\n# Report\\n')\n")
    rel_exec.chmod(0o755)
    (root / "sensors" / "bad_json.py").write_text("print('{not valid json')\n")
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: orch\njobs:\n"
        "  rev:\n    builtin: review\n"
        "  ev:\n    builtin: eval\n"
        "  rel-script:\n    run: [routines/exec_rel.py]\n    emit: action\n    keep: 1\n"
        "  need-missing:\n    builtin: build\n    requires: [no-such-binary-bf-xyz]\n"
        f"  bad-input:\n    run: [{sys.executable!r}, -c, 'pass']\n    input: prompts/missing.md\n"
        f"  bad-ing:\n    run: [{sys.executable!r}, sensors/bad_json.py]\n    ingest: feed\n"
        f"  post-fail:\n    run: [{sys.executable!r}, -c, 'print(\"hello\")']\n    channel: fail-send\n"
        "channels:\n"
        f"  fail-send:\n    send: [{sys.executable!r}, -c, 'import sys; sys.exit(2)']\n"
        "  req-send:\n    send: [echo]\n    requires: [no-such-binary-bf-xyz]\n".encode(),
    )
    rev_ev = jobs.run_jobs(store, ("rev", "ev", "rel-script"), now=t1)
    assert rev_ev["ok"]
    assert jobs.run_jobs(store, ("rel-script",), now=t1)["ok"]
    assert len([p for p in store.files("actions") if "rel-script-" in p]) == 1
    bad_ing = jobs.run_jobs(store, ("bad-ing",), now=t1)
    assert not bad_ing["ok"]
    # Failed outbound post in `post-fail` records failure in `logs/post-fail.json` rather than crashing uncaught!
    pf_res = jobs.run_jobs(store, ("post-fail",), now=t1)
    assert not pf_res["ok"]
    assert "exit code 2" in pf_res["results"][0]["error"]
    assert not jobs.status_summary(store, check=True)["ok"]
    with pytest.raises(Error, match="no-such-binary-bf-xyz"):
        jobs.run_jobs(store, ("need-missing",), now=t1)
    with pytest.raises(Error, match="input file"):
        jobs.run_jobs(store, ("bad-input",), now=t1)

    for bad_call in (
        lambda: jobs.exec_argv((), cwd=root, env={}, timeout=5),
        lambda: jobs.run_jobs(store, ("missing-job",)),
        lambda: jobs.run_jobs(store, hook="post-merge"),
        lambda: jobs.post_message(store, "   "),
        lambda: jobs.post_message(store, "hi"),  # 2 channels define `send` -> requires `--to`!
        lambda: jobs.post_message(store, "hi", to="no-such-channel"),
        lambda: jobs.post_message(store, "hi", to="req-send"),
        lambda: jobs.post_message(store, "hi", to="fail-send"),
    ):
        with pytest.raises((Error, InputError)):
            bad_call()


def test_failure_backoff_dotenv_env_map_and_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "resilient"
    engine.init_brain(root, name="resilient")
    store = Store(root)
    for sub in ("routines", "channels"):
        (root / sub).mkdir(exist_ok=True)
    (root / "routines" / "warn_ok.py").write_text(
        "import json, os, sys\n"
        "sys.stderr.write('Skipped feed x\\n')\n"
        "val = os.environ.get('GWS_DIR', '') + '|' + os.environ.get('LOCAL_SECRET', '') + '|' + os.environ.get('BF_BRAIN', '')\n"
        "print(json.dumps([{'id': 'w1', 'title': val}]))\n"
    )
    (root / "routines" / "fail_fast.py").write_text("import sys\nsys.stderr.write('transient err\\n')\nsys.exit(1)\n")
    (root / "channels" / "dup_stream.py").write_text(
        "import json\n"
        "for _ in range(2):\n"
        "    print(json.dumps({'id': 'm-dup', 'text': 'hello', 'direct': True}), flush=True)\n"
    )
    store.write(
        ".env",
        b"LOCAL_SECRET=from-dotenv\nGWS_DIR=override-gws\n",
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: resilient\n"
        "defaults:\n  env:\n    GWS_DIR: '{{home}}/.config/gws'\n    LOCAL_SECRET: default-val\n"
        "jobs:\n"
        f"  warn-ok:\n    run: [{sys.executable!r}, routines/warn_ok.py]\n    ingest:\n      source: warn-ok\n      mode: snapshot\n    every: 1h\n"
        f"  flaky:\n    run: [{sys.executable!r}, routines/fail_fast.py]\n    every: 5m\n"
        f"  flaky-ev:\n    run: [{sys.executable!r}, routines/fail_fast.py]\n    events:\n      - kind: channel.message\n"
        "channels:\n"
        f"  chat:\n    listen: [{sys.executable!r}, channels/dup_stream.py]\n".encode(),
    )
    # Run from outside the brain directory (`tmp_path`) to prove `.env` loads from `store.root`!
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BF_BRAIN", str(tmp_path / "evil-override"))

    ok_res = jobs.run_jobs(store, ("warn-ok",))
    assert ok_res["ok"]
    assert records.load_source(store, "warn-ok")[0]["title"] == f"override-gws|from-dotenv|{root.resolve()}"
    warn_log = decode(store.read("logs/warn-ok.json"), "logs/warn-ok.json")
    assert isinstance(warn_log, dict)
    assert "Skipped feed x" in warn_log["history"][-1]["stderr"]

    # Scheduled AND event-triggered failure backoff!
    t0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    assert not jobs.run_jobs(store, ("flaky",), now=t0)["ok"]
    cfg = config.load(store)
    flaky_log = jobs.load_run_log(store, "flaky", kind="job")
    assert flaky_log["consecutive_failures"] == 1
    assert not jobs.is_schedule_due(cfg.jobs["flaky"], flaky_log, t0 + timedelta(seconds=10))
    assert jobs.is_schedule_due(cfg.jobs["flaky"], flaky_log, t0 + timedelta(seconds=65))

    records.append_event(store, "channel.message", "chat", {"id": "ev-b1", "text": "ping"}, now=t0)
    ev_fail = jobs.run_jobs(store, due_only=True, now=t0)
    assert [r["job"] for r in ev_fail["results"]] == ["flaky-ev"]
    assert not ev_fail["ok"]
    # Immediate tick 5s later backs off instead of tight-looping on the unconsumed event cursor!
    assert jobs.run_jobs(store, due_only=True, now=t0 + timedelta(seconds=5))["results"] == []

    summary = jobs.status_summary(store)
    assert summary["jobs"]["warn-ok"]["mode"] == "snapshot"
    assert summary["jobs"]["warn-ok"]["last_run"]["added"] == 1
    assert "transient err" in summary["jobs"]["flaky"]["error"]

    # `DaemonStream` deduplicates redelivered message IDs in `logs/events.json`!
    stop_ev, wake_ev = threading.Event(), threading.Event()
    daemon = watch.DaemonStream(store, cfg.channels["chat"], stop_ev, wake_ev)
    daemon.start()
    assert wake_ev.wait(timeout=5.0)
    dup_deadline = time.monotonic() + 5.0
    while time.monotonic() < dup_deadline and not jobs.load_run_log(store, "chat", kind="channel").get("history"):
        time.sleep(0.02)
    stop_ev.set()
    daemon.terminate()
    ev_doc = decode(store.read("logs/events.json"), "logs/events.json")
    assert isinstance(ev_doc, dict)
    assert sum(1 for e in ev_doc["events"] if e["data"].get("id") == "m-dup") == 1

    # `watch.serve` catches transient `BusyError`/`Error` on a supervisor tick instead of crashing!
    stop_loop = threading.Event()

    def flaky_tick(_s: Store, **_kw: object) -> dict[str, object]:
        stop_loop.set()
        raise Error("transient lock busy")

    monkeypatch.setattr(watch, "tick", flaky_tick)
    capsys.readouterr()
    survived = watch.serve(store, json_stream=True, stop_event=stop_loop)
    assert not survived["ok"]
    assert "bf: watch: transient lock busy" in capsys.readouterr().err


def test_proxy_env_thread_reply_forwarding_and_reactive_schedule_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hybrid"
    engine.init_brain(root, name="hybrid")
    store = Store(root)
    for sub in ("routines", "channels"):
        (root / sub).mkdir(exist_ok=True)
    (root / "routines" / "echo_ctx.py").write_text(
        "import json, os, pathlib, sys\n"
        "ctx = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "px = os.environ.get('HTTPS_PROXY', '') + '|' + os.environ.get('no_proxy', '')\n"
        "print(f\"{ctx['kind']}:{ctx['start']}:{px}\")\n"
    )
    (root / "channels" / "rec_send.py").write_text(
        "import json, pathlib, sys\n"
        "msg = json.loads(sys.stdin.read())\n"
        "p = pathlib.Path(f'channels/{sys.argv[1]}.jsonl')\n"
        "p.write_text((p.read_text() if p.exists() else '') + json.dumps(msg) + '\\n')\n"
        "print(json.dumps({'id': 'out-1'}))\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: hybrid\ndefaults:\n  timezone: UTC\njobs:\n"
        "  bot:\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, '{{{{context}}}}']\n"
        "    at: '08:00'\n"
        "    events:\n"
        "      - kind: channel.message\n"
        "    channel: chat\n"
        "  cross-alert:\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, '{{{{context}}}}']\n"
        "    events:\n"
        "      - kind: channel.message\n"
        "        source: chat\n"
        "    channel: alerts\n"
        "  hook-every:\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, '{{{{context}}}}']\n"
        "    every: 1h\n"
        "    hook: pre-commit\n"
        "    channel: alerts\n"
        "channels:\n"
        f"  chat:\n    send: [{sys.executable!r}, channels/rec_send.py, chat]\n"
        f"  alerts:\n    send: [{sys.executable!r}, channels/rec_send.py, alerts]\n".encode(),
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8080")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")

    # 1. Reactive `channel.message` at 08:01 runs `event` first, forwards `conversation` + `reply_to` only on same channel,
    #    and preserves the unscheduled anchor so `at: '08:00'` still runs on the next tick!
    t_ev = datetime(2026, 11, 16, 8, 1, tzinfo=UTC)
    records.append_event(
        store, "channel.message", "chat", {"id": "in-42", "conversation": "room-1/t-9", "text": "hello"}, now=t_ev
    )
    r1 = jobs.run_jobs(store, due_only=True, now=t_ev)
    assert r1["ok"]
    assert [r["job"] for r in r1["results"]] == ["bot", "cross-alert", "hook-every"]
    chat_msgs = [json.loads(ln) for ln in (root / "channels" / "chat.jsonl").read_text().splitlines()]
    alert_msgs = [json.loads(ln) for ln in (root / "channels" / "alerts.jsonl").read_text().splitlines()]
    assert chat_msgs[0]["conversation"] == "room-1/t-9"
    assert chat_msgs[0]["reply_to"] == "in-42"
    assert "http://proxy.example.test:8080|localhost,127.0.0.1" in chat_msgs[0]["text"]
    assert "conversation" not in alert_msgs[0]
    assert "reply_to" not in alert_msgs[0]

    # Manual `bf run cross-alert` on an unscheduled event job uses `last_success` (`08:01`), not full `lookback`!
    man_res = jobs.run_jobs(store, ("cross-alert",), now=t_ev + timedelta(minutes=30))
    assert man_res["ok"]
    alert_msgs = [json.loads(ln) for ln in (root / "channels" / "alerts.jsonl").read_text().splitlines()]
    assert alert_msgs[-1]["text"].startswith("manual:2026-11-16T08:01:00Z:")

    # 2. Next tick at 08:02: `bot`'s daily `08:00` schedule still fires because `08:01` was an `event` run!
    r2 = jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=1))
    assert r2["ok"]
    assert [r["job"] for r in r2["results"]] == ["bot"]
    chat_msgs = [json.loads(ln) for ln in (root / "channels" / "chat.jsonl").read_text().splitlines()]
    assert chat_msgs[1]["text"].startswith("schedule:")
    assert "conversation" not in chat_msgs[1]

    # 3. Non-scalar/control `thread`/`reply_to` metadata degrades cleanly, and multi-event batches omit reply threading!
    assert jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=2))["results"] == []
    records.append_event(
        store,
        "channel.message",
        "chat",
        {"conversation": "   ", "thread": {"nested": True}, "reply_to": "bad\nid", "id": 99},
        now=t_ev,
    )
    assert [r["job"] for r in jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=4))["results"]] == [
        "bot",
        "cross-alert",
    ]
    chat_msgs = [json.loads(ln) for ln in (root / "channels" / "chat.jsonl").read_text().splitlines()]
    assert "conversation" not in chat_msgs[2]
    assert chat_msgs[2]["reply_to"] == "99"

    records.append_event(store, "channel.message", "chat", {"id": "in-44", "thread": "room-1/t-10"}, now=t_ev)
    records.append_event(store, "channel.message", "chat", {"id": "in-45", "thread": "room-1/t-11"}, now=t_ev)
    assert [r["job"] for r in jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=5))["results"]] == [
        "bot",
        "cross-alert",
    ]
    chat_msgs = [json.loads(ln) for ln in (root / "channels" / "chat.jsonl").read_text().splitlines()]
    assert "conversation" not in chat_msgs[3]
    assert "reply_to" not in chat_msgs[3]

    # 4. `hook: pre-commit` at 08:31 preserves `hook-every`'s `08:01` `every: 1h` schedule anchor so it fires at 09:01!
    assert jobs.run_jobs(store, hook="pre-commit", now=t_ev + timedelta(minutes=30))["ok"]
    assert jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=55))["results"] == []
    due_1h = jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=60))
    assert [r["job"] for r in due_1h["results"]] == ["hook-every"]


def test_watch_supervises_mcp_daemon_server_and_input_jobs(tmp_path: Path) -> None:
    root = tmp_path / "mcp-watch"
    engine.init_brain(root, name="mcp-watch")
    store = Store(root)
    for sub in ("routines", "prompts"):
        (root / sub).mkdir(exist_ok=True)
    (root / "routines" / "fake_harness.py").write_text(
        "import pathlib, sys\nprint('Brief from ' + pathlib.Path(sys.argv[1]).name)\n"
    )
    (root / "prompts" / "brief.md").write_text("# Brief\n")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: mcp-watch\n"
        "jobs:\n"
        "  brief:\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, '{{{{input}}}}']\n"
        "    input: prompts/brief.md\n"
        "    emit: action\n"
        "    keep: 1\n"
        "servers:\n"
        "  mcp:\n"
        f"    run: [{sys.executable!r}, -m, bf, mcp, --transport, http, --port, '{port}']\n"
        "    enabled: true\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )
    ran1 = jobs.run_jobs(store, ("brief",))
    assert ran1["ok"], ran1
    act1_rel = ran1["results"][0]["summary"]["action"]
    act1_mem = f"memories/actions/{Path(act1_rel).parent.name}.json"
    assert store.exists(act1_mem)
    ran2 = jobs.run_jobs(store, ("brief",))
    assert ran2["ok"], ran2
    act2_rel = ran2["results"][0]["summary"]["action"]
    assert act2_rel != act1_rel
    assert not store.exists(act1_mem)
    assert engine.validate(store)["valid"]

    # Port-conflict second brain + missing `requires:` on server record `failed` in `logs/`!
    conflict_root = tmp_path / "mcp-conflict"
    engine.init_brain(conflict_root, name="mcp-conflict")
    conflict_store = Store(conflict_root)
    conflict_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: mcp-conflict\n"
        "servers:\n"
        "  mcp:\n"
        f"    run: [{sys.executable!r}, -m, bf, mcp, --transport, http, --port, '{port}']\n"
        "    enabled: true\n"
        "  badreq:\n    run: [echo, hi]\n    requires: [nonexistent-req-cmd-xyz]\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )

    stop_ev = threading.Event()
    stop_conflict = threading.Event()
    srv_thread = threading.Thread(target=lambda: watch.serve(store, json_stream=True, stop_event=stop_ev), daemon=True)
    srv_thread.start()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    resp_text = ""
    deadline = time.monotonic() + 8.0
    try:
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310 - localhost ephemeral port
                    resp_text = resp.read().decode("utf-8")
                    break
            except OSError:
                time.sleep(0.1)

        conflict_thread = threading.Thread(
            target=lambda: watch.serve(conflict_store, json_stream=True, stop_event=stop_conflict), daemon=True
        )
        conflict_thread.start()
        try:
            c_deadline = time.monotonic() + 8.0
            while time.monotonic() < c_deadline:
                st = jobs.status_summary(conflict_store)
                if st["servers"]["mcp"]["consecutive_failures"] >= 1 and st["servers"]["badreq"]["status"] == "failed":
                    break
                time.sleep(0.1)
        finally:
            stop_conflict.set()
            conflict_thread.join(timeout=6)
    finally:
        stop_ev.set()
        srv_thread.join(timeout=6)

    assert "bf_query" in resp_text
    mcp_log = decode(store.read("logs/mcp.json"), "logs/mcp.json")
    assert isinstance(mcp_log, dict)
    assert mcp_log["status"] == "stopped"

    conflict_status = jobs.status_summary(conflict_store, check=True)
    assert not conflict_status["ok"]
    assert conflict_status["servers"]["mcp"]["status"] == "failed"
    assert conflict_status["servers"]["mcp"]["consecutive_failures"] >= 1
    assert conflict_status["servers"]["badreq"]["status"] == "failed"
    assert "nonexistent-req-cmd-xyz" in conflict_status["servers"]["badreq"]["error"]


def test_dual_schedule_and_event_job_advances_cursor_after_scheduled_run(tmp_path: Path) -> None:
    root = tmp_path / "dual"
    engine.init_brain(root, name="dual")
    store = Store(root)
    (root / "routines").mkdir(exist_ok=True)
    (root / "routines" / "count.py").write_text(
        "import pathlib, shutil, sys\nshutil.copyfile(sys.argv[1], 'routines/seen.json')\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: dual\n"
        "jobs:\n"
        "  combo:\n"
        f"    run: [{sys.executable!r}, routines/count.py, '{{{{context}}}}']\n"
        "    every: 1h\n"
        "    events:\n"
        "      - kind: custom.ping\n".encode(),
    )

    ev1 = records.append_event(store, "custom.other", "test", {"id": "o1"})

    dry = jobs.run_jobs(store, due_only=True, dry_run=True)
    assert [item["job"] for item in dry["results"]] == ["combo"]
    cursor_path = root / ".bf" / "state" / "combo.cursor.json"
    assert not cursor_path.exists()

    first = jobs.run_jobs(store, due_only=True)
    assert [item["job"] for item in first["results"]] == ["combo"]
    assert first["results"][0]["status"] == "ok"
    seen = decode((root / "routines" / "seen.json").read_bytes(), "seen.json")
    assert isinstance(seen, dict)
    assert seen["kind"] == "schedule"
    assert seen["events"] == []
    assert decode(cursor_path.read_bytes(), str(cursor_path)) == {"seq": ev1["seq"]}

    ev2 = records.append_event(store, "custom.other", "test", {"id": "o2"})
    second = jobs.run_jobs(store, due_only=True)
    assert second["ran"] == 0
    assert decode(cursor_path.read_bytes(), str(cursor_path)) == {"seq": ev2["seq"]}
