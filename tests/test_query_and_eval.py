"""Tests for `bf read`, `bf query` (`rg`, `jq`), and `bf check` evals."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from bf import engine, query, records
from bf.models import EVAL_FORMAT, FORMAT, Error, InputError
from bf.storage import Store


def test_read_target_urls_anchors_cross_brain_and_aliases(tmp_path: Path, brain: Store) -> None:
    with pytest.raises(InputError, match="target is required"):
        query.read_target(brain, "")

    # 1. Markdown note section slicing by `#anchor` and block `#id` fallback.
    sec = query.read_target(brain, "bf:///projects/offline.md?rel=cites#decision")
    assert sec["kind"] == "section"
    assert sec["title"] == "Decision"
    assert "## Decision" in sec["text"]
    assert "## Next actions" not in sec["text"]
    assert "markdown" not in sec
    assert "record" not in sec

    blk = query.read_target(brain, "projects/offline#summary")
    assert blk["element"]["id"] == "summary"

    # Task `#id` and Link `#id` anchors resolve cleanly in `bf read`!
    brain.write(
        "concepts/anchors.md",
        b"---\ntype: guide\nstatus: stable\n---\n# Anchors\n\n"
        b"Cite [doc](../memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json){#cite-1 rel=cites}.\n\n"
        b"- [ ] Verify anchors. {: #task-v1 due=2026-12-01}\n",
    )
    engine.build(brain)
    task_read = query.read_target(brain, "concepts/anchors.md#task-v1")
    assert task_read["task"]["id"] == "task-v1"
    link_read = query.read_target(brain, "concepts/anchors.md#cite-1")
    assert link_read["meta"] == {"rel": "cites"}

    brain.write(
        "concepts/fenced.md",
        b"---\ntype: guide\nstatus: stable\n---\n# Top\n\n## `cmd` flag\n\n"
        b"```python\n# fake comment\n## not a heading\n```\n\nBody.\n\n## Next\n\nOther.\n",
    )
    engine.build(brain)
    fenced_sec = query.read_target(brain, "concepts/fenced.md#cmd-flag")
    assert "## not a heading" in fenced_sec["text"]
    assert "## Next" not in fenced_sec["text"]

    # Prefix-overlapping `#id`, explicit `#other` on same title, and duplicate auto-slug `#notes_1` resolve accurately!
    brain.write(
        "concepts/collisions.md",
        b"---\ntype: guide\nstatus: stable\n---\n# Top\n\n"
        b"## Decisions {#decisions}\n\nWrong plural.\n\n"
        b"## Decision {#decision}\n\nRight singular.\n\n"
        b"## Summary {#intro}\n\nWrong intro.\n\n"
        b"## Summary\n\nRight summary.\n\n"
        b"## Notes\n\nFirst notes.\n\n"
        b"## Notes\n\nSecond notes.\n",
    )
    engine.build(brain)
    assert "Right singular." in query.read_target(brain, "concepts/collisions.md#decision")["text"]
    assert "Wrong plural." not in query.read_target(brain, "concepts/collisions.md#decision")["text"]
    assert "Right summary." in query.read_target(brain, "concepts/collisions.md#summary")["text"]
    assert "Wrong intro." not in query.read_target(brain, "concepts/collisions.md#summary")["text"]
    assert "Second notes." in query.read_target(brain, "concepts/collisions.md#notes_1")["text"]
    assert "First notes." not in query.read_target(brain, "concepts/collisions.md#notes_1")["text"]

    # Auto-anchored headings with underscores, inline emphasis, HTML entities, and Setext underlines slice cleanly!
    brain.write(
        "concepts/setext.md",
        b"---\ntype: guide\nstatus: stable\n---\n# Top\n\n"
        b"## Cost_benefit & *risk*\n\nInline body.\n\n"
        b"Setext Section\n--------------\n\nSetext body.\n\n"
        b"## Tail\n\nTail body.\n",
    )
    engine.build(brain)
    inline_sec = query.read_target(brain, "concepts/setext.md#cost_benefit-risk")
    assert "Inline body." in inline_sec["text"]
    assert "Setext body." not in inline_sec["text"]
    setext_sec = query.read_target(brain, "concepts/setext.md#setext-section")
    assert "Setext body." in setext_sec["text"]
    assert "Tail body." not in setext_sec["text"]

    # Spaceless ATX heading and reference-link heading extract accurately!
    brain.write(
        "concepts/ref-head.md",
        b"---\ntype: guide\nstatus: stable\n---\n##Spaceless\n\nBody1.\n\n## [MCP][m]\n\nBody2.\n\n[m]: https://example.com\n",
    )
    engine.build(brain)
    assert "Body1." in query.read_target(brain, "concepts/ref-head.md#spaceless")["text"]
    assert "Body2." in query.read_target(brain, "concepts/ref-head.md#mcp")["text"]

    for blocked in ("logs/events.log", "Logs/events.log", "LOGS", ".vale.ini"):
        with pytest.raises(InputError, match="private or runtime paths"):
            query.read_target(brain, blocked)

    non_okf = query.read_target(brain, "AGENTS.md")
    assert non_okf["kind"] == "file"

    with pytest.raises(Error, match="missing anchor #nope"):
        query.read_target(brain, "projects/offline.md#nope")

    # 2. JSON file read works, and `#fragment` on JSON or records is rejected with InputError!
    with pytest.raises(InputError, match="JSON records do not support #fragments"):
        query.read_target(brain, "bf:///memories/projects/offline.json#.title")

    plain_json = query.read_target(brain, "memories/projects/offline.json")
    assert plain_json["record"]["ref"] == "projects/offline.md"

    # Sensor memory JSON file read via `bf:///memories/<source>/<sha256>.json` and `#fragment` rejection!
    sensor_mem = "memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json"
    sensor_json = query.read_target(brain, f"bf:///{sensor_mem}")
    assert sensor_json["ref"] == "meetings:decision-1"
    assert sensor_json["kind"] == "memory"
    assert sensor_json["record"]["title"] == "Preserve durable evidence"
    with pytest.raises(InputError, match="JSON records do not support #fragments"):
        query.read_target(brain, f"{sensor_mem}#.title")
    with pytest.raises(Error, match=r"record memories/meetings/0000\.json not found"):
        query.read_target(brain, "bf:///memories/meetings/0000.json")

    # 3. Sensor record by `source:id` + alias lookup + `#fragment` rejection!
    rec = query.read_target(brain, "meetings:decision-1")
    assert rec["record"]["title"] == "Preserve durable evidence"
    with pytest.raises(InputError, match="JSON records do not support #fragments"):
        query.read_target(brain, "meetings:decision-1#.title")

    records.ingest(brain, "repos", b'[{"id":"r1","title":"Brain Repo","aliases":["repo:fmind/bf"]}]')
    by_alias = query.read_target(brain, "repo:fmind/bf")
    assert by_alias["ref"] == "repos:r1"
    by_urn_alias = query.read_target(brain, "urn:repo:fmind/bf")
    assert by_urn_alias["ref"] == "repos:r1"

    # 4. Cross-brain `bf://sib/...` read!
    sib_root = tmp_path / "sib"
    engine.init_brain(sib_root, name="sib")
    sib = Store(sib_root)
    sib.write(
        "concepts/topic.md", b"---\ntype: guide\nstatus: stable\n---\n# Sib Topic\n\n## Core {#core}\n\nHello sib.\n"
    )
    engine.build(sib)
    brain.write("bf.yaml", f"version: {FORMAT}\nname: fixture\nbrains:\n  sib: {sib_root}\n".encode())

    cross = query.read_target(brain, "bf://sib/concepts/topic#core")
    assert cross["brain"] == "sib"
    assert "Hello sib." in cross["text"]

    # Cross-brain percent-encoded paths (`%25`), percent-encoded & HTTP `source:id`, and JSON reads without fragment!
    sib.write("docs/100%25.md", b"# Literal Percent\n")
    cross_pct = query.read_target(brain, "bf://sib/docs/100%2525.md")
    assert cross_pct["brain"] == "sib"
    assert "Literal Percent" in cross_pct["text"]
    records.ingest(
        sib,
        "docs",
        json.dumps(
            [{"id": "a%2Fb", "title": "Encoded Slash"}, {"id": "My%20Report.pdf", "title": "Space Doc"}]
        ).encode(),
    )
    records.ingest(sib, "web", json.dumps([{"id": "https://example.com/p?a=1&b=2", "title": "Query Page"}]).encode())
    assert query.read_target(brain, "bf://sib/docs:a%2Fb")["record"]["title"] == "Encoded Slash"
    assert query.read_target(brain, "bf://sib/docs:My%20Report.pdf")["record"]["title"] == "Space Doc"
    assert query.read_target(brain, "bf://sib/web:https://example.com/p?a=1&b=2")["record"]["title"] == "Query Page"
    cross_json = query.read_target(brain, "bf://sib/memories/concepts/topic.json")
    assert cross_json["record"]["title"] == "Sib Topic"
    with pytest.raises(InputError, match="JSON records do not support #fragments"):
        query.read_target(brain, "bf://sib/memories/concepts/topic.json#.title")

    for bad, exc in (
        ("https://example.com", InputError),
        ("bf://bad/no-such.md", Error),
        ("concepts/missing.md", Error),
        ("meetings:missing", Error),
    ):
        with pytest.raises(exc):
            query.read_target(brain, bad)


def test_query_rg_jq_streaming_and_limits(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    rg_hits = query.search_rg(brain, "durable", limit=1)
    assert rg_hits["engine"] == "rg"
    assert len(rg_hits["items"]) == 1
    assert rg_hits["truncated"] is True

    # Match-centered `rg` snippets on compact single-line JSON records (> 240 chars, UTF-8 safe)!
    long_body = ("Café — " * 50) + "deep-centered-needle" + (" omega" * 50)
    records.ingest(brain, "docs", json.dumps([{"id": "d-long", "title": "EarlyHead", "text": long_body}]).encode())
    deep_hit = query.search_rg(brain, "deep-centered-needle", scope="docs")
    assert deep_hit["count"] == 1
    assert "deep-centered-needle" in deep_hit["items"][0]["snippet"]
    assert len(deep_hit["items"][0]["snippet"]) == 240
    head_hit = query.search_rg(brain, "d-long", scope="docs")
    assert head_hit["count"] == 1
    assert head_hit["items"][0]["snippet"] == "EarlyHead (d-long)"
    tail_hit = query.search_rg(brain, "EarlyHead", scope="docs")
    assert tail_hit["count"] == 1
    assert "EarlyHead" in tail_hit["items"][0]["snippet"]

    # Scope resolution: OKF note path, explicit memories/*.json path, subfolder, invalid scope, and empty scope!
    assert query.search_rg(brain, "durable", scope="projects/offline.md")["count"] == 1
    assert query.query_jq(brain, ".ref", scope="memories/projects/offline.json")["items"] == ["projects/offline.md"]
    assert query.query_jq(brain, ".ref", scope="concepts")["count"] >= 1
    assert query.search_rg(brain, "durable", scope="empty-source")["items"] == []
    assert query.query_jq(brain, ".ref", scope="empty-source")["items"] == []
    with pytest.raises(InputError, match="expected a normalized brain-relative path"):
        query.search_rg(brain, "durable", scope="../outside")
    with pytest.raises(InputError, match="jq failed"):
        query.query_jq(brain, "[[[", scope="empty-source")

    empty_rg = query.search_rg(brain, "zzz-never-matches-zzz")
    assert empty_rg["items"] == []
    assert empty_rg["truncated"] is False
    with pytest.raises(InputError, match="rg failed"):
        query.search_rg(brain, "[unclosed")

    jq_hits = query.query_jq(brain, ".ref", limit=1)
    assert len(jq_hits["items"]) == 1
    assert jq_hits["truncated"] is True
    jq_all = query.query_jq(brain, 'select(.kind == "project") | .ref', limit=20)
    assert jq_all["items"] == ["projects/offline.md"]
    assert jq_all["truncated"] is False

    # Streaming jq reports unreadable-file / feeder errors cleanly instead of silently dropping records.
    real_iter = brain.iter_read_files

    def broken_iter(_name: str, **_kw: object) -> object:
        raise Error("simulated read failure")
        yield  # pragma: no cover

    monkeypatch.setattr(brain, "iter_read_files", broken_iter)
    with pytest.raises(Error, match="simulated read failure"):
        query.query_jq(brain, ".")
    monkeypatch.setattr(brain, "iter_read_files", real_iter)

    # jq confinement blocks modulemeta/import/include file probes and strips host environment variables ($ENV).
    for forbidden_jq in ('import "foo" as f; .', 'include "foo"; .', 'modulemeta("foo")'):
        with pytest.raises(InputError, match="jq failed"):
            query.query_jq(brain, forbidden_jq)
    monkeypatch.setenv("SECRET_HOST_TOKEN", "should-not-leak")
    env_probe = query.query_jq(brain, "$ENV.SECRET_HOST_TOKEN // $ENV.HOME // empty")
    assert env_probe["items"] == []

    # Bad JSON returned by jq or invalid jq syntax raises InputError / Error cleanly.
    with pytest.raises(InputError, match="jq failed"):
        query.query_jq(brain, "invalid syntax [[[")
    for bad_fn in (
        lambda: query.search_rg(brain, ""),
        lambda: query.search_rg(brain, "x", limit=0),
        lambda: query.query_jq(brain, ""),
        lambda: query.query_jq(brain, ".", limit=0),
    ):
        with pytest.raises(InputError):
            bad_fn()

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert query.find_by_alias(brain, "meetings:nonexistent") is None
    with pytest.raises(Error, match="is required on PATH"):
        query.search_rg(brain, "durable")


def test_evaluate_passes_and_reports_failures(tmp_path: Path, brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write(
        "evals/suite.json",
        json.dumps(
            {
                "version": EVAL_FORMAT,
                "cases": [
                    {
                        "name": "rg-offline",
                        "engine": "rg",
                        "query": "durable",
                        "expect_refs": ["projects/offline.md"],
                        "forbid_refs": ["meetings:lunch"],
                        "min_count": 1,
                    },
                    {
                        "name": "jq-project",
                        "engine": "jq",
                        "query": 'select(.kind == "project") | {ref}',
                        "expect_equals": [{"ref": "projects/offline.md"}],
                    },
                    {
                        "name": "jq-count",
                        "engine": "jq",
                        "query": 'select(.kind == "concept") | .ref',
                        "max_count": 5,
                    },
                    {
                        "name": "read-decision",
                        "engine": "read",
                        "query": "bf:///projects/offline.md#decision",
                        "expect_text": ["retention"],
                    },
                ],
            }
        ).encode(),
    )
    res = query.evaluate(brain)
    assert res["ok"], res

    # Failing eval file reports structured failure reasons without raising (including expect_text against empty items!).
    brain.write(
        "evals/failing.yaml",
        f"version: {EVAL_FORMAT}\n"
        "cases:\n"
        "  - name: bad-case\n"
        "    engine: rg\n"
        "    query: durable\n"
        "    expect_refs: [concepts/missing.md]\n"
        "    forbid_refs: [projects/offline.md]\n"
        "    min_count: 99\n"
        "    max_count: 0\n"
        "  - name: self-match-guard\n"
        "    engine: rg\n"
        "    query: zzz-unmatched-query-literal\n"
        "    expect_text: [zzz-unmatched-query-literal]\n"
        "  - name: broken-read\n"
        "    engine: read\n"
        "    query: concepts/nonexistent.md\n".encode(),
    )
    brain.write("evals/broken.yaml", b"version: 99\ncases: []\n")
    brain.write("evals/badschema.yaml", f"version: {EVAL_FORMAT}\ncases: not-a-list\n".encode())
    brain.write("evals/scalar.json", b"123\n")
    fail_res = query.evaluate(brain, target="failing")
    assert not fail_res["ok"]
    assert fail_res["failed"] == 3
    assert len(fail_res["cases"][0]["reasons"]) == 4
    assert "missing expected text 'zzz-unmatched-query-literal'" in fail_res["cases"][1]["reasons"][0]

    all_res = query.evaluate(brain)
    assert not all_res["ok"]
    assert len(all_res["problems"]) >= 3

    with pytest.raises(Error, match="no eval file matching") as ev_exc:
        query.evaluate(brain, target="nonexistent-suite")
    assert not isinstance(ev_exc.value, InputError)

    # Corrupt stored JSON raises Error (not InputError) in query_jq; script shims preserve HOME/XDG/MISE while keeping $ENV/env empty and stripping secrets; rg exit 1 with stderr fails closed / falls back!
    brain.write("memories/corrupt/bad.json", b"{corrupt\n")
    with pytest.raises(Error, match="parse error:") as exc_info:
        query.query_jq(brain, ".", scope="corrupt")
    assert not isinstance(exc_info.value, InputError)
    brain.delete("memories/corrupt/bad.json")

    records.ingest(
        brain, "items", json.dumps([{"id": "alias-1", "title": "Alias item", "aliases": ["legacy:alias-1"]}]).encode()
    )
    target_mem = brain.root / records.record_file("items", "alias-1")
    shim_bin = tmp_path / "shim-bin"
    shim_bin.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "bf-home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "bf-xdg"))
    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "bf-mise"))
    monkeypatch.setenv("GITHUB_TOKEN", "secret-tok")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(tmp_path / "evil-rg"))
    real_jq = query.resolve_tool("jq")
    wrap_jq = shim_bin / "jq"
    wrap_jq.write_text(
        '#!/bin/sh\n[ -n "$HOME" ] && [ -n "$XDG_DATA_HOME" ] && [ -n "$MISE_DATA_DIR" ]'
        ' && [ -z "${GITHUB_TOKEN:-}" ] && [ -z "${RIPGREP_CONFIG_PATH:-}" ] || exit 2\n'
        f'exec "{real_jq}" "$@"\n'
    )
    wrap_jq.chmod(0o755)
    pass_rg = shim_bin / "rg-ok"
    pass_rg.write_text(
        '#!/bin/sh\n[ -n "$HOME" ] && [ -n "$XDG_DATA_HOME" ] && [ -n "$MISE_DATA_DIR" ]'
        ' && [ -z "${GITHUB_TOKEN:-}" ] && [ -z "${RIPGREP_CONFIG_PATH:-}" ] || exit 2\n'
        f'printf \'{{"type":"match","data":{{"path":{{"text":"{target_mem}"}},'
        '"lines":{"text":"Alias item"},"submatches":[{"start":0}]}}\\n\'\n'
    )
    pass_rg.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda n: str(wrap_jq) if n == "jq" else str(pass_rg) if n == "rg" else None)
    assert query.query_jq(brain, "[$ENV, env]", limit=1)["items"] == [[{}, {}]]
    assert query.search_rg(brain, "Alias")["items"][0]["path"] == records.record_file("items", "alias-1")

    fail_rg = shim_bin / "rg"
    fail_rg.write_text("#!/bin/sh\necho 'mise ERROR: missing HOME' >&2\nexit 1\n")
    fail_rg.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda n: str(fail_rg) if n == "rg" else None)
    with pytest.raises(Error, match="rg failed: mise ERROR: missing HOME") as rg_exc:
        query.search_rg(brain, "durable")
    assert not isinstance(rg_exc.value, InputError)
    with pytest.raises(Error, match="rg failed: mise ERROR: missing HOME"):
        query.search_rg(brain, "durable", scope="nonexistent-scope")
    alias_hit = query.find_by_alias(brain, "legacy:alias-1")
    assert alias_hit is not None
    assert alias_hit[0] == records.record_file("items", "alias-1")

    # `resolve_tool` unwraps `mise` shims to the underlying executable, and `drain_tail` bounds memory:
    mise_dir = tmp_path / "fake-mise"
    mise_dir.mkdir()
    fake_mise = mise_dir / "mise"
    fake_mise.write_text(f'#!/bin/sh\nif [ "$1" = "which" ] && [ "$2" = "jq" ]; then echo "{real_jq}"; fi\n')
    fake_mise.chmod(0o755)
    mise_shim = mise_dir / "jq"
    mise_shim.symlink_to(fake_mise)
    monkeypatch.setattr(shutil, "which", lambda n: str(mise_shim) if n == "jq" else None)
    assert query.resolve_tool("jq") == real_jq
    assert set(query.safe_env(real_jq)) == {"LC_ALL", "LANG"}
    assert "HOME" in query.safe_env(str(mise_shim))

    import io
    import subprocess

    tail_t, tail_buf = query.drain_tail(io.BytesIO(b"a" * 20000 + b"TAIL"), 16)
    tail_t.join(timeout=2)
    assert bytes(tail_buf) == b"aaaaaaaaaaaaTAIL"

    # Real pipe `drain_tail` captures `< 8192` bytes and exits cleanly on `stop` even if a grandchild holds stderr!
    import threading

    stop_err = threading.Event()
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)']); sys.stderr.write('boom\\n'); sys.stderr.flush()",
        ],
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as gc_proc:
        assert gc_proc.stderr is not None
        gt, gbuf = query.drain_tail(gc_proc.stderr, 64, stop_err)
        gc_proc.wait(timeout=5)
        stop_err.set()
        gt.join(timeout=2)
        assert not gt.is_alive()
        assert b"boom" in bytes(gbuf)
        query.stop_proc(gc_proc)

    # Failing `jq` wrapper shim (exit 1) maps to `Error` (exit 1), not `InputError` (exit 2):
    fail_jq = shim_bin / "jq-fail"
    fail_jq.write_text("#!/bin/sh\necho 'mise ERROR: boom' >&2\nexit 1\n")
    fail_jq.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda n: str(fail_jq) if n == "jq" else None)
    with pytest.raises(Error, match="jq failed: mise ERROR: boom") as jq_exc:
        query.query_jq(brain, ".ref")
    assert not isinstance(jq_exc.value, InputError)

    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(["rg"], 5))
    )
    monkeypatch.setattr(shutil, "which", lambda n: str(fail_rg) if n == "rg" else None)
    with pytest.raises(Error, match="rg timed out after 5s"):
        query.search_rg(brain, "durable", scope="nonexistent-scope")
    monkeypatch.undo()

    # `rg` exit 127 (wrapper/exec failure) maps to `Error` (exit 1), not `InputError` (exit 2):
    rc127_rg = shim_bin / "rg-127"
    rc127_rg.write_text("#!/bin/sh\necho 'not found' >&2\nexit 127\n")
    rc127_rg.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda n: str(rc127_rg) if n == "rg" else None)
    with pytest.raises(Error, match="rg failed: not found") as rc127_exc:
        query.search_rg(brain, "durable")
    assert not isinstance(rc127_exc.value, InputError)
    monkeypatch.undo()

    # `drain_tail` uses `select.poll()` and handles fds >= 1024 (`FD_SETSIZE`) cleanly:
    import fcntl
    import os
    import resource

    r_fd, w_fd = os.pipe()
    high_fd = -1
    try:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft > 1030:
            high_fd = fcntl.fcntl(r_fd, fcntl.F_DUPFD, 1025)
            os.close(r_fd)
            r_fd = -1
            with os.fdopen(high_fd, "rb", buffering=0) as high_stream:
                high_fd = -1
                ht, hbuf = query.drain_tail(high_stream, 64)
                os.write(w_fd, b"hello-high-fd")
                os.close(w_fd)
                w_fd = -1
                ht.join(timeout=2)
                assert bytes(hbuf) == b"hello-high-fd"
    finally:
        for fd_i in (r_fd, w_fd, high_fd):
            if fd_i >= 0:
                os.close(fd_i)

    # `resolve_tool` invalidates stale cached paths, skips negative-caching failed `mise which`, and respects shim env:
    cache_dir = tmp_path / "cache-mise"
    cache_dir.mkdir()
    target_a = cache_dir / "jq-a"
    target_a.write_text(f'#!/bin/sh\nexec "{real_jq}" "$@"\n')
    target_a.chmod(0o755)
    target_b = cache_dir / "jq-b"
    target_b.write_text(f'#!/bin/sh\nexec "{real_jq}" "$@"\n')
    target_b.chmod(0o755)
    dyn_mise = cache_dir / "mise"
    dyn_mise.write_text(
        f'#!/bin/sh\nif [ -f "{target_a}" ]; then printf "%s\\n" "{target_a}"; exit 0; fi\n'
        f'if [ -f "{target_b}" ]; then printf "%s\\n" "{target_b}"; exit 0; fi\nexit 1\n'
    )
    dyn_mise.chmod(0o755)
    dyn_shim = cache_dir / "jq"
    dyn_shim.symlink_to(dyn_mise)
    monkeypatch.setattr(shutil, "which", lambda n: str(dyn_shim) if n == "jq" else None)
    assert query.resolve_tool("jq") == str(target_a)
    target_a.unlink()
    assert query.resolve_tool("jq") == str(target_b)
    target_b.unlink()
    assert query.resolve_tool("jq") == str(dyn_shim)
    target_b.write_text(f'#!/bin/sh\nexec "{real_jq}" "$@"\n')
    target_b.chmod(0o755)
    assert query.resolve_tool("jq") == str(target_b)


def test_read_modes_and_query_format15(brain: Store) -> None:
    # Note outline, lines, raw, full, fields, max_bytes, conflicts, and dirty auto-sync.
    outl = query.read_target(brain, "projects/offline.md", outline=True)
    assert outl["ref"] == "projects/offline.md"
    assert "headings" in outl
    assert "text" not in outl
    lns = query.read_target(brain, "projects/offline.md", lines="1:3")
    assert lns["start"] == 1
    assert lns["count"] == 3
    assert "text" in lns
    raw = query.read_target(brain, "projects/offline.md", raw=True)
    assert "text" in raw
    assert "markdown" not in raw
    assert "record" not in raw
    full = query.read_target(brain, "projects/offline.md", full=True, fields=["ref", "record"])
    assert set(full) == {"ref", "record"}

    capped = query.read_target(brain, "projects/offline.md", max_bytes=120)
    assert capped["truncated"] is True
    tiny = query.read_target(brain, "projects/offline.md", max_bytes=30)
    assert tiny["truncated"] is True
    assert "text" not in tiny

    with pytest.raises(InputError):
        query.read_target(brain, "projects/offline.md", outline=True, raw=True)
    with pytest.raises(InputError):
        query.read_target(brain, "projects/offline.md", max_bytes=0)
    with pytest.raises(InputError, match="cannot be combined with #anchor"):
        query.read_target(brain, "projects/offline.md#decision", outline=True)
    with pytest.raises(InputError, match="apply only to OKF notes"):
        query.read_target(brain, "AGENTS.md", outline=True)
    with pytest.raises(InputError, match="apply only to Markdown notes"):
        query.read_target(brain, "meetings:decision-1", raw=True)

    # Dirty note auto-syncs on read and query.
    brain.write("concepts/dirty.md", b"---\ntype: guide\nstatus: draft\n---\n# Dirty\n\nFresh body.\n")
    dirty_rd = query.read_target(brain, "concepts/dirty.md")
    assert "Fresh body." in dirty_rd["text"]
    assert query.read_target(brain, "memories/concepts/dirty.json")["record"]["title"] == "Dirty"

    # Combined rg + jq, kind/source/fields filters, and streaming jq parity.
    comb = query.query(brain, "durable", jq_expr=".ref", kind="note")
    assert comb["engine"] == "rg+jq"
    assert "projects/offline.md" in comb["items"]
    miss = query.query(brain, "zzz-never-zzz", jq_expr=".ref")
    assert miss["engine"] == "rg+jq"
    assert miss["items"] == []
    mem_only = query.query(brain, "decision", kind="memory", scope="meetings", fields="ref,kind")
    assert mem_only["items"]
    assert all(it["kind"] == "memory" for it in mem_only["items"])
    idx_q = query.query_jq(brain, 'select(.kind == "concept") | .ref', kind="note")
    assert "concepts/dirty.md" in idx_q["items"]
    for bad_q in (
        lambda: query.query(brain),
        lambda: query.query(brain, "x", kind="bad"),
        lambda: query.query(brain, "x", scope="../escape"),
        lambda: query.query(brain, "x", limit=5001),
    ):
        with pytest.raises(InputError):
            bad_q()

    # Additional branch coverage: safe_env(""), UTF-8 byte cap, anchor/path fallbacks, eval engines & scalar cases.
    assert query.safe_env("") == {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
    brain.write("concepts/utf8.md", ("---\ntype: guide\nstatus: stable\n---\n# U\n\n" + ("é" * 120) + "\n").encode())
    engine.build(brain)
    utf_cap = query.read_target(brain, "concepts/utf8.md", max_bytes=140)
    assert utf_cap["truncated"] is True
    sec_cap = query.read_target(brain, "bf:///projects/offline.md#decision", max_bytes=95)
    assert sec_cap["truncated"] is True
    with pytest.raises(Error, match="missing anchor"):
        query.read_target(brain, "AGENTS.md#head")
    with pytest.raises(Error, match=r"record memories/concepts/none\.json not found"):
        query.read_target(brain, "memories/concepts/none.json")
    assert query.query(brain, "durable", scope="concepts/dirty.md")["count"] == 0
    assert query.query(brain, "durable", scope="memories")["count"] >= 1
    assert query.query(brain, "durable", scope="memories/meetings/none.json")["items"] == []
    assert query.query(brain, "durable", kind="note", scope="meetings")["items"] == []
    assert query.query_jq(brain, ".ref", scope="memories/concepts")["items"]
    assert query.query_jq(brain, ".ref", scope="concepts/dirty")["items"] == ["concepts/dirty.md"]
    assert query.query_jq(brain, ".ref", kind="memory", fields=["ref"])["count"] >= 1
    assert query.query(brain, "durable", jq_expr=".ref", limit=1)["truncated"] is True
    brain.write(
        "evals/badengine.yaml",
        f"version: {EVAL_FORMAT}\ncases:\n  - name: bad-eng\n    engine: nope\n    query: x\n".encode(),
    )
    ev_bad = query.evaluate(brain, target="badengine")
    assert not ev_bad["ok"]
    assert ev_bad["problems"]
    brain.write(
        "evals/eqfail.yaml",
        f"version: {EVAL_FORMAT}\ncases:\n"
        "  - name: eq-fail\n    engine: jq\n    query: '.ref'\n    expect_equals: [nope]\n".encode(),
    )
    ev_eq = query.evaluate(brain, target="eqfail")
    assert not ev_eq["ok"]
    assert ev_eq["failed"] == 1


def test_query_autosync_index_guard_readonly_and_max_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bf.models import terminal

    b_root = tmp_path / "fresh"
    engine.init_brain(b_root, name="fresh")
    st = Store(b_root)

    # 1. `query()` auto-syncs unbuilt notes BEFORE `_target_scope`, even when `memories/projects/` does not exist yet.
    st.write(
        "projects/unbuilt.md",
        b"---\ntype: project\nstatus: draft\n---\n# Unbuilt\n\nneedle-beta in fresh project.\n",
    )
    assert query.query(st, "needle-beta", kind="note")["items"][0]["ref"] == "projects/unbuilt.md"
    st.write(
        "concepts/unbuilt2.md",
        b"---\ntype: guide\nstatus: stable\n---\n# Unbuilt Two\n\nneedle-alpha\n",
    )
    assert query.query(st, "needle-alpha", scope="concepts/unbuilt2.md")["items"][0]["ref"] == "concepts/unbuilt2.md"

    # 2. Direct streaming `jq` evaluates rich expressions and sensor `.updated` faithfully.
    st.write(
        "concepts/rich.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-09-02\n---\n# Rich\n\n"
        b"See [welcome](welcome.md#conventions).\n",
    )
    engine.build(st)
    records.ingest(st, "cal", b'[{"id":"ev1","title":"Meet","time":"2026-08-01T10:00:00Z"}]')
    cal_p = records.record_file("cal", "ev1")
    st.write(
        cal_p,
        b'{"id":"ev1","source":"cal","kind":"memory","ref":"cal:ev1","title":"Meet","time":"2026-08-01T10:00:00Z"}',
    )
    engine.build(st, force=True)

    shorthand = query.query_jq(st, 'select(.kind == "concept" and .ref == "concepts/rich.md") | {ref, text}')["items"]
    assert shorthand == [{"ref": "concepts/rich.md", "text": "Rich\nSee welcome."}]
    with_links = query.query_jq(st, 'select(.ref == "concepts/rich.md") | {ref, links}')["items"]
    assert with_links[0]["links"][0]["section"] == "rich"
    assert with_links[0]["links"][0]["anchor"] == "conventions"
    assert query.query_jq(st, 'select(.ref == "concepts/rich.md") | .["text"]')["items"] == ["Rich\nSee welcome."]
    assert query.query_jq(st, 'select(.ref == "concepts/rich.md") | ."text"')["items"] == ["Rich\nSee welcome."]
    assert query.query_jq(st, 'select(.ref == "concepts/rich.md") | has("text")')["items"] == [True]
    if_dot = query.query_jq(st, 'if .ref == "concepts/rich.md" then . else empty end')["items"][0]
    assert "text" in if_dot
    assert "path" not in if_dot
    del_status = query.query_jq(st, 'select(.ref == "concepts/rich.md") | del(.status)')["items"][0]
    assert "text" in del_status
    assert "status" not in del_status
    chained_sel = query.query_jq(st, 'select(.kind == "concept") | select(.ref == "concepts/rich.md")')["items"][0]
    assert "text" in chained_sel
    assert query.query_jq(st, 'select(.source == "cal") | .updated')["items"] == [None]
    assert "2026-09-02" in query.query_jq(st, 'select(.kind == "concept") | .updated', kind="note")["items"]

    # Streaming jq regressions: string interpolation `\(...`, comma-separated zero-arg filters, and `tojson`/`values`.
    assert query.query_jq(st, 'select("\\(.text)" | test("See welcome")) | .ref')["items"] == ["concepts/rich.md"]
    assert query.query_jq(st, 'select(.status == "stable" and .ref == "concepts/rich.md") | .ref + ":\\(.text)"')[
        "items"
    ] == ["concepts/rich.md:Rich\nSee welcome."]
    tojson_items = query.query_jq(st, 'select(.ref == "concepts/rich.md") | .ref, tojson')["items"]
    assert tojson_items[0] == "concepts/rich.md"
    tojson_obj = json.loads(tojson_items[1])
    assert "text" in tojson_obj
    assert "path" not in tojson_obj
    val_items = query.query_jq(st, 'select(.ref == "concepts/rich.md") | .ref, values')["items"]
    assert val_items[0] == "concepts/rich.md"
    assert "text" in val_items[1]
    assert "path" not in val_items[1]
    no_pipe_sel = query.query_jq(st, 'select(.ref == "concepts/rich.md")')["items"][0]
    assert "text" in no_pipe_sel
    assert "path" not in no_pipe_sel

    records.ingest(st, "cal", b'[{"id":"ev2","title":"EmptySum","summary":"","time":"2026-08-01T10:00:00Z"}]')
    assert query.query_jq(st, 'select(.source == "cal" and .ref == "cal:ev2") | .summary')["items"] == [""]

    # 3. `read_target` `--max-bytes` strictly caps output bytes or raises `InputError`.
    st.write(
        "concepts/mb.md",
        ("---\ntype: guide\nstatus: stable\n---\n# MB 🚀 日本語\n\n" + ("🚀日本語éàü " * 20) + "\n").encode(),
    )
    engine.build(st)
    untruncated = query.read_target(st, "concepts/mb.md", max_bytes=10000)
    assert "truncated" not in untruncated
    for budget in (140, 80, 40):
        capped = query.read_target(st, "concepts/mb.md", max_bytes=budget)
        assert capped["truncated"] is True
        assert len(terminal(capped)) <= budget
    for too_small in (1, 10, 18):
        with pytest.raises(InputError, match="too small"):
            query.read_target(st, "concepts/mb.md", max_bytes=too_small)

    # 4. `read_target` auto-syncs dirty notes cleanly even when `.bf/` and `memories/` are `0o500` read-only.
    st.write("concepts/rich.md", b"---\ntype: guide\nstatus: stable\n---\n# Rich\n\nDirty2\n")
    (b_root / ".bf").chmod(0o500)
    (b_root / "memories").chmod(0o500)
    try:
        ro_res = query.read_target(st, "concepts/rich.md")
        assert "Dirty2" in ro_res["text"]
    finally:
        (b_root / ".bf").chmod(0o700)
        (b_root / "memories").chmod(0o700)

    # 5. Snippet fallback (`_HEAD_RE`, nested list/dict, key-name miss), non-dict JSON, empty `memories/`, and eval cases.
    records.ingest(
        st,
        "notes2",
        json.dumps(
            [{"id": "n1", "title": "Snip", "text": "## Auto Sec\n\nfind-in-sec", "extra": [{"k": ["deep-tag"]}]}]
        ).encode(),
    )
    assert query.search_rg(st, "find-in-sec", scope="notes2")["items"][0]["anchor"] == "auto-sec"
    assert query.search_rg(st, "deep-tag", scope="notes2")["items"][0]["snippet"] == "deep-tag"
    assert query.search_rg(st, "extra", scope="notes2")["items"][0]["snippet"] == "Snip"

    st.write("memories/corrupt/list.json", b"[1, 2]\n")
    with pytest.raises(Error, match="expected a JSON object"):
        query.read_target(st, "memories/corrupt/list.json")
    st.delete("memories/corrupt/list.json")

    shim = tmp_path / "rg-filter"
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    note_mem = b_root / "memories" / "concepts" / "rich.json"
    cal_mem = b_root / "memories" / records.record_file("cal", "ev1").removeprefix("memories/")
    shim.write_text(
        "#!/bin/sh\n"
        f'printf \'{{"type":"match","data":{{"path":{{"text":"{outside}"}},"lines":{{"text":"x"}}}}}}\\n\'\n'
        f'printf \'{{"type":"match","data":{{"path":{{"text":"{note_mem}"}},"lines":{{"text":"x"}}}}}}\\n\'\n'
        f'printf \'{{"type":"match","data":{{"path":{{"text":"{cal_mem}"}},"lines":{{"text":"Meet"}}}}}}\\n\'\n'
    )
    shim.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda n: str(shim) if n == "rg" else None)
    filt = query.search_rg(st, "Meet", kind="memory")
    assert [it["ref"] for it in filt["items"]] == ["cal:ev1"]
    monkeypatch.undo()

    st.write("schemas/evals.json", b'{"type": "object"}\n')
    st.write(
        "evals/custom.yaml",
        f'version: {EVAL_FORMAT}\ncases:\n  - "not-a-dict"\n  - name: bad-eng\n    engine: nope\n    query: x\n'.encode(),
    )
    custom_ev = query.evaluate(st, target="custom")
    assert custom_ev["failed"] == 1
    assert "unsupported eval engine" in custom_ev["cases"][0]["reasons"][0]

    shutil.rmtree(b_root / "memories")
    assert query.query(st, "anything", kind="memory")["items"] == []


def test_query_jq_parity_and_ordering(tmp_path: Path) -> None:
    import subprocess

    from bf.models import AUTHORED

    b_root = tmp_path / "parity"
    engine.init_brain(b_root, name="parity")
    st = Store(b_root)
    st.write(
        "concepts/alpha.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-09-02\n---\n# Alpha\n\nBody alpha.\n",
    )
    st.write(
        "projects/empty-upd.md",
        b'---\ntype: project\nstatus: draft\nupdated: ""\n---\n# Empty Upd\n\nValid empty updated.\n',
    )
    st.write(
        "actions/2026-10-01_act/ACTION.md",
        b"---\ntype: action\nstatus: draft\n---\n# Act One\n\nAction body.\n",
    )
    assert engine.build(st)["ok"]
    assert engine.validate(st)["ok"]
    st.write(
        "projects/empty-status.md",
        b'---\ntype: project\nstatus: ""\nupdated: ""\n---\n# Empty Status\n\nCompiled empty status.\n',
    )
    assert engine.build(st)["ok"]

    records.ingest(st, "cal", b'[{"id":"ev1","title":"Meet","summary":""}]')
    records.ingest(st, "cal", b'[{"id":"a@b#c","title":"AtHash"}]')
    st.write(
        records.record_file("cal", "empty-fields"),
        b'{"id":"empty-fields","source":"cal","kind":"memory","ref":"cal:empty-fields",'
        b'"title":"","status":"","entity":"","stale_after":"","summary":"","updated":""}',
    )
    engine.build(st, force=True)

    jq_bin = query.resolve_tool("jq")

    def run_real_jq(expr: str, knd: str = "") -> list[object]:
        if knd == "note":
            files = sorted(
                p
                for d in sorted(AUTHORED)
                if (b_root / "memories" / d).is_dir()
                for p in (b_root / "memories" / d).rglob("*.json")
                if p.is_file()
            )
        else:
            files = sorted(p for p in (b_root / "memories").rglob("*.json") if p.is_file())
        blob = b"".join(p.read_bytes().rstrip(b"\n") + b"\n" for p in files)
        proc = subprocess.run(  # noqa: S603
            [jq_bin, "-L", "/dev/null", "-c", "--", f"{query.JQ_PRELUDE}\n{expr}"],
            input=blob,
            capture_output=True,
            cwd="/",
            env=query.safe_env(jq_bin),
            timeout=30,
            check=True,
        )
        return [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]

    for expr, knd in (
        (".ref", ""),
        ('select(.kind == "concept" or .kind == "project") | .ref, .kind', ""),
        ('select(.ref | startswith("concepts/")) | .ref', ""),
        ('select(.kind == "concept") | .title, .sha256', "note"),
        ('select(.kind == "project") | .updated', "note"),
        ('select(.source == "cal") | .summary', ""),
        (".", ""),
    ):
        assert query.query_jq(st, expr, kind=knd, limit=100)["items"] == run_real_jq(expr, knd)

    assert query.query_jq(st, ".ref", scope="memories/concepts/alpha.json")["items"] == ["concepts/alpha.md"]
    assert query.query_jq(st, ".ref", kind="note", limit=2)["items"] == run_real_jq(".ref", "note")[:2]
    with pytest.raises(InputError, match="jq failed"):
        query.query_jq(st, 'select(.kind == "concept")) | .ref')

    # Nested note ordering parity.
    for rel, title in (("concepts/a-b.md", "A-B"), ("concepts/a.md", "A"), ("concepts/a/c.md", "A/C")):
        st.write(rel, f"---\ntype: guide\nstatus: stable\n---\n# {title}\n\nBody.\n".encode())
    assert engine.build(st)["ok"]
    concepts_items = query.query_jq(st, ".ref", scope="concepts", kind="note", limit=100)["items"]
    assert concepts_items == [
        "concepts/a/c.md",
        "concepts/a-b.md",
        "concepts/a.md",
        "concepts/alpha.md",
        "concepts/welcome.md",
    ]

    # Cross-brain `bf://<sib>/<source>:<id>` falls back to `find_by_alias` on the sibling brain!
    sib_root = tmp_path / "sib-alias"
    engine.init_brain(sib_root, name="sib-alias")
    sib_st = Store(sib_root)
    sib_st.write(
        "concepts/demo-repo.md",
        b"---\ntype: guide\nstatus: stable\naliases:\n  - repo:acme/demo\n---\n# Demo Repo\n",
    )
    engine.build(sib_st)
    st.write("bf.yaml", f"version: {FORMAT}\nname: jq-parity\nbrains:\n  sib-alias: {sib_root}\n".encode())
    cross_alias = query.read_target(st, "bf://sib-alias/repo:acme/demo", read_only=True)
    assert cross_alias["ref"] == "concepts/demo-repo.md"
