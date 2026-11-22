"""Tests for `loops.py`, `status.py`, and `watch.py`: ENV+stdin loops, `sensors/`/`routines/`/`agents/` conventions, JSONL logs, and watch."""

from __future__ import annotations

import json
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

from bf import cli, config, engine, loops, models, proc, records, status, watch
from bf.models import FORMAT, Error, InputError, decode
from bf.storage import Store, private_dir

OBJ_SCHEMA = b'{"type":"object"}\n'


def _load_jsonl(store: Store, path: str) -> list[dict[str, Any]]:
    return [rec for _, rec in records.iter_jsonl(store, path)]


def test_loops_schedules_events_convention_daemons_emit_and_timeout(tmp_path: Path) -> None:
    root = tmp_path / "orch-brain"
    engine.init_brain(root, name="orch")
    store = Store(root)
    for sub in ("sensors", "services", "routines", "prompts"):
        (root / sub).mkdir(exist_ok=True)
    for src in ("feed", "conv-sensor", "rel-script"):
        store.write(f"schemas/{src}.json", OBJ_SCHEMA)

    (root / "sensors" / "feed.py").write_text(
        "import json, os\nprint(json.dumps([{'id': 'f1', 'title': 'Feed item', 'text': os.environ['BF_START']}]))\n"
    )
    (root / "sensors" / "conv-sensor.py").write_text(
        "import json\nprint(json.dumps([{'id': 'c1', 'title': 'Convention item'}]))\n"
    )
    (root / "services" / "mock_chat.py").write_text(
        "import json, time\n"
        "print(json.dumps({'id': 'in-live', 'text': 'ping from chat', 'direct': True}), flush=True)\n"
        "time.sleep(0.2)\n"
    )
    (root / "routines" / "fake_harness.py").write_text(
        "import json, os, pathlib, sys\n"
        "ctx = json.loads(sys.stdin.read())\n"
        "prompt_path = sys.argv[1]\n"
        "assert ctx['loop'] == os.environ['BF_LOOP'] and os.environ['BF_STATE'].endswith(ctx['loop'])\n"
        "assert ctx['trigger'] == os.environ['BF_TRIGGER'] and ctx['run'] == os.environ['BF_RUN']\n"
        "assert pathlib.Path(prompt_path).read_text().startswith('# ')\n"
        "out = {'trigger': ctx['trigger'], 'events': len(ctx['events']), 'prompt': pathlib.Path(prompt_path).name}\n"
        "open('services/sent.jsonl', 'a').write(json.dumps(out) + '\\n')\n"
        "print(json.dumps(out))\n"
    )
    (root / "routines" / "emit_events.py").write_text(
        "import json\n"
        "print(json.dumps({'kind': 'task.delegate', 'id': 'd1', 'target': 'worker'}))\n"
        "print(json.dumps({'id': 'd2', 'note': 'default-kind'}))\n"
    )
    (root / "routines" / "slow.py").write_text("import time\ntime.sleep(30)\n")
    (root / "prompts" / "digest.md").write_text("# Digest prompt\nSummarize recent updates.\n")
    (root / "prompts" / "reply.md").write_text("# Reactive prompt\nAnswer direct chat messages.\n")

    store.write(
        "bf.yaml",
        f"version: {FORMAT}\n"
        "name: orch\n"
        "description: Orchestration test brain.\n"
        "timezone: UTC\n"
        "loops:\n"
        "  feed:\n"
        "    description: Ingest sample feed.\n"
        "    when: 1h\n"
        "    window:\n      backfill: 1d\n      overlap: 10m\n"
        "  conv-sensor:\n"
        "    description: Convention auto-ingest sensor.\n"
        "  no-script:\n"
        "    enabled: false\n"
        "  rebuild:\n"
        "    when:\n      event:\n        kind: records.ingested\n        source: feed\n"
        "    run: [bf, build]\n"
        "  brief:\n"
        "    when:\n      - 1d\n      - '0 8 * * 0'\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, prompts/digest.md]\n"
        "  assistant:\n"
        "    when:\n      - event:\n          kind: loop.message\n          source: chat\n          filter: '.data.direct == true'\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, prompts/reply.md]\n"
        "  ev-emitter:\n"
        f"    run: [{sys.executable!r}, routines/emit_events.py]\n"
        "    emit: loop.event\n"
        "    enabled: false\n"
        "  delegated:\n"
        "    when:\n      - event:\n          kind: task.delegate\n          source: ev-emitter\n"
        f"    run: [{sys.executable!r}, -c, 'print(\"delegated-ok\")']\n"
        "  slow:\n"
        f"    run: [{sys.executable!r}, routines/slow.py]\n"
        "    timeout: 1s\n"
        "    enabled: false\n"
        "  chat:\n"
        "    when: daemon\n"
        f"    run: [{sys.executable!r}, services/mock_chat.py]\n"
        "    emit: loop.message\n".encode(),
    )

    # 2026-11-15 is a Sunday! At 09:00 UTC both `feed` (`1h`) and `brief` (`[1d, '0 8 * * 0']`) are due.
    t0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    dry = loops.run_loops(store, ("feed",), dry_run=True, now=t0)
    assert dry["results"][0]["status"] == "dry_run"

    # Convention `sensors/<name>.py` auto-discovers AND implies `ingest: upsert`!
    conv_res = loops.run_loops(store, ("conv-sensor",), now=t0)
    assert conv_res["ok"]
    assert _load_jsonl(store, records.memory_file("conv-sensor"))[0]["id"] == "c1"
    # Glob matching & `--all` selection:
    glob_dry = loops.run_loops(store, ("conv-*",), dry_run=True, now=t0)
    assert [r["loop"] for r in glob_dry["results"]] == ["conv-sensor"]
    all_dry = loops.run_loops(store, all_loops=True, dry_run=True, now=t0)
    assert "feed" in [r["loop"] for r in all_dry["results"]]
    assert "chat" not in [r["loop"] for r in all_dry["results"]]
    assert "slow" not in [r["loop"] for r in all_dry["results"]]
    with pytest.raises(InputError, match="matched no enabled cycle loops"):
        loops.run_loops(store, ("zzz-*",), now=t0)
    with pytest.raises(Error, match="no script found in sensors/"):
        loops.run_loops(store, ("no-script",), now=t0)

    # `bf run` selector guards, daemon rejection, and `-- EXTRA_ARGS` checks.
    with pytest.raises(InputError, match="specify loop names"):
        loops.run_loops(store, dry_run=True, now=t0)
    with pytest.raises(InputError, match="mutually exclusive"):
        loops.run_loops(store, ("feed",), due_only=True, now=t0)
    with pytest.raises(InputError, match="mutually exclusive"):
        loops.run_loops(store, due_only=True, all_loops=True, now=t0)
    with pytest.raises(InputError, match="is a daemon; run it with bf watch"):
        loops.run_loops(store, ("chat",), now=t0)
    with pytest.raises(InputError, match="require a single loop"):
        loops.run_loops(store, due_only=True, extra_args=("--x",), now=t0)
    with pytest.raises(InputError, match="require a single loop"):
        loops.run_loops(store, ("feed", "brief"), extra_args=("--x",), now=t0)

    # `due_only=True` rebuilds dirty notes first before evaluating scheduled/event-triggered loops!
    store.write("concepts/dirty-due.md", b"---\ntype: guide\nstatus: draft\n---\n\n# Dirty Before Due\n")
    due1 = loops.run_loops(store, due_only=True, now=t0)
    assert due1["ok"], due1
    assert [r["loop"] for r in due1["results"]] == ["feed", "brief"]
    assert "concepts/dirty-due.md" in store.read("memories/concepts.jsonl").decode()
    feed_saved = _load_jsonl(store, records.memory_file("feed"))[0]
    assert feed_saved["id"] == "f1"
    assert feed_saved["text"] == "2026-11-14T09:00:00Z"

    # Follow-up run rewinds `BF_START` by `window.overlap` (`10m` before `t0`).
    t_ov = t0 + timedelta(minutes=2)
    assert loops.run_loops(store, ("feed",), now=t_ov)["ok"]
    feed_ov = _load_jsonl(store, records.memory_file("feed"))[0]
    assert feed_ov["text"] == "2026-11-15T08:50:00Z"

    # Next tick 5 minutes later: `rebuild` triggers on `records.ingested` and `brief`'s 2nd schedule trigger fires independently!
    t1 = t0 + timedelta(minutes=5)
    due2 = loops.run_loops(store, due_only=True, now=t1)
    assert due2["ok"], due2
    assert [r["loop"] for r in due2["results"]] == ["rebuild", "brief"]
    assert set(loops.load_unit_state(store, "brief")["schedule_last"]) == {"every:1d", "cron:0 8 * * 0"}

    # Third tick 5 minutes later: cursors advanced, nothing due!
    due3 = loops.run_loops(store, due_only=True, now=t1 + timedelta(minutes=5))
    assert due3["results"] == []

    # Cycle loop with `emit: loop.event` publishes JSONL events into `logs/events.jsonl` that wake downstream `delegated`!
    ev_res = loops.run_loops(store, ("ev-emitter",), now=t1)
    assert ev_res["ok"]
    assert ev_res["results"][0]["summary"] == {"emitted": 2}
    assert (root / "logs" / "events.jsonl").is_file()
    del_res = loops.run_loops(store, due_only=True, now=t1 + timedelta(minutes=6))
    assert [r["loop"] for r in del_res["results"]] == ["delegated"]
    assert del_res["ok"]

    # Invalid emitted output fails atomically with zero partial commits.
    seq_before = records.latest_event_seq(store)
    with pytest.raises(Error, match="must be a JSON object"):
        records.emit_output_events(store, "ev-emitter", "loop.event", b'{"kind":"ok.ev","id":"p0"}\n[1,2]\n')
    assert records.latest_event_seq(store) == seq_before
    with pytest.raises(Error, match="event kind must be a string"):
        records.emit_output_events(store, "ev-emitter", "loop.event", b'{"kind":123}\n')
    assert records.latest_event_seq(store) == seq_before
    with pytest.raises(Error, match="exceeds"):
        records.emit_output_events(store, "ev-emitter", "loop.event", b"x" * (models.MAX_BATCH + 1))

    # Reactive `loops.assistant` triggered via `run_loops(due_only=True)` on `loop.message`!
    records.append_event(
        store,
        "loop.message",
        "chat",
        {"id": "ev-msg-9", "conversation": "space-9/threads/t-1", "text": "hi assistant", "direct": True},
        now=t1,
    )
    tick_out = loops.run_loops(store, due_only=True, now=t1 + timedelta(minutes=7))
    assert tick_out["ran"] == 1
    assert tick_out["ok"]
    sent_after_lines = [json.loads(ln) for ln in (root / "services" / "sent.jsonl").read_text().splitlines()]
    assert any(m == {"trigger": "event", "events": 1, "prompt": "reply.md"} for m in sent_after_lines)

    # `DaemonStream` captures JSON line output from `loops.chat` into `logs/events.jsonl`!
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
    st_sum = status.status_summary(store)
    assert st_sum["ok"]
    assert st_sum["description"] == "Orchestration test brain."
    assert set(st_sum["counts"]) == {"notes", "records", "usages", "sources", "loops"}
    assert st_sum["counts"]["notes"] >= 2
    st_all = status.status_summary(store, all_units=True)
    assert st_sum["counts"]["loops"] == len(st_all["loops"])
    assert st_all["loops"]["feed"]["description"] == "Ingest sample feed."
    assert st_all["loops"]["feed"]["ingest_mode"] == "upsert"
    assert st_sum["attention"] == []
    assert st_sum["orphaned_states"] == []
    assert "loops" not in st_sum

    # Bird's-eye status text table renders cleanly with zero external UI deps!
    rendered = status.format_status_text(status.status_summary(store, all_units=True))
    assert "orch" in rendered
    assert "daemon (loop.message)" in rendered
    assert "2 schedules" in rendered
    assert "3 disabled" in rendered

    # Non-TTY `watch.serve` automatically emits JSONL lines and rebuilds live edits!
    serve_stop = threading.Event()
    serve_thread = threading.Thread(target=lambda: watch.serve(store, stop_event=serve_stop), daemon=True)
    serve_thread.start()
    deadline = time.monotonic() + 8.0
    step = 0
    while time.monotonic() < deadline and "concepts/live-edit.md" not in store.read("memories/concepts.jsonl").decode():
        if step % 12 == 2:
            store.write("concepts/live-edit.md", b"---\ntype: guide\nstatus: stable\n---\n# Live Edit\n")
        step += 1
        time.sleep(0.05)
    serve_stop.set()
    serve_thread.join(timeout=5)
    assert "concepts/live-edit.md" in store.read("memories/concepts.jsonl").decode()

    # Builtin `review`/`check`, relative script executable (`ingest: snapshot`), failed loop attention!
    rel_exec = root / "routines" / "exec_rel.py"
    rel_exec.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps([{{'id': 'r1', 'title': 'Report'}}]))\n")
    rel_exec.chmod(0o755)
    (root / "sensors" / "bad_json.py").write_text("print('{not valid json')\n")
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: orch\nloops:\n"
        "  rev:\n    run: [bf, review]\n"
        "  chk:\n    run: [bf, check]\n"
        "  rel-script:\n    run: [routines/exec_rel.py]\n    ingest: snapshot\n"
        f"  bad-ing:\n    run: [{sys.executable!r}, sensors/bad_json.py]\n    ingest:\n      source: feed\n"
        f"  exit-fail:\n    run: [{sys.executable!r}, -c, 'import sys; sys.exit(2)']\n".encode(),
    )
    rev_chk = loops.run_loops(store, ("rev", "chk", "rel-script"), now=t1)
    assert rev_chk["ok"]
    assert loops.run_loops(store, ("rel-script",), now=t1)["ok"]
    assert _load_jsonl(store, records.memory_file("rel-script"))[0]["id"] == "r1"
    bad_ing = loops.run_loops(store, ("bad-ing",), now=t1)
    assert not bad_ing["ok"]
    pf_res = loops.run_loops(store, ("exit-fail",), now=t1)
    assert not pf_res["ok"]
    assert "exit code 2" in pf_res["results"][0]["error"]
    st_fail = status.status_summary(store)
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

    for bad_call in (
        lambda: proc.exec_argv((), cwd=root, env={}, timeout=5),
        lambda: loops.run_loops(store, ("missing-loop",)),
    ):
        with pytest.raises((Error, InputError)):
            bad_call()


def test_concurrency_nonblocking_watch_adaptive_delay_and_multi_schedule(tmp_path: Path) -> None:
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
        f"version: {FORMAT}\nname: conc\nconcurrency: 3\nloops:\n"
        f"  a:\n    when: 1h\n    run: [{sys.executable!r}, routines/barrier.py, a]\n"
        f"  b:\n    when: 1h\n    run: [{sys.executable!r}, routines/barrier.py, b]\n"
        f"  c:\n    when: 1h\n    run: [{sys.executable!r}, routines/barrier.py, c]\n".encode(),
    )
    res = loops.run_loops(store, due_only=True)
    assert res["ok"]
    assert [r["loop"] for r in res["results"]] == ["a", "b", "c"]

    # Adaptive `next_due_delay`: clamps to [0.5, 60.0], respects interval, cron, and failure backoff!
    cfg_conc = config.load(store)
    now_d = datetime(2026, 11, 16, 7, 59, 40, tzinfo=UTC)
    assert watch.next_due_delay(store, cfg_conc, now_d) == 60.0
    cron_cfg = config.Config(
        name="conc",
        loops={
            "c": config.LoopSpec(
                name="c", run=("echo",), schedules=(config.ScheduleSpec("cron:0 8 * * *", cron="0 8 * * *"),)
            ),
            "e": config.LoopSpec(name="e", run=("echo",), schedules=(config.ScheduleSpec("every:30s", every=30),)),
            "f": config.LoopSpec(name="f", run=("echo",), schedules=(config.ScheduleSpec("every:5m", every=300),)),
        },
    )
    loops.save_unit_state(
        store, {**loops.load_unit_state(store, "c"), "schedule_last": {"cron:0 8 * * *": "2026-11-15T08:00:00Z"}}
    )
    loops.save_unit_state(
        store, {**loops.load_unit_state(store, "e"), "schedule_last": {"every:30s": "2026-11-16T07:59:25Z"}}
    )
    loops.save_unit_state(
        store,
        {
            **loops.load_unit_state(store, "f"),
            "status": "failed",
            "consecutive_failures": 1,
            "last_end": "2026-11-16T07:59:30Z",
        },
    )
    assert 10.0 <= watch.next_due_delay(store, cron_cfg, now_d) <= 20.0
    # Overdue cron returns 0.5 immediately instead of sleeping for the next cycle!
    assert watch.next_due_delay(store, cron_cfg, datetime(2026, 11, 16, 8, 0, 5, tzinfo=UTC)) == 0.5
    with pytest.raises(Error, match="reserved"):
        loops.resolve_loop(
            store, config.LoopSpec(name="concepts", run=("sensors/concepts.py",)), cfg_conc.reserved_sources
        )

    # Bounded concurrency upper bound: `concurrency: 2` with 4 due loops never exceeds 2 active at once.
    (root / "routines" / "cap.py").write_text(
        "import fcntl, json, pathlib, sys, time\n"
        "p = pathlib.Path('routines/cap.state')\n"
        "lk = pathlib.Path('routines/cap.lock').open('w')\n"
        "def bump(delta):\n"
        "    fcntl.flock(lk, fcntl.LOCK_EX)\n"
        "    st = json.loads(p.read_text()) if p.exists() else {'cur': 0, 'peak': 0, 'seen': 0}\n"
        "    st['cur'] += delta\n"
        "    st['peak'] = max(st['peak'], st['cur'])\n"
        "    if delta > 0:\n"
        "        st['seen'] += 1\n"
        "    p.write_text(json.dumps(st))\n"
        "    fcntl.flock(lk, fcntl.LOCK_UN)\n"
        "    return st['peak']\n"
        "bump(1)\n"
        "dl = time.monotonic() + 5.0\n"
        "while time.monotonic() < dl:\n"
        "    if bump(0) >= 2:\n"
        "        break\n"
        "    time.sleep(0.01)\n"
        "time.sleep(0.05)\n"
        "bump(-1)\n"
    )
    cap_loops = "".join(
        f"  c{i}:\n    when: 1h\n    run: [{sys.executable!r}, routines/cap.py, c{i}]\n" for i in range(4)
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: conc\nconcurrency: 2\nloops:\n{cap_loops}".encode(),
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
        f"version: {FORMAT}\nname: conc\nconcurrency: 2\nloops:\n"
        f"  slow-bg:\n    when:\n      - 1s\n      - event: poke.slow\n    run: [{sys.executable!r}, routines/slow_bg.py]\n"
        f"  fast-ev:\n    when:\n      event: wake.now\n    run: [{sys.executable!r}, routines/fast_ev.py]\n".encode(),
    )
    stop_w = threading.Event()
    wt = threading.Thread(target=lambda: watch.serve(store, fmt="jsonl", stop_event=stop_w), daemon=True)
    wt.start()
    try:
        dl = time.monotonic() + 5.0
        while time.monotonic() < dl and not (root / "routines" / "slow.pids").exists():
            time.sleep(0.02)
        assert (root / "routines" / "slow.pids").exists()
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

    # Multi-schedule per-trigger `schedule_last`, widest-backfill tie-breaker, and per-trigger `backfill`/`overlap: 0`!
    s_hour = config.ScheduleSpec("every:1h", every=3600, backfill=7200, overlap=300)
    s_day = config.ScheduleSpec("cron:30 7 * * 1", cron="30 7 * * 1", backfill=604_800, overlap=0)
    multi = config.LoopSpec(name="work", run=("echo",), schedules=(s_hour, s_day))
    mon_0731 = datetime(2026, 11, 16, 7, 31, tzinfo=UTC)
    assert config.due_schedule(multi, {"schedule_last": {}}, mon_0731) == s_day
    assert config.due_schedule(multi, {"schedule_last": {"every:1h": "2026-11-16T07:15:00Z"}}, mon_0731) == s_day
    assert (
        config.due_schedule(
            multi,
            {"schedule_last": {"every:1h": "2026-11-16T07:31:00Z", "cron:30 7 * * 1": "2026-11-16T07:31:00Z"}},
            mon_0731 + timedelta(minutes=10),
        )
        is None
    )

    # Execute per-trigger `backfill`, per-trigger `overlap: 0` overriding loop `window.overlap: 10m`, and `started_iso` anchor!
    (root / "routines" / "echo_win.py").write_text(
        "import json, os, pathlib\n"
        "pathlib.Path('routines/win.log').open('a').write(os.environ['BF_START'] + '|' + os.environ['BF_END'] + '\\n')\n"
        "print(json.dumps({'ok': True}))\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: conc\nloops:\n"
        "  win-test:\n"
        "    window:\n      backfill: 1d\n      overlap: 10m\n"
        "    when:\n"
        "      - every: 1h\n        backfill: 2h\n        overlap: 0\n"
        "      - cron: '30 7 * * 1'\n        backfill: 1w\n        overlap: 5m\n"
        f"    run: [{sys.executable!r}, routines/echo_win.py]\n".encode(),
    )
    t_m0 = datetime(2026, 11, 16, 7, 31, tzinfo=UTC)
    assert loops.run_loops(store, due_only=True, now=t_m0)["ok"]
    assert loops.run_loops(store, due_only=True, now=t_m0 + timedelta(minutes=1))["ok"]
    assert loops.run_loops(store, due_only=True, now=t_m0 + timedelta(hours=1, minutes=1))["ok"]
    win_lines = (root / "routines" / "win.log").read_text().splitlines()
    assert win_lines == [
        "2026-11-09T07:31:00Z|2026-11-16T07:31:00Z",
        "2026-11-16T05:32:00Z|2026-11-16T07:32:00Z",
        "2026-11-16T07:32:00Z|2026-11-16T08:32:00Z",
    ]
    (root / "routines" / "nap.py").write_text(
        "import os, pathlib, time\n"
        "pathlib.Path('routines/nap.end').write_text(os.environ['BF_END'])\n"
        "time.sleep(1.05)\n"
    )
    nap_spec = config.LoopSpec(
        name="nap", run=(sys.executable, "routines/nap.py"), schedules=(config.ScheduleSpec("every:1h", every=3600),)
    )
    nap_res = loops.execute_one_loop(store, nap_spec, trigger="schedule", schedule=nap_spec.schedules[0])
    assert nap_res["status"] == "ok"
    nap_st = loops.load_unit_state(store, "nap")
    assert nap_st["schedule_last"]["every:1h"] == (root / "routines" / "nap.end").read_text()
    assert nap_st["last_end"] > nap_st["last_start"]


def test_failure_backoff_dotenv_env_map_and_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "resilient"
    engine.init_brain(root, name="resilient")
    store = Store(root)
    for sub in ("routines", "services"):
        (root / sub).mkdir(exist_ok=True)
    for src in ("warn-ok", "loop-override"):
        store.write(f"schemas/{src}.json", OBJ_SCHEMA)
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
        b"LOCAL_SECRET=from-dotenv\nGWS_DIR=override-gws\nLOOP_KEY=from-dotenv\nLIT_ENV=raw-val\n",
        mode=0o600,
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: resilient\n"
        "env:\n  GWS_DIR: /base/gws\n  BASE_ONLY: /base/only\n  GH_TOKEN: from-yaml\n  LOCAL_SECRET: default-val\n  LOOP_ONLY: from-base\n"
        "loops:\n"
        f"  warn-ok:\n    when: 1h\n    run: [{sys.executable!r}, routines/warn_ok.py]\n    ingest: snapshot\n"
        f'  loop-override:\n    run: [{sys.executable!r}, -c, \'import json, os, sys; print(json.dumps([{{"id":"lo","title":os.environ["LOOP_KEY"]+"|"+os.environ["LOOP_ONLY"]+"|"+"|".join(sys.argv[1:])}}]))\']\n'
        "    ingest: upsert\n    env:\n      LOOP_KEY: from-loop\n      LOOP_ONLY: from-loop-only\n"
        f"  flaky:\n    when: 5m\n    run: [{sys.executable!r}, routines/fail_fast.py]\n"
        f"  flaky-ev:\n    when:\n      event: loop.message\n    run: [{sys.executable!r}, routines/fail_fast.py]\n"
        f"  chat:\n    when: daemon\n    run: [{sys.executable!r}, services/dup_stream.py]\n    emit: loop.message\n".encode(),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BF_BRAIN", str(tmp_path / "evil-override"))
    monkeypatch.setenv("GH_TOKEN", "from-host")
    monkeypatch.setenv("UNALLOW_HOST", "secret-host-val")

    # Precedence: host allowlist < bf.yaml env < defaults.env < loops.<name>.env < .env < BF_*, plus literal extra_args.
    ok_res = loops.run_loops(store, ("warn-ok",))
    assert ok_res["ok"]
    warn_saved = _load_jsonl(store, records.memory_file("warn-ok"))[0]
    assert warn_saved["title"] == f"override-gws|/base/only|from-yaml|from-dotenv|raw-val||{root.resolve()}"
    lo_res = loops.run_loops(store, ("loop-override",), extra_args=("--lit",))
    assert lo_res["ok"]
    lo_saved = _load_jsonl(store, records.memory_file("loop-override"))[0]
    assert lo_saved["title"] == "from-dotenv|from-loop-only|--lit"
    warn_lines = [
        decode(ln, "logs/warn-ok.jsonl") for ln in store.read("logs/warn-ok.jsonl").splitlines() if ln.strip()
    ]
    assert isinstance(warn_lines[-1], dict)
    assert warn_lines[-1]["message"] == "loop.succeeded"
    assert "Skipped feed x" in warn_lines[-1]["stderr"]

    t0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    assert not loops.run_loops(store, ("flaky",), now=t0)["ok"]
    cfg = config.load(store)
    flaky_state = loops.load_unit_state(store, "flaky")
    assert flaky_state["consecutive_failures"] == 1
    assert config.due_schedule(cfg.loops["flaky"], flaky_state, t0 + timedelta(seconds=10)) is None
    assert config.due_schedule(cfg.loops["flaky"], flaky_state, t0 + timedelta(seconds=65)) is not None

    assert status.status_summary(store, all_units=True)["loops"]["warn-ok"]["last_run"]["added"] == 1
    records.append_event(store, "loop.message", "chat", {"id": "ev-b1", "text": "ping"}, now=t0)
    ev_fail = loops.run_loops(store, due_only=True, now=t0)
    assert [r["loop"] for r in ev_fail["results"]] == ["warn-ok", "flaky-ev"]
    assert not ev_fail["ok"]
    assert loops.run_loops(store, due_only=True, now=t0 + timedelta(seconds=5))["results"] == []

    summary = status.status_summary(store, all_units=True)
    assert summary["loops"]["warn-ok"]["ingest_mode"] == "snapshot"
    assert summary["loops"]["warn-ok"]["last_run"]["unchanged"] == 1
    assert "transient err" in summary["loops"]["flaky"]["error"]

    stop_ev, wake_ev = threading.Event(), threading.Event()
    daemon = watch.DaemonStream(store, cfg.loops["chat"], stop_ev, wake_ev)
    daemon.start()
    assert wake_ev.wait(timeout=5.0)
    dup_deadline = time.monotonic() + 5.0
    while time.monotonic() < dup_deadline and not store.exists("logs/chat.jsonl"):
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
    survived = watch.serve(store, fmt="jsonl", stop_event=stop_loop)
    assert not survived["ok"]
    assert "transient lock busy" in capsys.readouterr().out


def test_proxy_env_reactive_schedule_anchors_and_cursor_self_healing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hybrid"
    engine.init_brain(root, name="hybrid")
    store = Store(root)
    (root / "routines").mkdir(exist_ok=True)
    (root / "routines" / "echo_ctx.py").write_text(
        "import json, os, pathlib, sys\n"
        "ctx = json.loads(sys.stdin.read())\n"
        "px = os.environ.get('HTTPS_PROXY', '') + '|' + os.environ.get('no_proxy', '')\n"
        "line = f\"{ctx['trigger']}:{ctx['start']}:{px}:{len(ctx['events'])}\"\n"
        "p = pathlib.Path(f'routines/{sys.argv[1]}.log')\n"
        "p.write_text((p.read_text() if p.exists() else '') + line + '\\n')\n"
        "print(line)\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: hybrid\ntimezone: UTC\nloops:\n"
        "  bot:\n"
        "    when:\n      - '0 8 * * *'\n      - event: loop.message\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, bot]\n"
        "  cross-alert:\n"
        "    when:\n      event:\n        kind: loop.message\n        source: chat\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, alerts]\n"
        "  sched-every:\n"
        "    when: 1h\n"
        f"    run: [{sys.executable!r}, routines/echo_ctx.py, sched]\n".encode(),
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8080")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")

    t_ev = datetime(2026, 11, 16, 8, 1, tzinfo=UTC)
    records.append_event(
        store, "loop.message", "chat", {"id": "in-42", "conversation": "room-1/t-9", "text": "hello"}, now=t_ev
    )
    r1 = loops.run_loops(store, due_only=True, now=t_ev)
    assert r1["ok"]
    assert [r["loop"] for r in r1["results"]] == ["bot", "cross-alert", "sched-every"]
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
    store.delete("logs/events.jsonl")
    assert loops.run_loops(store, due_only=True, now=t_ev + timedelta(minutes=5, seconds=30))["results"] == []
    assert loops.load_unit_state(store, "cross-alert")["event_seq"] == 0
    records.append_event(store, "loop.message", "chat", {"id": "after-reset"}, now=t_ev)
    healed = loops.run_loops(store, due_only=True, now=t_ev + timedelta(minutes=6))
    assert [r["loop"] for r in healed["results"]] == ["bot", "cross-alert"]
    assert loops.load_unit_state(store, "cross-alert")["event_seq"] == 1

    # Manual run does NOT advance `schedule_last` or suppress the next scheduled trigger!
    assert loops.run_loops(store, ("sched-every",), now=t_ev + timedelta(minutes=30))["ok"]
    assert loops.run_loops(store, due_only=True, now=t_ev + timedelta(minutes=55))["results"] == []
    due_1h = loops.run_loops(store, due_only=True, now=t_ev + timedelta(minutes=60))
    assert [r["loop"] for r in due_1h["results"]] == ["sched-every"]


def test_watch_supervises_daemon_loop_and_snapshot_memory_loops(tmp_path: Path) -> None:
    root = tmp_path / "svc-watch"
    engine.init_brain(root, name="svc-watch")
    store = Store(root)
    for sub in ("routines", "services", "prompts"):
        (root / sub).mkdir(exist_ok=True)
    store.write("schemas/brief.json", OBJ_SCHEMA)
    (root / "routines" / "fake_harness.py").write_text(
        "import json, pathlib, sys\n"
        "print(json.dumps([{'id': 'latest', 'title': 'Brief from ' + pathlib.Path(sys.argv[1]).name}]))\n"
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
        f"    run: [{sys.executable!r}, routines/fake_harness.py, prompts/brief.md]\n"
        "    ingest: snapshot\n"
        "  tcp:\n"
        "    when: daemon\n"
        f"    run: [{sys.executable!r}, services/tcp_srv.py, '{port}']\n"
        "    enabled: true\n".encode(),
    )
    ran1 = loops.run_loops(store, ("brief",))
    assert ran1["ok"], ran1
    assert ran1["results"][0]["summary"]["records"] == 1
    assert _load_jsonl(store, records.memory_file("brief"))[0]["id"] == "latest"
    assert engine.validate(store)["ok"]

    conflict_root = tmp_path / "svc-conflict"
    engine.init_brain(conflict_root, name="svc-conflict")
    conflict_store = Store(conflict_root)
    conflict_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: svc-conflict\n"
        "loops:\n"
        "  crash:\n"
        "    when: daemon\n"
        f"    run: [{sys.executable!r}, -c, 'import sys; sys.stderr.write(\"bind failed\\\\n\"); sys.exit(1)']\n"
        "    enabled: true\n"
        "  missing:\n    when: daemon\n    run: [routines/no-such-script.py]\n".encode(),
    )

    stop_ev = threading.Event()
    stop_conflict = threading.Event()
    srv_thread = threading.Thread(target=lambda: watch.serve(store, fmt="jsonl", stop_event=stop_ev), daemon=True)
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
            target=lambda: watch.serve(conflict_store, fmt="jsonl", stop_event=stop_conflict), daemon=True
        )
        conflict_thread.start()
        try:
            c_deadline = time.monotonic() + 8.0
            while time.monotonic() < c_deadline:
                st = status.status_summary(conflict_store, all_units=True)
                if st["loops"]["crash"]["consecutive_failures"] >= 1 and st["loops"]["missing"]["status"] == "failed":
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

    conflict_status = status.status_summary(conflict_store, all_units=True)
    assert not conflict_status["ok"]
    assert conflict_status["loops"]["crash"]["status"] == "failed"
    assert conflict_status["loops"]["crash"]["consecutive_failures"] >= 1
    assert conflict_status["loops"]["missing"]["status"] == "failed"


def test_dual_schedule_and_event_loop_advances_cursor_after_scheduled_run(tmp_path: Path) -> None:
    root = tmp_path / "dual"
    engine.init_brain(root, name="dual")
    store = Store(root)
    (root / "routines").mkdir(exist_ok=True)
    (root / "routines" / "count.py").write_text(
        "import pathlib, sys\npathlib.Path('routines/seen.json').write_text(sys.stdin.read())\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: dual\n"
        "loops:\n"
        "  combo:\n"
        "    when:\n      - 1h\n      - event: custom.ping\n"
        f"    run: [{sys.executable!r}, routines/count.py]\n".encode(),
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
    assert seen["trigger"] == "schedule"
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


def test_status_text_formatting_sanitization_and_watch_text_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    empty_root = tmp_path / "empty-tui"
    engine.init_brain(empty_root, name="empty-tui")
    empty_store = Store(empty_root)
    empty_store.write("bf.yaml", f"version: {FORMAT}\nname: empty-tui\n".encode())
    now = datetime(2026, 11, 16, 12, 30, 0, tzinfo=UTC)
    empty_txt = status.format_status_text(status.status_summary(empty_store, all_units=True), now=now)
    assert "empty-tui" in empty_txt
    assert "HEALTHY" in empty_txt
    assert "loops=0" in empty_txt

    root = tmp_path / "rich-tui"
    engine.init_brain(root, name="rich-tui")
    store = Store(root)
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: rich-tui\n"
        "loops:\n"
        "  ing-diff:\n    when: 30m\n    run: [echo]\n    ingest:\n      source: items\n"
        "  ing-same:\n    when: '0 8 * * 1-5'\n    run: [echo]\n    ingest:\n      source: archive\n      mode: snapshot\n"
        "  b-build:\n    when: '0 9 * * *'\n    run: [bf, build]\n"
        "  b-val:\n    run: [bf, check]\n"
        "  b-rev:\n    when:\n      - event: a\n      - event: b\n    run: [bf, review]\n"
        "  b-emit:\n    run: [echo]\n    emit: loop.event\n"
        '  ev-san:\n    when:\n      - event:\n          kind: "ev\\u200b"\n    run: [echo]\n'
        "  idle-ing:\n    run: [echo]\n    ingest: upsert\n"
        "  idle-plain:\n    run: [echo]\n"
        "  failing:\n    when: 1m\n    run: [echo]\n"
        "  off-loop:\n    run: [bf, build]\n    enabled: false\n"
        "  live-msg:\n    when: daemon\n    run: [echo]\n    emit: loop.message\n"
        "  live-ev:\n    when: daemon\n    run: [echo]\n    emit: loop.event\n"
        "  daemon:\n    when: daemon\n    run: [echo]\n"
        "  halted:\n    when: daemon\n    run: [echo]\n".encode(),
    )

    def save_state(
        name: str,
        st_name: str,
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
            "status": st_name,
            "updated": ts,
            "last_start": ts,
            "last_end": "" if st_name in {"running", "stopped"} else ts,
            "last_success": "" if st_name == "failed" else ts,
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
    save_state("b-rev", "ok", ago_s=30, summary={"total_stale": 1, "total_tasks_open": 1})
    save_state("b-emit", "ok", ago_s=30, summary={"emitted": 2})
    save_state("ev-san", "ok", ago_s=30, summary={})
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

    out_txt = status.format_status_text(status.status_summary(store, all_units=True), now=now)
    assert "\x1b" not in out_txt
    assert "\u200b" not in out_txt
    assert "ATTENTION (2)" in out_txt
    assert out_txt.index("failing") < out_txt.index("b-val") < out_txt.index("ing-diff")
    for expected in (
        "(3x) Traceback (most recent call last): File x.py boom \\u001b[31m bad",
        "idle-ing.json: invalid JSON",
        "on ev\\u200b",
        "+2 ~1 (5 total)",
        "now",
        "4 unchanged",
        "cron 0 8 * * 1-5",
        "cron 0 9 * * *",
        "3 notes (1 built)",
        "1d ago",
        "on 2 events",
        "2 emitted",
        "daemon (loop.message)",
        "daemon (loop.event)",
        "up 1h",
        "up 45s",
        "stopped",
        "1 disabled",
    ):
        assert expected in out_txt, f"missing {expected!r} in:\n{out_txt}"

    paris_cfg = config.LoopSpec(
        name="paris", run=("echo",), schedules=(config.ScheduleSpec("0 2 * * *", None, "0 2 * * *"),)
    )
    pre_dst = datetime(2026, 3, 29, 0, 59, tzinfo=UTC)
    assert (
        config.due_schedule(paris_cfg, {"last_scheduled": "2026-03-28T01:00:00Z"}, pre_dst, tz_name="Europe/Paris")
        is None
    )
    post_dst = datetime(2026, 3, 29, 1, 5, tzinfo=UTC)
    assert (
        config.due_schedule(paris_cfg, {"last_scheduled": "2026-03-28T01:00:00Z"}, post_dst, tz_name="Europe/Paris")
        is not None
    )

    # Exercise `watch.serve(fmt="text")`: prints bird's-eye table on startup and live single-line events!
    stop_txt = threading.Event()
    calls = 0
    real_sel = loops.select_due_loops

    def stop_on_second_tick(*a: Any, **kw: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls >= 2:
            stop_txt.set()
        return real_sel(*a, **kw)

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(loops, "select_due_loops", stop_on_second_tick)
    monkeypatch.setattr(watch, "next_due_delay", lambda *_a, **_kw: 0.05)
    capsys.readouterr()
    res = watch.serve(empty_store, fmt="text", stop_event=stop_txt)
    assert res["ok"] is True
    watch_out = capsys.readouterr().out
    assert "empty-tui" in watch_out
    assert "Watching empty-tui" in watch_out

    # Daemon loop with `emit: loop.event` streams JSON stdout objects into `logs/events.jsonl` and logs malformed lines!
    svc_root = tmp_path / "svc-events"
    engine.init_brain(svc_root, name="svc-events")
    svc_store = Store(svc_root)
    svc_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: svc-events\n"
        "loops:\n  emitter:\n"
        "    when: daemon\n"
        f'    run: [{sys.executable!r}, -c, \'import json, sys; sys.stderr.write(json.dumps({{"severity": "INFO", "message": "daemon boot"}}) + chr(10)); sys.stderr.flush(); print("[1,2]", flush=True); print(json.dumps({{"id": "s1", "kind": "svc.ping"}}), flush=True)\']\n'
        "    emit: loop.event\n".encode(),
    )
    svc_cfg = config.load(svc_store)
    s_stop, s_wake = threading.Event(), threading.Event()
    svc_d = watch.DaemonStream(svc_store, svc_cfg.loops["emitter"], s_stop, s_wake)
    svc_d.start()
    assert s_wake.wait(timeout=5.0)
    s_stop.set()
    svc_d.terminate()
    svc_evs, _ = records.read_events(svc_store)
    assert any(ev["kind"] == "svc.ping" and ev["source"] == "emitter" for ev in svc_evs)
    emitter_logs = svc_store.read("logs/emitter.jsonl").decode()
    assert "daemon boot" in emitter_logs
    assert "emitted event must be a JSON object" in emitter_logs

    # Immediate exit-0 daemon does not reset backoff.
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
        "    when: daemon\n"
        f"    run: [{sys.executable!r}, -c, \"import signal, sys, time; signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0)); print('x' * 120, flush=True); time.sleep(5)\"]\n"
        "    emit: loop.event\n".encode(),
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
        f"    run: [{sys.executable!r}, -c, 'import sys; sys.stderr.write(\"Authorization: bearer secret_tok_12345678~v2 ghp_1234567890abcdef\\n-----BEGIN PRIVATE KEY-----\\nABC\\n-----END PRIVATE KEY-----\\n\"); sys.exit(2)']\n".encode(),
    )
    cfg = config.load(store)

    missing_res = loops.execute_one_loop(store, cfg.loops["missing-bin"], trigger="manual")
    assert (missing_res["status"], missing_res["exit_code"]) == ("failed", 127)
    assert loops.load_unit_state(store, "missing-bin")["status"] == "failed"
    assert oct((root / "logs").stat().st_mode & 0o777) == "0o700"
    assert oct((root / "logs" / "missing-bin.jsonl").stat().st_mode & 0o777) == "0o600"

    leaky_res = loops.execute_one_loop(store, cfg.loops["leaky"], trigger="manual")
    assert leaky_res["status"] == "failed"
    for secret in ("secret_tok_12345678~v2", "ghp_1234567890abcdef", "BEGIN PRIVATE KEY", "ABC"):
        assert secret not in leaky_res["error"]
        assert secret not in loops.load_unit_state(store, "leaky")["error"]
        assert secret not in (root / "logs" / "leaky.jsonl").read_text()
    assert "[REDACTED]" in leaky_res["error"]

    # `watch.serve(fmt="jsonl")` emits JSON `watch.tick` lines on stdout.
    stop_json = threading.Event()
    real_build = engine.build

    def stop_build(s: Store, **kw: Any) -> dict[str, Any]:
        out = real_build(s, **kw)
        stop_json.set()
        return out

    monkeypatch.setattr(engine, "build", stop_build)
    capsys.readouterr()
    res = watch.serve(store, fmt="jsonl", stop_event=stop_json)
    assert res["ok"] is True
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(ev.get("message") == "watch.started" and ev.get("brain") == "sec-watch" for ev in lines)

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
    monkeypatch.setattr(watch, "next_due_delay", lambda *_a, **_kw: 0.05)
    capsys.readouterr()
    retry_res = watch.serve(store, fmt="jsonl", stop_event=stop_retry)
    assert retry_res["ok"] is True
    assert calls >= 2
    assert "OSError" in capsys.readouterr().out


def test_batched_jq_filters_stop_event_due_isolation_and_live_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
        "  broken-rel:\n    when: 1m\n    run: [routines/missing.py]\n"
        f"  healthy:\n    when: 1m\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n"
        f"  ev-zero:\n    when:\n      - event:\n          kind: custom.num\n          filter: '.data.n'\n    run: [{sys.executable!r}, -c, 'pass']\n"
        f"  ev-env:\n    when:\n      - event:\n          kind: custom.num\n          filter: '$ENV.SECRET_VAR != null'\n    run: [{sys.executable!r}, -c, 'pass']\n"
        f"  hang:\n    run: [{sys.executable!r}, routines/hang.py]\n    enabled: false\n".encode(),
    )

    t0 = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    due_res = loops.run_loops(store, due_only=True, now=t0)
    by_loop = {r["loop"]: r["status"] for r in due_res["results"]}
    assert by_loop == {"broken-rel": "failed", "healthy": "ok"}

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
    live_store.write("bf.yaml", f"version: {FORMAT}\nname: live-reload\n".encode())
    stop_live = threading.Event()
    live_t = threading.Thread(target=lambda: watch.serve(live_store, fmt="jsonl", stop_event=stop_live), daemon=True)
    live_t.start()
    try:
        added_yaml = (
            f"version: {FORMAT}\nname: live-reload\nloops:\n  added:\n"
            "    when: daemon\n"
            f'    run: [{sys.executable!r}, -c, \'import json, time; print(json.dumps({{"id": "lr-1", "kind": "reload.ok"}}), flush=True); time.sleep(2)\']\n'
            "    emit: loop.event\n"
        ).encode()
        lr_deadline = time.monotonic() + 8.0
        found_reload = False
        while time.monotonic() < lr_deadline:
            live_store.write("bf.yaml", added_yaml)
            for _ in range(12):
                evs, _ = records.read_events(live_store)
                if any(e.get("kind") == "reload.ok" for e in evs):
                    found_reload = True
                    break
                time.sleep(0.05)
            if found_reload:
                break
        assert found_reload
        with pytest.raises(models.BusyError, match="another bf watch is already running"):
            watch.serve(live_store, fmt="jsonl")
        assert cli.run(["watch", "--format", "jsonl", "--brain", str(live_root)]) == 1
        assert "another bf watch is already running" in capsys.readouterr().err
        live_store.write("bf.yaml", f"version: {FORMAT}\nname: live-reload\n".encode())
        stop_dl = time.monotonic() + 8.0
        while time.monotonic() < stop_dl and loops.load_unit_state(live_store, "added")["status"] != "stopped":
            time.sleep(0.05)
        assert loops.load_unit_state(live_store, "added")["status"] == "stopped"
    finally:
        stop_live.set()
        live_t.join(timeout=6.0)
        assert not live_t.is_alive()

    # BrokenPipeError on `watch.serve(fmt="jsonl")` triggers clean shutdown without hanging.
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
    bp_t = threading.Thread(target=lambda: watch.serve(live_store, fmt="jsonl"), daemon=True)
    bp_t.start()
    bp_t.join(timeout=6.0)
    assert not bp_t.is_alive()
    monkeypatch.undo()

    ro_root = tmp_path / "ro-status"
    engine.init_brain(ro_root, name="ro-status")
    shutil.rmtree(ro_root / ".bf")
    ro_sum = status.status_summary(Store(ro_root))
    assert ro_sum["events_seq"] == 1
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
        "  bad-flt:\n    when:\n      - event:\n          kind: custom.x\n          filter: '[[['\n    run: [echo, no]\n"
        f"  good-loop:\n    when: 1m\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n".encode(),
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


def test_status_attention_orphans_fallbacks_and_lazy_imports(tmp_path: Path) -> None:
    proc_chk = subprocess.run(
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
    assert proc_chk.returncode == 0, proc_chk.stderr

    root = tmp_path / "v17-ops"
    engine.init_brain(root, name="v17-ops")
    store = Store(root)
    t0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: v17-ops\nloops:\n"
            "  b:\n    run: [bf, build]\n"
            "  c:\n    run: [bf, check]\n"
            "  fail-empty:\n    run: [echo, nope]\n"
        ).encode(),
    )
    assert loops.run_loops(store, ("b", "c"), now=t0)["ok"]

    store.write(
        "concepts/broken.md", b"---\ntype: guide\nstatus: stable\n---\n# Broken\n\n[Missing](concepts/nope.md)\n"
    )
    engine.build(store)
    bv_fail = loops.run_loops(store, ("c",), now=t0)
    assert not bv_fail["ok"]
    assert "concepts/nope.md" in bv_fail["results"][0]["error"]

    loops.record_run(store, "fail-empty", "r1", "2026-11-20T11:00:00Z", ended=t0, dur_ms=10, code=2, err_msg="  ")
    st_doc = loops.load_unit_state(store, "fail-empty")
    assert st_doc["error"] == "exit code 2"
    loops.save_unit_state(store, {**st_doc, "status": "running", "error": ""})
    st_running = status.status_summary(store)
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
    st_fb = status.status_summary(store)
    assert any(a["name"] == "fail-empty" and "exit code 2" in a["error"] for a in st_fb["attention"])

    loops.save_unit_state(store, {**st_doc, "name": "no-log", "status": "failed", "error": ""})
    store.write("bf.yaml", store.read("bf.yaml") + b"  no-log:\n    run: [echo, ok]\n")
    assert any(
        a["name"] == "no-log" and a["error"] == "last run failed" for a in status.status_summary(store)["attention"]
    )

    loops.save_unit_state(store, {**st_doc, "name": "ws-err", "status": "failed", "error": ""})
    store.write("bf.yaml", store.read("bf.yaml") + b"  ws-err:\n    run: [echo, ok]\n")
    store.append_log(
        "ws-err", models.encode({"message": "loop.failed", "error": "   ", "stderr": "real stderr failure"})
    )
    assert any(
        a["name"] == "ws-err" and a["error"] == "real stderr failure" for a in status.status_summary(store)["attention"]
    )

    ev_stop = threading.Event()
    ev_stop.set()
    assert proc.exec_argv(["echo", "hi"], cwd=root, env={}, timeout=5, stop_event=ev_stop)[0] == 130
    assert loops.run_loops(store, ("b",), stop_event=ev_stop)["ran"] == 0
    assert loops.pending_events(store, 0, ()) == ([], 0)
    assert loops.pending_events(store, 999999, (config.EventMatch(kind="x"),)) == (
        [],
        loops.records.latest_event_seq(store),
    )

    loops.save_unit_state(store, {**st_doc, "name": "stale_unit"})
    (root / ".bf" / "b").mkdir(mode=0o700, exist_ok=True)
    (root / ".bf" / "b" / "nested-link").symlink_to(root / "bf.yaml")
    assert "stale_unit" in status.status_summary(store)["orphaned_states"]
    pruned_dry = loops.prune_state(store, dry_run=True)
    assert pruned_dry["dry_run"] is True
    assert "stale_unit" in pruned_dry["pruned"]
    pruned = loops.prune_state(store)
    assert "stale_unit" in pruned["pruned"]
    assert status.status_summary(store)["orphaned_states"] == []


def test_builtin_bf_check_surfaces_errors_past_warnings(tmp_path: Path) -> None:
    root = tmp_path / "chk-warn"
    engine.init_brain(root, name="chk-warn")
    store = Store(root)
    t0 = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)
    store.write("bf.yaml", f"version: {FORMAT}\nname: chk-warn\nloops:\n  check-loop:\n    run: [bf, check]\n".encode())
    store.write("memories/jira.forgotten", b"PROJ-999\n")
    store.write(
        "concepts/warn-note.md",
        b"---\ntype: guide\nstatus: stable\n---\n\n# Warn Note\n\nSee [ticket](jira:PROJ-999).\n",
    )
    engine.build(store)

    val = engine.validate(store)
    assert val["ok"] is True
    assert val["warnings_count"] == 1
    assert val["problems"]

    cfg = config.load(store)
    res_ok = loops.execute_one_loop(store, cfg.loops["check-loop"], trigger="manual", now=t0)
    assert res_ok["status"] == "ok"

    store.write("concepts/bad-fm.md", b"---\nstatus: [\n---\n# Bad\n")
    res_err = loops.execute_one_loop(store, cfg.loops["check-loop"], trigger="manual", now=t0)
    assert res_err["status"] == "failed"
    assert res_err["exit_code"] == 1
    assert "concepts/bad-fm.md" in res_err["error"]
    assert "PROJ-999" not in res_err["error"]


def test_loops_argv0_confinement_cli_order_redaction_and_note_counts(tmp_path: Path) -> None:
    root = tmp_path / "hardened-loops"
    engine.init_brain(root, name="hardened-loops")
    store = Store(root)
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\necho PWNED\n")
    outside.chmod(0o755)
    (root / "sensors").mkdir(exist_ok=True)
    (root / "sensors" / "escape.sh").symlink_to(outside)

    # Relative `argv[0]` with `/` fails closed on `..` traversal, missing file, or symlink escape.
    for bad_cmd in ("../outside.sh", "sensors/escape.sh", "sensors/missing.sh"):
        with pytest.raises(Error):
            proc.prepare_unit(store, "bad", (bad_cmd,), "r1", "2026-11-20T00:00:00Z")

    # `parse_emitted_event` enforces `EVENT_KIND_RE`.
    with pytest.raises(Error, match="invalid event kind"):
        records.parse_emitted_event(b'{"kind":"BAD KIND"}', "loop.event", "loops.x")

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
    store.write("actions/2026-11-20_demo/index.md", b"---\ntype: action\nstatus: draft\n---\n# Demo\n")
    store.write("actions/2026-11-20_demo/outputs/report.md", b"# Attachment\n")
    st = status.status_summary(store)
    assert st["counts"]["notes"] == 2


def test_daemon_health_timer_resets_consecutive_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "daemon-heal"
    engine.init_brain(root, name="daemon-heal")
    store = Store(root)
    unit = config.LoopSpec(
        name="bot",
        run=(sys.executable, "-c", "import time; time.sleep(5)"),
        daemon=True,
        emit="loop.message",
    )
    loops.save_unit_state(
        store,
        {
            **loops.load_unit_state(store, "bot"),
            "status": "failed",
            "consecutive_failures": 12,
            "error": "URLError",
        },
    )
    monkeypatch.setattr(watch, "_HEALTHY_SECS", 0.08)
    stop_ev, wake_ev = threading.Event(), threading.Event()
    stream = watch.DaemonStream(store, unit, stop_ev, wake_ev)
    stream.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            doc = loops.load_unit_state(store, "bot")
            if doc.get("status") == "running" and doc.get("consecutive_failures") == 0 and doc.get("error") == "":
                break
            time.sleep(0.03)
        final_doc = loops.load_unit_state(store, "bot")
        assert final_doc["status"] == "running"
        assert final_doc["consecutive_failures"] == 0
        assert final_doc["error"] == ""
        assert final_doc["last_success"]
    finally:
        stop_ev.set()
        stream.terminate()
    assert loops.load_unit_state(store, "bot")["status"] == "stopped"


def test_convention_sensors_routines_agents_ambiguity_symlinks_and_daemon(tmp_path: Path) -> None:
    root = tmp_path / "conv-brain"
    engine.init_brain(root, name="conv-brain")
    store = Store(root)
    for d in ("sensors", "routines", "agents"):
        (root / d).mkdir(exist_ok=True)
    store.write("schemas/prio.json", OBJ_SCHEMA)

    (root / "sensors" / "prio").write_text(
        f"#!{sys.executable}\nimport json\nprint(json.dumps([{{'id':'p1','title':'From sensor'}}]))\n"
    )
    (root / "sensors" / "prio").chmod(0o755)
    (root / "sensors" / "dup.py").write_text("print('[]')\n")
    (root / "routines" / "dup.sh").write_text("echo hi\n")
    (root / "sensors" / "exec-py.py").write_text(f"#!{sys.executable}\nprint('[]')\n")
    (root / "sensors" / "exec-py.py").chmod(0o755)
    (root / "routines" / "r-only.py").write_text("print('routine-ok')\n")
    (root / "routines" / "sh-only.sh").write_text("echo sh-ok\n")
    (root / "agents" / "agent-only.sh").write_text("echo agent-ok\n")
    (root / "sensors" / "sym-loop.py").symlink_to(root / "routines" / "r-only.py")
    (root / "routines" / "conv-daemon.py").write_text(
        "import json, time\nprint(json.dumps({'id': 'cd-1', 'ok': True}), flush=True)\ntime.sleep(0.2)\n"
    )

    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: conv-brain\n"
        "loops:\n"
        "  prio: {}\n"
        "  dup:\n    enabled: false\n"
        "  exec-py:\n    enabled: false\n"
        "  r-only:\n    enabled: false\n"
        "  sh-only:\n    enabled: false\n"
        "  agent-only:\n    enabled: false\n"
        "  sym-loop:\n    enabled: false\n"
        "  interp-sensor:\n    run: [python3, sensors/exec-py.py]\n    enabled: false\n"
        "  conv-daemon:\n    when: daemon\n    emit: loop.message\n".encode(),
    )
    cfg = config.load(store)
    assert loops.resolve_loop(store, cfg.loops["prio"]) == (("sensors/prio",), "prio", "memories", "upsert", 300)
    assert loops.resolve_loop(store, cfg.loops["exec-py"]) == (
        ("sensors/exec-py.py",),
        "exec-py",
        "memories",
        "upsert",
        300,
    )
    assert loops.resolve_loop(store, cfg.loops["r-only"]) == (
        (sys.executable, "routines/r-only.py"),
        "",
        "memories",
        "upsert",
        300,
    )
    assert loops.resolve_loop(store, cfg.loops["sh-only"]) == (
        ("bash", "routines/sh-only.sh"),
        "",
        "memories",
        "upsert",
        300,
    )
    assert loops.resolve_loop(store, cfg.loops["agent-only"]) == (
        ("bash", "agents/agent-only.sh"),
        "",
        "memories",
        "upsert",
        900,
    )
    assert loops.resolve_loop(store, cfg.loops["interp-sensor"]) == (
        ("python3", "sensors/exec-py.py"),
        "interp-sensor",
        "memories",
        "upsert",
        300,
    )
    uv_spec = config.LoopSpec(name="uv-s", run=("uv", "run", "--quiet", "sensors/exec-py.py"))
    assert loops.resolve_loop(store, uv_spec)[1] == "uv-s"
    with pytest.raises(Error, match="ambiguous convention scripts"):
        loops.resolve_loop(store, cfg.loops["dup"])
    with pytest.raises(Error, match="expected a regular file"):
        loops.resolve_loop(store, cfg.loops["sym-loop"])

    assert loops.run_loops(store, ("prio",))["ok"]
    assert _load_jsonl(store, records.memory_file("prio"))[0]["title"] == "From sensor"

    # Daemon convention script in `routines/` runs cleanly in `DaemonStream`!
    s_stop, s_wake = threading.Event(), threading.Event()
    d = watch.DaemonStream(store, cfg.loops["conv-daemon"], s_stop, s_wake)
    d.start()
    assert s_wake.wait(timeout=5.0)
    s_stop.set()
    d.terminate()
    evs, _ = records.read_events(store)
    assert any(ev["data"].get("id") == "cd-1" for ev in evs)


def test_v32_watch_text_rate_limit_self_trigger_and_structured_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. `format_watch_line` covers all event types in both plain and color modes (including ms and s durations).
    for col in (False, True):
        assert "build" in watch.format_watch_line("watch.build", {"ok": True, "notes": 3, "written": 1}, color=col)
        assert "build" in watch.format_watch_line("watch.build", {"ok": False, "notes": 3, "written": 0}, color=col)
        out_rec = watch.format_watch_line(
            "watch.loop",
            {"loop": "s", "status": "ok", "duration_ms": 1200, "summary": {"records": 5, "added": 2}},
            color=col,
        )
        assert "+2" in out_rec
        assert "1.2s" in out_rec
        out_unch = watch.format_watch_line(
            "watch.loop",
            {"loop": "s", "status": "ok", "duration_ms": 50, "summary": {"records": 5}},
            color=col,
        )
        assert "5 unchanged" in out_unch
        assert "50ms" in out_unch
        assert "3 emitted" in watch.format_watch_line(
            "watch.loop",
            {"loop": "e", "status": "ok", "duration_ms": 50, "summary": {"emitted": 3}},
            color=col,
        )
        assert "boom" in watch.format_watch_line(
            "watch.loop", {"loop": "f", "status": "failed", "duration_ms": 50, "error": "boom"}, color=col
        )
        assert "daemon" in watch.format_watch_line("watch.daemon", {"loop": "d", "state": "started"}, color=col)
        assert "crash" in watch.format_watch_line(
            "watch.daemon", {"loop": "d", "state": "failed", "error": "crash"}, color=col
        )
        assert "bad" in watch.format_watch_line("watch.error", {"error": "bad"}, color=col)

    # 2. Structured JSONL/structlog stderr -> `logs/<loop>.jsonl`, `_last_error_line`, non-exec script, self-trigger suppression, and usages target.
    root = tmp_path / "v32-loops"
    engine.init_brain(root, name="v32-loops")
    store = Store(root)
    (root / "sensors" / "noexec").write_text("#!/bin/sh\necho '[]'\n")
    (root / "routines" / "struct_err.py").write_text(
        "import json, sys\n"
        "sys.stderr.write(json.dumps({'event': 'boot_step', 'level': 'info', 'k': 1}) + '\\n')\n"
        "sys.stderr.write(json.dumps({'severity': 'ERROR', 'message': 'structured boom'}) + '\\n')\n"
        "sys.exit(1)\n"
    )
    (root / "sensors" / "toks.py").write_text(
        "import json\nprint(json.dumps([{'id': 'u1', 'title': 'Turn 1', 'model': 'm1', 'input_tokens': 10}]))\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: v32-loops\n"
        "loops:\n"
        "  noexec:\n    enabled: false\n"
        f"  struct-err:\n    run: [{sys.executable!r}, routines/struct_err.py]\n"
        "  toks:\n    ingest:\n      target: usages\n"
        f"  self-sub:\n    when:\n      - event: loop.succeeded\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n"
        f"  corrupt-st:\n    when: 1m\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n".encode(),
    )
    cfg = config.load(store)
    with pytest.raises(Error, match="not executable"):
        loops.resolve_loop(store, cfg.loops["noexec"])

    err_res = loops.run_loops(store, ("struct-err",))
    assert not err_res["ok"]
    assert err_res["results"][0]["error"] == "structured boom"
    log_rows = [json.loads(ln) for ln in store.read("logs/struct-err.jsonl").decode().splitlines() if ln.strip()]
    assert any(r.get("message") == "boot_step" and r.get("k") == 1 for r in log_rows)
    assert any(r.get("message") == "structured boom" and r.get("severity") == "ERROR" for r in log_rows)

    tok_res = loops.run_loops(store, ("toks",))
    assert tok_res["ok"]
    assert store.exists("usages/toks.jsonl")

    # Self-sourced `loop.succeeded` is ignored by wildcard rule (`source=""`), while other-loop events match!
    records.append_event(store, "loop.succeeded", "self-sub", {"run": "r1"})
    records.append_event(store, "loop.succeeded", "other", {"run": "r2"})
    matched, _ = loops.pending_events(store, 0, cfg.loops["self-sub"].events, self_name="self-sub")
    assert [ev["source"] for ev in matched] == ["toks", "other"]

    # Orphaned state renders in `format_status_text`, and `inside_brain` checks `enclosing.root == store.root`!
    loops.save_unit_state(store, {**loops.load_unit_state(store, "ghost"), "status": "idle"})
    st_text_col = status.format_status_text(status.status_summary(store, all_units=True), color=True)
    assert "orphaned states: ghost" in st_text_col
    assert "\033[" in st_text_col
    # `status_summary` handles missing `memories/` and `usages/` dirs cleanly and reports future/missing timestamps!
    bare_b = tmp_path / "bare-status"
    engine.init_brain(bare_b, name="bare-status")
    bare_st = Store(bare_b)
    shutil.rmtree(bare_b / "memories")
    shutil.rmtree(bare_b / "usages")
    bare_st.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: bare-status\nloops:\n  fut:\n    when: 1h\n    run: [echo]\n".encode(),
    )
    loops.save_unit_state(
        bare_st,
        {
            **loops.load_unit_state(bare_st, "fut"),
            "status": "ok",
            "last_end": "2099-01-01T00:00:00Z",
        },
    )
    bare_txt = status.format_status_text(
        status.status_summary(bare_st, all_units=True),
        now=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
    )
    assert "bare-status" in bare_txt
    assert "now" in bare_txt
    # `reset_loops(..., dry_run=True)` handles missing `.bf/` cleanly.
    shutil.rmtree(bare_b / ".bf", ignore_errors=True)
    assert loops.reset_loops(bare_st, ("fut",), dry_run=True) == {
        "ok": True,
        "brain": "bare-status",
        "reset": [],
        "dry_run": True,
    }
    # Concurrency saturation (`concurrency: 1` with 2 due loops) leaves event quota untouched while slots == 0!
    bare_st.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: bare-status\nconcurrency: 1\nloops:\n"
        f"  s1:\n    when: 1m\n    run: [{sys.executable!r}, -c, 'import time; time.sleep(0.25)']\n"
        f"  s2:\n    when: 1m\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n".encode(),
    )
    stop_sat = threading.Event()
    t_sat = threading.Thread(target=lambda: watch.serve(bare_st, fmt="jsonl", stop_event=stop_sat), daemon=True)
    t_sat.start()
    try:
        dl_sat = time.monotonic() + 5.0
        while time.monotonic() < dl_sat and loops.load_unit_state(bare_st, "s2")["runs"] < 1:
            time.sleep(0.05)
        assert loops.load_unit_state(bare_st, "s1")["runs"] >= 1
        assert loops.load_unit_state(bare_st, "s2")["runs"] >= 1
    finally:
        stop_sat.set()
        t_sat.join(timeout=5.0)
    # `prepare_unit` rewrites `bf` argv and `./` relative script paths cleanly.
    bf_argv, _, _ = proc.prepare_unit(store, "b", ("bf", "build"), "r1", "2026-11-20T00:00:00Z")
    assert bf_argv[:3] == [sys.executable, "-m", "bf"]
    rel_argv, _, _ = proc.prepare_unit(store, "t", ("./sensors/toks.py",), "r1", "2026-11-20T00:00:00Z")
    assert rel_argv[0].endswith("/sensors/toks.py")
    other_b = tmp_path / "other-brain"
    engine.init_brain(other_b, name="other-brain")
    monkeypatch.chdir(other_b)
    assert status.status_summary(store)["inside_brain"] is False
    monkeypatch.chdir(root)
    assert status.status_summary(store)["inside_brain"] is True

    # Corrupt `.bf/<loop>.json` is isolated in `select_due_loops` without breaking healthy loops!
    private_dir(store).write("corrupt-st.json", b"{corrupt", mode=0o600)
    _, pre_fails = loops.select_due_loops(store, cfg, sched_now=datetime.now(UTC))
    assert any(f["loop"] == "corrupt-st" for f in pre_fails)

    # 3. `watch.serve(fmt="text")` executes due loop, trips event rate circuit breaker, and handles config reload error!
    rl_root = tmp_path / "rl-watch"
    engine.init_brain(rl_root, name="rl-watch")
    rl_store = Store(rl_root)
    rl_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: rl-watch\nloops:\n"
        f"  ev-burst:\n    when:\n      - event: burst.ev\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n"
        "  bad-pre:\n    when: 1m\n    run: [routines/missing.py]\n"
        f"  corrupt-w:\n    when: 1m\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n"
        f"  quiet-d:\n    when: daemon\n    run: [{sys.executable!r}, -c, 'import time; print(\"ignored\", flush=True); time.sleep(5)']\n".encode(),
    )
    private_dir(rl_store).write("corrupt-w.json", b"{bad", mode=0o600)
    monkeypatch.setattr(watch, "_EVENT_RATE_MAX", 1)
    monkeypatch.setattr(watch, "next_due_delay", lambda *_a, **_kw: 0.05)
    stop_rl = threading.Event()
    capsys.readouterr()
    t_rl = threading.Thread(target=lambda: watch.serve(rl_store, fmt="text", stop_event=stop_rl), daemon=True)
    t_rl.start()
    try:
        time.sleep(0.25)
        for i in range(3):
            records.append_event(rl_store, "burst.ev", "test", {"id": f"b-{i}"})
            time.sleep(0.12)
        dl = time.monotonic() + 5.0
        while time.monotonic() < dl and loops.load_unit_state(rl_store, "ev-burst")["runs"] < 1:
            time.sleep(0.05)
        # Write broken bf.yaml briefly to exercise config reload error path, then restore valid renamed bf.yaml.
        rl_store.write("bf.yaml", b"version: 999\nname: bad\n")
        time.sleep(0.25)
        rl_store.write("bf.yaml", f"version: {FORMAT}\nname: rl-renamed\n".encode())
        time.sleep(0.25)
    finally:
        stop_rl.set()
        t_rl.join(timeout=6.0)
    out_text = capsys.readouterr().out
    assert "Watching rl-watch" in out_text
    assert "ev-burst" in out_text


def test_sensor_ingest_false_and_agent_timeout_with_defaults(tmp_path: Path) -> None:
    root = tmp_path / "conv-opt"
    engine.init_brain(root, name="conv-opt")
    store = Store(root)
    store.write("sensors/custom.py", b"print('custom-output')\n")
    store.write("agents/worker.py", b"print('ok')\n")
    store.write("agents/fast.py", b"print('ok')\n")
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: conv-opt\n"
            "defaults:\n  timeout: 5m\n"
            "loops:\n"
            "  custom:\n    ingest: false\n"
            "  worker: {}\n"
            "  fast:\n    timeout: 2m\n"
        ).encode(),
    )
    cfg = config.load(store)
    _, ing_src, _, _, _ = loops.resolve_loop(store, cfg.loops["custom"], cfg.reserved_sources)
    assert ing_src == ""
    assert loops.effective_ingest(store, cfg.loops["custom"], cfg.reserved_sources)[0] == ""
    assert loops.resolve_loop(store, cfg.loops["worker"], cfg.reserved_sources)[4] == 900
    assert loops.resolve_loop(store, cfg.loops["fast"], cfg.reserved_sources)[4] == 120
    assert engine.validate(store)["ok"] is True
    ran = loops.run_loops(store, ["custom"])
    assert ran["ok"] is True
    assert not store.exists("memories/custom.jsonl")

    # Explicit `{{...}}` placeholders in `run:` expand to exact runtime values in both dry-run and execution!
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: conv-opt\nloops:\n"
            "  ph:\n"
            f"    run: [{sys.executable!r}, -c, 'import json, sys; print(json.dumps({{\"args\": sys.argv[1:]}}))', "
            "'{{brain}}', '{{loop}}', '{{state}}', '{{start}}', '{{end}}', '{{trigger}}', '{{run}}']\n"
        ).encode(),
    )
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    dry = loops.run_loops(store, ["ph"], dry_run=True, now=now)
    dry_argv = dry["results"][0]["argv"]
    assert dry_argv[3] == str(store.root)
    assert dry_argv[4] == "ph"
    assert dry_argv[5].endswith("/.bf/ph")
    assert dry_argv[6] == "2026-11-21T12:00:00Z"
    assert dry_argv[7] == "2026-11-22T12:00:00Z"
    assert dry_argv[8] == "manual"
    assert dry_argv[9].startswith("ph-")
    exec_res = loops.run_loops(store, ["ph"], now=now)
    assert exec_res["ok"] is True
    got_args = exec_res["results"][0]["summary"]["args"]
    assert got_args[:6] == [
        str(store.root),
        "ph",
        str(store.root / ".bf" / "ph"),
        "2026-11-21T12:00:00Z",
        "2026-11-22T12:00:00Z",
        "manual",
    ]
    assert got_args[6].startswith("ph-")

    # Interpreter-prefixed `{{brain}}/sensors/...` retains `sensors/` auto-ingest convention detection.
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: conv-opt\nloops:\n"
            f"  bph:\n    run: [{sys.executable!r}, '{{{{brain}}}}/sensors/custom.py']\n"
        ).encode(),
    )
    cfg_bph = config.load(store)
    _, ing_src, ing_tgt, ing_mode, _ = loops.resolve_loop(store, cfg_bph.loops["bph"], cfg_bph.reserved_sources)
    assert (ing_src, ing_tgt, ing_mode) == ("bph", "memories", "upsert")
