"""Tests for `jobs.py` and `watch.py`: schedules, `.bf/`, `structlog` JSON logs, services (`emit: messages|events|none`), and watch."""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from bf import config, engine, jobs, models, records, watch
from bf.models import FORMAT, Error, InputError, decode
from bf.storage import Store, private_dir


def test_jobs_schedules_events_hooks_input_services_and_timeout(tmp_path: Path) -> None:
    root = tmp_path / "orch-brain"
    engine.init_brain(root, name="orch")
    store = Store(root)
    for sub in ("sensors", "services", "routines", "prompts"):
        (root / sub).mkdir(exist_ok=True)

    (root / "sensors" / "feed.py").write_text(
        "import json, sys\nprint(json.dumps([{'id': 'f1', 'title': 'Feed item', 'text': sys.argv[1]}]))\n"
    )
    (root / "services" / "mock_chat.py").write_text(
        "import json, time\n"
        "print(json.dumps({'id': 'in-live', 'text': 'ping from chat', 'direct': True}), flush=True)\n"
        "time.sleep(0.2)\n"
    )
    (root / "routines" / "fake_harness.py").write_text(
        "import json, pathlib, sys\n"
        "ctx = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "prompt_path = sys.argv[2]\n"
        "stdin_txt = sys.stdin.read()\n"
        "assert oct(pathlib.Path(sys.argv[1]).stat().st_mode & 0o777) == '0o600'\n"
        "assert ctx['stdin'] == '' and ctx['input'] == prompt_path\n"
        "assert stdin_txt.startswith('# ')\n"
        "out = {'kind': ctx['kind'], 'events': len(ctx['events']), 'prompt': pathlib.Path(prompt_path).name}\n"
        "open('services/sent.jsonl', 'a').write(json.dumps(out) + '\\n')\n"
        "print(json.dumps(out))\n"
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
        "timezone: UTC\n"
        "jobs:\n"
        "  feed:\n"
        f"    run: [{sys.executable!r}, sensors/feed.py, '{{{{start}}}}']\n"
        "    ingest:\n"
        "      source: feed\n"
        "      mode: upsert\n"
        "    schedule: 1h\n"
        "  rebuild:\n"
        "    run: [bf, build]\n"
        "    events:\n"
        "      - kind: records.ingested\n"
        "        source: feed\n"
        "  brief:\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, '{{{{context}}}}', '{{{{input}}}}']\n"
        "    input: prompts/digest.md\n"
        "    schedule: '0 8 * * 0'\n"
        "  assistant:\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, '{{{{context}}}}', '{{{{input}}}}']\n"
        "    input: prompts/reply.md\n"
        "    events:\n"
        "      - kind: service.message\n"
        "        source: chat\n"
        "        filter: '.data.direct == true'\n"
        "  ev-emitter:\n"
        f"    run: [{sys.executable!r}, routines/emit_events.py]\n"
        "    enabled: false\n"
        "  slow:\n"
        f"    run: [{sys.executable!r}, routines/slow.py]\n"
        "    timeout: 1s\n"
        "    enabled: false\n"
        "  guard-commit:\n"
        "    run: [bf, validate]\n"
        "    hook: pre-commit\n"
        "  guard-push:\n"
        f"    run: [{sys.executable!r}, routines/hook_guard.py]\n"
        "    hook: [pre-push]\n"
        "services:\n"
        "  chat:\n"
        f"    run: [{sys.executable!r}, services/mock_chat.py]\n"
        "    emit: messages\n".encode(),
    )

    # 2026-11-15 is a Sunday! At 09:00 UTC both `feed` (`1h`) and `brief` (`0 8 * * 0`) are due.
    t0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    dry = jobs.run_jobs(store, ("feed",), dry_run=True, now=t0)
    assert dry["results"][0]["status"] == "dry_run"

    # `bf run` requires an explicit selector (`JOB ...`, `--due`, or `--hook`), and guards `-- EXTRA_ARGS`.
    with pytest.raises(InputError, match="specify job names, --due, or --hook"):
        jobs.run_jobs(store, dry_run=True, now=t0)
    with pytest.raises(InputError, match="specify job names, --due, or --hook"):
        jobs.run_jobs(store, ("feed",), due_only=True, now=t0)
    with pytest.raises(InputError, match="require a single job or --hook"):
        jobs.run_jobs(store, due_only=True, extra_args=("--x",), now=t0)
    with pytest.raises(InputError, match="require a single job or --hook"):
        jobs.run_jobs(store, ("feed", "brief"), extra_args=("--x",), now=t0)

    # `due_only=True` rebuilds dirty notes first before evaluating scheduled/event-triggered jobs!
    store.write("concepts/dirty-due.md", b"---\ntype: guide\nstatus: draft\n---\n\n# Dirty Before Due\n")
    due1 = jobs.run_jobs(store, due_only=True, now=t0)
    assert due1["ok"], due1
    assert [r["job"] for r in due1["results"]] == ["feed", "brief"]
    assert store.exists("memories/concepts/dirty-due.json")
    feed_saved = decode(store.read(records.record_file("feed", "f1")), "feed")
    assert isinstance(feed_saved, dict)
    assert feed_saved["id"] == "f1"

    # Next tick 5 minutes later: `rebuild` triggers on `records.ingested`!
    t1 = t0 + timedelta(minutes=5)
    due2 = jobs.run_jobs(store, due_only=True, now=t1)
    assert [r["job"] for r in due2["results"]] == ["rebuild"]

    # Third tick 5 minutes later: cursor advanced, nothing due!
    due3 = jobs.run_jobs(store, due_only=True, now=t1 + timedelta(minutes=5))
    assert due3["results"] == []

    # One-shot JSON stdout summary dict captured in `summary` (`{"kind":"custom.ev","id":"e1"}`)!
    ev_res = jobs.run_jobs(store, ("ev-emitter",), now=t1)
    assert ev_res["ok"]
    assert ev_res["results"][0]["summary"] == {"kind": "custom.ev", "id": "e1"}

    # Git hook execution (`pre-commit` and `pre-push` with extra_args and stdin_bytes)!
    hk1 = jobs.run_jobs(store, hook="pre-commit", now=t1)
    assert hk1["ok"]
    assert [r["job"] for r in hk1["results"]] == ["guard-commit"]
    hk2 = jobs.run_jobs(
        store, hook="pre-push", extra_args=("--pre-push", "origin"), stdin_bytes=b"refs/heads/main", now=t1
    )
    assert hk2["ok"]
    assert (root / "routines" / "hook.args").read_text() == "--pre-push origin|refs/heads/main"

    # Reactive `jobs.assistant` triggered via `watch.tick` on `service.message`!
    records.append_event(
        store,
        "service.message",
        "chat",
        {"id": "ev-msg-9", "conversation": "space-9/threads/t-1", "text": "hi assistant", "direct": True},
        now=t1,
    )
    tick_out = watch.tick(store, now=t1 + timedelta(minutes=6))
    assert tick_out["jobs_ran"] == 1
    assert tick_out["ok"]
    sent_after_lines = [json.loads(ln) for ln in (root / "services" / "sent.jsonl").read_text().splitlines()]
    assert any(m == {"kind": "event", "events": 1, "prompt": "reply.md"} for m in sent_after_lines)

    # `DaemonStream` captures JSON line output from `services.chat` (`emit: messages`) into `.bf/events.jsonl` & `logs/events.log`!
    cfg = config.load(store)
    stop_ev, wake_ev = threading.Event(), threading.Event()
    daemon = watch.DaemonStream(store, cfg.services["chat"], stop_ev, wake_ev)
    daemon.start()
    assert wake_ev.wait(timeout=5.0)
    stop_ev.set()
    daemon.terminate()
    ev_items, _ = records.read_events(store)
    assert not (root / ".bf" / "state").exists()
    assert not (root / ".bf" / "locks").exists()
    assert not (root / ".bf" / "feed").exists()
    assert not (root / ".bf" / "chat").exists()
    assert any(
        ev["kind"] == "service.message" and ev["source"] == "chat" and ev["data"].get("text") == "ping from chat"
        for ev in ev_items
    )

    # Timeout reaps slow process group and records failure in `.bf/slow.json`; `status_summary` includes `counts` + `attention`!
    slow_res = jobs.run_jobs(store, ("slow",), now=t1)
    assert not slow_res["ok"]
    assert slow_res["results"][0]["exit_code"] == 124
    assert "timed out" in slow_res["results"][0]["error"]
    st_sum = jobs.status_summary(store, check=True)
    assert st_sum["ok"]
    assert st_sum["counts"]["concepts"] >= 2
    assert st_sum["attention"] == []
    assert "channels" not in st_sum

    # Render TUI dashboard panel (`service` + `job` rows) and supervise file changes via `watch.serve`!
    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False).print(watch.render_dashboard(store))
    rendered = buf.getvalue()
    assert "orch · watch" in rendered
    assert "service · messages" in rendered
    assert "+1 · 1 record" in rendered
    assert "2 disabled" in rendered

    # Non-TTY `watch.serve` automatically emits JSON tick lines even with default `json_stream=False`!
    serve_stop = threading.Event()
    store.write("bf.yaml", store.read("bf.yaml") + b"watch:\n  interval: 500ms\n  debounce: 50ms\n")
    serve_thread = threading.Thread(target=lambda: watch.serve(store, stop_event=serve_stop), daemon=True)
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

    # Builtin `review`/`eval`, relative script executable, `requires`, missing input, failed job attention!
    rel_exec = root / "routines" / "exec_rel.py"
    rel_exec.write_text(f"#!{sys.executable}\nprint('---\\ntype: action\\nstatus: draft\\n---\\n# Report\\n')\n")
    rel_exec.chmod(0o755)
    (root / "sensors" / "bad_json.py").write_text("print('{not valid json')\n")
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: orch\njobs:\n"
        "  rev:\n    run: [bf, review]\n"
        "  ev:\n    run: [bf, eval]\n"
        "  rel-script:\n    run: [routines/exec_rel.py]\n    action:\n      keep: 1\n"
        "  need-missing:\n    run: [bf, build]\n    requires: [no-such-binary-bf-xyz]\n"
        f"  bad-input:\n    run: [{sys.executable!r}, -c, 'pass']\n    input: prompts/missing.md\n"
        f"  bad-ing:\n    run: [{sys.executable!r}, sensors/bad_json.py]\n    ingest: feed\n"
        f"  exit-fail:\n    run: [{sys.executable!r}, -c, 'import sys; sys.exit(2)']\n".encode(),
    )
    rev_ev = jobs.run_jobs(store, ("rev", "ev", "rel-script"), now=t1)
    assert rev_ev["ok"]
    assert jobs.run_jobs(store, ("rel-script",), now=t1)["ok"]
    assert len([p for p in store.files("actions") if "rel-script-" in p]) == 1
    bad_ing = jobs.run_jobs(store, ("bad-ing",), now=t1)
    assert not bad_ing["ok"]
    pf_res = jobs.run_jobs(store, ("exit-fail",), now=t1)
    assert not pf_res["ok"]
    assert "exit code 2" in pf_res["results"][0]["error"]
    st_fail = jobs.status_summary(store, check=True)
    assert not st_fail["ok"]
    assert {"kind": "job", "name": "exit-fail", "failed": True} in st_fail["attention"]
    with pytest.raises(Error, match="no-such-binary-bf-xyz"):
        jobs.run_jobs(store, ("need-missing",), now=t1)
    with pytest.raises(Error, match="input file"):
        jobs.run_jobs(store, ("bad-input",), now=t1)

    for bad_call in (
        lambda: jobs.exec_argv((), cwd=root, env={}, timeout=5),
        lambda: jobs.run_jobs(store, ("missing-job",)),
        lambda: jobs.run_jobs(store, hook="post-merge"),
    ):
        with pytest.raises((Error, InputError)):
            bad_call()


def test_failure_backoff_dotenv_env_map_and_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "resilient"
    engine.init_brain(root, name="resilient")
    store = Store(root)
    for sub in ("routines", "services"):
        (root / sub).mkdir(exist_ok=True)
    (root / "routines" / "warn_ok.py").write_text(
        "import json, os, sys\n"
        "sys.stderr.write('Skipped feed x\\n')\n"
        "val = os.environ.get('GWS_DIR', '') + '|' + os.environ.get('LOCAL_SECRET', '') + '|' + os.environ.get('BF_BRAIN', '')\n"
        "print(json.dumps([{'id': 'w1', 'title': val}]))\n"
    )
    (root / "routines" / "fail_fast.py").write_text("import sys\nsys.stderr.write('transient err\\n')\nsys.exit(1)\n")
    (root / "services" / "dup_stream.py").write_text(
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
        "env:\n  GWS_DIR: '{{home}}/.config/gws'\n  LOCAL_SECRET: default-val\n"
        "jobs:\n"
        f"  warn-ok:\n    run: [{sys.executable!r}, routines/warn_ok.py]\n    ingest:\n      source: warn-ok\n      mode: snapshot\n    schedule: 1h\n"
        f"  flaky:\n    run: [{sys.executable!r}, routines/fail_fast.py]\n    schedule: 5m\n"
        f"  flaky-ev:\n    run: [{sys.executable!r}, routines/fail_fast.py]\n    events:\n      - kind: service.message\n"
        "services:\n"
        f"  chat:\n    run: [{sys.executable!r}, services/dup_stream.py]\n    emit: messages\n".encode(),
    )
    # Run from outside the brain directory (`tmp_path`) to prove `.env` loads from `store.root`!
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BF_BRAIN", str(tmp_path / "evil-override"))

    ok_res = jobs.run_jobs(store, ("warn-ok",))
    assert ok_res["ok"]
    warn_saved = decode(store.read(records.record_file("warn-ok", "w1")), "warn-ok")
    assert isinstance(warn_saved, dict)
    assert warn_saved["title"] == f"override-gws|from-dotenv|{root.resolve()}"
    warn_lines = [decode(ln, "logs/warn-ok.log") for ln in store.read("logs/warn-ok.log").splitlines() if ln.strip()]
    assert isinstance(warn_lines[-1], dict)
    assert warn_lines[-1]["message"] == "job.succeeded"
    assert "Skipped feed x" in warn_lines[-1]["stderr"]

    # Scheduled AND event-triggered failure backoff!
    t0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    assert not jobs.run_jobs(store, ("flaky",), now=t0)["ok"]
    cfg = config.load(store)
    flaky_state = jobs.load_unit_state(store, "flaky", kind="job")
    assert flaky_state["consecutive_failures"] == 1
    assert not jobs.is_schedule_due(cfg.jobs["flaky"], flaky_state, t0 + timedelta(seconds=10))
    assert jobs.is_schedule_due(cfg.jobs["flaky"], flaky_state, t0 + timedelta(seconds=65))

    records.append_event(store, "service.message", "chat", {"id": "ev-b1", "text": "ping"}, now=t0)
    ev_fail = jobs.run_jobs(store, due_only=True, now=t0)
    assert [r["job"] for r in ev_fail["results"]] == ["flaky-ev"]
    assert not ev_fail["ok"]
    # Immediate tick 5s later backs off instead of tight-looping on the unconsumed event cursor!
    assert jobs.run_jobs(store, due_only=True, now=t0 + timedelta(seconds=5))["results"] == []

    summary = jobs.status_summary(store)
    assert summary["jobs"]["warn-ok"]["mode"] == "snapshot"
    assert summary["jobs"]["warn-ok"]["last_run"]["added"] == 1
    assert "transient err" in summary["jobs"]["flaky"]["error"]

    # `DaemonStream` deduplicates redelivered message IDs in `.bf/events.jsonl` & records `logs/chat.log`!
    stop_ev, wake_ev = threading.Event(), threading.Event()
    daemon = watch.DaemonStream(store, cfg.services["chat"], stop_ev, wake_ev)
    daemon.start()
    assert wake_ev.wait(timeout=5.0)
    dup_deadline = time.monotonic() + 5.0
    while time.monotonic() < dup_deadline and not store.exists("logs/chat.log"):
        time.sleep(0.02)
    stop_ev.set()
    daemon.terminate()
    ev_items, _ = records.read_events(store)
    assert sum(1 for e in ev_items if e["data"].get("id") == "m-dup") == 1

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


def test_proxy_env_reactive_schedule_anchors_and_cursor_self_healing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hybrid"
    engine.init_brain(root, name="hybrid")
    store = Store(root)
    (root / "routines").mkdir(exist_ok=True)
    (root / "routines" / "echo_ctx.py").write_text(
        "import json, os, pathlib, sys\n"
        "ctx = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "px = os.environ.get('HTTPS_PROXY', '') + '|' + os.environ.get('no_proxy', '')\n"
        "line = f\"{ctx['kind']}:{ctx['start']}:{px}:{len(ctx['events'])}\"\n"
        "p = pathlib.Path(f'routines/{sys.argv[2]}.log')\n"
        "p.write_text((p.read_text() if p.exists() else '') + line + '\\n')\n"
        "print(line)\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: hybrid\ntimezone: UTC\njobs:\n"
        "  bot:\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, '{{{{context}}}}', bot]\n"
        "    schedule: '0 8 * * *'\n"
        "    events:\n"
        "      - kind: service.message\n"
        "  cross-alert:\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, '{{{{context}}}}', alerts]\n"
        "    events:\n"
        "      - kind: service.message\n"
        "        source: chat\n"
        "  hook-every:\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, '{{{{context}}}}', hook]\n"
        "    schedule: 1h\n"
        "    hook: pre-commit\n".encode(),
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8080")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")

    # 1. Reactive `service.message` at 08:01 runs `event` first and preserves the unscheduled anchor so `08:00` still runs next tick!
    t_ev = datetime(2026, 11, 16, 8, 1, tzinfo=UTC)
    records.append_event(
        store, "service.message", "chat", {"id": "in-42", "conversation": "room-1/t-9", "text": "hello"}, now=t_ev
    )
    r1 = jobs.run_jobs(store, due_only=True, now=t_ev)
    assert r1["ok"]
    assert [r["job"] for r in r1["results"]] == ["bot", "cross-alert", "hook-every"]
    bot_lines = (root / "routines" / "bot.log").read_text().splitlines()
    assert "http://proxy.example.test:8080|localhost,127.0.0.1:1" in bot_lines[0]

    # Manual `bf run cross-alert` on an unscheduled event job uses `last_success` (`08:01`), not full `lookback`!
    man_res = jobs.run_jobs(store, ("cross-alert",), now=t_ev + timedelta(minutes=30))
    assert man_res["ok"]
    alert_lines = (root / "routines" / "alerts.log").read_text().splitlines()
    assert alert_lines[-1].startswith("manual:2026-11-16T08:01:00Z:")

    # 2. Next tick at 08:02: `bot`'s daily `08:00` schedule still fires because `08:01` was an `event` run!
    r2 = jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=1))
    assert r2["ok"]
    assert [r["job"] for r in r2["results"]] == ["bot"]
    bot_lines = (root / "routines" / "bot.log").read_text().splitlines()
    assert bot_lines[1].startswith("schedule:")

    # 3. Cursor self-healing when `.bf/events.jsonl` is truncated/recreated (`latest_seq < stored event_seq`):
    records.append_event(store, "service.message", "chat", {"id": "in-44"}, now=t_ev)
    records.append_event(store, "service.message", "chat", {"id": "in-45"}, now=t_ev)
    assert [r["job"] for r in jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=5))["results"]] == [
        "bot",
        "cross-alert",
    ]
    assert jobs.load_unit_state(store, "cross-alert", kind="job")["event_seq"] >= 8
    private_dir(store).delete("events.jsonl")
    # Deleting `.bf/events.jsonl` with zero new events (`latest_seq == 0`) heals stored `event_seq` to `0` on `due_only`!
    assert jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=5, seconds=30))["results"] == []
    assert jobs.load_unit_state(store, "cross-alert", kind="job")["event_seq"] == 0
    records.append_event(store, "service.message", "chat", {"id": "after-reset"}, now=t_ev)
    healed = jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=6))
    assert [r["job"] for r in healed["results"]] == ["bot", "cross-alert"]
    assert jobs.load_unit_state(store, "cross-alert", kind="job")["event_seq"] == 1

    # 4. `hook: pre-commit` at 08:31 preserves `hook-every`'s `08:01` `every: 1h` schedule anchor so it fires at 09:01!
    assert jobs.run_jobs(store, hook="pre-commit", now=t_ev + timedelta(minutes=30))["ok"]
    assert jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=55))["results"] == []
    due_1h = jobs.run_jobs(store, due_only=True, now=t_ev + timedelta(minutes=60))
    assert [r["job"] for r in due_1h["results"]] == ["hook-every"]


def test_watch_supervises_daemon_service_and_input_jobs(tmp_path: Path) -> None:
    root = tmp_path / "svc-watch"
    engine.init_brain(root, name="svc-watch")
    store = Store(root)
    for sub in ("routines", "services", "prompts"):
        (root / sub).mkdir(exist_ok=True)
    (root / "routines" / "fake_harness.py").write_text(
        "import pathlib, sys\nprint('Brief from ' + pathlib.Path(sys.argv[1]).name)\n"
    )
    (root / "services" / "tcp_srv.py").write_text(
        "import signal, socket, sys\n"
        "srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "srv.bind(('127.0.0.1', int(sys.argv[1])))\n"
        "srv.listen(4)\n"
        "srv.settimeout(0.2)\n"
        "stop = False\n"
        "signal.signal(signal.SIGTERM, lambda *_: globals().update(stop=True))\n"
        "while not stop:\n"
        "    try:\n"
        "        conn, _ = srv.accept()\n"
        "        conn.sendall(b'daemon-ready')\n"
        "        conn.close()\n"
        "    except TimeoutError:\n"
        "        pass\n"
        "srv.close()\n"
    )
    (root / "prompts" / "brief.md").write_text("# Brief\n")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: svc-watch\n"
        "jobs:\n"
        "  brief:\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, '{{{{input}}}}']\n"
        "    input: prompts/brief.md\n"
        "    action:\n      keep: 1\n"
        "services:\n"
        "  tcp:\n"
        f"    run: [{sys.executable!r}, services/tcp_srv.py, '{port}']\n"
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
    assert engine.validate(store)["ok"]

    # Failing service + missing `requires:` on service record `failed` in `.bf/`!
    conflict_root = tmp_path / "svc-conflict"
    engine.init_brain(conflict_root, name="svc-conflict")
    conflict_store = Store(conflict_root)
    conflict_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: svc-conflict\n"
        "services:\n"
        "  crash:\n"
        f"    run: [{sys.executable!r}, -c, 'import sys; sys.stderr.write(\"bind failed\\\\n\"); sys.exit(1)']\n"
        "    enabled: true\n"
        "  badreq:\n    run: [echo, hi]\n    requires: [nonexistent-req-cmd-xyz]\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )

    stop_ev = threading.Event()
    stop_conflict = threading.Event()
    srv_thread = threading.Thread(target=lambda: watch.serve(store, json_stream=True, stop_event=stop_ev), daemon=True)
    srv_thread.start()
    resp_text = ""
    deadline = time.monotonic() + 8.0
    try:
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
                    resp_text = client.recv(64).decode("utf-8")
                    break
            except OSError:
                time.sleep(0.05)

        conflict_thread = threading.Thread(
            target=lambda: watch.serve(conflict_store, json_stream=True, stop_event=stop_conflict), daemon=True
        )
        conflict_thread.start()
        try:
            c_deadline = time.monotonic() + 8.0
            while time.monotonic() < c_deadline:
                st = jobs.status_summary(conflict_store)
                if (
                    st["services"]["crash"]["consecutive_failures"] >= 1
                    and st["services"]["badreq"]["status"] == "failed"
                ):
                    break
                time.sleep(0.05)
        finally:
            stop_conflict.set()
            conflict_thread.join(timeout=6)
    finally:
        stop_ev.set()
        srv_thread.join(timeout=6)

    assert resp_text == "daemon-ready"
    tcp_state = jobs.load_unit_state(store, "tcp", kind="service")
    assert tcp_state["status"] == "stopped"

    conflict_status = jobs.status_summary(conflict_store, check=True)
    assert not conflict_status["ok"]
    assert conflict_status["services"]["crash"]["status"] == "failed"
    assert conflict_status["services"]["crash"]["consecutive_failures"] >= 1
    assert conflict_status["services"]["badreq"]["status"] == "failed"
    assert "nonexistent-req-cmd-xyz" in conflict_status["services"]["badreq"]["error"]


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
        "    schedule: 1h\n"
        "    events:\n"
        "      - kind: custom.ping\n".encode(),
    )

    ev1 = records.append_event(store, "custom.other", "test", {"id": "o1"})

    dry = jobs.run_jobs(store, due_only=True, dry_run=True)
    assert [item["job"] for item in dry["results"]] == ["combo"]
    state_path = root / ".bf" / "combo.json"
    assert not state_path.exists()

    first = jobs.run_jobs(store, due_only=True)
    assert [item["job"] for item in first["results"]] == ["combo"]
    assert first["results"][0]["status"] == "ok"
    seen = decode((root / "routines" / "seen.json").read_bytes(), "seen.json")
    assert isinstance(seen, dict)
    assert seen["kind"] == "schedule"
    assert seen["events"] == []
    st1 = decode(state_path.read_bytes(), str(state_path))
    assert isinstance(st1, dict)
    assert st1["event_seq"] == ev1["seq"]

    ev2 = records.append_event(store, "custom.other", "test", {"id": "o2"})
    second = jobs.run_jobs(store, due_only=True)
    assert second["ran"] == 0
    st2 = decode(state_path.read_bytes(), str(state_path))
    assert isinstance(st2, dict)
    assert st2["event_seq"] == ev2["seq"]


def test_watch_dashboard_states_sanitization_and_live_tui_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def render_text(st: Store, *, now: datetime | None = None, max_rows: int | None = None, width: int = 140) -> str:
        out = io.StringIO()
        Console(file=out, width=width, force_terminal=False).print(
            watch.render_dashboard(st, now=now, max_rows=max_rows)
        )
        return out.getvalue()

    empty_root = tmp_path / "empty-tui"
    engine.init_brain(empty_root, name="empty-tui")
    empty_store = Store(empty_root)
    empty_store.write(
        "bf.yaml", f"version: {FORMAT}\nname: empty-tui\nwatch:\n  interval: 500ms\n  debounce: 50ms\n".encode()
    )
    now = datetime(2026, 11, 16, 12, 30, 0, tzinfo=UTC)
    empty_txt = render_text(empty_store, now=now)
    assert "All healthy" in empty_txt
    assert "No enabled jobs or services in bf.yaml" in empty_txt
    assert "12:30:00 UTC" in empty_txt

    root = tmp_path / "rich-tui"
    engine.init_brain(root, name="rich-tui")
    store = Store(root)
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: rich-tui\n"
        "jobs:\n"
        "  ing-diff:\n    run: [echo]\n    ingest: items\n    schedule: 30m\n"
        "  ing-same:\n    run: [echo]\n    ingest:\n      source: archive\n      mode: snapshot\n    schedule: '0 8 * * 1-5'\n"
        "  b-build:\n    run: [bf, build]\n    schedule: '0 9 * * *'\n"
        "  b-val:\n    run: [bf, validate]\n    hook: pre-commit\n"
        "  b-eval:\n    run: [bf, eval]\n    events:\n      - kind: a\n      - kind: b\n"
        "  b-rev:\n    run: [bf, review]\n"
        '  act-job:\n    run: [echo]\n    action:\n      keep: 1\n    events:\n      - kind: "ev\\u200b"\n'
        "  idle-ing:\n    run: [echo]\n    ingest: raw\n"
        "  idle-plain:\n    run: [echo]\n"
        "  failing:\n    run: [echo]\n    schedule: 1m\n"
        "  off-job:\n    run: [bf, build]\n    enabled: false\n"
        "services:\n"
        "  live-msg:\n    run: [echo]\n    emit: messages\n"
        "  live-ev:\n    run: [echo]\n    emit: events\n"
        "  daemon:\n    run: [echo]\n"
        "  halted:\n    run: [echo]\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )

    def save_state(
        name: str,
        kind: str,
        status: str,
        *,
        ago_s: int = 10,
        summary: dict[str, object] | None = None,
        err: str = "",
        consec: int = 0,
    ) -> None:
        ts = (now - timedelta(seconds=ago_s)).isoformat().replace("+00:00", "Z")
        doc: dict[str, object] = {
            "kind": kind,
            "name": name,
            "status": status,
            "updated": ts,
            "last_start": ts,
            "last_end": "" if status in {"running", "stopped"} else ts,
            "last_success": "" if status == "failed" else ts,
            "runs": 2,
            "failures": consec,
            "consecutive_failures": consec,
            "duration_ms": 250,
            "last_run": summary or {},
            "error": err,
        }
        jobs.save_unit_state(store, doc)

    save_state(
        "ing-diff",
        "job",
        "ok",
        ago_s=2,
        summary={"records": 5, "added": 2, "updated": 1, "removed": 0, "incomplete": True},
    )
    save_state("ing-same", "job", "ok", ago_s=7200, summary={"records": 4, "added": 0, "updated": 0, "removed": 0})
    save_state("b-build", "job", "ok", ago_s=120, summary={"notes": 3, "written": 1})
    save_state("b-val", "job", "ok", ago_s=90000, summary={"ok": True, "notes": 3, "records": 5})
    save_state("b-eval", "job", "ok", ago_s=30, summary={"passed": 4, "failed": 0})
    save_state("b-rev", "job", "ok", ago_s=30, summary={"stale": [{"ref": "concepts/welcome.md"}], "tasks_open": 1})
    save_state("act-job", "job", "ok", ago_s=30, summary={"action": "actions/2026-11-16_demo/ACTION.md"})
    save_state(
        "failing",
        "job",
        "failed",
        ago_s=15,
        err="Traceback (most recent call last):\n  File x.py\nboom \x1b[31m  bad",
        consec=3,
    )
    save_state("live-msg", "service", "running", ago_s=3600)
    save_state("daemon", "service", "running", ago_s=45)
    save_state("halted", "service", "stopped", ago_s=300)
    # Corrupt one `.bf/<name>.json` file to verify per-row resilience!
    private_dir(store).write("idle-ing.json", b"{corrupt json")

    out_txt = render_text(store, now=now, max_rows=50)
    assert "\x1b" not in out_txt
    assert "\u200b" not in out_txt
    assert "2 failed · 2 running · 7 ok · 1 event · 1 disabled" in out_txt
    # Services sit in their own section above Jobs; inside Jobs, failed sorts ahead of ok (newest first)!
    assert out_txt.index("Services (4)") < out_txt.index("Jobs (10)")
    assert out_txt.index("failing") < out_txt.index("ing-diff") < out_txt.index("b-val")
    for expected in (
        "(3x) boom \\u001b[31m bad",
        "idle-ing.json: invalid JSON",
        "on ev\\u200b",
        "items: +2 ~1 · 5 records · incomplete · 250ms",
        "just now",
        "archive: 4 records unchanged",
        "cron 0 8 * * 1-5",
        "cron 0 9 * * *",
        "3 notes (1 built)",
        "valid (3 notes, 5 records)",
        "1d ago",
        "on 2 events",
        "4/4 passed",
        "1 stale · 1 tasks",
        "→ actions/2026-11-16_demo/ACTION.md",
        "service · messages",
        "service · events",
        "service · daemon",
        "listening",
        "ready",
        "up 1h",
        "up 45s",
        "○ stopped",
    ):
        assert expected in out_txt, f"missing {expected!r} in:\n{out_txt}"

    # Verify 80-column narrow layout never crops State (`● running`) or When (`just now`)!
    monkeypatch.setattr(watch.shutil, "get_terminal_size", lambda _fb=(100, 40): os.terminal_size((80, 40)))
    narrow_txt = render_text(store, now=now, max_rows=50, width=80)
    assert "● running" in narrow_txt
    assert "just now" in narrow_txt

    capped_txt = render_text(store, now=now, max_rows=3)
    assert "+ 8 more" in capped_txt
    assert "7 ok · 1 idle" in capped_txt

    # Verify cron DST transition handling + ServiceSpec emit=events + watch.serve `--json` structlog lines!
    paris_cfg = config.JobSpec(name="paris", run=("echo",), schedule="0 2 * * *", cron="0 2 * * *")
    pre_dst = datetime(2026, 3, 29, 0, 59, tzinfo=UTC)  # 01:59 CET before 02:00->03:00 CET->CEST spring forward
    assert not jobs.is_schedule_due(
        paris_cfg, {"last_scheduled": "2026-03-28T01:00:00Z"}, pre_dst, tz_name="Europe/Paris"
    )
    post_dst = datetime(2026, 3, 29, 1, 5, tzinfo=UTC)  # 03:05 CEST after spring-forward 02:xx gap
    assert jobs.is_schedule_due(paris_cfg, {"last_scheduled": "2026-03-28T01:00:00Z"}, post_dst, tz_name="Europe/Paris")

    # Exercise `watch.serve` with `use_tui=True` cleanly via an in-memory Rich Console!
    tui_buf = io.StringIO()
    stop_tui = threading.Event()
    ticks = 0
    real_tick = watch.tick

    def stop_on_second_tick(
        s: Store, *, rebuild_notes: bool = True, now: datetime | None = None, **_kw: object
    ) -> dict[str, object]:
        nonlocal ticks
        ticks += 1
        if ticks >= 2:
            stop_tui.set()
        return real_tick(s, rebuild_notes=rebuild_notes, now=now)

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(watch, "Console", lambda **_kw: Console(file=tui_buf, width=100, force_terminal=True))
    monkeypatch.setattr(watch, "tick", stop_on_second_tick)
    res = watch.serve(empty_store, stop_event=stop_tui)
    assert res["ok"] is True
    assert "empty-tui" in tui_buf.getvalue()

    # `ServiceSpec(emit="events")` streams JSON stdout objects from `services.<name>.run` into `.bf/events.jsonl`!
    svc_root = tmp_path / "svc-events"
    engine.init_brain(svc_root, name="svc-events")
    svc_store = Store(svc_root)
    svc_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: svc-events\n"
        "services:\n  emitter:\n"
        f'    run: [{sys.executable!r}, -c, \'import json; print(json.dumps({{"id": "s1", "kind": "svc.ping"}}, ), flush=True)\']\n'
        "    emit: events\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )
    svc_cfg = config.load(svc_store)
    s_stop, s_wake, s_ui = threading.Event(), threading.Event(), threading.Event()
    svc_d = watch.DaemonStream(svc_store, svc_cfg.services["emitter"], s_stop, s_wake, s_ui)
    svc_d.start()
    assert s_wake.wait(timeout=5.0)
    s_stop.set()
    svc_d.terminate()
    svc_evs, _ = records.read_events(svc_store)
    assert any(ev["kind"] == "svc.ping" and ev["source"] == "emitter" for ev in svc_evs)

    # Oversized daemon stdout line (`> MAX_BATCH`) records `"failed"` even when SIGTERM handler exits `0`!
    monkeypatch.setattr(models, "MAX_BATCH", 64)
    svc_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: svc-events\nservices:\n  huge:\n"
        f"    run: [{sys.executable!r}, -c, \"import signal, sys, time; signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0)); print('x' * 120, flush=True); time.sleep(5)\"]\n"
        "    emit: events\n".encode(),
    )
    huge_cfg = config.load(svc_store)
    h_stop, h_wake = threading.Event(), threading.Event()
    huge_d = watch.DaemonStream(svc_store, huge_cfg.services["huge"], h_stop, h_wake)
    huge_d.start()
    h_deadline = time.monotonic() + 5.0
    while (
        time.monotonic() < h_deadline and jobs.load_unit_state(svc_store, "huge", kind="service")["status"] != "failed"
    ):
        time.sleep(0.02)
    h_stop.set()
    huge_d.terminate()
    huge_st = jobs.load_unit_state(svc_store, "huge", kind="service")
    assert huge_st["status"] == "failed"
    assert "exceeds 64 bytes" in huge_st["error"]


def test_job_popen_failure_secret_redaction_and_watch_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "sec-watch"
    engine.init_brain(root, name="sec-watch")
    store = Store(root)
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: sec-watch\n"
        "jobs:\n"
        "  missing-bin:\n    run: [nonexistent-bf-bin-xyz]\n"
        "  leaky:\n"
        f"    run: [{sys.executable!r}, -c, \"import sys; sys.stderr.write('Authorization: bearer secret_tok_12345678~v2 ghp_1234567890abcdef\\n-----BEGIN PRIVATE KEY-----\\nABC\\n-----END PRIVATE KEY-----\\n'); sys.exit(2)\"]\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )
    cfg = config.load(store)

    # Failed Popen (`OSError`) never leaves `.bf/<job>.json` stuck in `"running"`.
    missing_res = jobs.execute_one_job(store, cfg.jobs["missing-bin"], trigger="manual")
    assert (missing_res["status"], missing_res["exit_code"]) == ("failed", 127)
    assert jobs.load_unit_state(store, "missing-bin", kind="job")["status"] == "failed"
    assert oct((root / "logs").stat().st_mode & 0o777) == "0o700"
    assert oct((root / "logs" / "missing-bin.log").stat().st_mode & 0o777) == "0o600"

    # Secrets in failing job stderr are redacted in `.bf/`, CLI result, and `logs/`.
    leaky_res = jobs.execute_one_job(store, cfg.jobs["leaky"], trigger="manual")
    assert leaky_res["status"] == "failed"
    for secret in ("secret_tok_12345678~v2", "ghp_1234567890abcdef", "BEGIN PRIVATE KEY", "ABC"):
        assert secret not in leaky_res["error"]
        assert secret not in jobs.load_unit_state(store, "leaky", kind="job")["error"]
        assert secret not in (root / "logs" / "leaky.log").read_text()
    assert "[REDACTED]" in leaky_res["error"]

    # `watch.serve(json_stream=True)` emits `structlog` JSON `watch.tick` lines on stdout.
    stop_json = threading.Event()
    real_tick = watch.tick

    def stop_tick(
        s: Store, *, rebuild_notes: bool = True, now: datetime | None = None, **_kw: object
    ) -> dict[str, object]:
        out_tick = real_tick(s, rebuild_notes=rebuild_notes, now=now)
        stop_json.set()
        return out_tick

    monkeypatch.setattr(watch, "tick", stop_tick)
    capsys.readouterr()
    res = watch.serve(store, json_stream=True, stop_event=stop_json)
    assert res["ok"] is True
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(ev.get("message") == "watch.tick" and ev.get("brain") == "sec-watch" for ev in lines)

    # Failed first tick keeps `rebuild_notes=True` on retry, and `watch_files` logs `OSError` instead of dying silently.
    rebuilds: list[bool] = []
    stop_retry = threading.Event()

    def flaky_tick(
        s: Store, *, rebuild_notes: bool = True, now: datetime | None = None, **_kw: object
    ) -> dict[str, object]:
        rebuilds.append(rebuild_notes)
        if len(rebuilds) == 1:
            raise Error("transient lock")
        stop_retry.set()
        return real_tick(s, rebuild_notes=rebuild_notes, now=now)

    monkeypatch.setattr(watch, "tick", flaky_tick)
    monkeypatch.setattr(watch, "watch_fs", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("inotify limit")))
    capsys.readouterr()
    retry_res = watch.serve(store, json_stream=True, stop_event=stop_retry)
    assert retry_res["ok"] is True
    assert rebuilds[:2] == [True, True]
    assert "OSError" in capsys.readouterr().err


def test_batched_jq_filters_stop_event_due_isolation_and_live_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hardened-orch"
    engine.init_brain(root, name="hardened-orch")
    store = Store(root)
    (root / "routines").mkdir(exist_ok=True)
    (root / "routines" / "hang.py").write_text(
        "import pathlib, time\npathlib.Path('routines/started').write_text('1')\ntime.sleep(30)\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: hardened-orch\n"
        "jobs:\n"
        "  broken-req:\n    run: [echo, no]\n    requires: [missing-bin-bf-isolated]\n    schedule: 1m\n"
        "  broken-inp:\n    run: [echo, no]\n    input: prompts/missing.md\n    schedule: 1m\n"
        f"  healthy:\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n    schedule: 1m\n"
        f"  ev-zero:\n    run: [{sys.executable!r}, -c, 'pass']\n    events:\n      - kind: custom.num\n        filter: '.data.n'\n"
        f"  ev-env:\n    run: [{sys.executable!r}, -c, 'pass']\n    events:\n      - kind: custom.num\n        filter: '$ENV.SECRET_VAR != null'\n"
        f"  hang:\n    run: [{sys.executable!r}, routines/hang.py]\n    enabled: false\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )

    # 1. `due_only=True` isolates broken `requires:` and `input:` per job so `healthy` still runs!
    t0 = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    due_res = jobs.run_jobs(store, due_only=True, now=t0)
    by_job = {r["job"]: r["status"] for r in due_res["results"]}
    assert by_job == {"broken-req": "failed", "broken-inp": "failed", "healthy": "ok"}

    # 2. Batched `pending_events` jq filters treat `0` as truthy (`jq -e`), `false`/`null`/`empty` as falsy, and strip `$ENV`!
    monkeypatch.setenv("SECRET_VAR", "top-secret")
    records.append_event(store, "custom.num", "src", {"id": "n-false", "n": False}, now=t0)
    records.append_event(store, "custom.num", "src", {"id": "n-null", "n": None}, now=t0)
    records.append_event(store, "custom.num", "src", {"id": "n-zero", "n": 0}, now=t0)
    cfg = config.load(store)
    matched, _ = jobs.pending_events(store, 0, cfg.jobs["ev-zero"].events)
    assert [ev["data"]["id"] for ev in matched] == ["n-zero"]
    env_matched, _ = jobs.pending_events(store, 0, cfg.jobs["ev-env"].events)
    assert env_matched == []
    bad_spec = config.JobSpec(
        name="bad-flt", run=("echo",), events=(config.EventMatch(kind="custom.num", filter='modulemeta("x")'),)
    )
    with pytest.raises(Error, match="event filter failed"):
        jobs.pending_events(store, 0, bad_spec.events)

    # 3. `stop_event` terminates in-flight job process groups and marks the job `"stopped"` with `failures == 0`!
    stop_hang = threading.Event()
    hang_out: list[dict[str, Any]] = []
    hang_t = threading.Thread(
        target=lambda: hang_out.append(jobs.run_jobs(store, ("hang",), now=t0, stop_event=stop_hang)), daemon=True
    )
    hang_t.start()
    wait_until = time.monotonic() + 5.0
    while time.monotonic() < wait_until and not (root / "routines" / "started").exists():
        time.sleep(0.02)
    stop_hang.set()
    hang_t.join(timeout=5.0)
    assert hang_out[0]["results"][0]["status"] == "stopped"
    hang_state = jobs.load_unit_state(store, "hang", kind="job")
    assert (hang_state["status"], hang_state["failures"], hang_state["consecutive_failures"]) == ("stopped", 0, 0)

    # 4. Live `bf.yaml` reload in `watch.serve` starts newly added daemons without restarting `bf watch`!
    live_root = tmp_path / "live-reload"
    engine.init_brain(live_root, name="live-reload")
    live_store = Store(live_root)
    live_store.write(
        "bf.yaml", f"version: {FORMAT}\nname: live-reload\nwatch:\n  interval: 500ms\n  debounce: 50ms\n".encode()
    )
    stop_live = threading.Event()
    live_t = threading.Thread(
        target=lambda: watch.serve(live_store, json_stream=True, stop_event=stop_live), daemon=True
    )
    live_t.start()
    try:
        time.sleep(0.15)
        live_store.write(
            "bf.yaml",
            f"version: {FORMAT}\nname: live-reload\nservices:\n  added:\n"
            f'    run: [{sys.executable!r}, -c, \'import json, time; print(json.dumps({{"id": "lr-1", "kind": "reload.ok"}}), flush=True); time.sleep(2)\']\n'
            "    emit: events\nwatch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
        )
        lr_deadline = time.monotonic() + 8.0
        found_reload = False
        while time.monotonic() < lr_deadline:
            evs, _ = records.read_events(live_store)
            if any(e.get("kind") == "reload.ok" for e in evs):
                found_reload = True
                break
            time.sleep(0.05)
        assert found_reload
        # Disabling the live service stops it cleanly via `reconcile_config` without restarting `bf watch`!
        live_store.write(
            "bf.yaml", f"version: {FORMAT}\nname: live-reload\nwatch:\n  interval: 500ms\n  debounce: 50ms\n".encode()
        )
        stop_dl = time.monotonic() + 8.0
        while (
            time.monotonic() < stop_dl
            and jobs.load_unit_state(live_store, "added", kind="service")["status"] != "stopped"
        ):
            time.sleep(0.05)
        assert jobs.load_unit_state(live_store, "added", kind="service")["status"] == "stopped"
    finally:
        stop_live.set()
        live_t.join(timeout=6.0)

    # 5. Read-only `status_summary` never creates `.bf/`, and `pending_events` handles comments, heterogeneous nulls, timeouts, and `--due` filter isolation!
    ro_root = tmp_path / "ro-status"
    engine.init_brain(ro_root, name="ro-status")
    import shutil as _shutil

    _shutil.rmtree(ro_root / ".bf")
    assert jobs.status_summary(Store(ro_root))["events_seq"] == 0
    assert not (ro_root / ".bf").exists()

    records.append_event(store, "custom.het", "src", {"id": "h-null", "arr": None}, now=t0)
    records.append_event(store, "custom.het", "src", {"id": "h-hit", "arr": [1]}, now=t0)
    het_rules = (config.EventMatch(kind="custom.het", filter=".data.arr[] == 1 # comment"),)
    het_matched, _ = jobs.pending_events(store, 0, het_rules)
    assert [ev["data"]["id"] for ev in het_matched] == ["h-hit"]

    iso_root = tmp_path / "iso-filter"
    engine.init_brain(iso_root, name="iso-filter")
    iso_store = Store(iso_root)
    iso_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: iso-filter\njobs:\n"
        "  bad-flt:\n    run: [echo, no]\n    events:\n      - kind: custom.x\n        filter: '[[['\n"
        f"  good-job:\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n    schedule: 1m\n".encode(),
    )
    records.append_event(iso_store, "custom.x", "src", {"id": "x1"}, now=t0)
    iso_res = jobs.run_jobs(iso_store, due_only=True, now=t0)
    assert {r["job"]: r["status"] for r in iso_res["results"]} == {"bad-flt": "failed", "good-job": "ok"}

    import subprocess as _sub

    monkeypatch.setattr(_sub, "run", lambda *_a, **_kw: (_ for _ in ()).throw(_sub.TimeoutExpired(["jq"], 5)))
    with pytest.raises(Error, match="event filter timed out after 5s"):
        jobs.pending_events(store, 0, het_rules)

    # Stale pre-reset `cursor_seq > disk_seq` never overwrites a healed post-reset `event_seq`:
    reset_root = tmp_path / "cursor-reset"
    engine.init_brain(reset_root, name="cursor-reset")
    reset_store = Store(reset_root)
    jobs.save_unit_state(reset_store, {**jobs.load_unit_state(reset_store, "j1"), "event_seq": 1})
    jobs.record_run(reset_store, "job", "j1", "r1", "2026-11-16T08:00:00Z", ended=t0, dur_ms=1, code=0, cursor_seq=100)
    assert jobs.load_unit_state(reset_store, "j1")["event_seq"] == 1
