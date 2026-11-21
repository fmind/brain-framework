"""Tests for `loops.py` and `watch.py`: unified loops, schedules, `.bf/`, JSON logs, emit chaining, concurrency, and watch."""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from bf import config, engine, loops, models, records, watch
from bf.models import FORMAT, Error, InputError, decode
from bf.storage import Store, private_dir


def test_loops_schedules_events_hooks_input_daemons_emit_and_timeout(tmp_path: Path) -> None:
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
        "assert ctx['stdin'] == '' and ctx['input'] == prompt_path and ctx['loop']\n"
        "assert stdin_txt.startswith('# ')\n"
        "out = {'kind': ctx['kind'], 'events': len(ctx['events']), 'prompt': pathlib.Path(prompt_path).name}\n"
        "open('services/sent.jsonl', 'a').write(json.dumps(out) + '\\n')\n"
        "print(json.dumps(out))\n"
    )
    (root / "routines" / "emit_events.py").write_text(
        "import json\n"
        "print(json.dumps({'kind': 'task.delegate', 'id': 'd1', 'target': 'worker'}))\n"
        "print(json.dumps({'id': 'd2', 'note': 'default-kind'}))\n"
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
        "loops:\n"
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
        "    schedule:\n"
        "      - 1d\n"
        "      - '0 8 * * 0'\n"
        "  assistant:\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, '{{{{context}}}}', '{{{{input}}}}']\n"
        "    input: prompts/reply.md\n"
        "    events:\n"
        "      - kind: loop.message\n"
        "        source: chat\n"
        "        filter: '.data.direct == true'\n"
        "  ev-emitter:\n"
        f"    run: [{sys.executable!r}, routines/emit_events.py]\n"
        "    emit: true\n"
        "    enabled: false\n"
        "  delegated:\n"
        f"    run: [{sys.executable!r}, -c, 'print(\"delegated-ok\")']\n"
        "    events:\n"
        "      - kind: task.delegate\n"
        "        source: ev-emitter\n"
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
        "  chat:\n"
        f"    run: [{sys.executable!r}, services/mock_chat.py]\n"
        "    daemon: true\n"
        "    emit: loop.message\n".encode(),
    )

    # 2026-11-15 is a Sunday! At 09:00 UTC both `feed` (`1h`) and `brief` (`[1d, '0 8 * * 0']`) are due.
    t0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    dry = loops.run_loops(store, ("feed",), dry_run=True, now=t0)
    assert dry["results"][0]["status"] == "dry_run"

    # `bf run` requires an explicit selector, rejects running daemons, and guards `-- EXTRA_ARGS`.
    with pytest.raises(InputError, match="specify loop names, --due, or --hook"):
        loops.run_loops(store, dry_run=True, now=t0)
    with pytest.raises(InputError, match="specify loop names, --due, or --hook"):
        loops.run_loops(store, ("feed",), due_only=True, now=t0)
    with pytest.raises(InputError, match="is a daemon; run it with bf watch"):
        loops.run_loops(store, ("chat",), now=t0)
    with pytest.raises(InputError, match="require a single loop or --hook"):
        loops.run_loops(store, due_only=True, extra_args=("--x",), now=t0)
    with pytest.raises(InputError, match="require a single loop or --hook"):
        loops.run_loops(store, ("feed", "brief"), extra_args=("--x",), now=t0)

    # `due_only=True` rebuilds dirty notes first before evaluating scheduled/event-triggered loops!
    store.write("concepts/dirty-due.md", b"---\ntype: guide\nstatus: draft\n---\n\n# Dirty Before Due\n")
    due1 = loops.run_loops(store, due_only=True, now=t0)
    assert due1["ok"], due1
    assert [r["loop"] for r in due1["results"]] == ["feed", "brief"]
    assert store.exists("memories/concepts/dirty-due.json")
    feed_saved = decode(store.read(records.record_file("feed", "f1")), "feed")
    assert isinstance(feed_saved, dict)
    assert feed_saved["id"] == "f1"

    # Next tick 5 minutes later: `rebuild` triggers on `records.ingested`!
    t1 = t0 + timedelta(minutes=5)
    due2 = loops.run_loops(store, due_only=True, now=t1)
    assert [r["loop"] for r in due2["results"]] == ["rebuild"]

    # Third tick 5 minutes later: cursor advanced, nothing due!
    due3 = loops.run_loops(store, due_only=True, now=t1 + timedelta(minutes=5))
    assert due3["results"] == []

    # Cycle loop with `emit: true` publishes JSONL events that immediately wake downstream `delegated` loop!
    ev_res = loops.run_loops(store, ("ev-emitter",), now=t1)
    assert ev_res["ok"]
    assert ev_res["results"][0]["summary"] == {"emitted": 2}
    del_res = loops.run_loops(store, due_only=True, now=t1 + timedelta(minutes=6))
    assert [r["loop"] for r in del_res["results"]] == ["delegated"]
    assert del_res["ok"]

    # Invalid emitted output (non-object, non-string kind, or oversized) fails atomically with zero partial commits.
    seq_before = records.latest_event_seq(store)
    with pytest.raises(Error, match="must be a JSON object"):
        loops.emit_output_events(store, "ev-emitter", "loop.event", b'{"kind":"ok.ev","id":"p0"}\n[1,2]\n')
    assert records.latest_event_seq(store) == seq_before
    with pytest.raises(Error, match="event kind must be a string"):
        loops.emit_output_events(store, "ev-emitter", "loop.event", b'{"kind":123}\n')
    assert records.latest_event_seq(store) == seq_before
    with pytest.raises(Error, match="exceeds"):
        loops.emit_output_events(store, "ev-emitter", "loop.event", b"x" * (models.MAX_BATCH + 1))

    # Git hook execution (`pre-commit` and `pre-push` with extra_args and stdin_bytes)!
    hk1 = loops.run_loops(store, hook="pre-commit", now=t1)
    assert hk1["ok"]
    assert [r["loop"] for r in hk1["results"]] == ["guard-commit"]
    hk2 = loops.run_loops(
        store, hook="pre-push", extra_args=("--pre-push", "origin"), stdin_bytes=b"refs/heads/main", now=t1
    )
    assert hk2["ok"]
    assert (root / "routines" / "hook.args").read_text() == "--pre-push origin|refs/heads/main"

    # Reactive `loops.assistant` triggered via `watch.tick` on `loop.message`!
    records.append_event(
        store,
        "loop.message",
        "chat",
        {"id": "ev-msg-9", "conversation": "space-9/threads/t-1", "text": "hi assistant", "direct": True},
        now=t1,
    )
    tick_out = watch.tick(store, now=t1 + timedelta(minutes=7))
    assert tick_out["loops_ran"] == 1
    assert tick_out["ok"]
    sent_after_lines = [json.loads(ln) for ln in (root / "services" / "sent.jsonl").read_text().splitlines()]
    assert any(m == {"kind": "event", "events": 1, "prompt": "reply.md"} for m in sent_after_lines)

    # `DaemonStream` captures JSON line output from `loops.chat` (`daemon: true`, `emit: loop.message`) into `.bf/events.jsonl`!
    cfg = config.load(store)
    stop_ev, wake_ev = threading.Event(), threading.Event()
    daemon = watch.DaemonStream(store, cfg.loops["chat"], stop_ev, wake_ev)
    daemon.start()
    assert wake_ev.wait(timeout=5.0)
    stop_ev.set()
    daemon.terminate()
    ev_items, _ = records.read_events(store)
    assert not (root / ".bf" / "state").exists()
    assert not (root / ".bf" / "locks").exists()
    assert any(
        ev["kind"] == "loop.message" and ev["source"] == "chat" and ev["data"].get("text") == "ping from chat"
        for ev in ev_items
    )

    # Timeout reaps slow process group and records failure in `.bf/slow.json`; `status_summary` includes `counts` + `attention`!
    slow_res = loops.run_loops(store, ("slow",), now=t1)
    assert not slow_res["ok"]
    assert slow_res["results"][0]["exit_code"] == 124
    assert "timed out" in slow_res["results"][0]["error"]
    st_sum = loops.status_summary(store, check=True)
    assert st_sum["ok"]
    assert set(st_sum["counts"]) == {"notes", "records", "sources", "loops"}
    assert st_sum["counts"]["notes"] >= 2
    assert st_sum["counts"]["loops"] == len(loops.status_summary(store, all_units=True)["loops"])
    assert st_sum["attention"] == []
    assert st_sum["orphaned_states"] == []
    assert "loops" not in st_sum

    # Render TUI dashboard panel (single unified Loops table) and supervise file changes via `watch.serve`!
    buf = io.StringIO()
    Console(file=buf, width=120, force_terminal=False).print(watch.render_dashboard(store))
    rendered = buf.getvalue()
    assert "orch · watch" in rendered
    assert "daemon · loop.message" in rendered
    assert "2 schedules" in rendered
    assert "+1 · 1 record" in rendered
    assert "2 disabled" in rendered

    # Non-TTY `watch.serve` automatically emits JSON tick lines and rebuilds live edits!
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

    # Builtin `review`/`eval`, relative script executable, `requires`, missing input, failed loop attention!
    rel_exec = root / "routines" / "exec_rel.py"
    rel_exec.write_text(f"#!{sys.executable}\nprint('---\\ntype: action\\nstatus: draft\\n---\\n# Report\\n')\n")
    rel_exec.chmod(0o755)
    (root / "sensors" / "bad_json.py").write_text("print('{not valid json')\n")
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: orch\nloops:\n"
        "  rev:\n    run: [bf, review]\n"
        "  ev:\n    run: [bf, eval]\n"
        "  rel-script:\n    run: [routines/exec_rel.py]\n    action:\n      keep: 1\n"
        "  need-missing:\n    run: [bf, build]\n    requires: [no-such-binary-bf-xyz]\n"
        f"  bad-input:\n    run: [{sys.executable!r}, -c, 'pass']\n    input: prompts/missing.md\n"
        f"  bad-ing:\n    run: [{sys.executable!r}, sensors/bad_json.py]\n    ingest: feed\n"
        f"  exit-fail:\n    run: [{sys.executable!r}, -c, 'import sys; sys.exit(2)']\n".encode(),
    )
    rev_ev = loops.run_loops(store, ("rev", "ev", "rel-script"), now=t1)
    assert rev_ev["ok"]
    assert loops.run_loops(store, ("rel-script",), now=t1)["ok"]
    assert len([p for p in store.files("actions") if "rel-script-" in p]) == 1
    bad_ing = loops.run_loops(store, ("bad-ing",), now=t1)
    assert not bad_ing["ok"]
    pf_res = loops.run_loops(store, ("exit-fail",), now=t1)
    assert not pf_res["ok"]
    assert "exit code 2" in pf_res["results"][0]["error"]
    st_fail = loops.status_summary(store, check=True)
    assert not st_fail["ok"]
    assert {
        "kind": "loop",
        "mode": "cycle",
        "name": "exit-fail",
        "status": "failed",
        "failed": True,
        "consecutive_failures": 1,
        "error": "exit code 2",
    } in st_fail["attention"]
    with pytest.raises(Error, match="no-such-binary-bf-xyz"):
        loops.run_loops(store, ("need-missing",), now=t1)
    with pytest.raises(Error, match="input file"):
        loops.run_loops(store, ("bad-input",), now=t1)

    for bad_call in (
        lambda: loops.exec_argv((), cwd=root, env={}, timeout=5),
        lambda: loops.run_loops(store, ("missing-loop",)),
        lambda: loops.run_loops(store, hook="post-merge"),
    ):
        with pytest.raises((Error, InputError)):
            bad_call()


def test_concurrency_nonblocking_watch_and_multi_schedule(tmp_path: Path) -> None:
    root = tmp_path / "conc"
    engine.init_brain(root, name="conc")
    store = Store(root)
    (root / "routines").mkdir(exist_ok=True)
    (root / "routines" / "barrier.py").write_text(
        "import json, pathlib, sys, time\n"
        "p = pathlib.Path('routines')\n"
        "(p / f'up.{sys.argv[1]}').write_text('1')\n"
        "dl = time.monotonic() + 5.0\n"
        "while time.monotonic() < dl and len(list(p.glob('up.*'))) < 3:\n"
        "    time.sleep(0.01)\n"
        "assert len(list(p.glob('up.*'))) == 3, 'loops did not overlap concurrently'\n"
        "print(json.dumps({'ok': True}))\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: conc\nloops:\n"
        f"  a:\n    run: [{sys.executable!r}, routines/barrier.py, a]\n    schedule: 1h\n"
        f"  b:\n    run: [{sys.executable!r}, routines/barrier.py, b]\n    schedule: 1h\n"
        f"  c:\n    run: [{sys.executable!r}, routines/barrier.py, c]\n    schedule: 1h\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n  concurrency: 3\n".encode(),
    )
    res = loops.run_loops(store, due_only=True)
    assert res["ok"]
    assert [r["loop"] for r in res["results"]] == ["a", "b", "c"]

    # Bounded concurrency upper bound: `concurrency: 2` with 4 due loops never exceeds 2 active at once.
    (root / "routines" / "cap.py").write_text(
        "import fcntl, json, pathlib, sys, time\n"
        "p = pathlib.Path('routines/cap.state')\n"
        "lk = pathlib.Path('routines/cap.lock').open('w')\n"
        "def bump(delta):\n"
        "    fcntl.flock(lk, fcntl.LOCK_EX)\n"
        "    st = json.loads(p.read_text()) if p.exists() else {'cur': 0, 'peak': 0}\n"
        "    st['cur'] += delta\n"
        "    st['peak'] = max(st['peak'], st['cur'])\n"
        "    p.write_text(json.dumps(st))\n"
        "    fcntl.flock(lk, fcntl.LOCK_UN)\n"
        "bump(1)\n"
        "time.sleep(0.12)\n"
        "bump(-1)\n"
    )
    cap_loops = "".join(
        f"  c{i}:\n    run: [{sys.executable!r}, routines/cap.py, c{i}]\n    schedule: 1h\n" for i in range(4)
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: conc\nloops:\n{cap_loops}watch:\n  interval: 500ms\n  debounce: 50ms\n  concurrency: 2\n".encode(),
    )
    cap_res = loops.run_loops(store, due_only=True)
    assert cap_res["ok"]
    assert cap_res["ran"] == 4
    cap_stat = json.loads((root / "routines" / "cap.state").read_text())
    assert cap_stat["peak"] == 2

    # Non-blocking `watch.serve` + per-loop single-flight: slow loop runs once despite repeated wakeups, never blocks fast-ev.
    (root / "routines" / "slow_bg.py").write_text(
        "import os, pathlib, time\n"
        "with pathlib.Path('routines/slow.pids').open('a') as fh:\n"
        "    fh.write(f'{os.getpid()}\\n')\n"
        "time.sleep(10)\n"
    )
    (root / "routines" / "fast_ev.py").write_text(
        "import pathlib\npathlib.Path('routines/fast.done').write_text('1')\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: conc\nloops:\n"
        f"  slow-bg:\n    run: [{sys.executable!r}, routines/slow_bg.py]\n    schedule: 1s\n    events:\n      - kind: poke.slow\n"
        f"  fast-ev:\n    run: [{sys.executable!r}, routines/fast_ev.py]\n    events:\n      - kind: wake.now\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n  concurrency: 2\n".encode(),
    )
    stop_w = threading.Event()
    wt = threading.Thread(target=lambda: watch.serve(store, json_stream=True, stop_event=stop_w), daemon=True)
    wt.start()
    try:
        dl = time.monotonic() + 5.0
        while time.monotonic() < dl and not (root / "routines" / "slow.pids").exists():
            time.sleep(0.02)
        assert (root / "routines" / "slow.pids").exists()
        # Trigger another event matching `slow-bg` AND `fast-ev` while `slow-bg` is still running.
        records.append_event(store, "poke.slow", "test", {"id": "p1"})
        records.append_event(store, "wake.now", "test", {"id": "w1"})
        dl2 = time.monotonic() + 5.0
        while time.monotonic() < dl2 and not (root / "routines" / "fast.done").exists():
            time.sleep(0.02)
        assert (root / "routines" / "fast.done").exists()
        assert len((root / "routines" / "slow.pids").read_text().splitlines()) == 1
    finally:
        stop_w.set()
        wt.join(timeout=6.0)
        assert not wt.is_alive()

    # Multi-schedule: hourly + Monday 07:30 fires at 07:31 even if hourly ran at 07:15!
    multi = config.LoopSpec(
        name="work",
        run=("echo",),
        schedules=(config.ScheduleSpec("1h", 3600, ""), config.ScheduleSpec("30 7 * * 1", None, "30 7 * * 1")),
    )
    mon_0731 = datetime(2026, 11, 16, 7, 31, tzinfo=UTC)  # Monday
    assert loops.is_schedule_due(multi, {"last_scheduled": "2026-11-16T07:15:00Z"}, mon_0731)
    assert not loops.is_schedule_due(
        multi, {"last_scheduled": "2026-11-16T07:31:00Z"}, mon_0731 + timedelta(minutes=10)
    )


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
        "keys = ('GWS_DIR', 'BASE_ONLY', 'GH_TOKEN', 'LOCAL_SECRET', 'LIT_ENV', 'UNALLOW_HOST', 'BF_BRAIN')\n"
        "val = '|'.join(os.environ.get(k, '') for k in keys)\n"
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
        b"LOCAL_SECRET=from-dotenv\nGWS_DIR=override-gws\nLOOP_KEY=from-dotenv\nLIT_ENV={{home}}/raw\n",
        mode=0o600,
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: resilient\n"
        "env:\n  GWS_DIR: '{{home}}/.config/gws'\n  BASE_ONLY: '{{home}}/base'\n  GH_TOKEN: from-yaml\n  LOCAL_SECRET: default-val\n"
        "loops:\n"
        f"  warn-ok:\n    run: [{sys.executable!r}, routines/warn_ok.py]\n    ingest:\n      source: warn-ok\n      mode: snapshot\n    schedule: 1h\n"
        f'  loop-override:\n    run: [{sys.executable!r}, -c, \'import json, os, sys; print(json.dumps([{{"id":"lo","title":os.environ["LOOP_KEY"]+"|"+"|".join(sys.argv[1:])}}]))\']\n'
        "    ingest: loop-override\n    env:\n      LOOP_KEY: from-loop\n"
        f"  flaky:\n    run: [{sys.executable!r}, routines/fail_fast.py]\n    schedule: 5m\n"
        f"  flaky-ev:\n    run: [{sys.executable!r}, routines/fail_fast.py]\n    events:\n      - kind: loop.message\n"
        f"  chat:\n    run: [{sys.executable!r}, services/dup_stream.py]\n    daemon: true\n    emit: loop.message\n".encode(),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BF_BRAIN", str(tmp_path / "evil-override"))
    monkeypatch.setenv("GH_TOKEN", "from-host")
    monkeypatch.setenv("UNALLOW_HOST", "secret-host-val")

    # Full precedence via real `run_loops`: host allowlist < bf.yaml env < .env < loops.<name>.env < BF_*, plus literal extra_args.
    ok_res = loops.run_loops(store, ("warn-ok",))
    assert ok_res["ok"]
    warn_saved = decode(store.read(records.record_file("warn-ok", "w1")), "warn-ok")
    assert isinstance(warn_saved, dict)
    expected_home = str(Path.home())
    assert (
        warn_saved["title"]
        == f"override-gws|{expected_home}/base|from-yaml|from-dotenv|{{{{home}}}}/raw||{root.resolve()}"
    )
    lo_res = loops.run_loops(store, ("loop-override",), extra_args=("{{brain}}",))
    assert lo_res["ok"]
    lo_saved = decode(store.read(records.record_file("loop-override", "lo")), "loop-override")
    assert isinstance(lo_saved, dict)
    assert lo_saved["title"] == "from-loop|{{brain}}"
    # Stdin broken-pipe regression: a child exiting 0 without draining large stdin still returns exit 0, not 127.
    from bf.proc import exec_argv

    bp_rc, bp_out, bp_err = exec_argv(
        [sys.executable, "-c", "print('ok')"],
        cwd=root,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=10,
        stdin_bytes=b"x" * 262_144,
    )
    assert (bp_rc, bp_out.strip(), bp_err) == (0, b"ok", "")
    warn_lines = [decode(ln, "logs/warn-ok.log") for ln in store.read("logs/warn-ok.log").splitlines() if ln.strip()]
    assert isinstance(warn_lines[-1], dict)
    assert warn_lines[-1]["message"] == "loop.succeeded"
    assert "Skipped feed x" in warn_lines[-1]["stderr"]

    t0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    assert not loops.run_loops(store, ("flaky",), now=t0)["ok"]
    cfg = config.load(store)
    flaky_state = loops.load_unit_state(store, "flaky")
    assert flaky_state["consecutive_failures"] == 1
    assert not loops.is_schedule_due(cfg.loops["flaky"], flaky_state, t0 + timedelta(seconds=10))
    assert loops.is_schedule_due(cfg.loops["flaky"], flaky_state, t0 + timedelta(seconds=65))

    records.append_event(store, "loop.message", "chat", {"id": "ev-b1", "text": "ping"}, now=t0)
    ev_fail = loops.run_loops(store, due_only=True, now=t0)
    assert [r["loop"] for r in ev_fail["results"]] == ["flaky-ev"]
    assert not ev_fail["ok"]
    assert loops.run_loops(store, due_only=True, now=t0 + timedelta(seconds=5))["results"] == []

    summary = loops.status_summary(store, all_units=True)
    assert summary["loops"]["warn-ok"]["ingest_mode"] == "snapshot"
    assert summary["loops"]["warn-ok"]["last_run"]["added"] == 1
    assert "transient err" in summary["loops"]["flaky"]["error"]

    stop_ev, wake_ev = threading.Event(), threading.Event()
    daemon = watch.DaemonStream(store, cfg.loops["chat"], stop_ev, wake_ev)
    daemon.start()
    assert wake_ev.wait(timeout=5.0)
    dup_deadline = time.monotonic() + 5.0
    while time.monotonic() < dup_deadline and not store.exists("logs/chat.log"):
        time.sleep(0.02)
    stop_ev.set()
    daemon.terminate()
    ev_items, _ = records.read_events(store)
    assert sum(1 for e in ev_items if e["data"].get("id") == "m-dup") == 1

    # `watch.serve` catches transient `Error` on a supervisor tick instead of crashing!
    stop_loop = threading.Event()

    def flaky_build(_s: Store, **_kw: object) -> dict[str, object]:
        stop_loop.set()
        raise Error("transient lock busy")

    monkeypatch.setattr(engine, "build", flaky_build)
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
        f"version: {FORMAT}\nname: hybrid\ntimezone: UTC\nloops:\n"
        "  bot:\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, '{{{{context}}}}', bot]\n"
        "    schedule: '0 8 * * *'\n"
        "    events:\n"
        "      - kind: loop.message\n"
        "  cross-alert:\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, '{{{{context}}}}', alerts]\n"
        "    events:\n"
        "      - kind: loop.message\n"
        "        source: chat\n"
        "  hook-every:\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, '{{{{context}}}}', hook]\n"
        "    schedule: 1h\n"
        "    hook: pre-commit\n".encode(),
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8080")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")

    t_ev = datetime(2026, 11, 16, 8, 1, tzinfo=UTC)
    records.append_event(
        store, "loop.message", "chat", {"id": "in-42", "conversation": "room-1/t-9", "text": "hello"}, now=t_ev
    )
    r1 = loops.run_loops(store, due_only=True, now=t_ev)
    assert r1["ok"]
    assert [r["loop"] for r in r1["results"]] == ["bot", "cross-alert", "hook-every"]
    bot_lines = (root / "routines" / "bot.log").read_text().splitlines()
    assert "http://proxy.example.test:8080|localhost,127.0.0.1:1" in bot_lines[0]

    man_res = loops.run_loops(store, ("cross-alert",), now=t_ev + timedelta(minutes=30))
    assert man_res["ok"]
    alert_lines = (root / "routines" / "alerts.log").read_text().splitlines()
    assert alert_lines[-1].startswith("manual:2026-11-16T08:01:00Z:")

    r2 = loops.run_loops(store, due_only=True, now=t_ev + timedelta(minutes=1))
    assert r2["ok"]
    assert [r["loop"] for r in r2["results"]] == ["bot"]
    bot_lines = (root / "routines" / "bot.log").read_text().splitlines()
    assert bot_lines[1].startswith("schedule:")

    records.append_event(store, "loop.message", "chat", {"id": "in-44"}, now=t_ev)
    records.append_event(store, "loop.message", "chat", {"id": "in-45"}, now=t_ev)
    assert [r["loop"] for r in loops.run_loops(store, due_only=True, now=t_ev + timedelta(minutes=5))["results"]] == [
        "bot",
        "cross-alert",
    ]
    assert loops.load_unit_state(store, "cross-alert")["event_seq"] >= 8
    private_dir(store).delete("events.jsonl")
    assert loops.run_loops(store, due_only=True, now=t_ev + timedelta(minutes=5, seconds=30))["results"] == []
    assert loops.load_unit_state(store, "cross-alert")["event_seq"] == 0
    records.append_event(store, "loop.message", "chat", {"id": "after-reset"}, now=t_ev)
    healed = loops.run_loops(store, due_only=True, now=t_ev + timedelta(minutes=6))
    assert [r["loop"] for r in healed["results"]] == ["bot", "cross-alert"]
    assert loops.load_unit_state(store, "cross-alert")["event_seq"] == 1

    assert loops.run_loops(store, hook="pre-commit", now=t_ev + timedelta(minutes=30))["ok"]
    assert loops.run_loops(store, due_only=True, now=t_ev + timedelta(minutes=55))["results"] == []
    due_1h = loops.run_loops(store, due_only=True, now=t_ev + timedelta(minutes=60))
    assert [r["loop"] for r in due_1h["results"]] == ["hook-every"]


def test_watch_supervises_daemon_loop_and_input_loops(tmp_path: Path) -> None:
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
        "loops:\n"
        "  brief:\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, '{{{{input}}}}']\n"
        "    input: prompts/brief.md\n"
        "    action:\n      keep: 1\n"
        "  tcp:\n"
        f"    run: [{sys.executable!r}, services/tcp_srv.py, '{port}']\n"
        "    daemon: true\n"
        "    enabled: true\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )
    ran1 = loops.run_loops(store, ("brief",))
    assert ran1["ok"], ran1
    act1_rel = ran1["results"][0]["summary"]["action"]
    act1_mem = f"memories/actions/{Path(act1_rel).parent.name}.json"
    assert store.exists(act1_mem)
    ran2 = loops.run_loops(store, ("brief",))
    assert ran2["ok"], ran2
    act2_rel = ran2["results"][0]["summary"]["action"]
    assert act2_rel != act1_rel
    assert not store.exists(act1_mem)
    assert engine.validate(store)["ok"]

    conflict_root = tmp_path / "svc-conflict"
    engine.init_brain(conflict_root, name="svc-conflict")
    conflict_store = Store(conflict_root)
    conflict_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: svc-conflict\n"
        "loops:\n"
        "  crash:\n"
        f"    run: [{sys.executable!r}, -c, 'import sys; sys.stderr.write(\"bind failed\\\\n\"); sys.exit(1)']\n"
        "    daemon: true\n"
        "    enabled: true\n"
        "  badreq:\n    run: [echo, hi]\n    daemon: true\n    requires: [nonexistent-req-cmd-xyz]\n"
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
                st = loops.status_summary(conflict_store, all_units=True)
                if st["loops"]["crash"]["consecutive_failures"] >= 1 and st["loops"]["badreq"]["status"] == "failed":
                    break
                time.sleep(0.05)
        finally:
            stop_conflict.set()
            conflict_thread.join(timeout=6)
    finally:
        stop_ev.set()
        srv_thread.join(timeout=6)

    assert resp_text == "daemon-ready"
    assert loops.load_unit_state(store, "tcp")["status"] == "stopped"

    conflict_status = loops.status_summary(conflict_store, check=True, all_units=True)
    assert not conflict_status["ok"]
    assert conflict_status["loops"]["crash"]["status"] == "failed"
    assert conflict_status["loops"]["crash"]["consecutive_failures"] >= 1
    assert conflict_status["loops"]["badreq"]["status"] == "failed"
    assert "nonexistent-req-cmd-xyz" in conflict_status["loops"]["badreq"]["error"]


def test_dual_schedule_and_event_loop_advances_cursor_after_scheduled_run(tmp_path: Path) -> None:
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
        "loops:\n"
        "  combo:\n"
        f"    run: [{sys.executable!r}, routines/count.py, '{{{{context}}}}']\n"
        "    schedule: 1h\n"
        "    events:\n"
        "      - kind: custom.ping\n".encode(),
    )

    ev1 = records.append_event(store, "custom.other", "test", {"id": "o1"})
    dry = loops.run_loops(store, due_only=True, dry_run=True)
    assert [item["loop"] for item in dry["results"]] == ["combo"]
    state_path = root / ".bf" / "combo.json"
    assert not state_path.exists()

    first = loops.run_loops(store, due_only=True)
    assert [item["loop"] for item in first["results"]] == ["combo"]
    assert first["results"][0]["status"] == "ok"
    seen = decode((root / "routines" / "seen.json").read_bytes(), "seen.json")
    assert isinstance(seen, dict)
    assert seen["kind"] == "schedule"
    assert seen["events"] == []
    st1 = decode(state_path.read_bytes(), str(state_path))
    assert isinstance(st1, dict)
    assert st1["event_seq"] == ev1["seq"]

    ev2 = records.append_event(store, "custom.other", "test", {"id": "o2"})
    second = loops.run_loops(store, due_only=True)
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
    assert "No enabled loops in bf.yaml" in empty_txt
    assert "12:30:00 UTC" in empty_txt

    root = tmp_path / "rich-tui"
    engine.init_brain(root, name="rich-tui")
    store = Store(root)
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: rich-tui\n"
        "loops:\n"
        "  ing-diff:\n    run: [echo]\n    ingest: items\n    schedule: 30m\n"
        "  ing-same:\n    run: [echo]\n    ingest:\n      source: archive\n      mode: snapshot\n    schedule: '0 8 * * 1-5'\n"
        "  b-build:\n    run: [bf, build]\n    schedule: '0 9 * * *'\n"
        "  b-val:\n    run: [bf, validate]\n    hook: pre-commit\n"
        "  b-eval:\n    run: [bf, eval]\n    events:\n      - kind: a\n      - kind: b\n"
        "  b-rev:\n    run: [bf, review]\n"
        "  b-emit:\n    run: [echo]\n    emit: true\n"
        '  act-loop:\n    run: [echo]\n    action:\n      keep: 1\n    events:\n      - kind: "ev\\u200b"\n'
        "  idle-ing:\n    run: [echo]\n    ingest: raw\n"
        "  idle-plain:\n    run: [echo]\n"
        "  failing:\n    run: [echo]\n    schedule: 1m\n"
        "  off-loop:\n    run: [bf, build]\n    enabled: false\n"
        "  live-msg:\n    run: [echo]\n    daemon: true\n    emit: loop.message\n"
        "  live-ev:\n    run: [echo]\n    daemon: true\n    emit: true\n"
        "  daemon:\n    run: [echo]\n    daemon: true\n"
        "  halted:\n    run: [echo]\n    daemon: true\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )

    def save_state(
        name: str,
        status: str,
        *,
        ago_s: int = 10,
        summary: dict[str, object] | None = None,
        err: str = "",
        consec: int = 0,
    ) -> None:
        ts = (now - timedelta(seconds=ago_s)).isoformat().replace("+00:00", "Z")
        doc: dict[str, object] = {
            "kind": "loop",
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
        loops.save_unit_state(store, doc)

    save_state(
        "ing-diff", "ok", ago_s=2, summary={"records": 5, "added": 2, "updated": 1, "removed": 0, "incomplete": True}
    )
    save_state("ing-same", "ok", ago_s=7200, summary={"records": 4, "added": 0, "updated": 0, "removed": 0})
    save_state("b-build", "ok", ago_s=120, summary={"notes": 3, "written": 1})
    save_state("b-val", "ok", ago_s=90000, summary={"ok": True, "notes": 3, "records": 5})
    save_state("b-eval", "ok", ago_s=30, summary={"passed": 4, "failed": 0})
    save_state("b-rev", "ok", ago_s=30, summary={"stale": [{"ref": "concepts/welcome.md"}], "tasks_open": 1})
    save_state("b-emit", "ok", ago_s=30, summary={"emitted": 2})
    save_state("act-loop", "ok", ago_s=30, summary={"action": "actions/2026-11-16_demo/ACTION.md"})
    save_state(
        "failing",
        "failed",
        ago_s=15,
        err="Traceback (most recent call last):\n  File x.py\nboom \x1b[31m  bad",
        consec=3,
    )
    save_state("live-msg", "running", ago_s=3600)
    save_state("daemon", "running", ago_s=45)
    save_state("halted", "stopped", ago_s=300)
    private_dir(store).write("idle-ing.json", b"{corrupt json")

    out_txt = render_text(store, now=now, max_rows=50)
    assert "\x1b" not in out_txt
    assert "\u200b" not in out_txt
    assert "2 failed · 2 running · 8 ok · 1 event · 1 disabled" in out_txt
    assert "Loops (15)" in out_txt
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
        "2 events emitted",
        "→ actions/2026-11-16_demo/ACTION.md",
        "daemon · loop.message",
        "daemon · loop.event",
        "listening",
        "ready",
        "up 1h",
        "up 45s",
        "○ stopped",
    ):
        assert expected in out_txt, f"missing {expected!r} in:\n{out_txt}"

    monkeypatch.setattr(watch.shutil, "get_terminal_size", lambda _fb=(100, 40): os.terminal_size((80, 40)))
    narrow_txt = render_text(store, now=now, max_rows=50, width=80)
    assert "● running" in narrow_txt
    assert "just now" in narrow_txt

    capped_txt = render_text(store, now=now, max_rows=3)
    assert "+ 13 more" in capped_txt

    paris_cfg = config.LoopSpec(
        name="paris", run=("echo",), schedules=(config.ScheduleSpec("0 2 * * *", None, "0 2 * * *"),)
    )
    pre_dst = datetime(2026, 3, 29, 0, 59, tzinfo=UTC)
    assert not loops.is_schedule_due(
        paris_cfg, {"last_scheduled": "2026-03-28T01:00:00Z"}, pre_dst, tz_name="Europe/Paris"
    )
    post_dst = datetime(2026, 3, 29, 1, 5, tzinfo=UTC)
    assert loops.is_schedule_due(
        paris_cfg, {"last_scheduled": "2026-03-28T01:00:00Z"}, post_dst, tz_name="Europe/Paris"
    )

    # Exercise `watch.serve` with `use_tui=True` cleanly via an in-memory Rich Console!
    tui_buf = io.StringIO()
    stop_tui = threading.Event()
    calls = 0
    real_sel = loops.select_due_loops

    def stop_on_second_tick(*a: Any, **kw: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls >= 2:
            stop_tui.set()
        return real_sel(*a, **kw)

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(watch, "Console", lambda **_kw: Console(file=tui_buf, width=100, force_terminal=True))
    monkeypatch.setattr(loops, "select_due_loops", stop_on_second_tick)
    res = watch.serve(empty_store, stop_event=stop_tui)
    assert res["ok"] is True
    assert "empty-tui" in tui_buf.getvalue()

    # Daemon loop with `emit: true` streams JSON stdout objects into `.bf/events.jsonl`!
    svc_root = tmp_path / "svc-events"
    engine.init_brain(svc_root, name="svc-events")
    svc_store = Store(svc_root)
    svc_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: svc-events\n"
        "loops:\n  emitter:\n"
        f'    run: [{sys.executable!r}, -c, \'import json; print("[1,2]", flush=True); print(json.dumps({{"id": "s1", "kind": "svc.ping"}}), flush=True)\']\n'
        "    daemon: true\n"
        "    emit: true\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )
    svc_cfg = config.load(svc_store)
    s_stop, s_wake, s_ui = threading.Event(), threading.Event(), threading.Event()
    svc_d = watch.DaemonStream(svc_store, svc_cfg.loops["emitter"], s_stop, s_wake, s_ui)
    svc_d.start()
    assert s_wake.wait(timeout=5.0)
    s_stop.set()
    svc_d.terminate()
    svc_evs, _ = records.read_events(svc_store)
    assert any(ev["kind"] == "svc.ping" and ev["source"] == "emitter" for ev in svc_evs)

    # Immediate exit-0 daemon does not reset backoff (avoids 1s infinite restart spin).
    waits: list[float] = []
    spin_stop = threading.Event()
    real_wait = spin_stop.wait

    def spy_wait(timeout: float | None = None) -> bool:
        if timeout is not None:
            waits.append(timeout)
        if len(waits) >= 2:
            spin_stop.set()
            return True
        return real_wait(0.01)

    monkeypatch.setattr(spin_stop, "wait", spy_wait)
    spin_unit = config.LoopSpec(name="spin0", run=(sys.executable, "-c", "pass"), daemon=True)
    spin_d = watch.DaemonStream(svc_store, spin_unit, spin_stop, threading.Event())
    spin_d.start()
    spin_d.thread.join(timeout=5.0)
    assert waits == [1.0, 2.0]

    monkeypatch.setattr(models, "MAX_BATCH", 64)
    svc_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: svc-events\nloops:\n  huge:\n"
        f"    run: [{sys.executable!r}, -c, \"import signal, sys, time; signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0)); print('x' * 120, flush=True); time.sleep(5)\"]\n"
        "    daemon: true\n"
        "    emit: true\n".encode(),
    )
    huge_cfg = config.load(svc_store)
    h_stop, h_wake = threading.Event(), threading.Event()
    huge_d = watch.DaemonStream(svc_store, huge_cfg.loops["huge"], h_stop, h_wake)
    huge_d.start()
    h_deadline = time.monotonic() + 5.0
    while time.monotonic() < h_deadline and loops.load_unit_state(svc_store, "huge")["status"] != "failed":
        time.sleep(0.02)
    h_stop.set()
    huge_d.terminate()
    huge_st = loops.load_unit_state(svc_store, "huge")
    assert huge_st["status"] == "failed"
    assert "exceeds 64 bytes" in huge_st["error"]


def test_loop_popen_failure_secret_redaction_and_watch_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "sec-watch"
    engine.init_brain(root, name="sec-watch")
    store = Store(root)
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: sec-watch\n"
        "loops:\n"
        "  missing-bin:\n    run: [nonexistent-bf-bin-xyz]\n"
        "  leaky:\n"
        f"    run: [{sys.executable!r}, -c, \"import sys; sys.stderr.write('Authorization: bearer secret_tok_12345678~v2 ghp_1234567890abcdef\\n-----BEGIN PRIVATE KEY-----\\nABC\\n-----END PRIVATE KEY-----\\n'); sys.exit(2)\"]\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )
    cfg = config.load(store)

    missing_res = loops.execute_one_loop(store, cfg.loops["missing-bin"], trigger="manual")
    assert (missing_res["status"], missing_res["exit_code"]) == ("failed", 127)
    assert loops.load_unit_state(store, "missing-bin")["status"] == "failed"
    assert oct((root / "logs").stat().st_mode & 0o777) == "0o700"
    assert oct((root / "logs" / "missing-bin.log").stat().st_mode & 0o777) == "0o600"

    leaky_res = loops.execute_one_loop(store, cfg.loops["leaky"], trigger="manual")
    assert leaky_res["status"] == "failed"
    for secret in ("secret_tok_12345678~v2", "ghp_1234567890abcdef", "BEGIN PRIVATE KEY", "ABC"):
        assert secret not in leaky_res["error"]
        assert secret not in loops.load_unit_state(store, "leaky")["error"]
        assert secret not in (root / "logs" / "leaky.log").read_text()
    assert "[REDACTED]" in leaky_res["error"]

    # `watch.serve(json_stream=True)` emits JSON `watch.tick` lines on stdout.
    stop_json = threading.Event()
    real_build = engine.build

    def stop_build(s: Store, **kw: Any) -> dict[str, Any]:
        out = real_build(s, **kw)
        stop_json.set()
        return out

    monkeypatch.setattr(engine, "build", stop_build)
    capsys.readouterr()
    res = watch.serve(store, json_stream=True, stop_event=stop_json)
    assert res["ok"] is True
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(ev.get("message") == "watch.tick" and ev.get("brain") == "sec-watch" for ev in lines)

    # Failed first build keeps `first=True` on retry, and `watch_files` logs `OSError` instead of dying silently.
    calls = 0
    stop_retry = threading.Event()

    def flaky_build(s: Store, **kw: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Error("transient lock")
        stop_retry.set()
        return real_build(s, **kw)

    monkeypatch.setattr(engine, "build", flaky_build)
    monkeypatch.setattr(watch, "watch_fs", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("inotify limit")))
    capsys.readouterr()
    retry_res = watch.serve(store, json_stream=True, stop_event=stop_retry)
    assert retry_res["ok"] is True
    assert calls >= 2
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
        "loops:\n"
        "  broken-req:\n    run: [echo, no]\n    requires: [missing-bin-bf-isolated]\n    schedule: 1m\n"
        "  broken-inp:\n    run: [echo, no]\n    input: prompts/missing.md\n    schedule: 1m\n"
        f"  healthy:\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n    schedule: 1m\n"
        f"  ev-zero:\n    run: [{sys.executable!r}, -c, 'pass']\n    events:\n      - kind: custom.num\n        filter: '.data.n'\n"
        f"  ev-env:\n    run: [{sys.executable!r}, -c, 'pass']\n    events:\n      - kind: custom.num\n        filter: '$ENV.SECRET_VAR != null'\n"
        f"  hang:\n    run: [{sys.executable!r}, routines/hang.py]\n    enabled: false\n"
        "watch:\n  interval: 500ms\n  debounce: 50ms\n".encode(),
    )

    t0 = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    due_res = loops.run_loops(store, due_only=True, now=t0)
    by_loop = {r["loop"]: r["status"] for r in due_res["results"]}
    assert by_loop == {"broken-req": "failed", "broken-inp": "failed", "healthy": "ok"}

    monkeypatch.setenv("SECRET_VAR", "top-secret")
    records.append_event(store, "custom.num", "src", {"id": "n-false", "n": False}, now=t0)
    records.append_event(store, "custom.num", "src", {"id": "n-null", "n": None}, now=t0)
    records.append_event(store, "custom.num", "src", {"id": "n-zero", "n": 0}, now=t0)
    cfg = config.load(store)
    matched, _ = loops.pending_events(store, 0, cfg.loops["ev-zero"].events)
    assert [ev["data"]["id"] for ev in matched] == ["n-zero"]
    env_matched, _ = loops.pending_events(store, 0, cfg.loops["ev-env"].events)
    assert env_matched == []
    bad_spec = config.LoopSpec(
        name="bad-flt", run=("echo",), events=(config.EventMatch(kind="custom.num", filter='modulemeta("x")'),)
    )
    with pytest.raises(Error, match="event filter failed"):
        loops.pending_events(store, 0, bad_spec.events)

    stop_hang = threading.Event()
    hang_out: list[dict[str, Any]] = []
    hang_t = threading.Thread(
        target=lambda: hang_out.append(loops.run_loops(store, ("hang",), now=t0, stop_event=stop_hang)), daemon=True
    )
    hang_t.start()
    wait_until = time.monotonic() + 5.0
    while time.monotonic() < wait_until and not (root / "routines" / "started").exists():
        time.sleep(0.02)
    stop_hang.set()
    hang_t.join(timeout=5.0)
    assert hang_out[0]["results"][0]["status"] == "stopped"
    hang_state = loops.load_unit_state(store, "hang")
    assert (hang_state["status"], hang_state["failures"], hang_state["consecutive_failures"]) == ("stopped", 0, 0)

    # Live `bf.yaml` reload in `watch.serve` starts and stops newly added daemon loops cleanly!
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
        added_yaml = (
            f"version: {FORMAT}\nname: live-reload\nloops:\n  added:\n"
            f'    run: [{sys.executable!r}, -c, \'import json, time; print(json.dumps({{"id": "lr-1", "kind": "reload.ok"}}), flush=True); time.sleep(2)\']\n'
            "    daemon: true\n    emit: true\nwatch:\n  interval: 500ms\n  debounce: 50ms\n"
        ).encode()
        lr_deadline = time.monotonic() + 8.0
        found_reload = False
        while time.monotonic() < lr_deadline:
            live_store.write("bf.yaml", added_yaml)
            for _ in range(6):
                evs, _ = records.read_events(live_store)
                if any(e.get("kind") == "reload.ok" for e in evs):
                    found_reload = True
                    break
                time.sleep(0.05)
            if found_reload:
                break
        assert found_reload
        base_yaml = f"version: {FORMAT}\nname: live-reload\nwatch:\n  interval: 500ms\n  debounce: 50ms\n".encode()
        live_store.write("bf.yaml", base_yaml)
        stop_dl = time.monotonic() + 8.0
        while time.monotonic() < stop_dl and loops.load_unit_state(live_store, "added")["status"] != "stopped":
            time.sleep(0.05)
        assert loops.load_unit_state(live_store, "added")["status"] == "stopped"
    finally:
        stop_live.set()
        live_t.join(timeout=6.0)
        assert not live_t.is_alive()

    # BrokenPipeError on `watch.serve(json_stream=True)` triggers clean shutdown without hanging.
    class _BrokenBuf:
        def write(self, _b: bytes) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            pass

    class _BrokenOut:
        buffer = _BrokenBuf()

        def isatty(self) -> bool:
            return False

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stdout", _BrokenOut())
    bp_t = threading.Thread(target=lambda: watch.serve(live_store, json_stream=True), daemon=True)
    bp_t.start()
    bp_t.join(timeout=6.0)
    assert not bp_t.is_alive()
    monkeypatch.undo()

    ro_root = tmp_path / "ro-status"
    engine.init_brain(ro_root, name="ro-status")
    shutil.rmtree(ro_root / ".bf")
    ro_sum = loops.status_summary(Store(ro_root))
    assert ro_sum["events_seq"] == 0
    assert ro_sum["orphaned_states"] == []
    assert not (ro_root / ".bf").exists()

    records.append_event(store, "custom.het", "src", {"id": "h-null", "arr": None}, now=t0)
    records.append_event(store, "custom.het", "src", {"id": "h-hit", "arr": [1]}, now=t0)
    het_rules = (config.EventMatch(kind="custom.het", filter=".data.arr[] == 1 # comment"),)
    het_matched, _ = loops.pending_events(store, 0, het_rules)
    assert [ev["data"]["id"] for ev in het_matched] == ["h-hit"]

    iso_root = tmp_path / "iso-filter"
    engine.init_brain(iso_root, name="iso-filter")
    iso_store = Store(iso_root)
    iso_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: iso-filter\nloops:\n"
        "  bad-flt:\n    run: [echo, no]\n    events:\n      - kind: custom.x\n        filter: '[[['\n"
        f"  good-loop:\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n    schedule: 1m\n".encode(),
    )
    records.append_event(iso_store, "custom.x", "src", {"id": "x1"}, now=t0)
    iso_res = loops.run_loops(iso_store, due_only=True, now=t0)
    assert {r["loop"]: r["status"] for r in iso_res["results"]} == {"bad-flt": "failed", "good-loop": "ok"}

    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(["jq"], 5))
    )
    with pytest.raises(Error, match="event filter timed out after 5s"):
        loops.pending_events(store, 0, het_rules)

    reset_root = tmp_path / "cursor-reset"
    engine.init_brain(reset_root, name="cursor-reset")
    reset_store = Store(reset_root)
    loops.save_unit_state(reset_store, {**loops.load_unit_state(reset_store, "l1"), "event_seq": 1})
    loops.record_run(reset_store, "l1", "r1", "2026-11-16T08:00:00Z", ended=t0, dur_ms=1, code=0, cursor_seq=100)
    assert loops.load_unit_state(reset_store, "l1")["event_seq"] == 1


def test_v30_status_attention_orphans_fields_fallbacks_and_lazy_imports(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, bf.markdown, bf.engine, bf.loops, bf.watch; "
                "bad = {'structlog', 'yaml', 'croniter', 'jsonschema', 'markdown', 'rich', 'watchfiles', 'mcp'}; "
                "assert not (bad & set(sys.modules)), bad & set(sys.modules)"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    root = tmp_path / "v30-ops"
    engine.init_brain(root, name="v30-ops")
    store = Store(root)
    t0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: v30-ops\nloops:\n"
            "  b:\n    run: [bf, build]\n"
            "  v:\n    run: [bf, validate]\n"
            "  c:\n    run: [bf, check]\n"
            "  fail-empty:\n    run: [echo, nope]\n"
        ).encode(),
    )
    assert loops.run_loops(store, ("b", "v", "c"), now=t0)["ok"]

    store.write(
        "concepts/broken.md", b"---\ntype: guide\nstatus: stable\n---\n# Broken\n\n[Missing](concepts/nope.md)\n"
    )
    bv_fail = loops.run_loops(store, ("v", "c"), now=t0)
    assert not bv_fail["ok"]
    assert "concepts/nope.md" in bv_fail["results"][0]["error"]

    loops.record_run(store, "fail-empty", "r1", "2026-11-20T11:00:00Z", ended=t0, dur_ms=10, code=2, err_msg="  ")
    st_doc = loops.load_unit_state(store, "fail-empty")
    assert st_doc["error"] == "exit code 2"
    loops.save_unit_state(store, {**st_doc, "status": "running", "error": ""})
    st_running = loops.status_summary(store)
    assert {
        "kind": "loop",
        "mode": "cycle",
        "name": "fail-empty",
        "status": "running",
        "failed": False,
        "consecutive_failures": 1,
    } in st_running["attention"]
    assert "loops.fail-empty: 1 consecutive failure(s)" in st_running["problems"]

    loops.save_unit_state(store, {**st_doc, "status": "failed", "error": ""})
    st_fb = loops.status_summary(store, fields="brain,ok,attention,orphaned_states")
    assert set(st_fb) == {"brain", "ok", "attention", "orphaned_states"}
    assert any(a["name"] == "fail-empty" and "exit code 2" in a["error"] for a in st_fb["attention"])

    loops.save_unit_state(store, {**st_doc, "name": "no-log", "status": "failed", "error": ""})
    store.write("bf.yaml", store.read("bf.yaml") + b"  no-log:\n    run: [echo, ok]\n  evf:\n    run: [bf, eval]\n")
    assert any(
        a["name"] == "no-log" and a["error"] == "last run failed" for a in loops.status_summary(store)["attention"]
    )
    store.write(
        "evals/fail.yaml",
        f"version: {FORMAT}\ncases:\n  - name: bad\n    query: 'select(false)'\n    min_count: 1\n".encode(),
    )
    ev_fail = loops.run_loops(store, ("evf",), now=t0)
    assert not ev_fail["ok"]
    assert "eval case(s) failed" in ev_fail["results"][0]["error"]
    store.delete("concepts/broken.md")
    chk_ev_fail = loops.run_loops(store, ("c",), now=t0)
    assert not chk_ev_fail["ok"]
    assert chk_ev_fail["results"][0]["error"] == "1 eval case(s) failed"

    loops.save_unit_state(store, {**st_doc, "name": "ws-err", "status": "failed", "error": ""})
    store.write("bf.yaml", store.read("bf.yaml") + b"  ws-err:\n    run: [echo, ok]\n")
    store.append_log(
        "ws-err", models.encode({"message": "loop.failed", "error": "   ", "stderr": "real stderr failure"})
    )
    assert any(
        a["name"] == "ws-err" and a["error"] == "real stderr failure" for a in loops.status_summary(store)["attention"]
    )
    ev_stop = threading.Event()
    ev_stop.set()
    assert loops.exec_argv(["echo", "hi"], cwd=root, env={}, timeout=5, stop_event=ev_stop)[0] == 130
    assert loops.run_loops(store, ("b",), stop_event=ev_stop)["ran"] == 0
    assert loops.pending_events(store, 0, ()) == ([], 0)
    assert loops.pending_events(store, 999999, (config.EventMatch(kind="x"),)) == (
        [],
        loops.records.latest_event_seq(store),
    )

    loops.save_unit_state(store, {**st_doc, "name": "stale_unit"})
    (root / ".bf" / "orphan.context.json").write_text("{}\n")
    (root / ".bf" / "orphan.context.json").chmod(0o600)
    assert "stale_unit" in loops.status_summary(store)["orphaned_states"]
    assert "orphan" not in loops.status_summary(store)["orphaned_states"]
    with pytest.raises(InputError):
        loops.status_summary(store, fields=" , ")


def test_builtin_bf_check_surfaces_eval_failures_and_problems_past_validate_warnings(tmp_path: Path) -> None:
    root = tmp_path / "chk-warn"
    engine.init_brain(root, name="chk-warn")
    store = Store(root)
    t0 = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)
    store.write("bf.yaml", f"version: {FORMAT}\nname: chk-warn\nloops:\n  check-loop:\n    run: [bf, check]\n".encode())
    store.write("memories/jira/.forgotten", b"PROJ-999\n")
    store.write(
        "concepts/warn-note.md",
        b"---\ntype: guide\nstatus: stable\n---\n\n# Warn Note\n\nSee [ticket](jira:PROJ-999).\n",
    )
    engine.build(store)
    store.write(
        "evals/smoke.yaml",
        f"version: {FORMAT}\ncases:\n  - name: smoke-miss\n    query: 'definitely-missing-token-xyz'\n    min_count: 1\n".encode(),
    )

    val = engine.validate(store)
    assert val["ok"] is True
    assert val["warnings_count"] == 1
    assert val["problems"]

    cfg = config.load(store)
    res1 = loops.execute_one_loop(store, cfg.loops["check-loop"], trigger="manual", now=t0)
    assert res1["status"] == "failed"
    assert res1["exit_code"] == 1
    assert res1["error"] == "1 eval case(s) failed"

    store.write("evals/bad.yaml", f"version: {FORMAT}\ncases: not-a-list\n".encode())
    res2 = loops.execute_one_loop(store, cfg.loops["check-loop"], trigger="manual", now=t0)
    assert res2["status"] == "failed"
    assert res2["exit_code"] == 1
    assert res2["error"].startswith("evals/bad.yaml:")

    store.delete("evals/bad.yaml")
    store.write("concepts/bad-fm.md", b"---\nstatus: [\n---\n# Bad\n")
    res3 = loops.execute_one_loop(store, cfg.loops["check-loop"], trigger="manual", now=t0)
    assert res3["status"] == "failed"
    assert res3["exit_code"] == 1
    assert "concepts/bad-fm.md" in res3["error"]
    assert "PROJ-999" not in res3["error"]


def test_loops_argv0_confinement_cli_order_redaction_and_note_counts(tmp_path: Path) -> None:
    root = tmp_path / "hardened-loops"
    engine.init_brain(root, name="hardened-loops")
    store = Store(root)
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\necho PWNED\n")
    outside.chmod(0o755)
    (root / "sensors").mkdir()
    (root / "sensors" / "escape.sh").symlink_to(outside)

    # Relative `argv[0]` with `/` fails closed on `..` traversal, missing file, or symlink escape.
    for bad_cmd in ("../outside.sh", "sensors/escape.sh", "sensors/missing.sh"):
        with pytest.raises(Error):
            loops.prepare_unit(store, "bad", (bad_cmd,), "r1", "2026-11-20T00:00:00Z")

    # `parse_emitted_event` enforces `EVENT_KIND_RE`.
    with pytest.raises(Error, match="invalid event kind"):
        loops.parse_emitted_event(b'{"kind":"BAD KIND"}', "loop.event", "loops.x")

    # Explicit loop names execute in CLI argument order (not `bf.yaml` order), and returned JSON summaries are redacted.
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: hardened-loops\nloops:\n"
        f'  first:\n    run: [{sys.executable!r}, -c, \'import json; print(json.dumps({{"OPENAI_API_KEY": "sk-secret12345", "ok": True}}))\']\n'
        f"  second:\n    run: [{sys.executable!r}, -c, 'print(\"{{}}\")']\n".encode(),
    )
    run_res = loops.run_loops(store, ("second", "first"))
    assert [r["loop"] for r in run_res["results"]] == ["second", "first"]
    assert run_res["results"][1]["summary"]["OPENAI_API_KEY"] == "[REDACTED]"

    # `status_summary` counts only OKF notes (ignoring attachments in `actions/.../outputs/`).
    store.write("actions/2026-11-20_demo/outputs/report.md", b"# Attachment\n")
    st = loops.status_summary(store)
    assert st["counts"]["notes"] == 1
