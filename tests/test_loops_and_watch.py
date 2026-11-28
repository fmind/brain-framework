"""Tests for v19 loops, least-privilege env_from, per-loop locks, dead-letter caps, status, and zero-dep headless watch."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bf import config, cron, engine, index, loops, models, proc, records, render, state, status, watch
from bf.models import FORMAT, NOTES_FILE, Error, InputError
from bf.storage import Store, loop_lock, private_dir


def _load_jsonl(store: Store, path: str) -> list[dict[str, Any]]:
    return [rec for _, rec in records.iter_jsonl(store, path)]


def test_loops_schedules_events_where_convention_daemons_emit_and_timeout(tmp_path: Path) -> None:
    root = tmp_path / "orch-brain"
    engine.init_brain(root, name="orch")
    store = Store(root)
    for sub in ("sensors", "services", "routines", "prompts"):
        (root / sub).mkdir(exist_ok=True)

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
        "raw = sys.stdin.read()\n"
        "evs = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]\n"
        "prompt_path = sys.argv[1]\n"
        "assert os.environ['BF_STATE'].endswith(os.environ['BF_LOOP'])\n"
        "assert pathlib.Path(prompt_path).read_text().startswith('# ')\n"
        "out = {'trigger': os.environ['BF_TRIGGER'], 'events': len(evs), 'prompt': pathlib.Path(prompt_path).name}\n"
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
        "    ingest: upsert\n"
        "    window:\n      backfill: 1d\n      overlap: 10m\n"
        "  conv-sensor:\n"
        "    description: Convention sensor with explicit ingest.\n"
        "    ingest: upsert\n"
        "  no-script:\n"
        "    enabled: false\n"
        "  rebuild:\n"
        "    when:\n      event:\n        kind: records.ingested\n        source: feed\n"
        "    run: [bf, build]\n"
        "  brief:\n"
        "    when:\n      - 1d\n      - '0 8 * * 0'\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, prompts/digest.md]\n"
        "  assistant:\n"
        "    when:\n      - event:\n          kind: loop.message\n          source: chat\n          where:\n            data.direct: true\n"
        f"    run: [{sys.executable!r}, routines/fake_harness.py, prompts/reply.md]\n"
        "  ev-emitter:\n"
        f"    run: [{sys.executable!r}, routines/emit_events.py]\n"
        "    emit: loop.event\n"
        "  delegated:\n"
        "    when:\n      - event:\n          kind: task.delegate\n          source: ev-emitter\n"
        f"    run: [{sys.executable!r}, -c, 'print(\"delegated-ok\")']\n"
        "  slow:\n"
        f"    run: [{sys.executable!r}, routines/slow.py]\n"
        "    timeout: 1s\n"
        "  chat:\n"
        "    when: daemon\n"
        f"    run: [{sys.executable!r}, services/mock_chat.py]\n"
        "    emit: loop.message\n".encode(),
    )

    # 2026-11-15 is a Sunday! At 09:00 UTC both `feed` (`1h`) and `brief` (`[1d, '0 8 * * 0']`) are due.
    t0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    dry = loops.run_loops(store, ("feed",), dry_run=True, now=t0)
    assert dry["results"][0]["status"] == "dry_run"

    # Convention `sensors/<name>.py` locates the script when `run:` is omitted (`ingest:` is declared explicitly).
    conv_res = loops.run_loops(store, ("conv-sensor",), now=t0)
    assert conv_res["ok"]
    assert _load_jsonl(store, records.memory_file("conv-sensor"))[0]["id"] == "c1"
    # Glob matching & `--all` selection:
    glob_dry = loops.run_loops(store, ("conv-*",), dry_run=True, now=t0)
    assert [r["loop"] for r in glob_dry["results"]] == ["conv-sensor"]
    all_dry = loops.run_loops(store, all_loops=True, dry_run=True, now=t0)
    assert "feed" in [r["loop"] for r in all_dry["results"]]
    assert "chat" not in [r["loop"] for r in all_dry["results"]]
    assert "no-script" not in [r["loop"] for r in all_dry["results"]]
    with pytest.raises(InputError, match="matched no matching loops"):
        loops.run_loops(store, ("zzz-*",), now=t0)
    with pytest.raises(InputError, match="is disabled"):
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

    # First-time event-driven loops anchor their cursor at current `latest_event_seq()` before events arrive!
    # Anchor `rebuild`, `assistant`, and `delegated` by running `due_only=True` at `t0`:
    # - `bf run --due` does NOT implicitly rebuild dirty notes!
    # - `brief` fires ONCE and advances BOTH due schedules (`every:86400` and `cron:0 8 * * 0`)!
    store.write("concepts/dirty-due.md", b"---\ntype: guide\nstatus: draft\n---\n\n# Dirty Before Due\n")
    # Pre-initialize event loop cursors at current seq so upcoming events trigger them cleanly.
    loops.reset_loops(store, ("rebuild", "assistant", "delegated"))
    due1 = loops.run_loops(store, due_only=True, now=t0)
    assert due1["ok"], due1
    assert [r["loop"] for r in due1["results"]] == ["feed", "brief"]
    assert "concepts/dirty-due.md" not in store.read(NOTES_FILE).decode()
    assert set(loops.load_unit_state(store, "brief")["schedule_last"]) == {"every:86400", "cron:0 8 * * 0"}
    feed_saved = _load_jsonl(store, records.memory_file("feed"))[0]
    assert feed_saved["id"] == "f1"
    assert feed_saved["text"] == "2026-11-14T09:00:00Z"

    # Follow-up run rewinds `BF_START` by `window.overlap` (`10m` before `t0`).
    t_ov = t0 + timedelta(minutes=2)
    assert loops.run_loops(store, ("feed",), now=t_ov)["ok"]
    feed_ov = _load_jsonl(store, records.memory_file("feed"))[0]
    assert feed_ov["text"] == "2026-11-15T08:50:00Z"

    # Next tick 5 minutes later: `rebuild` triggers on `records.ingested` (and rebuilds `concepts/dirty-due.md`), while `brief` already advanced both schedules!
    t1 = t0 + timedelta(minutes=5)
    due2 = loops.run_loops(store, due_only=True, now=t1)
    assert due2["ok"], due2
    assert [r["loop"] for r in due2["results"]] == ["rebuild"]
    assert "concepts/dirty-due.md" in store.read(NOTES_FILE).decode()

    # Third tick 5 minutes later: cursors advanced, nothing due!
    due3 = loops.run_loops(store, due_only=True, now=t1 + timedelta(minutes=5))
    assert due3["results"] == []

    # Cycle loop with `emit: loop.event` publishes JSONL events into `.bf/state/events.jsonl` that wake downstream `delegated`!
    ev_res = loops.run_loops(store, ("ev-emitter",), now=t1)
    assert ev_res["ok"]
    assert ev_res["results"][0]["summary"] == {"emitted": 2}
    assert (root / ".bf" / "state" / state.EVENTS_FILE).is_file()
    assert not (root / "logs" / "events.jsonl").exists()
    del_res = loops.run_loops(store, due_only=True, now=t1 + timedelta(minutes=6))
    assert [r["loop"] for r in del_res["results"]] == ["delegated"]
    assert del_res["ok"]

    # Invalid emitted output fails atomically with zero partial commits.
    seq_before = state.latest_event_seq(store)
    with pytest.raises(Error, match="must be a JSON object"):
        state.emit_output_events(store, "ev-emitter", "loop.event", b'{"kind":"ok.ev","id":"p0"}\n[1,2]\n')
    assert state.latest_event_seq(store) == seq_before
    with pytest.raises(Error, match="event kind must be a string"):
        state.emit_output_events(store, "ev-emitter", "loop.event", b'{"kind":123}\n')
    assert state.latest_event_seq(store) == seq_before
    with pytest.raises(Error, match="exceeds"):
        state.emit_output_events(store, "ev-emitter", "loop.event", b"x" * (state.MAX_EMIT_BATCH + 1))

    # Reactive `loops.assistant` triggered via `run_loops(due_only=True)` on `loop.message` matching `where: {data.direct: true}`!
    state.append_event(
        store, "loop.message", "chat", {"id": "ev-msg-ignored", "text": "noise", "direct": False}, now=t1
    )
    state.append_event(
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
    # Scheduled run received empty stdin (0 events)!
    assert any(m == {"trigger": "schedule", "events": 0, "prompt": "digest.md"} for m in sent_after_lines)

    # `DaemonStream` captures JSON line output from `loops.chat` into `.bf/state/events.jsonl`!
    cfg = config.load(store)
    stop_ev, wake_ev = threading.Event(), threading.Event()
    daemon = watch.DaemonStream(store, cfg.loops["chat"], stop_ev, wake_ev)
    daemon.start()
    assert wake_ev.wait(timeout=5.0)
    stop_ev.set()
    daemon.terminate()
    ev_items, _ = state.read_events(store)
    assert any(
        ev["kind"] == "loop.message" and ev["source"] == "chat" and ev["data"].get("text") == "ping from chat"
        for ev in ev_items
    )

    # Timeout reaps slow process group and records failure in `.bf/state/slow.json`; `status_summary` includes `counts` + `attention`!
    slow_res = loops.run_loops(store, ("slow",), now=t1)
    assert not slow_res["ok"]
    assert slow_res["results"][0]["exit_code"] == 124
    assert "timed out" in slow_res["results"][0]["error"]
    st_sum = status.status_summary(store, now=t1)
    assert not st_sum["ok"]  # slow is enabled and failed!
    assert st_sum["description"] == "Orchestration test brain."
    assert set(st_sum["counts"]) == {"notes", "memories", "sources", "loops"}
    assert st_sum["counts"]["notes"] >= 2
    st_all = status.status_summary(store, include_loops=True, now=t1)
    assert st_sum["counts"]["loops"] == len(st_all["loops"])
    assert st_all["loops"]["feed"]["description"] == "Ingest sample feed."
    assert st_all["loops"]["feed"]["ingest_mode"] == "upsert"
    assert "loops" not in st_sum

    # Bird's-eye status text table renders cleanly with zero external UI deps!
    rendered = render.format_status_text(st_all, now=t1)
    assert "orch" in rendered
    assert "daemon (loop.message)" in rendered
    assert "2 schedules" in rendered
    assert "1 disabled" in rendered

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
    st_fail = status.status_summary(store, now=t1)
    assert not st_fail["ok"]
    assert {
        "name": "exit-fail",
        "mode": "cycle",
        "reason": "failed",
        "status": "failed",
        "consecutive_failures": 1,
        "error": "exit code 2",
    } in st_fail["attention"]

    for bad_call in (
        lambda: proc.exec_argv((), cwd=root, env={}, timeout=5),
        lambda: loops.run_loops(store, ("missing-loop",)),
    ):
        with pytest.raises((Error, InputError)):
            bad_call()


def test_concurrency_per_loop_locks_dead_letter_and_where_predicates(tmp_path: Path) -> None:
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

    # Pure-Python `match_event_where` covers exact (strict bool vs int), `in`, `exists`, and `prefix`!
    ev_sample = {"kind": "x", "source": "s", "data": {"flag": True, "n": 1, "role": "admin-eu", "nested": {"k": "v"}}}
    assert loops.match_event_where(ev_sample, {"data.flag": True, "data.n": 1}) is True
    assert loops.match_event_where(ev_sample, {"data.flag": 1}) is False  # strict bool != int
    assert loops.match_event_where(ev_sample, {"data.n": True}) is False
    assert loops.match_event_where(ev_sample, {"data.role": {"prefix": "admin-"}}) is True
    assert loops.match_event_where(ev_sample, {"data.role": {"prefix": "user-"}}) is False
    assert loops.match_event_where(ev_sample, {"data.n": {"in": [0, 1, 2]}}) is True
    assert loops.match_event_where(ev_sample, {"data.n": {"in": [True, 2]}}) is False
    assert loops.match_event_where(ev_sample, {"data.nested.k": {"exists": True}}) is True
    assert loops.match_event_where(ev_sample, {"data.missing": {"exists": False}}) is True
    assert loops.match_event_where(ev_sample, {"data.missing": {"exists": True}}) is False

    # Per-loop lock `.bf/locks/loop-<name>.lock` rejects overlapping execution of the SAME loop cleanly (`BusyError` / `"busy"`).
    (root / "routines" / "hold.py").write_text(
        "import pathlib, time\npathlib.Path('routines/hold.up').write_text('1')\ntime.sleep(30)\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: conc\nloops:\n"
        f"  hold:\n    when: 1s\n    run: [{sys.executable!r}, routines/hold.py]\n".encode(),
    )
    stop_hold = threading.Event()
    t_hold = threading.Thread(target=lambda: loops.run_loops(store, ("hold",), stop_event=stop_hold), daemon=True)
    t_hold.start()
    try:
        dl = time.monotonic() + 5.0
        while time.monotonic() < dl and not (root / "routines" / "hold.up").exists():
            time.sleep(0.02)
        assert (root / "routines" / "hold.up").exists()
        with pytest.raises(models.BusyError, match="already running"):
            loops.run_loops(store, ("hold",))
        due_busy = loops.run_loops(store, due_only=True)
        assert due_busy["ok"] is True
        assert due_busy["results"][0]["status"] == "busy"
    finally:
        stop_hold.set()
        t_hold.join(timeout=5.0)

    # 3-retry poison-pill dead-letter cap advances `event_seq` past the poison event on the 3rd event failure!
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: conc\nloops:\n"
        f"  poison:\n    when:\n      event: bad.ev\n    run: [{sys.executable!r}, -c, 'import sys; sys.exit(1)']\n".encode(),
    )
    loops.reset_loops(store, ("poison",))
    ev_bad, _ = state.append_event(store, "bad.ev", "test", {"id": "p1"})
    t_base = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
    for attempt in range(1, 4):
        tick_t = t_base + timedelta(minutes=attempt * 30)
        r = loops.run_loops(store, due_only=True, now=tick_t)
        assert r["ran"] == 1
        assert not r["ok"]
        st_p = loops.load_unit_state(store, "poison")
        if attempt < 3:
            assert st_p["event_seq"] < ev_bad["seq"]
            assert st_p["event_failures"] == attempt
        else:
            assert st_p["event_seq"] >= ev_bad["seq"]
            assert st_p["event_failures"] == 0
            assert "dead_letter_seq" in store.read("logs/poison.jsonl").decode()
    # 4th tick does NOT re-run the dead-lettered event!
    assert loops.run_loops(store, due_only=True, now=t_base + timedelta(hours=5))["ran"] == 0

    # Adaptive `next_due_delay`: clamps to [0.5, 60.0], respects interval, cron, and failure backoff!
    now_d = datetime(2026, 11, 16, 7, 59, 40, tzinfo=UTC)
    cron_cfg = config.Config(
        name="conc",
        loops={
            "c": config.LoopSpec(
                name="c", run=("echo",), schedules=(config.ScheduleSpec("cron:0 8 * * *", cron="0 8 * * *"),)
            ),
            "e": config.LoopSpec(name="e", run=("echo",), schedules=(config.ScheduleSpec("every:30", every=30),)),
            "f": config.LoopSpec(name="f", run=("echo",), schedules=(config.ScheduleSpec("every:300", every=300),)),
        },
    )
    loops.save_unit_state(
        store, {**loops.load_unit_state(store, "c"), "schedule_last": {"cron:0 8 * * *": "2026-11-15T08:00:00Z"}}
    )
    loops.save_unit_state(
        store, {**loops.load_unit_state(store, "e"), "schedule_last": {"every:30": "2026-11-16T07:59:25Z"}}
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
    assert watch.next_due_delay(store, cron_cfg, datetime(2026, 11, 16, 8, 0, 5, tzinfo=UTC)) == 0.5


def test_env_from_least_privilege_secret_redaction_and_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "resilient"
    engine.init_brain(root, name="resilient")
    store = Store(root)
    for sub in ("routines", "services"):
        (root / sub).mkdir(exist_ok=True)
    (root / "routines" / "env_probe.py").write_text(
        "import json, os, sys\n"
        "sys.stderr.write('Skipped feed x\\n')\n"
        "keys = ('GWS_DIR', 'BASE_ONLY', 'GH_TOKEN', 'LOCAL_SECRET', 'UNLISTED_DOTENV', 'UNALLOW_HOST', 'BLANKED', 'BF_BRAIN')\n"
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
        b"LOCAL_SECRET=dotenv_secret_val\nUNLISTED_DOTENV=must_never_leak\nGH_TOKEN=gh_from_dotenv\n",
        mode=0o600,
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: resilient\n"
        "env:\n  GWS_DIR: /base/gws\n  BASE_ONLY: /base/only\n  BLANKED: remove-me\n  GH_TOKEN: yaml-default\n"
        "loops:\n"
        f"  warn-ok:\n    when: 1h\n    run: [{sys.executable!r}, routines/env_probe.py]\n    ingest: snapshot\n"
        "    env_from: [GH_TOKEN, LOCAL_SECRET]\n"
        "    env:\n      BLANKED: ''\n"
        f'  loop-override:\n    run: [{sys.executable!r}, -c, \'import json, os, sys; print(json.dumps([{{"id":"lo","title":os.environ.get("GWS_DIR","")+"|"+os.environ.get("LOCAL_SECRET","")+"|"+"|".join(sys.argv[1:])}}]))\']\n'
        "    ingest: upsert\n    env:\n      GWS_DIR: /loop/gws\n"
        f"  flaky:\n    when: 5m\n    run: [{sys.executable!r}, routines/fail_fast.py]\n"
        f"  flaky-ev:\n    when:\n      event: loop.message\n    run: [{sys.executable!r}, routines/fail_fast.py]\n"
        f"  chat:\n    when: daemon\n    run: [{sys.executable!r}, services/dup_stream.py]\n    emit: loop.message\n".encode(),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BF_BRAIN", str(tmp_path / "evil-override"))
    monkeypatch.setenv("GH_TOKEN", "gh_from_host")
    monkeypatch.setenv("UNALLOW_HOST", "secret-host-val")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "ssh-agent.sock"))

    # Least-privilege isolation & precedence:
    # - `GH_TOKEN` allowlisted on `warn-ok` -> `.env` value wins over host AND over `bf.yaml env:` (`gh_from_dotenv`).
    # - `LOCAL_SECRET` allowlisted only on `warn-ok` -> present on `warn-ok`, absent on `loop-override`!
    # - `UNLISTED_DOTENV` and `UNALLOW_HOST` are NEVER forwarded!
    # - `BLANKED: ''` clears inherited `bf.yaml env.BLANKED`!
    ok_res = loops.run_loops(store, ("warn-ok",))
    assert ok_res["ok"]
    warn_saved = _load_jsonl(store, records.memory_file("warn-ok"))[0]
    assert warn_saved["title"] == f"/base/gws|/base/only|gh_from_dotenv|dotenv_secret_val||||{root.resolve()}"

    lo_res = loops.run_loops(store, ("loop-override",), extra_args=("--lit",))
    assert lo_res["ok"]
    lo_saved = _load_jsonl(store, records.memory_file("loop-override"))[0]
    assert lo_saved["title"] == "/loop/gws||--lit"

    # Exact forwarded secret literal AND proxy URL password are redacted from loop stderr & error summaries!
    proxy_tok = "proxy_pass_123456"
    monkeypatch.setenv("HTTPS_PROXY", "http://alice" + ":" + proxy_tok + "@proxy.example.test:8080")
    store.write(
        "bf.yaml",
        store.read("bf.yaml")
        + (
            f"  leak-env:\n    env_from: [LOCAL_SECRET]\n"
            f'    run: [{sys.executable!r}, -c, \'import os, sys; sys.stderr.write("leaked=" + os.environ["LOCAL_SECRET"] + " p=" + os.environ["HTTPS_PROXY"] + "\\n"); sys.exit(1)\']\n'
        ).encode(),
    )
    leak_res = loops.run_loops(store, ("leak-env",))
    assert not leak_res["ok"]
    assert "dotenv_secret_val" not in leak_res["results"][0]["error"]
    assert "proxy_pass_123456" not in leak_res["results"][0]["error"]
    assert "dotenv_secret_val" not in store.read("logs/leak-env.jsonl").decode()
    assert "proxy_pass_123456" not in store.read("logs/leak-env.jsonl").decode()
    assert "[REDACTED]" in leak_res["results"][0]["error"]

    t0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    assert not loops.run_loops(store, ("flaky",), now=t0)["ok"]
    cfg = config.load(store)
    flaky_state = loops.load_unit_state(store, "flaky")
    assert flaky_state["consecutive_failures"] == 1
    assert cron.due_schedules(cfg.loops["flaky"], flaky_state, t0 + timedelta(seconds=10)) == ()
    assert cron.due_schedules(cfg.loops["flaky"], flaky_state, t0 + timedelta(seconds=65)) != ()

    loops.reset_loops(store, ("flaky-ev",))
    state.append_event(store, "loop.message", "chat", {"id": "ev-b1", "text": "ping"}, now=t0)
    ev_fail = loops.run_loops(store, due_only=True, now=t0)
    assert [r["loop"] for r in ev_fail["results"]] == ["flaky-ev", "warn-ok"]
    assert not ev_fail["ok"]
    assert loops.run_loops(store, due_only=True, now=t0 + timedelta(seconds=5))["results"] == []

    stop_ev, wake_ev = threading.Event(), threading.Event()
    daemon = watch.DaemonStream(store, cfg.loops["chat"], stop_ev, wake_ev)
    daemon.start()
    assert wake_ev.wait(timeout=5.0)
    stop_ev.set()
    daemon.terminate()
    ev_items, _ = state.read_events(store)
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
    out_lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any("transient lock busy" in str(ev.get("error", "")) for ev in out_lines)
    assert any(ev.get("message") == "watch.stopped" for ev in out_lines)


def test_convention_explicit_run_decoupling_and_bf_pe_isolation(tmp_path: Path) -> None:
    root = tmp_path / "conv-brain"
    engine.init_brain(root, name="conv-brain")
    store = Store(root)
    for d in ("sensors", "routines", "agents"):
        (root / d).mkdir(exist_ok=True)

    (root / "sensors" / "prio").write_text(
        f"#!{sys.executable}\nimport json\nprint(json.dumps([{{'id':'p1','title':'From sensor'}}]))\n"
    )
    (root / "sensors" / "prio").chmod(0o755)
    (root / "sensors" / "dup.py").write_text("print('[]')\n")
    (root / "routines" / "dup.sh").write_text("echo hi\n")
    (root / "sensors" / "exec-py.py").write_text(f"#!{sys.executable}\nprint('[]')\n")
    (root / "sensors" / "exec-py.py").chmod(0o755)
    (root / "sensors" / "noexec").write_text("#!/bin/sh\necho '[]'\n")
    (root / "sensors" / "custom.py").write_text("print('custom-output')\n")
    (root / "routines" / "r-only.py").write_text("print('routine-ok')\n")
    (root / "routines" / "sh-only.sh").write_text("echo sh-ok\n")
    (root / "agents" / "agent-only.sh").write_text("echo agent-ok\n")
    (root / "agents" / "fast.py").write_text("print('ok')\n")
    (root / "sensors" / "sym-loop.py").symlink_to(root / "routines" / "r-only.py")

    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: conv-brain\n"
        "loops:\n"
        "  prio:\n    ingest: upsert\n"
        "  custom: {}\n"
        "  dup:\n    enabled: false\n"
        "  exec-py:\n    ingest: upsert\n    enabled: false\n"
        "  noexec:\n    enabled: false\n"
        "  r-only:\n    enabled: false\n"
        "  sh-only:\n    enabled: false\n"
        "  agent-only:\n    enabled: false\n"
        "  fast:\n    timeout: 2m\n    enabled: false\n"
        "  sym-loop:\n    enabled: false\n"
        "  explicit-sensor:\n    run: [python3, sensors/exec-py.py]\n    enabled: false\n"
        "  explicit-ingest:\n    run: [python3, sensors/exec-py.py]\n    ingest:\n      source: gh-2026\n      mode: snapshot\n      schema: gh\n".encode(),
    )
    cfg = config.load(store)
    # Convention-only loops (omitted `run:`): zero folder-name magic for `ingest:` or `timeout:`!
    assert config.resolve_loop(store, cfg.loops["prio"]) == (("sensors/prio",), "prio", "upsert", "prio", 300)
    assert config.resolve_loop(store, cfg.loops["custom"]) == (
        (shutil.which("python3") or sys.executable, "sensors/custom.py"),
        "",
        "upsert",
        "",
        300,
    )
    assert config.resolve_loop(store, cfg.loops["exec-py"]) == (
        ("sensors/exec-py.py",),
        "exec-py",
        "upsert",
        "exec-py",
        300,
    )
    assert config.resolve_loop(store, cfg.loops["r-only"]) == (
        (shutil.which("python3") or sys.executable, "routines/r-only.py"),
        "",
        "upsert",
        "",
        300,
    )
    assert config.resolve_loop(store, cfg.loops["sh-only"]) == (("bash", "routines/sh-only.sh"), "", "upsert", "", 300)
    assert config.resolve_loop(store, cfg.loops["agent-only"]) == (
        ("bash", "agents/agent-only.sh"),
        "",
        "upsert",
        "",
        300,
    )
    assert config.resolve_loop(store, cfg.loops["fast"])[4] == 120

    # Explicit `run:` NEVER triggers argv-sniffing auto-ingest magic!
    assert config.resolve_loop(store, cfg.loops["explicit-sensor"]) == (
        ("python3", "sensors/exec-py.py"),
        "",
        "upsert",
        "",
        300,
    )
    assert config.resolve_loop(store, cfg.loops["explicit-ingest"]) == (
        ("python3", "sensors/exec-py.py"),
        "gh-2026",
        "snapshot",
        "gh",
        300,
    )

    with pytest.raises(Error, match="ambiguous convention scripts"):
        config.resolve_loop(store, cfg.loops["dup"])
    with pytest.raises(Error, match="expected a regular file"):
        config.resolve_loop(store, cfg.loops["sym-loop"])
    with pytest.raises(Error, match="not executable"):
        config.resolve_loop(store, cfg.loops["noexec"])

    assert loops.run_loops(store, ("prio", "custom"))["ok"]
    assert _load_jsonl(store, records.memory_file("prio"))[0]["title"] == "From sensor"
    assert not store.exists("memories/custom.jsonl")

    # `prepare_unit` runs internal `bf` subcommands via `[sys.executable, "-P", "-E", "-m", "bf", ...]`!
    bf_argv, _, stdin_b, _ = proc.prepare_unit(store, "b", ("bf", "build"), "r1", "2026-11-20T00:00:00Z")
    assert bf_argv == [sys.executable, "-P", "-E", "-m", "bf", "build"]
    assert stdin_b is None

    # `{{...}}` placeholders expand in dry-run and execution; relative scripts & symlink escapes are confined!
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: conv-brain\nloops:\n"
            "  ph:\n"
            f"    run: [{sys.executable!r}, -c, 'import json, sys; print(json.dumps({{\"args\": sys.argv[1:]}}))', "
            "'{{brain}}', '{{loop}}', '{{state}}', '{{start}}', '{{end}}', '{{trigger}}', '{{run}}']\n"
        ).encode(),
    )
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    dry = loops.run_loops(store, ["ph"], dry_run=True, now=now)
    assert dry["results"][0]["argv"][3:9] == [
        str(store.root),
        "ph",
        str(store.root / ".bf" / "state" / "ph"),
        "2026-11-21T12:00:00Z",
        "2026-11-22T12:00:00Z",
        "manual",
    ]
    exec_res = loops.run_loops(store, ["ph"], now=now)
    assert exec_res["ok"] is True
    assert exec_res["results"][0]["summary"]["args"][:6] == dry["results"][0]["argv"][3:9]

    outside = tmp_path / "outside.py"
    outside.write_text("print('evil')\n")
    (root / "sensors" / "bad.py").symlink_to(outside)
    for bad_argv in (
        ("../outside.py",),
        ("sensors/bad.py",),
        (f"{store.root}//sensors/bad.py",),
        (f"//{store.root.as_posix().lstrip('/')}/sensors/bad.py",),
        (sys.executable, "concepts/../sensors/bad.py"),
        (sys.executable, f"//{store.root.as_posix().lstrip('/')}/sensors/bad.py"),
        (sys.executable, "/{{brain}}/sensors/bad.py"),
        (sys.executable, "/./{{brain}}/sensors/bad.py"),
        (sys.executable, "{{state}}/../../sensors/bad.py"),
        (sys.executable, f"../{store.root.name}/sensors/bad.py"),
    ):
        with pytest.raises(Error, match=r"regular file inside brain|leaves|normalized"):
            proc.prepare_unit(store, "bad", bad_argv, "r1", "2026-11-23T00:00:00Z")
    # Clean `{{state}}/<file>` targets remain allowed even before runtime creation!
    ok_state_argv, _, _, _ = proc.prepare_unit(
        store, "ok-st", (sys.executable, "-c", "print(1)", "{{state}}/cursor.json"), "r1", "2026-11-23T00:00:00Z"
    )
    assert ok_state_argv[-1].endswith("/.bf/state/ok-st/cursor.json")


def test_status_degraded_overdue_orphans_lazy_imports_and_watch_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Lazy import guard: importing core modules must NOT load third-party deps!
    proc_chk = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, bf.markdown, bf.engine, bf.loops, bf.watch, bf.index; "
                "bad = {'yaml', 'croniter', 'jsonschema', 'markdown', 'markdown_it', 'rich', 'watchfiles', 'mcp'}; "
                "assert not (bad & set(sys.modules)), bad & set(sys.modules)"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc_chk.returncode == 0, proc_chk.stderr

    # 2. `status_summary` surfaces `failed`, `degraded` (`last_run.incomplete`), and `overdue` scheduled loops!
    root = tmp_path / "ops-status"
    engine.init_brain(root, name="ops-status")
    store = Store(root)
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: ops-status\nloops:\n"
            "  ing-deg:\n    when: 30m\n    run: [echo]\n    ingest:\n      source: items\n"
            "  overdue-every:\n    when: 1h\n    run: [echo]\n"
            "  overdue-cron:\n    when: '0 8 * * *'\n    run: [echo]\n"
            "  healthy:\n    when: 1h\n    run: [echo]\n"
            "  failing:\n    when: 5m\n    run: [echo]\n"
            "  off:\n    run: [echo]\n    enabled: false\n"
            "  live-d:\n    when: daemon\n    run: [echo]\n    emit: loop.message\n"
        ).encode(),
    )

    def save_st(
        name: str,
        st_name: str,
        *,
        ago_s: int = 10,
        summary: dict[str, object] | None = None,
        err: str = "",
        consec: int = 0,
    ) -> None:
        ts = (now - timedelta(seconds=ago_s)).isoformat().replace("+00:00", "Z")
        loops.save_unit_state(
            store,
            {
                "name": name,
                "status": st_name,
                "updated": ts,
                "last_start": ts,
                "last_end": "" if st_name == "running" else ts,
                "last_success": "" if st_name == "failed" else ts,
                "runs": 2,
                "failures": consec,
                "consecutive_failures": consec,
                "event_failures": 0,
                "duration_ms": 200,
                "last_run": summary or {},
                "error": err,
            },
        )

    save_st("ing-deg", "ok", ago_s=60, summary={"records": 5, "added": 2, "incomplete": True})
    save_st("overdue-every", "ok", ago_s=12000, summary={"records": 2})  # > max(3*3600, 7200) -> overdue!
    save_st("overdue-cron", "ok", ago_s=86400, summary={"records": 2})  # > 2h past 08:00 UTC fire -> overdue!
    save_st("healthy", "ok", ago_s=300, summary={"records": 4})
    save_st("failing", "failed", ago_s=15, err="boom \x1b[31m bad", consec=3)
    save_st("live-d", "running", ago_s=3600)

    sm = status.status_summary(store, include_loops=True, now=now)
    assert sm["ok"] is False
    att_by_name = {a["name"]: a for a in sm["attention"]}
    assert att_by_name["failing"]["reason"] == "failed"
    assert att_by_name["ing-deg"]["reason"] == "degraded"
    assert att_by_name["overdue-every"]["reason"] == "overdue"
    assert att_by_name["overdue-cron"]["reason"] == "overdue"
    assert "healthy" not in att_by_name
    assert sm["loops"]["ing-deg"]["degraded"] is True
    assert sm["loops"]["overdue-every"]["overdue"] is True

    txt = render.format_status_text(sm, now=now)
    assert "\x1b" not in txt
    assert "ATTENTION (4)" in txt
    assert "degraded" in txt
    assert "overdue" in txt
    assert "1 disabled" in txt

    # 3. `watch.serve` supervises TCP daemon, rebuilds authored notes while ignoring bundle attachment `.md`, and reloads live!
    w_root = tmp_path / "svc-watch"
    engine.init_brain(w_root, name="svc-watch")
    w_store = Store(w_root)
    (w_root / "services").mkdir(exist_ok=True)
    (w_root / "services" / "tcp_srv.py").write_text(
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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    w_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: svc-watch\nloops:\n"
        f"  tcp:\n    when: daemon\n    run: [{sys.executable!r}, services/tcp_srv.py, '{port}']\n".encode(),
    )
    stop_w = threading.Event()
    capsys.readouterr()
    wt = threading.Thread(target=lambda: watch.serve(w_store, fmt="jsonl", stop_event=stop_w), daemon=True)
    wt.start()
    resp_text = ""
    try:
        dl = time.monotonic() + 8.0
        while time.monotonic() < dl:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
                    resp_text = client.recv(64).decode("utf-8")
                    break
            except OSError:
                time.sleep(0.05)
        assert resp_text == "daemon-ready"

        # Single-instance watch.lock prevents second `bf watch` while read-only `status_summary` works freely!
        with pytest.raises(models.BusyError, match="another bf watch is already running"):
            watch.serve(w_store, fmt="jsonl")
        assert status.status_summary(w_store)["brain"] == "svc-watch"

        # Live note edit triggers incremental build automatically!
        dl_b = time.monotonic() + 8.0
        step = 0
        while time.monotonic() < dl_b and "concepts/live-edit.md" not in w_store.read(NOTES_FILE).decode():
            if step % 10 == 1:
                w_store.write("concepts/live-edit.md", b"---\ntype: guide\nstatus: stable\n---\n# Live Edit\n")
            step += 1
            time.sleep(0.05)
        assert "concepts/live-edit.md" in w_store.read(NOTES_FILE).decode()
    finally:
        stop_w.set()
        wt.join(timeout=6.0)
        assert not wt.is_alive()

    assert loops.load_unit_state(w_store, "tcp")["status"] == "stopped"
    w_events = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(e.get("message") == "watch.started" for e in w_events)
    assert any(e.get("message") == "watch.daemon" and e.get("ok") is True for e in w_events)
    assert any(e.get("message") == "watch.stopped" for e in w_events)

    # `watch.serve(fmt="text")` prints overview + live stream and handles `_poll_changes` OSError cleanly!
    stop_txt = threading.Event()
    monkeypatch.setattr(watch, "_poll_changes", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("poll failed")))
    monkeypatch.setattr(watch, "next_due_delay", lambda *_a, **_kw: 0.05)
    capsys.readouterr()
    txt_res: dict[str, Any] = {}
    txt_t = threading.Thread(
        target=lambda: txt_res.update(watch.serve(store, fmt="text", stop_event=stop_txt)), daemon=True
    )
    txt_t.start()
    try:
        dl_o = time.monotonic() + 5.0
        txt_buf = ""
        while time.monotonic() < dl_o:
            txt_buf += capsys.readouterr().out
            if "OSError" in txt_buf and "Watching ops-status" in txt_buf:
                break
            time.sleep(0.02)
        assert "Watching ops-status" in txt_buf
        assert "OSError" in txt_buf
    finally:
        stop_txt.set()
        txt_t.join(timeout=5.0)
        assert not txt_t.is_alive()
    assert txt_res["exit_code"] == 0


def test_daemon_health_timer_overflow_and_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    events_seen: list[tuple[str, dict[str, Any]]] = []
    stream = watch.DaemonStream(store, unit, stop_ev, wake_ev, on_event=lambda k, p: events_seen.append((k, p)))
    stream.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            doc = loops.load_unit_state(store, "bot")
            if doc.get("status") == "running" and doc.get("consecutive_failures") == 0 and doc.get("error") == "":
                break
            time.sleep(0.03)
        final_doc = loops.load_unit_state(store, "bot")
        assert (final_doc["status"], final_doc["consecutive_failures"], final_doc["error"]) == ("running", 0, "")
        assert final_doc["last_success"]
    finally:
        stop_ev.set()
        stream.terminate()
    assert loops.load_unit_state(store, "bot")["status"] == "stopped"
    assert any(p.get("state") == "healthy" and p.get("ok") is True for _, p in events_seen)

    # Immediate exit-0 daemon does not reset backoff (restore default healthy threshold so startup latency never triggers it).
    monkeypatch.setattr(watch, "_HEALTHY_SECS", 30.0)
    waits: list[float] = []
    spin_stop = threading.Event()
    real_wait = spin_stop.wait

    def spy_wait(timeout: float | None = None) -> bool:
        if timeout is not None:
            waits.append(timeout)
        if len(waits) >= 10:
            spin_stop.set()
            return True
        return real_wait(0.005)

    monkeypatch.setattr(spin_stop, "wait", spy_wait)
    spin_unit = config.LoopSpec(name="spin0", run=(sys.executable, "-c", "pass"), daemon=True)
    spin_d = watch.DaemonStream(store, spin_unit, spin_stop, threading.Event())
    spin_d.start()
    spin_d.thread.join(timeout=10.0)
    assert len(waits) == 10
    assert 0.85 <= waits[0] <= 1.0
    assert 1.7 <= waits[1] <= 2.0
    # Repeated daemon failures climb past 30s and cap at 300s to limit offline retry storms.
    assert 255.0 <= waits[-1] <= 300.0

    # Oversized stdout line terminates daemon cleanly and records failure.
    monkeypatch.setattr(models, "MAX_BATCH", 64)
    huge_unit = config.LoopSpec(
        name="huge",
        run=(
            sys.executable,
            "-c",
            "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0)); print('x' * 120, flush=True); time.sleep(5)",
        ),
        daemon=True,
        emit="loop.event",
    )
    h_stop = threading.Event()
    huge_d = watch.DaemonStream(store, huge_unit, h_stop, threading.Event())
    huge_d.start()
    h_dl = time.monotonic() + 5.0
    while time.monotonic() < h_dl and loops.load_unit_state(store, "huge")["status"] != "failed":
        time.sleep(0.02)
    h_stop.set()
    huge_d.terminate()
    huge_st = loops.load_unit_state(store, "huge")
    assert huge_st["status"] == "failed"
    assert "exceeds 64 bytes" in huge_st["error"]


def test_incomplete_watermark_per_trigger_window_crashed_daemon_and_prune_lock(tmp_path: Path) -> None:
    """Verify `incomplete=True` holds watermarks, per-trigger `BF_START`, partial multi-loop failures, crashed daemon detection, and active lock pruning safety."""
    root = tmp_path / "adv-loops"
    engine.init_brain(root, name="adv-loops")
    store = Store(root)
    (root / "sensors").mkdir(exist_ok=True)
    (root / "sensors" / "inc.py").write_text(
        "import json, os\n"
        "print(json.dumps({'records': [{'id': 'i1', 'title': os.environ['BF_START']}], 'incomplete': True}))\n"
    )
    (root / "sensors" / "win.py").write_text(
        "import json, os\nprint(json.dumps([{'id': 'w1', 'title': os.environ['BF_START']}]))\n"
    )
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: adv-loops\nloops:\n"
            "  inc:\n"
            "    when: 1h\n"
            "    ingest: upsert\n"
            "    window:\n      backfill: 1d\n"
            "  win:\n"
            "    ingest: upsert\n"
            "    window:\n      backfill: 1d\n"
            "    when:\n"
            "      - 1d\n"
            "      - cron: '0 8 * * 1'\n"
            "        backfill: 1w\n"
            "  broken-multi:\n"
            "    run: [routines/does-not-exist.py]\n"
            "  crashed-d:\n"
            "    when: daemon\n"
            f"    run: [{sys.executable!r}, -c, 'import time; time.sleep(10)']\n"
        ).encode(),
    )

    # 1. `incomplete=True` holds `last_success`, `last_scheduled`, and `schedule_last` so next run re-covers the window!
    t_mon = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)  # Monday 09:00 UTC -> both 1d and Monday 08:00 cron are due!
    inc_res = loops.run_loops(store, ("inc",), now=t_mon)
    assert inc_res["ok"] is True
    inc_st = state.load_unit_state(store, "inc")
    assert inc_st["last_success"] == ""
    assert inc_st["last_scheduled"] == ""
    assert inc_st["schedule_last"] == {}
    assert inc_st["last_run"]["incomplete"] is True

    # 2. Per-trigger window picks the widest/earliest start (`1w` backfill wins over `1d` when both schedules are due!).
    due_win = loops.run_loops(store, due_only=True, now=t_mon)
    assert due_win["ok"] is True
    win_rec = _load_jsonl(store, records.memory_file("win"))[0]
    assert win_rec["title"] == "2026-11-09T09:00:00Z"  # 7 days before 2026-11-16T09:00:00Z!

    # 3. Multi-loop `bf run` captures pre-flight error in `broken-multi` while still executing sibling `win`!
    multi_res = loops.run_loops(store, ("broken-multi", "win"), now=t_mon)
    assert multi_res["ok"] is False
    by_name = {r["loop"]: r["status"] for r in multi_res["results"]}
    assert by_name == {"broken-multi": "failed", "win": "ok"}

    # 4. Crashed daemon detection: state `"running"` + existing unlocked `.bf/locks/loop-crashed-d.lock` -> `crashed`!
    state.mark_unit_state(store, "crashed-d", status="running")
    locks_dir = private_dir(store, ".bf", "locks")
    locks_dir.write("loop-crashed-d.lock", b"", mode=0o600)
    st_crash = status.status_summary(store, include_loops=True, now=t_mon)
    assert any(a["name"] == "crashed-d" and a["reason"] == "crashed" for a in st_crash["attention"])
    assert st_crash["loops"]["crashed-d"]["crashed"] is True
    assert st_crash["loops"]["crashed-d"]["status"] == "stopped"
    # While the lock IS held, it is NOT reported as crashed:
    with loop_lock(store, "crashed-d", wait=0.0):
        st_live = status.status_summary(store, include_loops=True, now=t_mon)
        assert not any(a["name"] == "crashed-d" for a in st_live["attention"])
        assert st_live["loops"]["crashed-d"]["status"] == "running"

    # 5. `prune_state` skips an actively locked orphan `loop-*.lock` instead of unlinking an active lock inode!
    from bf.storage import is_loop_locked, try_unlink_loop_lock

    assert is_loop_locked(store, "never-existed") is False
    assert try_unlink_loop_lock(store, "loop-never-existed.lock") is False
    with loop_lock(store, "active-orphan", wait=0.0):
        pruned = loops.prune_state(store)
        assert "loop-active-orphan.lock" not in pruned["pruned"]["locks"]
        assert (root / ".bf" / "locks" / "loop-active-orphan.lock").exists()
    pruned_after = loops.prune_state(store)
    assert "loop-active-orphan.lock" in pruned_after["pruned"]["locks"]
    assert not (root / ".bf" / "locks" / "loop-active-orphan.lock").exists()

    # Multi-loop batch captures BusyError on locked loop while running sibling; single dry-run re-raises; `DaemonStream` handles held lock cleanly!
    with loop_lock(store, "inc", wait=0.0):
        busy_batch = loops.run_loops(store, ("inc", "win"), now=t_mon)
        assert busy_batch["ok"] is False
        assert {r["loop"]: r["status"] for r in busy_batch["results"]} == {"inc": "failed", "win": "ok"}
    with pytest.raises(Error):
        loops.run_loops(store, ("broken-multi",), dry_run=True, now=t_mon)

    with loop_lock(store, "crashed-d", wait=0.0):
        d_stop = threading.Event()
        d_ev: list[tuple[str, dict[str, Any]]] = []
        ds = watch.DaemonStream(
            store,
            config.load(store).loops["crashed-d"],
            d_stop,
            threading.Event(),
            on_event=lambda k, p: d_ev.append((k, p)),
        )
        ds.start()
        dl_d = time.monotonic() + 3.0
        while time.monotonic() < dl_d and not any("already running" in str(p.get("error", "")) for _, p in d_ev):
            time.sleep(0.03)
        d_stop.set()
        ds.terminate()
        assert any("already running" in str(p.get("error", "")) for _, p in d_ev)

    # 6. `status_summary` catches invalid `bf.yaml` cleanly instead of raising!
    store.write("bf.yaml", b"version: 999\nname: broken\n")
    st_bad = status.status_summary(store, now=t_mon)
    assert st_bad["ok"] is False
    assert st_bad["problems"]


def test_dry_run_no_bf_dir_record_run_log_failure_and_dst_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. `bf run --dry-run` does not create `.bf/` on disk.
    root = tmp_path / "dry"
    engine.init_brain(root, name="dry")
    store = Store(root)
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: dry\nloops:\n  echo:\n    run: [{sys.executable!r}, -c, 'print(1)']\n".encode(),
    )
    shutil.rmtree(root / ".bf", ignore_errors=True)
    dry_res = loops.run_loops(store, ["echo"], dry_run=True)
    assert dry_res["ok"] is True
    assert not (root / ".bf").exists()

    # 2. `record_run` preserves `"ok"` state and emits `loop.succeeded` even if `append_log` raises.
    monkeypatch.setattr(Store, "append_log", lambda *_a, **_kw: (_ for _ in ()).throw(Error("logs busy")))
    ran = loops.run_loops(store, ["echo"])
    assert ran["ok"] is True
    assert state.load_unit_state(store, "echo")["status"] == "ok"
    evs, _ = state.read_events(store)
    assert any(e["kind"] == "loop.succeeded" and e["source"] == "echo" for e in evs)

    # 3. PEP 495 DST fall-back hour: America/New_York repeated 01:00 on 2026-11-01 still triggers.
    dst_loop = config.LoopSpec(
        name="dst",
        run=("echo",),
        schedules=(config.ScheduleSpec("cron:0 * * * *", cron="0 * * * *"),),
    )
    state_edt = {"schedule_last": {"cron:0 * * * *": "2026-11-01T05:30:00Z"}}
    now_est = datetime(2026, 11, 1, 6, 15, tzinfo=UTC)
    assert cron.due_schedules(dst_loop, state_edt, now_est, tz_name="America/New_York") != ()
    assert cron.next_schedule_delay(dst_loop, state_edt, now_est, tz_name="America/New_York") == 0.5

    # 4. DST spring-forward in Europe/Paris (2026-03-29 02:00 skipped -> fires at 01:00Z).
    paris_cfg = config.LoopSpec(
        name="paris", run=("echo",), schedules=(config.ScheduleSpec("cron:0 2 * * *", cron="0 2 * * *"),)
    )
    assert (
        cron.due_schedules(
            paris_cfg,
            {"last_scheduled": "2026-03-28T01:00:00Z"},
            datetime(2026, 3, 29, 0, 59, tzinfo=UTC),
            tz_name="Europe/Paris",
        )
        == ()
    )
    assert (
        cron.due_schedules(
            paris_cfg,
            {"last_scheduled": "2026-03-28T01:00:00Z"},
            datetime(2026, 3, 29, 1, 5, tzinfo=UTC),
            tz_name="Europe/Paris",
        )
        != ()
    )


def test_watch_live_reload_rate_limit_broken_pipe_and_proc_status_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. `format_watch_line` across all event types in both plain and color modes!
    for col in (False, True):
        assert "build" in render.format_watch_line("watch.build", {"ok": True, "notes": 3, "written": 1}, color=col)
        assert "build" in render.format_watch_line(
            "watch.build", {"ok": False, "notes": 3, "written": 0, "problems": ["p1", "p2"]}, color=col
        )
        assert "+2" in render.format_watch_line(
            "watch.loop",
            {"loop": "s", "status": "ok", "duration_ms": 1200, "summary": {"records": 5, "added": 2}},
            color=col,
        )
        assert "5 unchanged" in render.format_watch_line(
            "watch.loop", {"loop": "s", "status": "ok", "duration_ms": 50, "summary": {"records": 5}}, color=col
        )
        assert "3 emitted" in render.format_watch_line(
            "watch.loop", {"loop": "e", "status": "ok", "duration_ms": 50, "summary": {"emitted": 3}}, color=col
        )
        assert "boom" in render.format_watch_line(
            "watch.loop", {"loop": "f", "status": "failed", "duration_ms": 50, "error": "boom"}, color=col
        )
        assert "daemon" in render.format_watch_line("watch.daemon", {"loop": "d", "state": "started"}, color=col)
        assert "crash" in render.format_watch_line(
            "watch.daemon", {"loop": "d", "state": "failed", "error": "crash"}, color=col
        )
        assert "start" in render.format_watch_line("watch.started", {"brain": "b"}, color=col)
        assert "stop" in render.format_watch_line("watch.stopped", {"brain": "b"}, color=col)
        assert "bad" in render.format_watch_line("watch.error", {"error": "bad"}, color=col)

    # 2. `proc` edges: missing binary (127), stdout overflow (1), pre-set stop_event (130), host env_from fallback & bf PYTHON* strip!
    root = tmp_path / "edge-ops"
    engine.init_brain(root, name="edge-ops")
    store = Store(root)
    assert proc.exec_argv(["nonexistent-bf-bin-xyz"], cwd=root, env={}, timeout=5)[0] == 127
    ev_stop = threading.Event()
    ev_stop.set()
    assert proc.exec_argv(["echo", "hi"], cwd=root, env={}, timeout=5, stop_event=ev_stop)[0] == 130
    with monkeypatch.context() as mp_ov:
        mp_ov.setattr(proc, "MAX_BATCH", 32)
        ov_rc, _, ov_err = proc.exec_argv([sys.executable, "-c", "print('x' * 100)"], cwd=root, env={}, timeout=5)
        assert ov_rc == 1
        assert "exceeds 32 bytes" in ov_err
    host_val = "super_secret_host_1234"
    monkeypatch.setenv("HOST_ONLY_SECRET", host_val)
    _, p_env, _, p_secs = proc.prepare_unit(
        store, "u", ("echo", "ok"), "r1", "2026-11-20T00:00:00Z", unit_env_from=("HOST_ONLY_SECRET", "PATH")
    )
    assert p_env["HOST_ONLY_SECRET"] == host_val
    assert host_val in p_secs
    assert "PATH" in p_env

    # 3. `loops` edges: structured stderr `_last_error_line`, `_bf_error_summary` past warnings, `status._log_error_fallback`, corrupt state isolation!
    (root / "routines" / "struct_err.py").write_text(
        "import json, sys\n"
        "sys.stderr.write(json.dumps({'severity': 'ERROR', 'message': 'structured boom'}) + '\\n')\n"
        "sys.exit(1)\n"
    )
    store.write("memories/jira.forgotten", b"PROJ-9\n")
    store.write("concepts/warn.md", b"---\ntype: guide\nstatus: stable\n---\n# W\n\n[T](jira:PROJ-9)\n")
    engine.build(store)
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: edge-ops\nloops:\n"
        f"  struct-err:\n    run: [{sys.executable!r}, routines/struct_err.py]\n"
        "  chk:\n    run: [bf, check]\n"
        "  corrupt-st:\n    when: 1m\n    run: [echo, ok]\n"
        "  log-err:\n    run: [echo, ok]\n"
        "  log-code:\n    run: [echo, ok]\n"
        "  no-log:\n    run: [echo, ok]\n".encode(),
    )
    se_res = loops.run_loops(store, ("struct-err",))
    assert not se_res["ok"]
    assert se_res["results"][0]["error"] == "structured boom"

    # `_bf_error_summary` skips warning lines and reports actual error lines!
    store.write("concepts/bad.md", b"---\nstatus: [\n---\n# Bad\n")
    chk_res = loops.run_loops(store, ("chk",))
    assert not chk_res["ok"]
    assert "concepts/bad.md" in chk_res["results"][0]["error"]
    assert "PROJ-9" not in chk_res["results"][0]["error"]
    store.delete("concepts/bad.md")

    # Corrupt `.bf/state/<loop>.json` is isolated in `select_due_loops` & `status_summary`, and `_log_error_fallback` covers stderr/exit_code/default!
    private_dir(store, ".bf", "state").write("corrupt-st.json", b"{corrupt", mode=0o600)
    _, pre_fails = loops.select_due_loops(store, config.load(store), sched_now=datetime.now(UTC))
    assert any(f["loop"] == "corrupt-st" for f in pre_fails)

    private_dir(store, ".bf", "state").write("corrupt-st.json", b"{corrupt", mode=0o600)
    loops.save_unit_state(store, {**state.default_unit_state("log-err"), "status": "failed", "error": ""})
    store.append_log("log-err", models.encode({"message": "loop.failed", "error": "   ", "stderr": "real stderr boom"}))
    loops.save_unit_state(store, {**state.default_unit_state("log-code"), "status": "failed", "error": ""})
    store.append_log("log-code", models.encode({"message": "loop.failed", "exit_code": 7}))
    loops.save_unit_state(store, {**state.default_unit_state("no-log"), "status": "failed", "error": ""})

    st_fb = status.status_summary(store, include_loops=True)
    att_map = {a["name"]: a for a in st_fb["attention"]}
    assert att_map["log-err"]["error"] == "real stderr boom"
    assert att_map["log-code"]["error"] == "exit code 7"
    assert att_map["no-log"]["error"] == "last run failed"
    assert att_map["corrupt-st"]["reason"] == "failed"

    # `status.corpus_counts` scan fallback + scale_warnings when SQLite index is absent!
    records.ingest(store, "big", b'[{"id":"1","title":"A"},{"id":"2","title":"B"}]')
    store.delete(f".bf/cache/{index.INDEX_NAME}")
    with monkeypatch.context() as mp_sc:
        mp_sc.setattr(models, "SCALE_WARN_RECORDS", 1)
        sw_list: list[str] = []
        cnts, _, is_st = status.corpus_counts(store, config.load(store), scale_warnings=sw_list)
        assert is_st is True
        assert cnts["memories"] == 2
        assert sw_list

    # 4. Live `bf.yaml` + `.env` + daemon script reload + invalid `.env` error + `BrokenPipeError` + event rate limit in `watch.serve`!
    live_root = tmp_path / "live-reload"
    engine.init_brain(live_root, name="live-reload")
    live_store = Store(live_root)
    live_store.write(
        "routines/live_d.py",
        b"import json, os, time\n"
        b"print('[1,2]', flush=True)\n"
        b"print(json.dumps({'id': 'v1-' + os.environ.get('D_TAG', 'none'), 'kind': 'daemon.ver'}), flush=True)\n"
        b"time.sleep(10)\n",
    )
    live_store.write(".env", b"D_TAG=init\n", mode=0o600)
    live_store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: live-reload\nloops:\n"
        f"  live-d:\n    when: daemon\n    env_from: [D_TAG]\n    run: [{sys.executable!r}, routines/live_d.py]\n    emit: loop.event\n"
        f"  ev-burst:\n    when:\n      - event: burst.ev\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n".encode(),
    )
    loops.reset_loops(live_store, ("ev-burst",))

    def wait_event_id(target_id: str) -> bool:
        dl = time.monotonic() + 8.0
        while time.monotonic() < dl:
            evs, _ = state.read_events(live_store)
            if any(e.get("kind") == "daemon.ver" and e.get("data", {}).get("id") == target_id for e in evs):
                return True
            time.sleep(0.05)
        return False

    stop_live = threading.Event()
    capsys.readouterr()
    with monkeypatch.context() as mp_live:
        mp_live.setattr(watch, "_EVENT_RATE_MAX", 1)
        mp_live.setattr(watch, "next_due_delay", lambda *_a, **_kw: 0.05)
        live_t = threading.Thread(
            target=lambda: watch.serve(live_store, fmt="jsonl", stop_event=stop_live), daemon=True
        )
        live_t.start()
        try:
            assert wait_event_id("v1-init")
            assert "emitted event must be a JSON object" in live_store.read("logs/live-d.jsonl").decode()
            # Trigger event burst past rate limit (`_EVENT_RATE_MAX = 1`) and assert throttling fires!
            state.append_event(live_store, "burst.ev", "test", {"id": "b-0"})
            dl_rl = time.monotonic() + 5.0
            while time.monotonic() < dl_rl and loops.load_unit_state(live_store, "ev-burst")["runs"] < 1:
                time.sleep(0.03)
            assert loops.load_unit_state(live_store, "ev-burst")["runs"] >= 1
            state.append_event(live_store, "burst.ev", "test", {"id": "b-1"})
            dl_rl2 = time.monotonic() + 5.0
            rl_out = ""
            while time.monotonic() < dl_rl2:
                rl_out += capsys.readouterr().out
                if "event rate limit reached" in rl_out:
                    break
                time.sleep(0.03)
            assert "event rate limit reached" in rl_out
            # Changing an unrelated `.env` key must NOT restart a daemon that does not allowlist it.
            start_before = loops.load_unit_state(live_store, "live-d")["last_start"]
            live_store.write(".env", b"D_TAG=init\nUNRELATED_KEY=changed\n", mode=0o600)
            time.sleep(0.7)
            assert loops.load_unit_state(live_store, "live-d")["last_start"] == start_before
            # Rotate allowlisted `.env` key and edit daemon script -> live daemon restarts!
            live_store.write(".env", b"D_TAG=rotated\nUNRELATED_KEY=changed\n", mode=0o600)
            assert wait_event_id("v1-rotated")
            live_store.write(
                "routines/live_d.py",
                b"import json, os, time\n"
                b"print(json.dumps({'id': 'v2-' + os.environ.get('D_TAG', 'none'), 'kind': 'daemon.ver'}), flush=True)\n"
                b"time.sleep(10)\n",
            )
            assert wait_event_id("v2-rotated")
            # Invalid `.env` permissions emits `watch.error` without stopping running daemon!
            live_store.write(".env", b"D_TAG=bad\n", mode=0o644)
            dl_err = time.monotonic() + 5.0
            saw_err = False
            while time.monotonic() < dl_err:
                if "mode 600" in capsys.readouterr().out:
                    saw_err = True
                    break
                time.sleep(0.05)
            assert saw_err
            assert loops.load_unit_state(live_store, "live-d")["status"] == "running"
            live_store.write(".env", b"D_TAG=fixed\n", mode=0o600)
            # Remove daemon from bf.yaml -> supervisor terminates it cleanly!
            live_store.write("bf.yaml", f"version: {FORMAT}\nname: live-reload\n".encode())
            dl_stop = time.monotonic() + 6.0
            while time.monotonic() < dl_stop and loops.load_unit_state(live_store, "live-d")["status"] != "stopped":
                time.sleep(0.05)
            assert loops.load_unit_state(live_store, "live-d")["status"] == "stopped"
        finally:
            stop_live.set()
            live_t.join(timeout=6.0)
            assert not live_t.is_alive()

    # BrokenPipeError on `watch.serve(fmt="jsonl")` shuts down cleanly without redirecting worker OS fd 1!
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

    bp_calls: list[int] = []
    with monkeypatch.context() as mp_bp:
        mp_bp.setattr(models, "silence_broken_pipe", lambda: bp_calls.append(1))
        mp_bp.setattr(sys, "stdout", _BrokenOut())
        bp_t = threading.Thread(target=lambda: watch.serve(live_store, fmt="jsonl"), daemon=True)
        bp_t.start()
        bp_t.join(timeout=6.0)
        assert not bp_t.is_alive()
    assert len(bp_calls) >= 1

    # 5. Extra `proc.drain_tail` (incl. 64 KiB secret-boundary safety), `prepare_unit` security stripping, `loops`, and `watch` coverage!
    pr_fd, pw_fd = os.pipe()
    boundary_marker = "BOUNDARY_MARKER_VALUE_987654321"
    with os.fdopen(pr_fd, "rb", buffering=0) as pr_s:
        dt_t, dt_snap = proc.drain_tail(
            pr_s,
            256,
            on_line=lambda raw: "" if b"SKIP" in raw else None,
            secrets=(boundary_marker,),
        )
        with os.fdopen(pw_fd, "wb") as pw_s:
            pw_s.write(b"\nSKIP_ME\n" + (b"a" * 65_520) + boundary_marker.encode() + b" tail-over\nNEXT_OK\n")
        dt_t.join(timeout=3)
        snap_val = dt_snap()
        assert "NEXT_OK" in snap_val
        assert "[truncated]" in snap_val
        assert boundary_marker not in snap_val
        assert "BOUNDARY_MARKER_" not in snap_val

    # Self-`bf` `prepare_unit` strips injected `PYTHON*`/`LD_*`/`DYLD_*` + forwards `NO_COLOR` + keeps `.bf/` absent on `create_state=False`!
    bare_root = tmp_path / "bare-no-bf"
    bare_root.mkdir()
    bare_st = Store(bare_root)
    bare_st.write("bf.yaml", f"version: {FORMAT}\nname: bare\n".encode())
    with monkeypatch.context() as mp_env:
        mp_env.setenv("PYTHONSTARTUP", "/evil.py")
        mp_env.setenv("LD_PRELOAD", "/evil.so")
        mp_env.setenv("DYLD_INSERT_LIBRARIES", "/evil.dylib")
        mp_env.setenv("NO_COLOR", "1")
        _, bf_env, _, _ = proc.prepare_unit(
            bare_st,
            "chk",
            ("bf", "check"),
            "r1",
            "2026-11-20T00:00:00Z",
            unit_env_from=("PYTHONSTARTUP", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PATH"),
            create_state=False,
        )
    assert not any(k.startswith(("PYTHON", "LD_", "DYLD_")) for k in bf_env)
    assert bf_env.get("NO_COLOR") == "1"
    assert not (bare_root / ".bf").exists()

    # P0 `event-loop-unpersisted-cursor`, F8 self-trigger suppression, and BF-07 emitted-event secret redaction!
    ev_auto_root = tmp_path / "ev-auto"
    engine.init_brain(ev_auto_root, name="ev-auto")
    ev_st = Store(ev_auto_root)
    ev_st.write(".env", b"OPAQUE_CRED=opaque_secret_value_123456\n", mode=0o600)
    ev_st.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: ev-auto\nloops:\n"
        f"  brand-new:\n    when:\n      - event: ping.now\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n"
        f"  self-guard:\n    when:\n      - event: loop.succeeded\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n"
        f'  emitter:\n    env_from: [OPAQUE_CRED]\n    run: [{sys.executable!r}, -c, \'import json, os; print(json.dumps({{"note": os.environ["OPAQUE_CRED"]}}))\']\n    emit: secret.ev\n'.encode(),
    )
    # Historical event emitted BEFORE first tick must NOT trigger brand-new loop,
    # while first tick persists `.bf/state/brand-new.json` so a SUBSEQUENT event DOES trigger it (no manual `bf state reset`)!
    state.append_event(ev_st, "ping.now", "test", {"id": "old"})
    due0, _ = loops.select_due_loops(ev_st, config.load(ev_st), sched_now=datetime.now(UTC))
    assert due0 == []
    assert state.has_unit_state(ev_st, "brand-new") is True
    state.append_event(ev_st, "ping.now", "test", {"id": "fresh"})
    due1, _ = loops.select_due_loops(ev_st, config.load(ev_st), sched_now=datetime.now(UTC))
    assert [it[0].name for it in due1] == ["brand-new"]

    # F8: `self-guard` ignores its OWN `loop.succeeded` event but fires on sibling `emitter`'s `loop.succeeded`!
    assert loops.run_loops(ev_st, ("self-guard",))["ok"] is True
    due_self, _ = loops.select_due_loops(ev_st, config.load(ev_st), sched_now=datetime.now(UTC))
    assert all(it[0].name != "self-guard" for it in due_self)
    assert loops.run_loops(ev_st, ("emitter",))["ok"] is True
    # BF-07: emitted event redacted `OPAQUE_CRED` before writing `.bf/state/events.jsonl`!
    ev_log_raw = private_dir(ev_st, ".bf", "state").read("events.jsonl").decode()
    assert "opaque_secret_value_123456" not in ev_log_raw
    assert "[REDACTED]" in ev_log_raw
    due_sib, _ = loops.select_due_loops(ev_st, config.load(ev_st), sched_now=datetime.now(UTC))
    assert any(it[0].name == "self-guard" for it in due_sib)

    # Render: failed + overdue loop shows `failed` (not `overdue`)!
    _, f_rows, _ = render._build_loop_rows(  # noqa: SLF001
        {"l1": {"enabled": True, "status": "failed", "overdue": True, "error": "boom"}},
        datetime(2026, 11, 20, tzinfo=UTC),
        color=False,
    )
    assert "failed" in f_rows[0]
    assert "overdue" not in f_rows[0]

    # F6: `_scan_watch_snapshot` excludes `.git/`, `memories/`, `logs/`, `.venv/`, and symlinks!
    (ev_auto_root / ".git").mkdir(exist_ok=True)
    (ev_auto_root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (ev_auto_root / ".venv").mkdir(exist_ok=True)
    (ev_auto_root / ".venv" / "x.py").write_text("")
    shutil.rmtree(ev_auto_root / "sensors")
    (ev_auto_root / "sensors").symlink_to(tmp_path)
    snap_keys = set(watch._scan_watch_snapshot(ev_st, config.load(ev_st).notes))  # noqa: SLF001
    assert not any(k.startswith((".git/", ".venv/", "memories/", "logs/", "sensors/")) for k in snap_keys)
    assert "concepts/welcome.md" in snap_keys

    # Concurrent event append during `select_due_loops` never replays historical events into a brand-new loop
    # and always persists the new loop's `.bf/state/<name>.json` cursor!
    race_root = tmp_path / "ev-race"
    engine.init_brain(race_root, name="ev-race")
    race_st = Store(race_root)
    race_st.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: ev-race\nloops:\n"
        f"  alpha:\n    when:\n      - event: a.ev\n    run: [{sys.executable!r}, -c, 'print(\"a\")']\n"
        f"  beta:\n    when:\n      - event: b.ev\n    run: [{sys.executable!r}, -c, 'print(\"b\")']\n".encode(),
    )
    state.append_event(race_st, "b.ev", "test", {"id": "hist-b"})
    state.append_event(race_st, "a.ev", "test", {"id": "a1"})
    loops.save_unit_state(race_st, state.default_unit_state("alpha", event_seq=1))
    real_load_u = loops.load_unit_state
    injected_ev = [False]

    def _racy_load(st: Store, name: str, **kw: Any) -> dict[str, Any]:
        if name == "beta" and not injected_ev[0]:
            injected_ev[0] = True
            state.append_event(race_st, "other.ev", "test", {"id": "concurrent"})
        return real_load_u(st, name, **kw)

    with monkeypatch.context() as mp_rc:
        mp_rc.setattr(loops, "load_unit_state", _racy_load)
        race_due, _ = loops.select_due_loops(race_st, config.load(race_st), sched_now=datetime.now(UTC))
    assert [it[0].name for it in race_due] == ["alpha"]
    assert state.has_unit_state(race_st, "beta") is True
    state.append_event(race_st, "b.ev", "test", {"id": "fresh-b"})
    race_due2, _ = loops.select_due_loops(race_st, config.load(race_st), sched_now=datetime.now(UTC))
    assert any(it[0].name == "beta" for it in race_due2)

    # `loops` edges: empty rules, source mismatch, corrupt state in `execute_one_loop` & `select_due_loops(dry_run=True)`, parallel `--due` + per-loop failure!
    assert loops.pending_events(store, 0, ()) == ([], 0)
    assert loops.pending_events(bare_st, 0, (config.EventMatch(kind="x.y"),)) == ([], 0)
    assert not loops._rule_matches({"kind": "x.y", "source": "other"}, config.EventMatch(kind="x.y", source="want"))  # noqa: SLF001
    assert loops._last_error_line("   \n") == ""  # noqa: SLF001
    assert loops._bf_error_summary(("echo", "x"), b"{}") == ""  # noqa: SLF001

    par_root = tmp_path / "par-due"
    engine.init_brain(par_root, name="par-due")
    par_st = Store(par_root)
    par_st.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: par-due\nconcurrency: 2\nloops:\n"
        f"  ok1:\n    when: 1m\n    run: [{sys.executable!r}, -c, 'print(\"ok1\")']\n"
        f"  ok2:\n    when: 1m\n    run: [{sys.executable!r}, -c, 'print(\"ok2\")']\n"
        "  bad-script:\n    when: 1m\n    run: [routines/missing.py]\n".encode(),
    )
    # Parallel `--due` runs >= 2 loops concurrently AND isolates pre-flight failure in `bad-script`!
    par_res = loops.run_loops(par_st, due_only=True)
    assert not par_res["ok"]
    by_lp = {r["loop"]: r["status"] for r in par_res["results"]}
    assert by_lp["ok1"] == "ok"
    assert by_lp["ok2"] == "ok"
    assert by_lp["bad-script"] == "failed"
    # `select_due_loops(dry_run=True)` re-raises corrupt state instead of mutating; `execute_one_loop` tolerates corrupt state!
    private_dir(par_st, ".bf", "state").write("ok1.json", b"{corrupt", mode=0o600)
    with pytest.raises(Error, match="invalid JSON"):
        loops.select_due_loops(par_st, config.load(par_st), sched_now=datetime.now(UTC), dry_run=True)
    assert loops.execute_one_loop(par_st, config.load(par_st).loops["ok1"], trigger="manual")["status"] == "ok"
    # `watch.next_due_delay` tolerates corrupt `.bf/state/<loop>.json`!
    private_dir(par_st, ".bf", "state").write("ok1.json", b"{corrupt", mode=0o600)
    assert watch.next_due_delay(par_st, config.load(par_st), datetime.now(UTC)) >= 0.5
    # `run_loops` with pre-set `stop_event` stops cleanly without executing!
    ev_pre = threading.Event()
    ev_pre.set()
    assert loops.run_loops(par_st, ("ok1",), stop_event=ev_pre)["ok"] is False

    # `watch.serve` with fake `_poll_changes` exercises bundle-dir, `schemas/*.json`, `notes:` reload, and `_run_bg` failure!
    wf_root = tmp_path / "wf-classify"
    engine.init_brain(wf_root, name="wf-classify")
    wf_st = Store(wf_root)
    wf_st.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: wf-classify\nloops:\n  fail-l:\n    when: 1m\n    run: [routines/nope.py]\n".encode(),
    )
    stop_wf = threading.Event()

    def _fake_poll(*_args: Any, **_kw: Any) -> Any:
        # Change `notes:` in `bf.yaml` so `reconcile_config` sets `note_dirty`!
        wf_st.write(
            "bf.yaml",
            f"version: {FORMAT}\nname: wf-classify\nnotes: [concepts, projects, actions, wiki]\nloops:\n  fail-l:\n    when: 1m\n    run: [routines/nope.py]\n".encode(),
        )
        yield {
            "concepts/file.png",
            "schemas/concepts.json",
            "bf.yaml",
            ".bf/state/events.jsonl",
            "concepts/welcome.md",
        }
        dl_wf = time.monotonic() + 5.0
        while time.monotonic() < dl_wf and loops.load_unit_state(wf_st, "fail-l")["failures"] < 1:
            time.sleep(0.03)
        stop_wf.set()

    with monkeypatch.context() as mp_wf:
        mp_wf.setattr(watch, "_poll_changes", _fake_poll)
        mp_wf.setattr(watch, "next_due_delay", lambda *_a, **_kw: 0.05)
        wf_res = watch.serve(wf_st, fmt="jsonl", stop_event=stop_wf)
        assert wf_res["brain"] == "wf-classify"
    assert loops.load_unit_state(wf_st, "fail-l")["failures"] >= 1

    # 6. Hardening behavioral tests for `proc.exec_argv` and live `watch` root-dir reload + SIGTERM!
    # `exec_argv`: stdin broken pipe, detached grandchild inheriting stdout (returns without hang!), timeout=124, mid-run stop=130, thread-start failure!
    bp_rc, bp_out, _ = proc.exec_argv(
        [sys.executable, "-c", "print('early')"], cwd=root, env={}, timeout=5, stdin_data=b"z" * 200_000
    )
    assert bp_rc == 0
    assert bp_out.strip() == b"early"

    t_gc = time.monotonic()
    gc_rc, gc_out, _ = proc.exec_argv(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); print('parent-done', flush=True)",
        ],
        cwd=root,
        env={},
        timeout=5,
    )
    assert gc_rc == 0
    assert b"parent-done" in gc_out
    assert time.monotonic() - t_gc < 3.0

    to_rc, _, to_err = proc.exec_argv([sys.executable, "-c", "import time; time.sleep(5)"], cwd=root, env={}, timeout=1)
    assert to_rc == 124
    assert "timed out" in to_err

    mid_stop = threading.Event()
    threading.Timer(0.15, mid_stop.set).start()
    ms_rc, _, ms_err = proc.exec_argv(
        [sys.executable, "-c", "import time; time.sleep(5)"], cwd=root, env={}, timeout=5, stop_event=mid_stop
    )
    assert ms_rc == 130
    assert ms_err == "stopped"

    # `drain_tail` oversized-line (>64 KiB) prefix safety margin (zero secret bisection!) + thread-start failure fd cleanup!
    ws_r, ws_w = os.pipe()
    cut_marker = "EXACT_CUT_MARKER_24CHARS"
    emitted_chunks: list[str] = []

    def _capture_chunk(raw_ln: bytes) -> str:
        txt = raw_ln.decode("utf-8", "replace")
        emitted_chunks.append(txt)
        return txt

    with os.fdopen(ws_r, "rb", buffering=0) as ws_rs:
        ws_t, ws_snap = proc.drain_tail(ws_rs, 256, on_line=_capture_chunk, secrets=(cut_marker,))
        with os.fdopen(ws_w, "wb") as ws_ws:
            ws_ws.write((b"x" * 65_520) + cut_marker.encode() + ("é" * 2048).encode() + b"\nendmark\n")
        ws_t.join(timeout=3)
        joined_chunks = "\n".join(emitted_chunks)
        assert cut_marker not in joined_chunks
        assert "EXACT_CUT_" not in joined_chunks
        assert "_24CHARS" not in joined_chunks
        assert "[truncated]" in joined_chunks
        assert "endmark" in ws_snap()

    real_dup, real_close = os.dup, os.close
    for th_stdin in (b"abc", None):
        duped_th: list[int] = []
        closed_th: list[int] = []

        def _spy_dup(fd: int, _d: list[int] = duped_th) -> int:
            nfd = real_dup(fd)
            _d.append(nfd)
            return nfd

        def _spy_close(fd: int, _c: list[int] = closed_th) -> None:
            _c.append(fd)
            real_close(fd)

        with monkeypatch.context() as mp_th:
            mp_th.setattr(os, "dup", _spy_dup)
            mp_th.setattr(os, "close", _spy_close)
            mp_th.setattr(threading.Thread, "start", lambda _self: (_ for _ in ()).throw(RuntimeError("thread cap")))
            with pytest.raises(RuntimeError, match="thread cap"):
                proc.exec_argv([sys.executable, "-c", "print('x')"], cwd=root, env={}, timeout=5, stdin_data=th_stdin)
        assert duped_th
        assert set(duped_th) <= set(closed_th)

    # Live `watch.serve` stdlib polling watcher: root-directory creation (`wiki/` added dynamically) + BusyError cooldown + SIGTERM (143)!
    dyn_root = tmp_path / "dyn-watch"
    engine.init_brain(dyn_root, name="dyn-watch")
    dyn_st = Store(dyn_root)
    dyn_st.write("bf.yaml", f"version: {FORMAT}\nname: dyn-watch\nnotes: [concepts, wiki]\n".encode())
    dyn_stop = threading.Event()
    capsys.readouterr()
    dyn_t = threading.Thread(target=lambda: watch.serve(dyn_st, fmt="jsonl", stop_event=dyn_stop), daemon=True)
    dyn_t.start()
    try:
        # Wait deterministically for startup + initial build before probing live FS watchers!
        dl_start = time.monotonic() + 5.0
        started_out = ""
        while time.monotonic() < dl_start:
            started_out += capsys.readouterr().out
            if '"watch.started"' in started_out and dyn_st.exists(NOTES_FILE):
                break
            time.sleep(0.03)
        assert '"watch.started"' in started_out
        # Wait for live polling watcher on existing `concepts/` before creating `wiki/`, confirming watcher is active!
        dl_c = time.monotonic() + 5.0
        dyn_st.write("concepts/ready.md", b"---\ntype: guide\nstatus: stable\n---\n# Ready\n")
        while time.monotonic() < dl_c and b"concepts/ready.md" not in dyn_st.read(NOTES_FILE):
            time.sleep(0.05)
        assert b"concepts/ready.md" in dyn_st.read(NOTES_FILE)
        # Step 1: Create new `wiki/` top-level dir + `wiki/fresh.md` (detected cleanly by `_poll_changes`).
        dyn_st.write("wiki/fresh.md", b"# Fresh Wiki\n")
        dl_w = time.monotonic() + 5.0
        while time.monotonic() < dl_w and b"wiki/fresh.md" not in dyn_st.read(NOTES_FILE):
            time.sleep(0.05)
        assert b"wiki/fresh.md" in dyn_st.read(NOTES_FILE)
        # Step 2: Create a SECOND file inside the already-existing `wiki/` dir and delete it -> `_poll_changes` detects both!
        dyn_st.write("wiki/second.md", b"# Second Wiki\n")
        dl_w2 = time.monotonic() + 5.0
        while time.monotonic() < dl_w2 and b"wiki/second.md" not in dyn_st.read(NOTES_FILE):
            time.sleep(0.05)
        assert b"wiki/second.md" in dyn_st.read(NOTES_FILE)
        dyn_st.delete("wiki/second.md")
        dl_w3 = time.monotonic() + 5.0
        while time.monotonic() < dl_w3 and b"wiki/second.md" in dyn_st.read(NOTES_FILE):
            time.sleep(0.05)
        assert b"wiki/second.md" not in dyn_st.read(NOTES_FILE)
    finally:
        dyn_stop.set()
        dyn_t.join(timeout=5.0)
        assert not dyn_t.is_alive()

    # `watch.serve` applies `BusyError` cooldown without spinning when a loop's lock is held externally!
    busy_root = tmp_path / "busy-watch"
    engine.init_brain(busy_root, name="busy-watch")
    busy_st = Store(busy_root)
    busy_st.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: busy-watch\nloops:\n  b1:\n    when: 1m\n    run: [{sys.executable!r}, -c, 'print(1)']\n".encode(),
    )
    busy_stop = threading.Event()
    exec_attempts = [0]
    real_exec_one = loops.execute_one_loop

    def _spy_exec_one(*a: Any, **kw: Any) -> dict[str, Any]:
        exec_attempts[0] += 1
        return real_exec_one(*a, **kw)

    with monkeypatch.context() as mp_b, loop_lock(busy_st, "b1", wait=0.0):
        mp_b.setattr(watch, "_BUSY_COOLDOWN", 0.3)
        mp_b.setattr(loops, "execute_one_loop", _spy_exec_one)
        bt = threading.Thread(target=lambda: watch.serve(busy_st, fmt="jsonl", stop_event=busy_stop), daemon=True)
        bt.start()
        try:
            dl_b1 = time.monotonic() + 4.0
            while time.monotonic() < dl_b1 and exec_attempts[0] < 2:
                time.sleep(0.03)
            assert exec_attempts[0] >= 2
        finally:
            busy_stop.set()
            bt.join(timeout=5.0)
            assert not bt.is_alive()

    # Deterministic post-handler SIGTERM via `engine.build` spy (never races handler registration!) -> `exit_code == 143`!
    real_build = engine.build
    sent_term = [False]

    def _term_after_build(st: Store, **kw: Any) -> dict[str, Any]:
        res_b = real_build(st, **kw)
        if not sent_term[0]:
            sent_term[0] = True
            os.kill(os.getpid(), signal.SIGTERM)
        return res_b

    with monkeypatch.context() as mp_term:
        mp_term.setattr(engine, "build", _term_after_build)
        term_res = watch.serve(dyn_st, fmt="jsonl")
    assert sent_term[0] is True
    assert term_res["exit_code"] == 143


def test_review_round1_regressions_incomplete_prune_pem_proxy_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock P0/P1/P2 fixes from fresh-session review: incomplete backoff, active-lock prune, oversized PEM, proxies, render."""
    import io

    root = tmp_path / "r1-reg"
    engine.init_brain(root, name="r1-reg")
    st = Store(root)
    st.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: r1-reg\nloops:\n  inc:\n    when: 1h\n    ingest: upsert\n    run: [{sys.executable!r}, -c, 'print(\"{{}}\")']\n".encode(),
    )
    # 1. P0 `incomplete-watch-hotloop`: `incomplete=True` increments `incomplete_runs` and backs off without spinning!
    t0 = datetime(2026, 11, 15, 10, 0, 0, tzinfo=UTC)
    loops.record_run(
        st,
        "inc",
        "r1",
        "2026-11-15T10:00:00Z",
        ended=t0,
        dur_ms=10,
        code=0,
        summary={"records": 1, "incomplete": True},
        trigger="schedule",
        schedule_keys=("every:3600",),
    )
    doc_inc = loops.load_unit_state(st, "inc")
    assert doc_inc["status"] == "ok"
    assert doc_inc["consecutive_failures"] == 0
    assert doc_inc["incomplete_runs"] == 1
    cfg = config.load(st)
    assert cron.due_schedules(cfg.loops["inc"], doc_inc, datetime(2026, 11, 15, 10, 0, 10, tzinfo=UTC)) == ()
    # Clean run resets `incomplete_runs` to 0!
    loops.record_run(
        st,
        "inc",
        "r2",
        "2026-11-15T10:05:00Z",
        ended=datetime(2026, 11, 15, 10, 5, 1, tzinfo=UTC),
        dur_ms=10,
        code=0,
        summary={"records": 1, "incomplete": False},
        trigger="schedule",
        schedule_keys=("every:3600",),
    )
    assert loops.load_unit_state(st, "inc")["incomplete_runs"] == 0

    # 2. P1 `prune-state-active-orphan`: actively locked orphaned loop preserves state, dir, lock, and log in both dry-run and real prune!
    (root / ".bf" / "state").mkdir(parents=True, mode=0o700, exist_ok=True)
    (root / ".bf" / "state" / "active-orph.json").write_text('{"name":"active-orph","status":"running"}\n')
    (root / ".bf" / "state" / "active-orph.json").chmod(0o600)
    (root / ".bf" / "state" / "active-orph").mkdir(mode=0o700)
    (root / ".bf" / "state" / "active-orph" / "work.txt").write_text("live")
    (root / "logs").mkdir(mode=0o700, exist_ok=True)
    (root / "logs" / "active-orph.jsonl").write_text("{}\n")
    (root / "logs" / "active-orph.jsonl").chmod(0o600)
    with loop_lock(st, "active-orph", wait=0.0):
        dry_p = loops.prune_state(st, dry_run=True)
        assert dry_p["pruned"] == {"states": [], "dirs": [], "locks": [], "logs": []}
        real_p = loops.prune_state(st, dry_run=False)
        assert real_p["pruned"] == {"states": [], "dirs": [], "locks": [], "logs": []}
        assert (root / ".bf" / "state" / "active-orph.json").is_file()
        assert (root / ".bf" / "state" / "active-orph" / "work.txt").is_file()
        assert (root / "logs" / "active-orph.jsonl").is_file()
    # Once released, prune removes all four artifacts cleanly!
    post_p = loops.prune_state(st, dry_run=False)
    assert post_p["pruned"] == {
        "states": ["active-orph"],
        "dirs": ["active-orph"],
        "locks": ["loop-active-orph.lock"],
        "logs": ["active-orph"],
    }

    # 3. P1 `proc-drain-pem-oversized`: PEM markers in oversized prefix OR past byte 65536 suppress subsequent PEM body lines!
    pem_beg = b"-----" + b"BEGIN PRIVATE KEY" + b"-----"
    pem_end = b"-----" + b"END PRIVATE KEY" + b"-----"
    pem_body_marker = "MIIE_SENSITIVE_BASE64_KEY_MATERIAL_987654321"
    for payload_bytes in (
        b"x" * 100 + pem_beg + b"a" * 70000 + b"\n" + pem_body_marker.encode() + b"\n" + pem_end + b"\nafter_ok\n",
        b"y" * 70000 + pem_beg + b"\n" + pem_body_marker.encode() + b"\n" + pem_end + b"\nafter_ok\n",
        pem_beg + b"\n" + b"z" * 70000 + pem_end + b"\nafter_ok\n",
        pem_beg + b"k" * 100 + pem_end + b"q" * 70000 + b"\nafter_ok\n",
        pem_beg + b"k" + pem_end + pem_beg + b"\n" + pem_body_marker.encode() + b"\n" + pem_end + b"\nafter_ok\n",
    ):
        logger = state.make_stderr_logger(st, "pemtest", "r-pem")
        t_dr, snap_dr = proc.drain_tail(io.BytesIO(payload_bytes), 4096, on_line=logger)
        t_dr.join(timeout=5)
        out_txt = snap_dr()
        assert pem_body_marker not in out_txt
        assert "after_ok" in out_txt
    log_txt = (root / "logs" / "pemtest.jsonl").read_text()
    assert pem_body_marker not in log_txt
    # Original-coordinate truncation: short input (`cut<=0`), plain text, exact secrets, overlapping spans, & bisected tail!
    assert models.redact_truncated_prefix("short", 100) == ""
    assert models.redact_truncated_prefix("clean text " + ("z" * 40), 10) == "clean text " + ("z" * 30)
    sec_lit = "custom_secret_literal_99"
    assert "[REDACTED]" in models.redact_truncated_prefix(
        f"a {sec_lit} token={sec_lit} " + ("z" * 40), 10, secrets=(sec_lit,)
    )
    many_sec = ("token=abcdefghij " * 1000) + ("x" * 50000) + "http://alice" + ":bisected_tail_secret_99"
    red_pref = models.redact_truncated_prefix(many_sec, 8192)
    assert "bisected_tail_secret_99" not in red_pref
    assert "[REDACTED]" in red_pref
    # Multi-chunk `dropping=True` stream (>80 KiB single line without newline spanning multiple 8 KiB reads + PEM marker):
    chunked_pem = (
        b"w" * 74000 + pem_beg + b"w" * 10000 + b"\n" + pem_body_marker.encode() + b"\n" + pem_end + b"\nok_end\n"
    )
    logger2 = state.make_stderr_logger(st, "pemtest2", "r-pem2")
    t_dr2, snap_dr2 = proc.drain_tail(io.BytesIO(chunked_pem), 4096, on_line=logger2)
    t_dr2.join(timeout=5)
    assert pem_body_marker not in snap_dr2()
    assert "ok_end" in snap_dr2()

    # 4. P1 `proxy-cred-redaction-bypass` + `env-blocklist-openssl-bypass` (`is_self_bf`)!
    p_tok = "p%40ssword99"
    with monkeypatch.context() as mp_px:
        mp_px.setenv("HTTP_PROXY", "alice:" + "schemeless_secret_99" + "@proxy.example.test:8080")
        mp_px.setenv("HTTPS_PROXY", "http://bob:" + p_tok + "@proxy.example.test:8080")
        mp_px.setenv("OPENSSL_CONF", str(tmp_path / "evil.cnf"))
        mp_px.setenv("SSLKEYLOGFILE", str(tmp_path / "keys.log"))
        _, _, _, px_secrets = proc.prepare_unit(st, "inc", ("echo",), "r1", "2026-11-15T10:00:00Z")
        assert "schemeless_secret_99" in px_secrets
        assert "p@ssword99" in px_secrets
        _, self_env, _, _ = proc.prepare_unit(st, "inc", ("bf", "build"), "r1", "2026-11-15T10:00:00Z")
        assert "OPENSSL_CONF" not in self_env
        assert "SSLKEYLOGFILE" not in self_env
    assert "[REDACTED]" in models.redact_secrets("https://" + ":emptypass123" + "@example.test/api")

    # 5. P2 `render.py`: attention failure shown for `running`/`stopped` loops, `degraded` beats `overdue`, and `watch.loop` `stopped` is NOT red `fail`!
    st_text = render.format_status_text(
        {
            "ok": False,
            "brain": "r1-reg",
            "counts": {"notes": 1, "memories": 0, "loops": 2},
            "events_seq": 0,
            "attention": [{"loop": "retrying", "reason": "failed"}, {"loop": "both", "reason": "degraded"}],
            "loops": {
                "retrying": {
                    "enabled": True,
                    "status": "running",
                    "consecutive_failures": 3,
                    "error": "upstream 503",
                    "trigger": "daemon",
                },
                "both": {
                    "enabled": True,
                    "status": "ok",
                    "degraded": True,
                    "overdue": True,
                    "trigger": "1h",
                },
            },
        },
        color=True,
    )
    assert "(3x) upstream 503" in st_text
    assert "degraded" in st_text
    stop_line = render.format_watch_line(
        "watch.loop",
        {"loop": "inc", "status": "stopped", "duration_ms": 42, "time": "2026-11-15T10:00:00Z"},
        color=False,
    )
    assert "stop   " in stop_line
    assert "fail" not in stop_line
    warn_b_line = render.format_watch_line(
        "watch.build",
        {
            "ok": True,
            "notes": 2,
            "written": 1,
            "removed": 0,
            "index_warning": "cache offline",
            "time": "2026-11-15T10:00:00Z",
        },
        color=False,
    )
    assert "index warning: cache offline" in warn_b_line


def test_always_on_pools_debounce_backoff_split_timers_pause_and_lag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "always-on"
    engine.init_brain(root, name="always-on")
    store = Store(root)
    (root / "routines").mkdir(exist_ok=True)

    # 1. Named concurrency pools isolate slow `agents` pool from `interactive` pool in `run_loops --due`!
    (root / "routines" / "pool_probe.py").write_text(
        "import json, os, pathlib, sys, time\n"
        "tag = sys.argv[1]\n"
        "p = pathlib.Path('routines')\n"
        "(p / f'start.{tag}').write_text(str(time.monotonic()))\n"
        "if tag == 'slow-ag':\n"
        "    dl = time.monotonic() + 4.0\n"
        "    while time.monotonic() < dl and not (p / 'done.fast-dm').exists():\n"
        "        time.sleep(0.02)\n"
        "    assert (p / 'done.fast-dm').exists(), 'interactive pool was starved by agents pool'\n"
        "elif tag == 'fast-dm':\n"
        "    (p / 'done.fast-dm').write_text('1')\n"
        "print(json.dumps({'tag': tag, 'missed': os.environ.get('BF_EVENTS_MISSED', '')}))\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: always-on\n"
        "concurrency:\n  default: 2\n  agents: 1\n  interactive: 1\n"
        "loops:\n"
        f"  slow-ag:\n    pool: agents\n    when: 1h\n    run: [{sys.executable!r}, routines/pool_probe.py, slow-ag]\n"
        f"  slow-ag2:\n    pool: agents\n    when: 1h\n    run: [{sys.executable!r}, routines/pool_probe.py, slow-ag2]\n"
        f"  fast-dm:\n    pool: interactive\n    when:\n      - event: loop.message\n    run: [{sys.executable!r}, routines/pool_probe.py, fast-dm]\n".encode(),
    )
    loops.reset_loops(store, ("fast-dm",))
    t0 = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    state.append_event(store, "loop.message", "chat", {"id": "dm-1", "text": "urgent"}, now=t0)
    pool_res = loops.run_loops(store, due_only=True, now=t0)
    assert pool_res["ok"]
    assert pool_res["results"][0]["loop"] == "fast-dm"  # event prioritized ahead of schedules!

    # 2. Schedule failure backoff does NOT block incoming event triggers on the same loop!
    (root / "routines" / "mix.py").write_text(
        "import json, os, sys\n"
        "trig = os.environ['BF_TRIGGER']\n"
        "if trig == 'schedule' or '--fail' in sys.argv:\n"
        "    sys.stderr.write('transient schedule failure\\n')\n"
        "    sys.exit(1)\n"
        "evs = [json.loads(ln) for ln in sys.stdin.read().splitlines() if ln.strip()]\n"
        "print(json.dumps({'trig': trig, 'count': len(evs), 'missed': os.environ.get('BF_EVENTS_MISSED', '')}))\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: always-on\n"
        "loops:\n"
        "  mixed:\n"
        "    when:\n"
        "      - 1h\n"
        "      - event:\n          kind: loop.message\n          source: chat\n"
        "      - event:\n          kind: records.ingested\n        debounce: 20s\n"
        f"    run: [{sys.executable!r}, routines/mix.py]\n".encode(),
    )
    loops.reset_loops(store, ("mixed",))
    fail_sched = loops.run_loops(store, due_only=True, now=t0)
    assert not fail_sched["ok"]
    st_mix = loops.load_unit_state(store, "mixed")
    assert st_mix["schedule_failures"] == 1
    assert (
        cron.backoff_remaining(st_mix, cron.backoff_cap(config.load(store).loops["mixed"]), t0 + timedelta(seconds=10))
        > 0
    )
    assert cron.event_backoff_remaining(st_mix, t0 + timedelta(seconds=10)) == 0.0
    # Urgent DM fires immediately 10s later despite active 60s schedule backoff, while preserving schedule_failures AND last_schedule_end!
    state.append_event(store, "loop.message", "chat", {"id": "dm-2"}, now=t0 + timedelta(seconds=10))
    dm_res = loops.run_loops(store, due_only=True, now=t0 + timedelta(seconds=10))
    assert dm_res["ok"]
    assert dm_res["results"][0]["summary"]["trig"] == "event"
    st_after_dm = loops.load_unit_state(store, "mixed")
    assert st_after_dm["schedule_failures"] == 1
    assert st_after_dm["last_schedule_end"] == models.now_utc(t0)
    # Schedule backoff is STILL measured from `last_schedule_end` (t0), NOT reset by the event at t0+10s!
    assert (
        cron.backoff_remaining(
            st_after_dm, cron.backoff_cap(config.load(store).loops["mixed"]), t0 + timedelta(seconds=61)
        )
        == 0.0
    )
    # Per-rule debounce isolation: a slow debounced rule (30m) must NOT stretch a fast debounced rule (5s)!
    fast_rule = config.EventMatch(kind="loop.fast", debounce=5)
    slow_rule = config.EventMatch(kind="loop.slow", debounce=1800)
    mixed_evs = [
        {"seq": 1, "kind": "loop.fast", "source": "s", "time": models.now_utc(t0), "data": {}},
        {"seq": 2, "kind": "loop.slow", "source": "s", "time": models.now_utc(t0 + timedelta(seconds=1)), "data": {}},
    ]
    assert loops.event_debounce_remaining(mixed_evs, (fast_rule, slow_rule), t0 + timedelta(seconds=6)) == 0.0
    # `watch.error` text rendering includes `loop` name when present!
    assert "noisy: rate limit" in render.format_watch_line("watch.error", {"loop": "noisy", "error": "rate limit"})
    # Mark the hourly schedule satisfied at t0 so step 3 isolates event debouncing from hourly retries.
    loops.save_unit_state(
        store,
        {
            **st_after_dm,
            "schedule_failures": 0,
            "last_schedule_end": "",
            "schedule_last": {"every:3600": models.now_utc(t0)},
        },
    )

    # 3. Trailing-edge `debounce:` coalesces bursts, caps wait at 4x debounce, and lets non-debounced rules fire immediately!
    t_deb = t0 + timedelta(minutes=5)
    state.append_event(store, "records.ingested", "src-a", {"added": 1}, now=t_deb)
    cfg_m = config.load(store)
    due_early, _ = loops.select_due_loops(store, cfg_m, sched_now=t_deb + timedelta(seconds=5))
    assert due_early == []
    assert 10.0 <= watch.next_due_delay(store, cfg_m, t_deb + timedelta(seconds=5)) <= 16.0
    # Second burst event extends trailing debounce!
    state.append_event(store, "records.ingested", "src-b", {"added": 1}, now=t_deb + timedelta(seconds=15))
    due_ext, _ = loops.select_due_loops(store, cfg_m, sched_now=t_deb + timedelta(seconds=25))
    assert due_ext == []
    # Ready once trailing window elapses: BOTH events delivered together in ONE run!
    deb_run = loops.run_loops(store, due_only=True, now=t_deb + timedelta(seconds=36))
    assert deb_run["ok"]
    assert deb_run["results"][0]["summary"]["count"] == 2
    # Max-wait cap (`4 * 20s = 80s`): continuous trickle still flushes!
    t_cap = t_deb + timedelta(minutes=2)
    evs_trickle = [
        {
            "seq": i + 1,
            "kind": "records.ingested",
            "source": "s",
            "time": models.now_utc(t_cap + timedelta(seconds=i * 15)),
            "data": {},
        }
        for i in range(6)
    ]
    assert (
        loops.event_debounce_remaining(evs_trickle, cfg_m.loops["mixed"].events, t_cap + timedelta(seconds=81)) == 0.0
    )
    # Non-debounced `loop.message` alongside a debounced event bypasses debounce immediately!
    state.append_event(store, "records.ingested", "src-c", {"added": 1}, now=t_cap)
    state.append_event(store, "loop.message", "chat", {"id": "urgent-3"}, now=t_cap + timedelta(seconds=1))
    urg_run = loops.run_loops(store, due_only=True, now=t_cap + timedelta(seconds=2))
    assert urg_run["ok"]
    assert urg_run["results"][0]["summary"]["count"] == 2

    # 4. Delayed `schedule_event` promotes via `select_due_loops` and wakes `next_due_delay`!
    t_wk = t_cap + timedelta(minutes=5)
    state.schedule_event(
        store, "loop.message", "chat", {"id": "wake-1"}, deliver_at=t_wk + timedelta(seconds=30), now=t_wk
    )
    assert 25.0 <= watch.next_due_delay(store, cfg_m, t_wk) <= 31.0
    wk_run = loops.run_loops(store, due_only=True, now=t_wk + timedelta(seconds=31))
    assert wk_run["ok"]
    assert wk_run["ran"] == 1

    # 5. `events_missed` surfaces in `status_summary`, `render.format_status_text`, and `BF_EVENTS_MISSED`!
    st_dir = private_dir(store, ".bf", "state")
    st_dir.write(
        state.EVENTS_FILE,
        (
            json.dumps(
                {
                    "seq": 500,
                    "id": "e500",
                    "time": models.now_utc(t_wk),
                    "kind": "loop.message",
                    "source": "chat",
                    "data": {"id": "gap-1"},
                }
            )
            + "\n"
        ).encode(),
        mode=0o600,
    )
    loops.save_unit_state(store, {**loops.load_unit_state(store, "mixed"), "event_seq": 100})
    st_lag = status.status_summary(store, include_loops=True, now=t_wk)
    assert st_lag["ok"] is False
    assert any(a["reason"] == "events_missed" and a["events_missed"] == 399 for a in st_lag["attention"])
    assert "lagging" in render.format_status_text(st_lag, now=t_wk)
    lag_run = loops.run_loops(store, due_only=True, now=t_wk + timedelta(minutes=1))
    assert lag_run["ok"]
    assert lag_run["results"][0]["summary"]["missed"] == "399"
    # Unmatched post-compaction fast-forward preserves `pending_events_missed` & logs `loop.events_missed`, and failed/crashed loops still surface `events_missed`!
    st_dir.write(
        state.EVENTS_FILE,
        (
            json.dumps(
                {
                    "seq": 900,
                    "id": "e900",
                    "time": models.now_utc(t_wk),
                    "kind": "other.unrelated",
                    "source": "other",
                    "data": {},
                }
            )
            + "\n"
        ).encode(),
        mode=0o600,
    )
    loops.save_unit_state(
        store, {**loops.load_unit_state(store, "mixed"), "event_seq": 600, "pending_events_missed": 0}
    )
    due_none, _ = loops.select_due_loops(store, cfg_m, sched_now=t_wk + timedelta(minutes=2))
    assert due_none == []
    st_ff = loops.load_unit_state(store, "mixed")
    assert st_ff["event_seq"] == 900
    assert st_ff["pending_events_missed"] == 299
    # Failed loop with `pending_events_missed > 0` surfaces BOTH failure AND lag in `status_summary` + text!
    loops.save_unit_state(store, {**st_ff, "status": "failed", "consecutive_failures": 1, "error": "boom"})
    st_fl = status.status_summary(store, include_loops=True, now=t_wk + timedelta(minutes=2))
    assert st_fl["attention"][0]["events_missed"] == 299
    assert "missed 299 compacted" in render.format_status_text(st_fl, now=t_wk + timedelta(minutes=2))
    # Crashed loop with `pending_events_missed > 0` surfaces BOTH crash AND lag!
    loops.save_unit_state(store, {**st_ff, "status": "running", "consecutive_failures": 0})
    st_cr = status.status_summary(store, include_loops=True, now=t_wk + timedelta(minutes=2))
    assert st_cr["attention"][0]["reason"] == "crashed"
    assert st_cr["attention"][0]["events_missed"] == 299
    # Manual `bf run` forwards `BF_EVENTS_MISSED`, preserves lag on failure, and clears `pending_events_missed` on success!
    loops.save_unit_state(store, {**st_ff, "status": "ok", "consecutive_failures": 0, "error": ""})
    man_fail = loops.run_loops(store, ["mixed"], extra_args=["--fail"], now=t_wk + timedelta(minutes=3))
    assert man_fail["ok"] is False
    assert man_fail["results"][0]["events_missed"] == 299
    assert loops.load_unit_state(store, "mixed")["pending_events_missed"] == 299
    man_ok = loops.run_loops(store, ["mixed"], now=t_wk + timedelta(minutes=4))
    assert man_ok["ok"] is True
    assert man_ok["results"][0]["events_missed"] == 299
    assert man_ok["results"][0]["summary"]["missed"] == "299"
    assert loops.load_unit_state(store, "mixed")["pending_events_missed"] == 0
    assert not any(
        a["name"] == "mixed" for a in status.status_summary(store, now=t_wk + timedelta(minutes=4))["attention"]
    )

    # 6. `bf pause` / `bf resume` for cycle loops & live daemon supervision under `bf watch`!
    (root / "routines" / "d_flag.py").write_text(
        "import pathlib, signal, time\n"
        "p = pathlib.Path('routines/d_alive')\n"
        "p.write_text('1')\n"
        "stop = False\n"
        "signal.signal(signal.SIGTERM, lambda *_: globals().update(stop=True))\n"
        "while not stop:\n"
        "    time.sleep(0.05)\n"
        "p.unlink(missing_ok=True)\n"
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: always-on\n"
        "concurrency:\n  default: 2\n  agents: 1\n"
        "loops:\n"
        f"  d-bot:\n    when: daemon\n    run: [{sys.executable!r}, routines/d_flag.py]\n"
        f"  c-job:\n    pool: agents\n    when: 1h\n    run: [{sys.executable!r}, -c, 'print(\"ok\")']\n".encode(),
    )
    t_p = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    assert loops.pause_loops(store, ("c-job",), duration_s=120, now=t_p)["paused"] == ["c-job"]
    assert loops.run_loops(store, due_only=True, now=t_p + timedelta(seconds=30))["ran"] == 0
    # Manual run still works while paused and preserves active pause!
    assert loops.run_loops(store, ("c-job",), now=t_p + timedelta(seconds=30))["ok"] is True
    assert state.is_paused(loops.load_unit_state(store, "c-job"), t_p + timedelta(seconds=31)) is True
    st_p = status.status_summary(store, include_loops=True, now=t_p + timedelta(seconds=31))
    assert st_p["ok"] is True
    assert st_p["paused"] == ["c-job"]
    assert st_p["loops"]["c-job"]["status"] == "paused"
    assert "paused (" in render.format_status_text(st_p, now=t_p + timedelta(seconds=31))
    assert 85.0 <= watch.next_due_delay(store, config.load(store), t_p + timedelta(seconds=31), max_wait=120.0) <= 90.0
    # Auto-resumes once `--for` duration passes!
    assert loops.run_loops(store, due_only=True, now=t_p + timedelta(seconds=125))["ran"] == 1
    assert loops.resume_loops(store, ("c-job",), now=t_p + timedelta(seconds=130))["resumed"] == ["c-job"]

    # Live `bf watch` stops a daemon on `pause_loops` and restarts it on `resume_loops`!
    monkeypatch.setattr(watch, "_POLL_INTERVAL", 0.05)
    stop_w = threading.Event()
    capsys.readouterr()
    wt = threading.Thread(target=lambda: watch.serve(store, fmt="jsonl", stop_event=stop_w), daemon=True)
    wt.start()
    try:
        dl = time.monotonic() + 6.0
        while time.monotonic() < dl and not (root / "routines" / "d_alive").exists():
            time.sleep(0.03)
        assert (root / "routines" / "d_alive").exists()
        loops.pause_loops(store, ("d-bot",))
        dl2 = time.monotonic() + 6.0
        while time.monotonic() < dl2 and (root / "routines" / "d_alive").exists():
            time.sleep(0.03)
        assert not (root / "routines" / "d_alive").exists()
        loops.resume_loops(store, ("d-bot",))
        dl3 = time.monotonic() + 6.0
        while time.monotonic() < dl3 and not (root / "routines" / "d_alive").exists():
            time.sleep(0.03)
        assert (root / "routines" / "d_alive").exists()
    finally:
        stop_w.set()
        wt.join(timeout=6.0)
        assert not wt.is_alive()
    watch_evs = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(e.get("message") == "watch.started" and e.get("pools") == {"default": 2, "agents": 1} for e in watch_evs)


def test_review_regressions_event_race_status_toctou_and_clock_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regressions for BF-001 (select_due_loops cursor race), BF-004/005 (status TOCTOU/problems), BF-008 (clock), SIGTERM 143."""
    root = tmp_path / "reg-brain"
    engine.init_brain(root, name="reg")
    store = Store(root)
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: reg\nloops:\n"
        f"  a-loop:\n    when:\n      - event: loop.ping\n    run: [{sys.executable!r}, -c, 'print(\"a\")']\n"
        f"  b-loop:\n    when:\n      - event: loop.other\n    run: [{sys.executable!r}, -c, 'print(\"b\")']\n".encode(),
    )
    cfg = config.load(store)
    for i in range(1, 10):
        state.append_event(store, f"loop.n{i}", "src", {"n": i})
    base_seq = state.latest_event_seq(store)
    loops.save_unit_state(store, state.default_unit_state("a-loop", event_seq=base_seq))
    # Simulate concurrent arrival of ping (for a-loop), other1, other2 AFTER initial `event_bounds()` saw `base_seq`!
    ev_ping = state.append_event(store, "loop.ping", "src", {"n": 11})[0]["seq"]
    ev_other1 = state.append_event(store, "loop.other", "src", {"n": 12})[0]["seq"]
    state.append_event(store, "loop.other", "src", {"n": 13})
    loops.save_unit_state(store, state.default_unit_state("b-loop", event_seq=ev_other1))
    bounds_calls = [0]
    real_bounds = state.event_bounds

    def racing_bounds(st: Store) -> tuple[int, int]:
        bounds_calls[0] += 1
        return (1, base_seq) if bounds_calls[0] == 1 else real_bounds(st)

    monkeypatch.setattr(state, "event_bounds", racing_bounds)
    due, _ = loops.select_due_loops(store, cfg, sched_now=datetime(2026, 11, 25, 12, 0, tzinfo=UTC), dry_run=True)
    monkeypatch.undo()
    by_name = {item[0].name: item for item in due}
    assert "a-loop" in by_name
    assert [ev["seq"] for ev in by_name["a-loop"][2]] == [ev_ping]

    # BF-004: `status_summary` re-reads state after unlocked `_is_crashed_running` so a healthy finish never false-alerts!
    loops.save_unit_state(
        store,
        {
            **state.default_unit_state("a-loop", event_seq=13),
            "status": "running",
            "last_start": "2026-11-25T10:00:00Z",
        },
    )

    def finish_during_check(st: Store, name: str) -> bool:
        loops.save_unit_state(
            st,
            {
                **state.default_unit_state(name, event_seq=13),
                "status": "ok",
                "last_start": "2026-11-25T10:00:00Z",
            },
        )
        return True

    with monkeypatch.context() as mp:
        mp.setattr(status, "_is_crashed_running", finish_during_check)
        st_ok = status.status_summary(store, include_loops=True)
    assert st_ok["ok"] is True
    assert st_ok["loops"]["a-loop"]["status"] == "ok"

    # Corrupt state on re-read inside crash check degrades to failed cleanly.
    loops.save_unit_state(
        store,
        {
            **state.default_unit_state("a-loop", event_seq=13),
            "status": "running",
            "last_start": "2026-11-25T10:00:00Z",
        },
    )

    def corrupt_during_check(st: Store, name: str) -> bool:
        private_dir(st, ".bf", "state").write(f"{name}.json", b"{corrupt")
        return True

    with monkeypatch.context() as mp:
        mp.setattr(status, "_is_crashed_running", corrupt_during_check)
        st_cor = status.status_summary(store, include_loops=True)
    assert st_cor["ok"] is False
    assert st_cor["loops"]["a-loop"]["status"] == "failed"
    loops.save_unit_state(store, state.default_unit_state("a-loop", event_seq=13))

    # BF-005: `status_summary` records note/memory symlinks, bundle collisions, unreadable memory files, and deleted CWD in `problems` (no crash).
    (root / "concepts" / "sym.md").symlink_to(root / "bf.yaml")
    store.write("concepts/dup.md", b"---\ntype: guide\nstatus: stable\n---\n# D\n")
    store.write("concepts/dup/index.md", b"---\ntype: guide\nstatus: stable\n---\n# D2\n")
    (root / "memories" / "badmem.jsonl").symlink_to(root / "bf.yaml")
    records.ingest(store, "unreadable", b'[{"id":"1","title":"U"}]')
    store.delete(f".bf/cache/{index.INDEX_NAME}")
    unr_p = root / "memories" / "unreadable.jsonl"
    unr_p.chmod(0o000)
    try:
        with monkeypatch.context() as mp:
            mp.setattr(config, "inside_brain", lambda: (_ for _ in ()).throw(FileNotFoundError("cwd gone")))
            st_prob = status.status_summary(store)
        assert st_prob["ok"] is False
        assert st_prob["inside_brain"] is False
        assert any("sym.md" in p for p in st_prob["problems"])
        assert any("collides" in p for p in st_prob["problems"])
        assert any("badmem.jsonl" in p for p in st_prob["problems"])
        assert any("unreadable.jsonl" in p for p in st_prob["problems"])
        with pytest.raises((Error, OSError)):
            status.corpus_counts(store, config.load(store))
    finally:
        unr_p.chmod(0o644)
        (root / "concepts" / "sym.md").unlink()
        (root / "memories" / "badmem.jsonl").unlink()

    # BF-R2-002: Symlinked `memories/` and unreadable `_notes.jsonl` surface cleanly in `status_summary` `problems`!
    sym_mem_root = tmp_path / "sym-mem-brain"
    engine.init_brain(sym_mem_root, name="symmem")
    notes_p = sym_mem_root / "memories" / "_notes.jsonl"
    notes_p.chmod(0o000)
    try:
        st_unr = status.status_summary(Store(sym_mem_root), check_inside=False)
        assert st_unr["ok"] is True or st_unr["inside_brain"] is False
    finally:
        notes_p.chmod(0o644)
    shutil.rmtree(sym_mem_root / "memories")
    (sym_mem_root / "memories").symlink_to(tmp_path)
    st_sym_mem = status.status_summary(Store(sym_mem_root))
    assert st_sym_mem["ok"] is False
    assert any("memories" in p and "symlinks" in p for p in st_sym_mem["problems"])
    with pytest.raises(Error, match="symlinks"):
        status.corpus_counts(Store(sym_mem_root), config.load(Store(sym_mem_root)))

    # BF-008: Backward wall-clock step never extends backoff or debounce beyond cap!
    t_last = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    t_past = t_last - timedelta(hours=2)
    doc_b = {
        "name": "a-loop",
        "consecutive_failures": 1,
        "event_failures": 1,
        "last_end": "2026-11-25T12:00:00Z",
    }
    assert cron.backoff_remaining(doc_b, 3600, t_past) <= 60.0
    assert cron.event_backoff_remaining(doc_b, t_past) <= 10.0
    ev_rule = config.EventMatch(kind="loop.ping", debounce=15)
    assert loops.event_debounce_remaining([], (ev_rule,), t_past) == 0.0
    assert (
        loops.event_debounce_remaining([{"kind": "loop.other", "time": "2026-11-25T12:00:00Z"}], (ev_rule,), t_past)
        == 0.0
    )
    assert (
        loops.event_debounce_remaining([{"kind": "loop.ping", "time": "2026-11-25T12:00:00Z"}], (ev_rule,), t_past)
        <= 15.0
    )

    # `watch-loop-sigterm-exit-code`: SIGTERM during cycle loop records exit_code 143 (not 130).
    slow_loop = config.LoopSpec(
        "slow-term",
        run=(sys.executable, "-c", "import time; time.sleep(30)"),
    )
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: reg\nloops:\n  slow-term:\n    run: [{sys.executable!r}, -c, 'import time; time.sleep(30)']\n".encode(),
    )
    stop_e, term_e = threading.Event(), threading.Event()

    def trigger_term() -> None:
        time.sleep(0.25)
        term_e.set()
        stop_e.set()

    tt = threading.Thread(target=trigger_term, daemon=True)
    tt.start()
    res_t = loops.execute_one_loop(store, slow_loop, trigger="manual", stop_event=stop_e, term_event=term_e)
    tt.join(timeout=2.0)
    assert res_t["exit_code"] == 143

    # `lock-probe-contention-race`: transient `LOCK_SH` probe does not fail `execute_one_loop` with spurious `BusyError`.
    import fcntl

    quick_loop = config.LoopSpec("quick-ok", run=(sys.executable, "-c", "pass"))
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: reg\nloops:\n  quick-ok:\n    run: [{sys.executable!r}, -c, 'pass']\n".encode(),
    )
    locks_p = private_dir(store, ".bf", "locks")
    locks_p.write("loop-quick-ok.lock", b"", mode=0o600)
    probe_fd = os.open(store.root / ".bf" / "locks" / "loop-quick-ok.lock", os.O_RDONLY)
    fcntl.flock(probe_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)

    def _drop_probe() -> None:
        time.sleep(0.015)
        os.close(probe_fd)

    pt = threading.Thread(target=_drop_probe, daemon=True)
    pt.start()
    try:
        ok_res = loops.execute_one_loop(store, quick_loop, trigger="manual")
    finally:
        pt.join(timeout=1.0)
    assert ok_res["status"] == "ok"

    # `select_due_with_snapshot` + `next_due_delay(..., snapshot=...)` reuses loaded state/events without re-reading disk.
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: reg\nloops:\n  quick-ok:\n    when: 1h\n    run: [{sys.executable!r}, -c, 'pass']\n".encode(),
    )
    t_snap = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)
    state.mark_unit_state(store, "quick-ok", paused=True, paused_until="2026-11-25T13:00:45Z")
    cfg_snap = config.load(store)
    _, _, snap = loops.select_due_with_snapshot(store, cfg_snap, sched_now=t_snap)
    assert "quick-ok" in snap.states
    expected_delay = watch.next_due_delay(store, cfg_snap, t_snap, max_wait=120.0)
    assert 44.0 <= expected_delay <= 46.0
    with monkeypatch.context() as mp_snap:
        mp_snap.setattr(state, "load_unit_state", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("disk read")))
        assert watch.next_due_delay(store, cfg_snap, t_snap, max_wait=120.0, snapshot=snap) == expected_delay


def test_status_watch_and_live_watch_screen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise read-only `status.watch_status` and zero-termios live `bf watch` across TTY, non-TTY, signals, and errors."""
    import io

    from bf.models import InputError

    # 1. Interval validation boundaries.
    assert status.validate_watch_interval(0.5) == 0.5
    assert status.validate_watch_interval(2.0) == 2.0
    assert status.validate_watch_interval(3600.0) == 3600.0
    for bad_iv in (0.0, 0.49, 3600.1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(InputError, match=r"between 0\.5 and 3600"):
            status.validate_watch_interval(bad_iv)

    root = tmp_path / "live-brain"
    engine.init_brain(root, name="live-brain")
    store = Store(root)
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: live-brain\n"
            "concurrency:\n  default: 2\n  llm: 1\n"
            "loops:\n"
            f"  ok-loop:\n    when: 1m\n    run: [{sys.executable!r}, -c, 'print(1)']\n"
            f"  bad-loop:\n    pool: llm\n    when: 1m\n    run: [{sys.executable!r}, -c, 'import sys; sys.stderr.write(\"boom\\n\"); sys.exit(1)']\n"
        ).encode(),
    )

    # 2. `status_summary(..., cached_corpus=...)` reuses counts/warnings/problems and keeps live loop count.
    cached_res = status.status_summary(
        store,
        include_loops=True,
        cached_corpus=(
            {"notes": 7, "memories": 42, "sources": 2, "loops": 99},
            ["concepts"],
            True,
            ["big file"],
            ["cprob"],
        ),
    )
    assert cached_res["counts"] == {"notes": 7, "memories": 42, "sources": 2, "loops": 2}
    assert cached_res["unbuilt_notes"] == ["concepts"]
    assert cached_res["index_stale"] is True
    assert cached_res["scale_warnings"] == ["big file"]
    assert "cprob" in cached_res["problems"]

    class _FakeStream:
        def __init__(self, *, tty: bool, fail_pipe: bool = False) -> None:
            self._tty = tty
            self._fail_pipe = fail_pipe
            self.raw = io.BytesIO()
            self.buffer: Any = self

        def isatty(self) -> bool:
            return self._tty

        def write(self, data: bytes) -> int:
            if self._fail_pipe:
                raise BrokenPipeError
            return self.raw.write(data)

        def flush(self) -> None:
            return None

    # 3. `status.watch_status`: non-TTY read-only (no alt-screen, never creates `.bf/`).
    ro_root = tmp_path / "ro-watch"
    ro_root.mkdir()
    ro_store = Store(ro_root)
    ro_store.write("bf.yaml", f"version: {FORMAT}\nname: ro-watch\n".encode())
    ev_stop = threading.Event()
    ev_stop.set()
    non_tty = _FakeStream(tty=False)
    with monkeypatch.context() as mp:
        mp.setattr(sys, "stdout", non_tty)
        rc = status.watch_status(ro_store, interval=1.0, stop_event=ev_stop)
    assert rc == 0
    assert not (ro_root / ".bf").exists()
    out_plain = non_tty.raw.getvalue()
    assert render.ALT_ENTER not in out_plain
    assert b"Brain: ro-watch" in out_plain
    assert b"Refreshing every 1s" in out_plain

    # 4. `status.watch_status`: fake TTY alt-screen + color + clean exit on stop_event, KeyboardInterrupt (130), BrokenPipeError (0), SIGTERM (143).
    tty_out = _FakeStream(tty=True)
    with monkeypatch.context() as mp:
        mp.setattr(sys, "stdout", tty_out)
        mp.setenv("TERM", "xterm-256color")
        mp.delenv("NO_COLOR", raising=False)
        rc_tty = status.watch_status(store, interval=0.5, stop_event=ev_stop)
    assert rc_tty == 0
    raw_tty = tty_out.raw.getvalue()
    assert raw_tty.startswith(render.ALT_ENTER)
    assert raw_tty.endswith(render.ALT_EXIT)
    assert b"\033[H" in raw_tty
    assert b"\033[J" in raw_tty

    class _InterruptEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            assert timeout == 0.5
            raise KeyboardInterrupt

    ki_out = _FakeStream(tty=True)
    with monkeypatch.context() as mp:
        mp.setattr(sys, "stdout", ki_out)
        mp.setenv("TERM", "xterm-256color")
        assert status.watch_status(store, interval=0.5, stop_event=_InterruptEvent()) == 130
    assert ki_out.raw.getvalue().endswith(render.ALT_EXIT)

    pipe_out = _FakeStream(tty=False, fail_pipe=True)
    with monkeypatch.context() as mp:
        mp.setattr(sys, "stdout", pipe_out)
        assert status.watch_status(store, interval=0.5, stop_event=ev_stop) == 0

    class _TermEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            assert timeout == 0.5
            os.kill(os.getpid(), signal.SIGTERM)
            return super().wait(1.0)

    term_out = _FakeStream(tty=False)
    with monkeypatch.context() as mp:
        mp.setattr(sys, "stdout", term_out)
        assert status.watch_status(store, interval=0.5, stop_event=_TermEvent()) == 143

    # 5. `watch.serve` with `fmt="auto"` on fake TTY runs live failure-first screen, caches corpus, repaints on events/notes, exits alt-screen cleanly!
    live_tty = _FakeStream(tty=True)
    w_stop = threading.Event()
    real_corpus = status.corpus_counts
    corpus_calls = [0]

    def _spy_corpus(st: Store, cfg_c: config.Config, **kw: Any) -> tuple[dict[str, int], list[str], bool]:
        corpus_calls[0] += 1
        if corpus_calls[0] == 1:
            raise Error("transient scan error")
        return real_corpus(st, cfg_c, **kw)

    with monkeypatch.context() as mp:
        mp.setattr(sys, "stdout", live_tty)
        mp.setenv("TERM", "xterm-256color")
        mp.delenv("NO_COLOR", raising=False)
        mp.setattr(status, "corpus_counts", _spy_corpus)
        mp.setattr(watch, "_LIVE_REFRESH_SECS", 0.08)
        wt = threading.Thread(target=lambda: watch.serve(store, fmt="auto", stop_event=w_stop), daemon=True)
        wt.start()
        try:
            dl = time.monotonic() + 6.0
            while time.monotonic() < dl:
                st_bad = state.load_unit_state(store, "bad-loop", default_event_seq=0)
                st_ok = state.load_unit_state(store, "ok-loop", default_event_seq=0)
                if st_bad.get("failures", 0) >= 1 and st_ok.get("runs", 0) >= 1:
                    break
                time.sleep(0.04)
            # Trigger note change so `_handle_changes` also exercises `render_dirty.set()`, plus wait one periodic tick for cache hit!
            store.write("concepts/live-note.md", b"---\ntype: guide\nstatus: stable\n---\n# Live\n")
            time.sleep(0.25)
        finally:
            w_stop.set()
            wt.join(timeout=6.0)
            assert not wt.is_alive()

    raw_live = live_tty.raw.getvalue()
    assert raw_live.startswith(render.ALT_ENTER)
    assert render.ALT_EXIT in raw_live
    assert b"RECENT" in raw_live
    assert b"Watching live-brain (default:2, llm:1)" in raw_live
    # Final stopped line appears AFTER ALT_EXIT on the normal screen!
    after_exit = raw_live.split(render.ALT_EXIT)[-1]
    assert b"stop" in after_exit
    assert b"live-brain" in after_exit
    # Failure row appears above ok row in the rendered frame once loops ran!
    last_frame = raw_live.split(b"\033[H")[-1].decode("utf-8", "replace")
    assert "bad-loop" in last_frame
    assert "ok-loop" in last_frame
    assert last_frame.index("bad-loop") < last_frame.index("ok-loop")

    # 6. `watch.serve(..., fmt="live")` on non-TTY skips alt-screen escapes and emits plain UTF-8 frames + final stop line!
    live_plain = _FakeStream(tty=False)
    p_stop = threading.Event()
    threading.Timer(0.15, p_stop.set).start()
    with monkeypatch.context() as mp:
        mp.setattr(sys, "stdout", live_plain)
        res_plain = watch.serve(store, fmt="live", stop_event=p_stop)
    assert res_plain["exit_code"] == 0
    plain_watch = live_plain.raw.getvalue()
    assert render.ALT_ENTER not in plain_watch
    assert render.ALT_EXIT not in plain_watch
    assert b"Brain: live-brain" in plain_watch
    assert b"stop" in plain_watch

    # 7. Double-interrupt during `watch.serve` teardown still writes `ALT_EXIT` via inner `finally:`!
    from concurrent.futures import ThreadPoolExecutor

    ki_watch = _FakeStream(tty=True)
    real_shutdown = ThreadPoolExecutor.shutdown
    shot = [False]

    def _boom_shutdown(self_p: ThreadPoolExecutor, *a: Any, **kw: Any) -> None:
        real_shutdown(self_p, *a, **kw)
        if not shot[0]:
            shot[0] = True
            raise KeyboardInterrupt

    ki_stop = threading.Event()
    ki_stop.set()
    with monkeypatch.context() as mp:
        mp.setattr(sys, "stdout", ki_watch)
        mp.setenv("TERM", "xterm-256color")
        mp.setattr(ThreadPoolExecutor, "shutdown", _boom_shutdown)
        with pytest.raises(KeyboardInterrupt):
            watch.serve(store, fmt="live", stop_event=ki_stop)
    assert ki_watch.raw.getvalue().endswith(render.ALT_EXIT)
