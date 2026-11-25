"""Tests for `bf read`, SQLite FTS5 BM25 + JSONL fallback `bf query`, alias lookup, and fd-safe `proc.drain_tail`."""

from __future__ import annotations

import fcntl
import io
import json
import os
import resource
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from bf import engine, index, markdown, models, proc, query, records, state
from bf import status as status_m
from bf.models import FORMAT, NOTES_FILE, Error, InputError, terminal
from bf.storage import Store


def test_read_target_urls_anchors_cross_brain_aliases_and_allowlist(tmp_path: Path, brain: Store) -> None:
    with pytest.raises(InputError, match="target is required"):
        query.read_target(brain, "")
    with pytest.raises(InputError, match="format must be"):
        query.read_target(brain, "projects/offline.md", fmt="xml")

    # 1. Markdown note section slicing by heading `#anchor` (JSON and raw).
    sec = query.read_target(brain, "bf:///projects/offline.md#decision")
    assert sec["ok"] is True
    assert sec["kind"] == "section"
    assert sec["heading"]["title"] == "Decision"
    assert "## Decision" in sec["text"]
    assert "## Next actions" not in sec["text"]
    assert "record" not in sec
    raw_sec = query.read_target(brain, "bf:///projects/offline.md#decision", fmt="raw")
    assert isinstance(raw_sec, str)
    assert "## Decision" in raw_sec
    assert "## Next actions" not in raw_sec

    # Extensionless note target resolves cleanly on disk!
    ext_sec = query.read_target(brain, "projects/offline#decision")
    assert ext_sec["heading"]["title"] == "Decision"

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

    # Auto-anchored headings with underscores, inline emphasis, HTML entities, Setext underlines, and reference links!
    brain.write(
        "concepts/setext.md",
        b"---\ntype: guide\nstatus: stable\n---\n# Top\n\n"
        b"## Cost_benefit & *risk*\n\nInline body.\n\n"
        b"Setext Section\n--------------\n\nSetext body.\n\n"
        b"## [MCP][m]\n\nRef body.\n\n"
        b"## Tail\n\nTail body.\n\n[m]: https://example.com\n",
    )
    engine.build(brain)
    inline_sec = query.read_target(brain, "concepts/setext.md#cost_benefit-risk")
    assert "Inline body." in inline_sec["text"]
    assert "Setext body." not in inline_sec["text"]
    setext_sec = query.read_target(brain, "concepts/setext.md#setext-section")
    assert "Setext body." in setext_sec["text"]
    assert "Ref body." not in setext_sec["text"]
    assert "Ref body." in query.read_target(brain, "concepts/setext.md#mcp")["text"]

    # Allowlist: only configured `cfg.notes` folders (including bundle attachments) are readable; root/private files fail closed!
    brain.write("actions/att/index.md", b"---\ntype: action\nstatus: open\ncreated: 2026-11-16\n---\n# Att\n")
    brain.write("actions/att/outputs/draft.md", b"# Draft Output\n")
    assert query.read_target(brain, "actions/att/outputs/draft.md")["kind"] == "file"
    assert query.read_target(brain, "actions/att/outputs/draft.md", fmt="raw") == "# Draft Output\n"
    with pytest.raises(Error, match="missing anchor"):
        query.read_target(brain, "actions/att/outputs/draft.md#head")

    for blocked in ("logs/events.jsonl", "Logs/events.jsonl", ".vale.ini", "AGENTS.md", "bf.yaml", ".env"):
        with pytest.raises((InputError, Error)):
            query.read_target(brain, blocked)

    with pytest.raises(Error, match="missing anchor #nope"):
        query.read_target(brain, "projects/offline.md#nope")

    # 2. Sensor record by `source:id`, plain & `urn:` alias lookup, `--format raw`, and `#fragment` rejection!
    rec = query.read_target(brain, "meetings:decision-1")
    assert rec["ok"] is True
    assert rec["ref"] == "meetings:decision-1"
    assert rec["kind"] == "memory"
    assert rec["path"] == "memories/meetings.jsonl"
    assert rec["record"]["title"] == "Preserve durable evidence"
    assert "offline JSONL retrieval" in query.read_target(brain, "meetings:decision-1", fmt="raw")
    with pytest.raises(InputError, match="JSON records do not support #fragments"):
        query.read_target(brain, "meetings:decision-1#.title")

    records.ingest(
        brain,
        "repos",
        json.dumps(
            [
                {"id": "r1", "title": "Brain Repo", "text": "Core repo", "aliases": ["repo:fmind/bf", "brain-core"]},
                {"id": "org/.github/issues/1", "title": "Dot Repo Issue"},
            ]
        ).encode(),
    )
    assert query.read_target(brain, "repos:org/.github/issues/1")["record"]["title"] == "Dot Repo Issue"
    assert query.read_target(brain, "repo:fmind/bf")["ref"] == "repos:r1"
    assert query.read_target(brain, "urn:repo:fmind/bf")["ref"] == "repos:r1"
    assert query.read_target(brain, "brain-core")["ref"] == "repos:r1"
    assert query.read_target(brain, "brain-core", fmt="raw") == "Core repo"

    # 3. Cross-brain `bf://sib/...` read (notes, percent-encoded paths, records, aliases)!
    sib_root = tmp_path / "sib"
    engine.init_brain(sib_root, name="sib")
    sib = Store(sib_root)
    sib.write(
        "concepts/topic.md",
        b"---\ntype: guide\nstatus: stable\naliases: [repo:acme/demo]\n---\n# Sib Topic\n\n## Core {#core}\n\nHello sib.\n",
    )
    sib.write("concepts/100%25.md", b"---\ntype: guide\nstatus: stable\n---\n# Literal Percent\n")
    engine.build(sib)
    brain.write("bf.yaml", f"version: {FORMAT}\nname: fixture\nbrains:\n  sib: {sib_root}\n".encode())

    cross = query.read_target(brain, "bf://sib/concepts/topic#core")
    assert cross["brain"] == "sib"
    assert "Hello sib." in cross["text"]
    cross_pct = query.read_target(brain, "bf://sib/concepts/100%2525.md")
    assert cross_pct["brain"] == "sib"
    assert cross_pct["record"]["title"] == "Literal Percent"
    assert query.read_target(brain, "bf://sib/repo:acme/demo")["ref"] == "concepts/topic.md"

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
    assert query.read_target(brain, "bf://sib/concepts/topic.md")["record"]["title"] == "Sib Topic"
    with pytest.raises(InputError, match="JSON records do not support #fragments"):
        query.read_target(brain, "bf://sib/docs:a%2Fb#.title")

    for bad, exc in (
        ("https://example.com", InputError),
        ("bf://bad/no-such.md", Error),
        ("concepts/missing.md", Error),
        ("meetings:missing", Error),
        ("nonexistent-plain-alias", Error),
    ):
        with pytest.raises(exc):
            query.read_target(brain, bad)


def test_query_fts5_ranking_filters_accent_folding_and_readonly_scan_fallback(brain: Store) -> None:
    # Add rich notes and memories to verify BM25 ranking, 2.5x note boost, accent folding, and structured filters.
    brain.write(
        "concepts/cafe-guide.md",
        b"---\ntype: guide\nstatus: stable\ncreated: 2026-10-01\nupdated: 2026-10-15\ntags: [architecture, paris]\n---\n\n"
        b"# Caf\xc3\xa9 Architecture\n\nDurable offline retrieval with r\xc3\xa9sum\xc3\xa9 search.\n",
    )
    brain.write(
        "projects/api.md",
        b"---\ntype: project\nstatus: active\nupdated: 2026-11-01\ntags: [api]\n---\n# API\n\nneedle-scope\n",
    )
    brain.write(
        "projects/api-v2.md",
        b"---\ntype: project\nstatus: paused\nupdated: 2026-11-02\ntags: [api]\n---\n# API v2\n\nneedle-scope\n",
    )
    records.ingest(
        brain,
        "docs",
        json.dumps(
            [
                {
                    "id": "d-long",
                    "title": "EarlyHead",
                    "type": "doc",
                    "status": "open",
                    "tags": ["noise"],
                    "updated": "2026-09-15T10:00:00Z",
                    "text": ("Café — " * 50) + "deep-centered-needle" + (" omega" * 50),
                },
                {
                    "id": "d-durable",
                    "title": "Durable note competition",
                    "text": "Durable offline retrieval repeated durable durable.",
                    "updated": "2026-11-10T10:00:00Z",
                },
            ]
        ).encode(),
    )
    engine.build(brain)

    # 1. FTS5 BM25 ranked search with note boost, accent folding, snippet centering, and truncation.
    fts_hits = query.query(brain, "durable", limit=1)
    assert fts_hits["ok"] is True
    assert fts_hits["engine"] == "fts5"
    assert len(fts_hits["items"]) == 1
    assert fts_hits["truncated"] is True
    assert fts_hits["items"][0]["kind"] == "note"  # 2.5x note boost ranks notes above memory records!
    assert "score" in fts_hits["items"][0]

    # Accent-folded matching (`cafe resume` matches `Café ... résumé`).
    accent_hit = query.query(brain, "cafe resume", kind="note")
    assert accent_hit["engine"] == "fts5"
    assert [it["ref"] for it in accent_hit["items"]] == ["concepts/cafe-guide.md"]

    # Match-centered snippets on long text (UTF-8 safe, <= 240 chars).
    deep_hit = query.query(brain, "deep-centered-needle", scope="docs")
    assert deep_hit["count"] == 1
    assert "deep-centered-needle" in deep_hit["items"][0]["snippet"]
    assert len(deep_hit["items"][0]["snippet"]) <= 240

    # Scope prefix boundary: `--scope projects/api` does NOT match sibling `projects/api-v2.md`!
    assert [it["ref"] for it in query.query(brain, "needle-scope", scope="projects/api")["items"]] == [
        "projects/api.md"
    ]

    # All structured filters work both in FTS5 mode and in read-only JSONL scan fallback!
    for use_fallback in (False, True):
        if use_fallback:
            brain.delete(f".bf/cache/{index.INDEX_NAME}")
        exp_engine = "scan" if use_fallback else "fts5"

        r_type = query.query(brain, rtype="project", status="active")
        assert r_type["engine"] == exp_engine
        assert all(it["kind"] == "note" for it in r_type["items"])
        assert "projects/api.md" in [it["ref"] for it in r_type["items"]]
        assert "projects/api-v2.md" not in [it["ref"] for it in r_type["items"]]

        r_tag = query.query(brain, tag="architecture")
        assert [it["ref"] for it in r_tag["items"]] == ["concepts/cafe-guide.md"]

        r_since = query.query(brain, kind="note", since="2026-11-01")
        since_refs = {it["ref"] for it in r_since["items"]}
        assert {"projects/api.md", "projects/api-v2.md"} <= since_refs
        assert "concepts/cafe-guide.md" not in since_refs

        r_excl = query.query(brain, "durable", exclude=("docs", "meetings"))
        assert all(it["source"] not in {"docs", "meetings"} for it in r_excl["items"])

        r_mem = query.query(brain, scope="memories", all_items=True)
        assert r_mem["truncated"] is False
        assert r_mem["count"] == 4
        assert query.query(brain, scope="memories/_notes.jsonl", kind="note")["count"] >= 4
        assert query.query(brain, scope="memories/docs.jsonl")["count"] == 2
        assert query.query(brain, "zzz-never-matches-zzz")["items"] == []
        if use_fallback:
            assert r_type["index_stale"] is True
        else:
            assert "index_stale" not in r_type

    # Corrupt index + read-only `.bf/` degrades cleanly to read-only JSONL scan fallback without writing disk!
    brain.write(f".bf/cache/{index.INDEX_NAME}", b"not a sqlite db", mode=0o600)
    (brain.root / ".bf").chmod(0o500)
    try:
        fb = query.query(brain, "cafe", kind="note")
        assert fb["engine"] == "scan"
        assert fb["index_stale"] is True
        assert [it["ref"] for it in fb["items"]] == ["concepts/cafe-guide.md"]
        assert query.find_by_alias(brain, "meetings:nonexistent") is None
        assert query.read_target(brain, "meetings:decision-1")["record"]["title"] == "Preserve durable evidence"
    finally:
        (brain.root / ".bf").chmod(0o700)

    # Validation of bad inputs and conflicting scope/kind pairs.
    for bad_fn in (
        lambda: query.query(brain),
        lambda: query.query(brain, ""),
        lambda: query.query(brain, "x", limit=0),
        lambda: query.query(brain, "x", limit=5001),
        lambda: query.query(brain, "x", kind="usage"),
        lambda: query.query(brain, "x", scope="   "),
        lambda: query.query(brain, "x", scope="../outside"),
        lambda: query.query(brain, "x", scope="nonexistent-source"),
        lambda: query.query(brain, "x", scope="memories", kind="note"),
        lambda: query.query(brain, "x", scope="memories/_notes.jsonl", kind="memory"),
        lambda: query.query(brain, "x", scope="memories/docs.jsonl", kind="note"),
        lambda: query.query(brain, "x", scope="memories/sub/bad"),
        lambda: query.query(brain, "x", scope="docs", kind="note"),
        lambda: query.query(brain, "x", scope="concepts", kind="memory"),
        lambda: query.query(brain, since="not-a-date"),
    ):
        with pytest.raises(InputError):
            bad_fn()


def test_read_modes_max_bytes_unbuilt_hints_and_escaped_json_scan(tmp_path: Path) -> None:
    b_root = tmp_path / "fresh"
    engine.init_brain(b_root, name="fresh")
    st = Store(b_root)

    # 1. `query()` is strictly read-only: unbuilt notes stay unbuilt until explicit `engine.build(st)`.
    st.write(
        "projects/unbuilt.md",
        b"---\ntype: project\nstatus: active\n---\n# Unbuilt\n\nneedle-beta in fresh project.\n",
    )
    unbuilt_q = query.query(st, "needle-beta", kind="note")
    assert unbuilt_q["items"] == []
    assert unbuilt_q["unbuilt_notes"] == ["projects"]
    engine.build(st)
    assert query.query(st, "needle-beta", kind="note")["items"][0]["ref"] == "projects/unbuilt.md"
    assert "unbuilt_notes" not in query.query(st, "needle-beta", kind="note")

    # Editing, adding, deleting, or corrupting compiled notes surfaces `unbuilt_notes` in status and query.
    st.write(
        "concepts/unbuilt2.md",
        b"---\ntype: guide\nstatus: stable\n---\n# Unbuilt Two\n\nneedle-alpha\n",
    )
    assert status_m.status_summary(st)["unbuilt_notes"] == ["concepts"]
    assert query.query(st, "needle-alpha", scope="concepts")["unbuilt_notes"] == ["concepts"]
    engine.build(st)
    assert status_m.status_summary(st)["unbuilt_notes"] == []

    st.delete("projects/unbuilt.md")
    assert status_m.status_summary(st)["unbuilt_notes"] == ["projects"]
    engine.build(st)
    assert status_m.status_summary(st)["unbuilt_notes"] == []

    st.write(NOTES_FILE, b"{corrupt\n")
    assert status_m.status_summary(st)["unbuilt_notes"] == ["actions", "concepts", "projects"]
    engine.build(st)
    assert status_m.status_summary(st)["unbuilt_notes"] == []

    # Fast mtime path skips note hashing when compiled JSONL is strictly newer and matches note count,
    # and detects `mv` renames via `st_ctime_ns`!
    mem_p = b_root / NOTES_FILE
    future_ns = mem_p.stat().st_mtime_ns + 5_000_000_000
    os.utime(mem_p, ns=(future_ns, future_ns))
    assert engine.unbuilt_note_dirs(st, ("concepts", "projects", "actions"), fast=True) == []
    raw_c = st.read(NOTES_FILE)
    st.write(NOTES_FILE, raw_c + raw_c.splitlines(keepends=True)[0])
    os.utime(mem_p, ns=(future_ns, future_ns))
    assert engine.unbuilt_note_dirs(st, ("concepts",), fast=True) == ["concepts"]
    engine.build(st)
    # Moving an older note into `concepts/` updates ctime -> detected by `unbuilt_note_dirs(fast=True)`!
    old_note = tmp_path / "moved-old.md"
    old_note.write_text("---\ntype: guide\nstatus: stable\n---\n# Moved\n")
    old_ns = mem_p.stat().st_mtime_ns - 60_000_000_000
    os.utime(old_note, ns=(old_ns, old_ns))
    old_note.replace(b_root / "concepts" / "moved.md")
    assert "concepts" in engine.unbuilt_note_dirs(st, ("concepts",), fast=True)
    st.delete("concepts/moved.md")
    engine.build(st)

    # 2. `read_target` `--max-bytes` shrinks fields inside `record` (never dropping `record`) or raises `InputError`.
    st.write(
        "concepts/mb.md",
        ("---\ntype: guide\nstatus: stable\n---\n# MB 🚀 日本語\n\n" + ("🚀日本語éàü " * 20) + "\n").encode(),
    )
    engine.build(st)
    untruncated = query.read_target(st, "concepts/mb.md", max_bytes=10000)
    assert "truncated" not in untruncated
    for budget in (300, 260):
        capped = query.read_target(st, "concepts/mb.md", max_bytes=budget)
        assert capped["truncated"] is True
        assert "record" in capped
        assert capped["record"]["ref"] == "concepts/mb.md"
        assert len(terminal(capped)) <= budget
    raw_capped = query.read_target(st, "concepts/mb.md", fmt="raw", max_bytes=35)
    assert len(raw_capped.encode("utf-8")) <= 35
    for too_small in (1, 10, 80, 140):
        with pytest.raises(InputError, match="too small"):
            query.read_target(st, "concepts/mb.md", max_bytes=too_small)
    with pytest.raises(InputError, match="at least 1"):
        query.read_target(st, "concepts/mb.md", max_bytes=0)

    # 3. `read_target` reads dirty notes in-memory even when `.bf/` and `memories/` are `0o500` read-only.
    st.write("concepts/mb.md", b"---\ntype: guide\nstatus: stable\n---\n# MB\n\nDirty2\n")
    (b_root / ".bf").chmod(0o500)
    (b_root / "memories").chmod(0o500)
    try:
        ro_res = query.read_target(st, "concepts/mb.md")
        assert "Dirty2" in ro_res["record"]["text"]
    finally:
        (b_root / ".bf").chmod(0o700)
        (b_root / "memories").chmod(0o700)

    # 4. Extensionless bundle resolution for `index.md` and flat/bundle collision!
    st.write("actions/act/index.md", b"---\ntype: action\nstatus: open\ncreated: 2026-10-01\n---\n# Act One\n")
    st.write("projects/bnd/index.md", b"---\ntype: project\nstatus: active\n---\n# Bundle P\n")
    engine.build(st)
    assert query.read_target(st, "actions/act")["ref"] == "actions/act/index.md"
    assert query.read_target(st, "projects/bnd")["ref"] == "projects/bnd/index.md"

    st.write("concepts/a.md", b"---\ntype: guide\nstatus: stable\n---\n# A\n")
    st.write("concepts/a/index.md", b"---\ntype: guide\nstatus: stable\n---\n# Collide\n")
    with pytest.raises(Error, match="collides with"):
        markdown.authored_notes(st)
    st.delete("concepts/a/index.md")

    # 5. Raw-byte fallback alias scan handles RFC 8259 `\uXXXX` and `\/` escapes and propagates `MAX_TOTAL_FILES`.
    st.write(
        "memories/escaped.jsonl",
        b'{"id":"caf\\u00e9","ref":"escaped:caf\\u00e9","source":"escaped","kind":"memory","title":"Cafe","text":"",'
        b'"aliases":["https:\\/\\/example.com\\/caf\\u00e9"]}\n',
    )
    st.delete(f".bf/cache/{index.INDEX_NAME}")
    assert query.read_target(st, "escaped:café")["ref"] == "escaped:café"
    assert query.find_by_alias(st, "https://example.com/café") is not None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(models, "MAX_TOTAL_FILES", 1)
        with pytest.raises(Error, match="total scan limit"):
            query.find_by_alias(st, "no-such-alias")
    st.delete("memories/escaped.jsonl")
    shutil.rmtree(b_root / "memories")
    assert query.query(st, "anything", kind="memory")["items"] == []


def test_proc_drain_tail_os_dup_redaction_and_high_fd() -> None:
    # 1. Bounded tail + incremental secret redaction before eviction (prevents boundary-cut secret leaks!).
    tail_t, tail_snap = proc.drain_tail(
        io.BytesIO(b"prefix line\nBearer top_secret_value_12345\n" + b"x" * 2000 + b"\nTAIL\n"),
        32,
        secrets=("top_secret_value_12345",),
    )
    tail_t.join(timeout=2)
    out_tail = tail_snap()
    assert "TAIL" in out_tail
    assert "top_secret_value_12345" not in out_tail

    # Incremental redaction on a single line directly in the tail window:
    red_t, red_snap = proc.drain_tail(
        io.BytesIO(b"leak exact_custom_secret_value_99\n"),
        64,
        secrets=("exact_custom_secret_value_99",),
    )
    red_t.join(timeout=2)
    assert red_snap() == "leak [REDACTED]"

    # P0 regression: oversized single line (>64 KiB) with secret spanning or after boundary NEVER leaks unredacted secret!
    over_marker = "OVERSIZE_EXACT_VALUE_99887766"
    over_payload = (b"a" * 65_520) + over_marker.encode() + b" Bearer tok_oversize_val_12345\nAFTER_OVER\n"
    over_seen: list[str] = []
    ov_t, ov_snap = proc.drain_tail(
        io.BytesIO(over_payload),
        128,
        on_line=lambda raw: (over_seen.append(raw.decode("utf-8", "replace")), raw.decode("utf-8", "replace"))[1],
        secrets=(over_marker,),
    )
    ov_t.join(timeout=2)
    ov_joined = "\n".join(over_seen) + "\n" + ov_snap()
    assert over_marker not in ov_joined
    assert "OVERSIZE_EXACT_" not in ov_joined
    assert "tok_oversize_val_12345" not in ov_joined
    assert "AFTER_OVER" in ov_snap()

    # 2. Real pipe `drain_tail` duplicates fd via `os.dup`, captures `< 8192` bytes, and exits cleanly even with grandchild holding stderr!
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
        gt, gsnap = proc.drain_tail(gc_proc.stderr, 64, stop_err)
        gc_proc.wait(timeout=5)
        stop_err.set()
        gt.join(timeout=2)
        assert not gt.is_alive()
        assert "boom" in gsnap()
        proc.stop_proc(gc_proc)

    # 3. `drain_tail` uses `select.poll()` and handles fds >= 1024 (`FD_SETSIZE`) cleanly!
    # (Dedicated test below so low-RLIMIT_NOFILE environments explicitly skip instead of silently green-washing.)


def test_drain_tail_supports_high_fd_above_1024(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft <= 1030:
        pytest.skip("RLIMIT_NOFILE <= 1030")
    dup_cmd = getattr(fcntl, "F_DUPFD_CLOEXEC", fcntl.F_DUPFD)
    duped_fds: list[int] = []

    def _high_dup(fd: int) -> int:
        hi = fcntl.fcntl(fd, dup_cmd, 1025)
        duped_fds.append(hi)
        return hi

    r_fd, w_fd = os.pipe()
    high_fd = -1
    try:
        high_fd = fcntl.fcntl(r_fd, dup_cmd, 1025)
        os.close(r_fd)
        r_fd = -1
        stop_ev = threading.Event()
        with (
            monkeypatch.context() as mp,
            os.fdopen(high_fd, "rb", buffering=0) as high_stream,
        ):
            high_fd = -1
            mp.setattr(os, "dup", _high_dup)
            ht, hsnap = proc.drain_tail(high_stream, 64, stop_ev)
            assert duped_fds
            assert all(fd >= 1024 for fd in duped_fds)
            os.write(w_fd, b"hello-high-fd\n")
            dl = time.monotonic() + 3.0
            while time.monotonic() < dl and hsnap() != "hello-high-fd":
                time.sleep(0.02)
            assert hsnap() == "hello-high-fd"
            # Keep `w_fd` open and prove `select.poll()` exits cleanly on `stop_ev` for fd >= 1024!
            stop_ev.set()
            ht.join(timeout=2)
            assert not ht.is_alive()
    finally:
        for fd_i in (r_fd, w_fd, high_fd):
            if fd_i >= 0:
                os.close(fd_i)


def test_query_snippets_headings_tasks_and_plain_alias_raw_note(brain: Store) -> None:
    # Snippet matching inside heading title and task text returns the matching anchor/section!
    brain.write(
        "concepts/snip-test.md",
        b"---\ntype: guide\nstatus: stable\naliases: [plain-note-alias]\n---\n\n"
        b"# Overview\n\n## Quantum Heading {#quantum}\n\nIntro.\n\n- [ ] Calibrate photonics sensor\n",
    )
    engine.build(brain)
    h_hit = query.query(brain, "Quantum Heading", scope="concepts/snip-test.md")
    assert h_hit["items"][0]["anchor"] == "quantum"
    t_hit = query.query(brain, "photonics", scope="concepts/snip-test.md")
    assert t_hit["items"][0]["anchor"] == "quantum"
    assert "photonics" in t_hit["items"][0]["snippet"]
    # Plain alias pointing to an authored note supports `fmt="raw"`, `max_bytes`, AND `<alias>#anchor`!
    raw_al = query.read_target(brain, "plain-note-alias", fmt="raw", max_bytes=30)
    assert isinstance(raw_al, str)
    assert len(raw_al.encode("utf-8")) <= 30
    json_al = query.read_target(brain, "plain-note-alias", max_bytes=280)
    assert json_al["ref"] == "concepts/snip-test.md"
    assert json_al["truncated"] is True
    assert "record" in json_al
    al_sec = query.read_target(brain, "plain-note-alias#quantum")
    assert al_sec["kind"] == "section"
    assert al_sec["ref"] == "concepts/snip-test.md"
    assert "Quantum Heading" in al_sec["text"]
    # Punctuation-only query returns empty result cleanly in FTS5 mode!
    assert query.query(brain, "!!!")["items"] == []

    # Scope & scan-fallback filter edges, snippet fallback, and raw/section byte capping!
    with pytest.raises(InputError, match="scope must be nonempty"):
        query.query(brain, "x", scope="   ")
    with pytest.raises(InputError, match="unknown memory source"):
        query.query(brain, "x", scope="memories/no-such.jsonl")
    brain.write("concepts/sub/deep.md", b"---\ntype: guide\nstatus: stable\n---\n# Deep\n\n" + (b"word " * 80) + b"\n")
    engine.build(brain)
    # Remove SQLite index to force `_scan_fallback` across kind/source/exclude/ref_prefix filters.
    brain.delete(f".bf/cache/{index.INDEX_NAME}")
    sf1 = query.query(brain, None, scope="concepts/sub", exclude=["meetings"])
    assert sf1["engine"] == "scan"
    assert [it["ref"] for it in sf1["items"]] == ["concepts/sub/deep.md"]
    sf2 = query.query(brain, "word", kind="note", exclude=["actions"])
    assert sf2["engine"] == "scan"
    assert any(it["ref"] == "concepts/sub/deep.md" for it in sf2["items"])
    sf3 = query.query(brain, None, kind="memory", scope="meetings")
    assert all(it["kind"] == "memory" for it in sf3["items"])
    # Snippet fallback when long description/text does not contain query term, and raw read within budget.
    snip_none, anc_none = query._extract_snippet({"description": "x " * 200}, "nomatch", width=40)  # noqa: SLF001
    assert len(snip_none) == 40
    assert anc_none == ""
    raw_full = query.read_target(brain, "concepts/snip-test.md", fmt="raw", max_bytes=10_000)
    assert "# Overview" in raw_full
    sec_cap = query.read_target(brain, "concepts/snip-test.md#quantum", max_bytes=150)
    assert sec_cap["truncated"] is True

    # Engine `build` / `validate` / `review` defensive branches:
    # bad dir schema, reserved `schemas/record.json`, deleting all notes, corrupt forgotten, review errors & fallback!
    ed_root = brain.root.parent / "eng-edges"
    engine.init_brain(ed_root, name="eng-edges")
    ed = Store(ed_root)
    ed.write("schemas/concepts.json", b"{not json")
    b_bad = engine.build(ed)
    assert not b_bad["ok"]
    assert any("schemas/concepts.json" in p for p in b_bad["problems"])
    ed.delete("schemas/concepts.json")
    ed.write("schemas/record.json", b'{"type":"object"}')
    assert any("reserved schema name 'record'" in p for p in engine.validate(ed)["problems"])
    ed.delete("schemas/record.json")
    ed.write(
        "concepts/ok.md", b"---\ntype: guide\nstatus: stable\nupdated: 2026-10-01\n---\n# Good Title\n\n[M](docs:m1)\n"
    )
    assert engine.build(ed)["ok"]
    # Corrupt `.forgotten` & invalid record link in validate:
    ed.write("memories/docs.forgotten", b"\xff\xfe")
    v_f = engine.validate(ed)
    assert any("not valid UTF-8" in p for p in v_f["problems"])
    ed.delete("memories/docs.forgotten")
    # Review `limit=0`, corrupt `_notes.jsonl`, and corrupt memory line in fallback scan!
    with pytest.raises(InputError, match="between 1 and 5000"):
        engine.review(ed, limit=0)
    ed.write(
        "memories/docs.jsonl",
        b'{"id":"m1","ref":"docs:m1","source":"docs","kind":"memory","title":"M","text":"","updated":"2026-11-01"}\n{corrupt\n',
    )
    ed.delete(f".bf/cache/{index.INDEX_NAME}")
    rv_fb = engine.review(ed)
    assert any("memories/docs.jsonl:2" in p for p in rv_fb["problems"])
    assert rv_fb["stale"][0]["ref"] == "concepts/ok.md"
    ed.write(NOTES_FILE, b"{corrupt\n")
    assert engine.review(ed)["problems"]
    ed.delete("memories/docs.jsonl")
    # Removing every authored note deletes `memories/_notes.jsonl` cleanly!
    for n_path in markdown.authored_notes(ed):
        ed.delete(n_path)
    b_empty = engine.build(ed)
    assert b_empty["ok"]
    assert not ed.exists(NOTES_FILE)

    # F4: `build()` `already_synced` preserves inode/events, repairs trailing corrupt/non-note lines, and backdates partial failures!
    ed.write("concepts/a.md", b"---\ntype: guide\nstatus: stable\n---\n# Alpha\n")
    ed.write("projects/p.md", b"---\ntype: project\nstatus: active\n---\n# Proj\n")
    assert engine.build(ed)["ok"]
    # Scoped query on one note folder NEVER falsely reports `unbuilt_notes` when sibling note folders exist!
    q_sc = query.query(ed, "Alpha", scope="concepts")
    assert "unbuilt_notes" not in q_sc
    st_before = ed.stat(NOTES_FILE)
    ev_before = state.latest_event_seq(ed)
    b_noop = engine.build(ed)
    assert (b_noop["written"], b_noop["reused"]) == (0, 2)
    assert ed.stat(NOTES_FILE).st_ino == st_before.st_ino
    assert state.latest_event_seq(ed) == ev_before
    # Valid lines followed by trailing `{corrupt\n` or `{}\n` MUST be repaired by `build()`,
    # AND repairing corrupt `_notes.jsonl` when `.bf/cache/index.sqlite3` is absent MUST rebuild FTS5 rows!
    ed.write(NOTES_FILE, ed.read(NOTES_FILE) + b"{corrupt\n")
    ed.delete(f".bf/cache/{index.INDEX_NAME}")
    assert engine.build(ed)["ok"]
    assert b"{corrupt" not in ed.read(NOTES_FILE)
    q_rep = query.query(ed, "Alpha")
    assert q_rep["engine"] == "fts5"
    assert q_rep["count"] == 1
    assert "index_stale" not in q_rep
    ed.write(NOTES_FILE, ed.read(NOTES_FILE) + b"{}\n")
    assert engine.build(ed)["ok"]
    assert b"\n{}\n" not in ed.read(NOTES_FILE)
    # Partial failure corrupting an EXISTING compiled note (keeping line_count == authored_count) alongside a valid write
    # backdates `_notes.jsonl` via `store.utime` so `unbuilt_note_dirs(fast=True)` flags it!
    ed.write("concepts/a.md", b"---\ntype: guide\nstatus: stable\n---\n# Alpha v2\n")
    ed.write("projects/p.md", b"---\nstatus: [\n---\n# Broken\n")
    b_part = engine.build(ed)
    assert not b_part["ok"]
    assert b_part["written"] == 1
    assert ed.stat(NOTES_FILE).st_mtime_ns < ed.stat("projects/p.md").st_mtime_ns
    assert "projects" in engine.unbuilt_note_dirs(ed, ("concepts", "projects", "actions"), fast=True)
    ed.write("projects/p.md", b"---\ntype: project\nstatus: active\n---\n# Proj\n")
    assert engine.build(ed)["ok"]

    # PY-01/BF-03: brain path containing `#` and `?` stays strictly read-only without creating stray files!
    tricky_parent = brain.root.parent / "work#1?v=2"
    tricky_root = tricky_parent / "brain"
    engine.init_brain(tricky_root, name="tricky")
    t_st = Store(tricky_root)
    assert query.query(t_st, "Welcome")["engine"] == "fts5"
    assert status_m.status_summary(t_st)["counts"]["notes"] >= 1
    assert not (brain.root.parent / "work").exists()

    # PY-05 & F8: multi-source O(1) `read_target`, exact `--tag`/`--scope` `_`/`%`/case parity, punctuation fallback,
    # `--links-to`, `search.exclude` + `--all-sources`, date-vs-timestamp `--since`, hyphenated prefix, & `sqlite3.Error` fallback!
    t_st.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: tricky\nsearch:\n  exclude: [noisy]\nloops:\n  empty-loop:\n    run: [echo, '[]']\n    ingest: upsert\n".encode(),
    )
    t_st.write(
        "concepts/day-note.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-11-15\ndescription: Brain-framework guide.\naliases: [guide:bf]\n---\n\n"
        b"# Brain-Framework Guide\n\nExact phrase search works with brain-framework and [alpha](alpha:1).\n",
    )
    engine.build(t_st)
    records.ingest(
        t_st,
        "alpha",
        b'[{"id":"1","title":"A1","tags":["foo_bar","CLI"],"text":"hello exact phrase","links":["bf:///concepts/day-note.md"]},'
        b'{"id":"2","title":"A2","tags":["fooxbar","cli"],"text":"hello wordother"}]',
    )
    records.ingest(t_st, "beta", b'[{"id":"9","title":"B9","text":"beta-rec","links":["urn:guide:bf"]}]')
    records.ingest(t_st, "noisy", b'[{"id":"n1","title":"Noisy","text":"hello noisy-only-term"}]')
    with pytest.MonkeyPatch.context() as mp_rd:
        mp_rd.setattr(query, "_find_record_scan", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("scanned!")))
        assert query.read_target(t_st, "alpha:1")["ref"] == "alpha:1"
        with pytest.raises(Error, match="not found"):
            query.read_target(t_st, "alpha:does-not-exist")
    for delete_idx in (False, True):
        if delete_idx:
            t_st.delete(f".bf/cache/{index.INDEX_NAME}")
        assert [it["ref"] for it in query.query(t_st, None, tag="foo_bar")["items"]] == ["alpha:1"]
        assert [it["ref"] for it in query.query(t_st, None, tag="%")["items"]] == []
        assert [it["ref"] for it in query.query(t_st, None, tag="CLI")["items"]] == ["alpha:1"]
        assert query.query(t_st, "---")["items"] == []
        # `search.exclude` omits `noisy` by default, surfaces `excluded_sources`, and is bypassed by `--all-sources` or explicit `--scope`:
        q_def = query.query(t_st, "noisy-only-term")
        assert q_def["items"] == []
        assert q_def["excluded_sources"] == ["noisy"]
        assert [it["ref"] for it in query.query(t_st, "noisy-only-term", all_sources=True)["items"]] == ["noisy:n1"]
        assert [it["ref"] for it in query.query(t_st, "noisy-only-term", scope="noisy")["items"]] == ["noisy:n1"]
        # Declared empty loop source is accepted by `--scope` with 0 items:
        assert query.query(t_st, None, scope="empty-loop")["items"] == []
        # `--links-to` resolves direct refs, `bf:///`, and alias expansion identically in FTS5 & scan fallback!
        lt_refs = {it["ref"] for it in query.query(t_st, links_to="concepts/day-note.md")["items"]}
        assert lt_refs == {"alpha:1", "beta:9"}
        assert [it["ref"] for it in query.query(t_st, links_to="bf:///alpha:1")["items"]] == ["concepts/day-note.md"]
        # Date-only `2026-11-15` matches `--since 2026-11-15T00:00:00Z`!
        assert any(
            it["ref"] == "concepts/day-note.md"
            for it in query.query(t_st, kind="note", since="2026-11-15T00:00:00Z")["items"]
        )
        # Hyphenated prefix `brain-frame*` and `"exact phrase"` + word-boundary parity across FTS5 & scan!
        assert [it["ref"] for it in query.query(t_st, "brain-frame*")["items"]] == ["concepts/day-note.md"]
        assert {it["ref"] for it in query.query(t_st, '"exact phrase"')["items"]} == {"concepts/day-note.md", "alpha:1"}
        assert query.query(t_st, "word")["items"] == []  # does NOT substring-match `wordother`!
        # Separate `description` vs `snippet` and `type`/`status` on summary rows:
        day_row = query.query(t_st, "brain-frame*")["items"][0]
        assert day_row["description"] == "Brain-framework guide."
        assert day_row["type"] == "guide"
        assert day_row["status"] == "stable"

    # Corrupt FTS table (valid `user_version` so `open_readonly` succeeds, then `search_fts` raises `sqlite3.Error`) falls back cleanly!
    import sqlite3
    from contextlib import closing

    engine.build(t_st)
    with closing(sqlite3.connect(str(t_st.root / ".bf" / "cache" / index.INDEX_NAME))) as con_bad, con_bad:
        con_bad.execute("DROP TABLE fts")
    q_fb = query.query(t_st, "hello")
    assert q_fb["engine"] == "scan"
    assert q_fb["index_stale"] is True
    assert len(q_fb["items"]) == 2
    assert query.read_target(t_st, "alpha:1")["ref"] == "alpha:1"

    # Extra query & index hardening: query caps (>1024 chars / >64 terms), `--links-to` alias/extensionless,
    # `--exclude` normalization/validation, scan top-K pruning (>200 rows), and hydration `IndexStaleError` fallback!
    engine.build(t_st)
    assert {it["ref"] for it in query.query(t_st, links_to="concepts/day-note")["items"]} == {"alpha:1", "beta:9"}
    assert {it["ref"] for it in query.query(t_st, links_to="guide:bf")["items"]} == {"alpha:1", "beta:9"}
    assert {it["ref"] for it in query.query(t_st, links_to="urn:guide:bf")["items"]} == {"alpha:1", "beta:9"}
    assert query.query(t_st, "hello", exclude=["memories/alpha.jsonl"])["items"] == []
    for bad_q in (
        lambda: query.query(t_st, "a" * 1025),
        lambda: query.query(t_st, " ".join(f"w{i}" for i in range(65))),
        lambda: query.query(t_st, rtype="   "),
        lambda: query.query(t_st, "x", exclude=[""]),
        lambda: query.query(t_st, "x", exclude=["BAD_SRC"]),
        lambda: query.query(t_st, links_to="#only"),
    ):
        with pytest.raises(InputError):
            bad_q()

    # Corrupt `file_offset` / `ref` / missing `sources` in SQLite -> `_hydrate_rows` raises `IndexStaleError` -> clean scan fallback!
    db_t = t_st.root / ".bf" / "cache" / index.INDEX_NAME
    for bad_sql in (
        "UPDATE records SET file_offset = 999999 WHERE ref = 'alpha:1'",
        "UPDATE records SET file_offset = 0, byte_len = 5 WHERE ref = 'alpha:1'",
        "DELETE FROM sources WHERE source = 'alpha'",
    ):
        index.sync_all(t_st, "tricky")
        with closing(sqlite3.connect(str(db_t))) as con_m, con_m:
            con_m.execute(bad_sql)
        q_rec = query.query(t_st, "exact", scope="alpha")
        assert q_rec["engine"] == "scan"
        assert [it["ref"] for it in q_rec["items"]] == ["alpha:1"]

    # Scan fallback top-K pruning (>200 rows) keeps bounded memory for both ranked and structured queries!
    bulk = [{"id": f"k{i:03d}", "title": f"Item {i:03d}", "text": "bulkterm"} for i in range(215)]
    records.ingest(t_st, "bulk", json.dumps(bulk).encode())
    t_st.delete(f".bf/cache/{index.INDEX_NAME}")
    q_bulk_txt = query.query(t_st, "bulkterm", scope="bulk", limit=5)
    assert (q_bulk_txt["engine"], len(q_bulk_txt["items"]), q_bulk_txt["truncated"]) == ("scan", 5, True)
    q_bulk_flt = query.query(t_st, None, scope="bulk", limit=5)
    assert (q_bulk_flt["engine"], len(q_bulk_flt["items"]), q_bulk_flt["truncated"]) == ("scan", 5, True)

    # FTS5 vs scan parity: underscore splitting (`stale_after`), NFD Latin (`re\u0301sume\u0301`), Korean Hangul, & Japanese dakuten!
    records.ingest(
        t_st,
        "parity",
        json.dumps([{"id": "p1", "title": "Résumé 한글 データ", "text": "check stale_after config"}]).encode(),
    )
    for q_str in ("stale", '"stale after"', "stale*", "re\u0301sume\u0301", "한글", "データ"):
        r_fts = query.query(t_st, q_str, scope="parity")
        assert (r_fts["engine"], [i["ref"] for i in r_fts["items"]]) == ("fts5", ["parity:p1"]), q_str
    assert query.query(t_st, "テータ", scope="parity")["items"] == []
    t_st.delete(f".bf/cache/{index.INDEX_NAME}")
    for q_str in ("stale", '"stale after"', "stale*", "re\u0301sume\u0301", "한글", "データ"):
        r_scan = query.query(t_st, q_str, scope="parity")
        assert (r_scan["engine"], [i["ref"] for i in r_scan["items"]]) == ("scan", ["parity:p1"]), q_str
    assert query.query(t_st, "テータ", scope="parity")["items"] == []

    # Empty brain with 0 notes, 0 memories, and no SQLite cache reports `index_stale: False`!
    empty_b = t_st.root.parent / "zero-brain"
    engine.init_brain(empty_b, name="zero-brain")
    eb = Store(empty_b)
    for n_p in markdown.authored_notes(eb):
        eb.delete(n_p)
    assert engine.build(eb)["ok"] is True
    eb.delete(f".bf/cache/{index.INDEX_NAME}")
    from bf import status as st_mod

    assert "index_stale" not in st_mod.status_summary(eb)

    # Disabled partition loop (`gmail-2025` sharing `ingest.schema: gmail`) STILL validates against `schemas/gmail.json` in `bf check`!
    eb.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: zero-brain\nloops:\n  gmail-2025:\n    enabled: false\n    ingest: {{source: gmail-2025, schema: gmail}}\n    run: [sensors/archived.py]\n".encode(),
    )
    eb.write(
        "schemas/gmail.json",
        b'{"type":"object","properties":{"attributes":{"type":"object","required":["thread_id"]}},"required":["attributes"]}\n',
    )
    eb.write(
        "memories/gmail-2025.jsonl",
        b'{"id":"m1","ref":"gmail-2025:m1","source":"gmail-2025","kind":"memory","title":"Old Mail"}\n',
    )
    v_dis = engine.validate(eb)
    assert v_dis["ok"] is False
    assert any("attributes" in p for p in v_dis["problems"])

    # Folder-level `bad_schema_dirs` failure still backdates `_notes.jsonl` when a sibling folder writes!
    eb.write("bf.yaml", f"version: {FORMAT}\nname: zero-brain\n".encode())
    eb.delete("memories/gmail-2025.jsonl")
    eb.write("concepts/c1.md", b"---\ntype: guide\nstatus: stable\n---\n# C1\n")
    eb.write("projects/p1.md", b"---\ntype: project\nstatus: active\n---\n# P1\n")
    assert engine.build(eb)["ok"] is True
    eb.write("schemas/concepts.json", b"{broken json")
    eb.write("concepts/c1.md", b"---\ntype: guide\nstatus: stable\n---\n# C1 Edited\n")
    eb.write("projects/p1.md", b"---\ntype: project\nstatus: active\n---\n# P1 Edited\n")
    b_part = engine.build(eb)
    assert b_part["ok"] is False
    eb.delete("schemas/concepts.json")
    assert "concepts" in engine.unbuilt_note_dirs(eb, ("concepts", "projects", "actions"), fast=True)

    # BF-006: Empty YAML frontmatter fields `created:`, `updated:`, `url:` omit `None` cleanly!
    eb.write(
        "concepts/null-opt.md",
        b"---\ntype: guide\nstatus: stable\ncreated:\nupdated:\nurl:\n---\n# Null Opt\n",
    )
    from bf import schemas as bf_schemas

    comp_null = markdown.compile_note("concepts/null-opt.md", eb.read("concepts/null-opt.md"), brain_name="zero-brain")
    assert all(k not in comp_null for k in ("created", "updated", "url"))
    assert bf_schemas.validate_core_record(comp_null, "concepts/null-opt.md") == []
    assert engine.build(eb)["ok"] is True
    assert all(k not in query.read_target(eb, "concepts/null-opt.md")["record"] for k in ("created", "updated", "url"))
