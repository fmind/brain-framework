"""Tests for `bf read`, `bf query` (`rg`, `jq`), and `bf eval`."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from bf import engine, query, records
from bf.models import FORMAT, Error, InputError
from bf.storage import Store


def test_read_target_urls_anchors_cross_brain_and_aliases(tmp_path: Path, brain: Store) -> None:
    with pytest.raises(InputError, match="target is required"):
        query.read_target(brain, "")

    # 1. Markdown note section slicing by `#anchor` and block `#id` fallback.
    sec = query.read_target(brain, "bf:///projects/offline.md?rel=cites#decision")
    assert sec["kind"] == "section"
    assert "## Decision" in sec["text"]
    assert "## Next actions" not in sec["text"]

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

    non_okf = query.read_target(brain, "docs/index.md")
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

    # Cross-brain percent-encoded paths (`%25`) and JSON reads without fragment!
    sib.write("docs/100%25.md", b"# Literal Percent\n")
    cross_pct = query.read_target(brain, "bf://sib/docs/100%2525.md")
    assert cross_pct["brain"] == "sib"
    assert "Literal Percent" in cross_pct["text"]
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
    assert head_hit["items"][0]["snippet"].startswith('{"id":"d-long"')
    assert len(head_hit["items"][0]["snippet"]) == 240
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
        query.query_jq(brain, ".ref")
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
                "version": FORMAT,
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
        f"version: {FORMAT}\n"
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
    brain.write("evals/badschema.yaml", f"version: {FORMAT}\ncases: not-a-list\n".encode())
    brain.write("evals/scalar.json", b"123\n")
    fail_res = query.evaluate(brain, target="failing")
    assert not fail_res["ok"]
    assert fail_res["failed"] == 3
    assert len(fail_res["cases"][0]["reasons"]) == 4
    assert "missing expected text 'zzz-unmatched-query-literal'" in fail_res["cases"][1]["reasons"][0]

    all_res = query.evaluate(brain)
    assert not all_res["ok"]
    assert len(all_res["problems"]) >= 3

    with pytest.raises(InputError, match="no eval file matching"):
        query.evaluate(brain, target="nonexistent-suite")

    # Corrupt stored JSON raises Error (not InputError) in query_jq; script shims preserve HOME/XDG/MISE while keeping $ENV/env empty and stripping secrets; rg exit 1 with stderr fails closed / falls back!
    brain.write("memories/corrupt/bad.json", b"{corrupt\n")
    with pytest.raises(Error, match="parse error:") as exc_info:
        query.query_jq(brain, ".ref", scope="corrupt")
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
