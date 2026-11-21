"""Tests for init_brain, pure JSON build, validate, and review."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bf import engine, records
from bf.models import FORMAT, Error, InputError, encode
from bf.storage import Store


def test_init_brain_build_sync_and_stale_note_pruning(tmp_path: Path) -> None:
    created = engine.init_brain(tmp_path / "123--My Brain!!")
    assert created["brain"] == "brain-123-my-brain"
    root = Path(created["path"])
    store = Store(root)
    assert (root / ".agents" / "skills" / "bf").is_symlink()
    assert (root / "memories" / "concepts" / "welcome.json").is_file()
    assert not (root / "docs").exists()
    assert (root / "schemas" / "concepts.json").is_file()
    for removed_schema in ("bf.schema.json", "record.json", "evals.json"):
        assert not (root / "schemas" / removed_schema).exists()
    assert (root / "evals" / "smoke.yaml").is_file()
    for folder in ("projects", "concepts", "actions", "memories", "logs", "schemas"):
        assert (root / folder).is_dir()
    for deferred in ("sensors", "routines", "services", "channels", "prompts", "settings"):
        assert not (root / deferred).exists()
    assert engine.validate(store)["ok"]
    assert engine.build(store)["ok"]

    # Plain Markdown under actions/ builds like projects/ and concepts/.
    store.write(
        "actions/2026-11-15_ship-pure-json/ACTION.md", b"---\ntype: action\nstatus: draft\n---\n\n# Ship pure JSON\n"
    )
    built_action = engine.build(store)
    assert built_action["ok"]
    assert (root / "memories" / "actions" / "2026-11-15_ship-pure-json.json").is_file()
    assert engine.validate(store)["ok"]

    # `validate` is strictly read-only: it detects unbuilt, orphaned, out-of-sync, and corrupt compiled JSONs without writing!
    raw_welcome = store.read("concepts/welcome.md")
    store.write("concepts/unbuilt.md", b"---\ntype: guide\nstatus: draft\n---\n\n# Unbuilt\n")
    val_unbuilt = engine.validate(store)
    assert not val_unbuilt["ok"]
    assert any("missing compiled note for concepts/unbuilt.md" in p for p in val_unbuilt["problems"])
    assert not store.exists("memories/concepts/unbuilt.json")
    store.delete("concepts/unbuilt.md")
    assert engine.validate(store)["ok"]

    store.write("memories/concepts/orphan.json", b'{"ref":"concepts/orphan.md"}\n')
    val_orphan = engine.validate(store)
    assert not val_orphan["ok"]
    assert any("orphaned compiled note" in p for p in val_orphan["problems"])
    assert store.exists("memories/concepts/orphan.json")
    engine.build(store)
    assert not store.exists("memories/concepts/orphan.json")
    assert engine.validate(store)["ok"]

    compiled_bytes = store.read("memories/concepts/welcome.json")
    store.write("concepts/welcome.md", raw_welcome + b"\nEdited without rebuild.\n")
    assert any("out of sync with concepts/welcome.md" in p for p in engine.validate(store)["problems"])
    store.write("memories/concepts/welcome.json", b"{corrupt\n")
    assert any("invalid JSON" in p for p in engine.validate(store)["problems"])
    store.write("concepts/welcome.md", raw_welcome)
    store.write("memories/concepts/welcome.json", compiled_bytes)
    assert engine.validate(store)["ok"]

    # Tampered compiled JSON with matching `sha256` is missed by incremental build, caught by validate, and fixed by force=True!
    tampered = json.loads(store.read("memories/actions/2026-11-15_ship-pure-json.json"))
    tampered["title"] = "Tampered Title"
    store.write("memories/actions/2026-11-15_ship-pure-json.json", encode(tampered))
    assert engine.build(store, incremental=True)["written"] == 0
    assert any(
        "out of sync with actions/2026-11-15_ship-pure-json/ACTION.md" in p for p in engine.validate(store)["problems"]
    )
    assert engine.build(store, force=True)["written"] == 1
    assert engine.validate(store)["ok"]

    # Deleting a note prunes its compiled JSON file on next `bf build`!
    store.write("projects/temp.md", b"---\ntype: project\nstatus: draft\n---\n# Temp\n")
    engine.build(store)
    assert store.exists("memories/projects/temp.json")
    store.delete("projects/temp.md")
    built_after = engine.build(store)
    assert built_after["removed"] == 1
    assert not store.exists("memories/projects/temp.json")

    with pytest.raises(Error, match="not empty"):
        engine.init_brain(root)

    file_dst = tmp_path / "not-a-dir"
    file_dst.write_text("x")
    with pytest.raises(Error, match="not a directory"):
        engine.init_brain(file_dst)

    with pytest.raises(InputError, match="lowercase letter"):
        engine.init_brain(tmp_path / "bad-name", name="Bad_Name")
    assert not (tmp_path / "bad-name").exists()

    dangling = tmp_path / "dangling-link"
    dangling.symlink_to(tmp_path / "missing-target")
    with pytest.raises(Error, match="not a directory"):
        engine.init_brain(dangling)
    empty_real = tmp_path / "empty-real"
    empty_real.mkdir()
    sym_dir = tmp_path / "sym-empty-dir"
    sym_dir.symlink_to(empty_real)
    with pytest.raises(Error, match="not a directory"):
        engine.init_brain(sym_dir)

    # `load_or_compile_note` hits cache for same-brain `bf:///` frontmatter links and bypasses for uppercase `BF://<host>`!
    store.write(
        "concepts/norm-uri.md", b"---\ntype: guide\nstatus: stable\nlinks:\n  - bf:///concepts/welcome.md\n---\n# N\n"
    )
    store.write(
        "concepts/up-uri.md",
        b"---\ntype: guide\nstatus: stable\nlinks:\n  - BF://brain-123-my-brain/concepts/welcome.md\n---\n# U\n",
    )
    engine.build(store)
    _, hit_norm = engine.load_or_compile_note(
        store, "concepts/norm-uri.md", store.read("concepts/norm-uri.md"), brain_name="brain-123-my-brain"
    )
    assert hit_norm is not None
    comp_up, hit_up = engine.load_or_compile_note(
        store, "concepts/up-uri.md", store.read("concepts/up-uri.md"), brain_name="renamed"
    )
    assert hit_up is None
    assert comp_up["links"][0]["kind"] == "brain"

    # Tampered compiled note JSON with matching `sha256` is still caught by `validate`!
    tampered = json.loads(store.read("memories/concepts/norm-uri.json"))
    tampered["status"] = "invalid-status"
    store.write("memories/concepts/norm-uri.json", encode(tampered))
    assert any(
        "memories/concepts/norm-uri.json" in p or "concepts/norm-uri.md" in p
        for p in engine.validate(store)["problems"]
    )
    engine.build(store, force=True)
    assert engine.validate(store)["ok"]


def test_validate_catches_schema_link_anchor_memory_log_eval_and_cross_brain_errors(
    tmp_path: Path, brain: Store
) -> None:
    assert engine.validate(brain)["ok"]

    bad_root = tmp_path / "bad-cfg"
    bad_root.mkdir()
    (bad_root / "bf.yaml").write_text("version: 99\nname: bad\n")
    invalid_cfg = engine.validate(Store(bad_root))
    assert not invalid_cfg["ok"]
    assert "version 99" in invalid_cfg["problems"][0]

    # Sibling brain for cross-brain link checks.
    sib_root = tmp_path / "sib"
    engine.init_brain(sib_root, name="sib")
    sib = Store(sib_root)
    records.ingest(sib, "events", b'[{"id":"ev-1","title":"Event 1","aliases":["repo:acme/demo"]}]')
    sib.write("memories/corrupt/bad.json", b"{bad\n")
    sib.write("concepts/shared.md", b"---\ntype: guide\nstatus: stable\n---\n\n# Shared\n\n## Known {#known}\n")
    sib.write("concepts/badutf.md", b"\xff\xfe")
    engine.build(sib)

    brain.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: fixture\nbrains:\n  sib: {sib_root}\n  missing-sib: {tmp_path / 'nope'}\n".encode(),
    )

    # Schema, .env, and eval errors (`logs/` holds plain JSON line logs and is not schema-validated).
    brain.write(".env", b'BAD_QUOTE="unclosed\\"\n', mode=0o600)
    brain.write("schemas/nested/extra.json", b"{}")
    brain.write("schemas/broken.json", b'{"type": "nope"}')
    brain.write("schemas/projects.json", json.dumps({"type": "object", "required": ["owner"]}).encode())
    brain.write("concepts/incomplete.md", b"---\ntype: guide\n---\n\n# Missing status\n")
    brain.write("evals/bad.yaml", f"version: {FORMAT}\ncases: not-a-list\n".encode())
    brain.write("evals/stray.txt", b"text")
    brain.write("evals/scalar.json", b"123\n")
    brain.write("evals/corrupt.json", b"{bad")

    # Attachment Markdown file for non-OKF anchor checking.
    brain.write("actions/2026-11-15_demo/ACTION.md", b"---\ntype: action\nstatus: draft\n---\n\n# Demo\n")
    brain.write("actions/2026-11-15_demo/outputs/report.md", b"# Report\n\n## Summary {#summary}\n")
    brain.write("actions/2026-11-15_demo/outputs/bad.md", b"\xff\xfe")

    # Pure JSON memory errors: root file in memories/, reserved source dir, corrupt json, duplicate id + mismatched source/ref.
    brain.write("memories/concepts/stray.txt", b"bad")
    brain.write("memories/repos/.forgotten", b"ignored\n")
    brain.write("memories/scalar/s.json", b"123\n")
    brain.write("memories/stray.json", b"{}\n")
    brain.write("memories/notes/bad.json", b"{}\n")
    brain.write("memories/corrupt/c.json", b"{broken\n")
    dupe_obj = json.dumps(
        {
            "id": "d1",
            "ref": "wrong:d1",
            "source": "wrong",
            "kind": "memory",
            "title": "Dupe",
            "text": "",
            "tags": [],
            "links": [],
        }
    ).encode()
    brain.write("memories/dupes/a.json", dupe_obj)
    brain.write("memories/dupes/b.json", dupe_obj)
    brain.write(
        "memories/relbad/r.json",
        json.dumps(
            {
                "id": "r1",
                "ref": "relbad:r1",
                "source": "relbad",
                "kind": "memory",
                "title": "Bad relations and legacy links",
                "text": "",
                "tags": [],
                "links": [
                    {"to": "concepts:welcome", "kind": "record", "anchor": "", "meta": {}, "section": ""},
                    {"to": "projects:bad", "kind": "note", "anchor": "", "meta": {}, "section": ""},
                    {"to": "projects/../../etc/passwd", "kind": "note", "anchor": "", "meta": {}, "section": ""},
                    {"to": "javascript:alert(1)", "kind": "url", "anchor": "", "meta": {}, "section": ""},
                    {"to": "bf://sib/concepts:bad", "kind": "brain", "anchor": "", "meta": {}, "section": ""},
                    {
                        "to": records.record_file("meetings", "decision-1"),
                        "kind": "record",
                        "anchor": "frag",
                        "meta": {},
                        "section": "",
                    },
                ],
                "relations": [],
            }
        ).encode(),
    )
    brain.write(
        "memories/wrongkind/k.json",
        json.dumps(
            {"id": "k1", "ref": "wrongkind:k1", "source": "wrongkind", "kind": "concept", "title": "Wrong kind"}
        ).encode(),
    )

    # Local record so `memories/repos/<sha256>.json` link checks validate!
    records.ingest(brain, "repos", b'[{"id":"r1","title":"Local repo","aliases":["repo:local/one"]}]')

    brain.write("projects/badstale.md", b"---\ntype: project\nstatus: draft\nowner: alice\nstale_after: -5\n---\n# S\n")
    brain.write(
        "projects/badstaledur.md", b"---\ntype: project\nstatus: draft\nowner: alice\nstale_after: nope\n---\n# S2\n"
    )
    brain.write(
        "projects/hugestale.md", b"---\ntype: project\nstatus: draft\nowner: alice\nstale_after: 999y\n---\n# S3\n"
    )
    brain.write(
        "projects/links.md",
        b"---\ntype: project\nstatus: draft\nowner: alice\nstale_after: 30d\nentity: org:acme\naliases: [proj:links]\n---\n\n"
        b"# Link Matrix\n\n"
        b"- [External](https://example.com)\n"
        b"- [Good memory](../memories/repos/82f3e9c695dc6b8d1b11818d5701919e286de8d47f7c3eb3100c485f79e57828.json)\n"
        b"- [Bad json frag](../memories/projects/offline.json#.title)\n"
        b"- [Missing memory](../memories/repos/0000000000000000000000000000000000000000000000000000000000000000.json)\n"
        b"- [Good attachment](../actions/2026-11-15_demo/outputs/report.md#summary)\n"
        b"- [Bad attachment anchor](../actions/2026-11-15_demo/outputs/report.md#missing)\n"
        b"- [Unreadable attachment](../actions/2026-11-15_demo/outputs/bad.md#sec)\n"
        b"- [Missing note](../concepts/ghost.md)\n"
        b"- [Missing anchor](../concepts/welcome.md#no-such-heading)\n"
        b"- [Unknown brain](bf://undeclared/concepts/x.md)\n"
        b"- [Good sib note](bf://sib/concepts/shared.md#known)\n"
        b"- [Good sib json](bf://sib/memories/concepts/shared.json)\n"
        b"- [Bad sib anchor](bf://sib/concepts/shared.md#missing)\n"
        b"- [Bad sib note](bf://sib/concepts/missing.md)\n"
        b"- [Bad sib mem](bf://sib/memories/events/0000000000000000000000000000000000000000000000000000000000000000.json)\n"
        b"- [Bad sib json frag](bf://sib/memories/concepts/shared.json#.title)\n"
        b"- [Corrupt sib heading](bf://sib/concepts/badutf.md#sec)\n",
    )

    engine.build(brain)
    report = engine.validate(brain, all_problems=True)
    assert not report["ok"]
    joined = "\n".join(report["problems"])
    for expected in (
        ".env:1: unclosed quote",
        "schemas/nested/extra.json: schemas/ holds only flat",
        "schemas/broken.json: invalid JSON Schema",
        "projects/offline.md: missing required 'owner'",
        "projects/badstale.md:",
        "projects/badstaledur.md:",
        "projects/hugestale.md: stale_after",
        "concepts/incomplete.md: missing required 'status'",
        "evals/bad.yaml:",
        "evals/stray.txt: evals/ holds .yaml or .json files",
        "evals/scalar.json: expected a mapping",
        "evals/corrupt.json: invalid JSON",
        "memories/concepts/stray.txt: compiled note memories must be .json files",
        "memories/scalar/s.json: expected a JSON object",
        "memories/stray.json: memories/ holds <source>/<sha256>.json files",
        "memories/notes/bad.json: invalid or reserved source directory",
        "memories/corrupt/c.json: invalid JSON",
        "duplicate record id 'd1' in dupes",
        "memories/dupes/a.json: source must equal 'dupes'",
        "memories/dupes/a.json: ref must equal 'dupes:d1'",
        "memories/relbad/r.json: 'relations' was removed; use 'links'",
        "memories/relbad/r.json: record link 'concepts:welcome' must use memories/<source>/<sha256>.json",
        "memories/relbad/r.json: note link 'projects:bad' must target projects/, concepts/, or actions/",
        "memories/relbad/r.json: note link 'projects/../../etc/passwd' must target projects/, concepts/, or actions/",
        "memories/relbad/r.json: url link 'javascript:alert(1)' must use https://",
        "memories/relbad/r.json: cross-brain link 'bf://sib/concepts:bad' must use bf://<brain>/<path>",
        "memories/wrongkind/k.json: kind must equal 'memory'",
        "bf.yaml brains.missing-sib:",
        "JSON record links do not support #anchor fragments",
        "missing anchor #missing in actions/2026-11-15_demo/outputs/report.md",
        "actions/2026-11-15_demo/outputs/bad.md: note is not UTF-8",
        "broken link to concepts/ghost.md",
        "broken link to memories/repos/0000000000000000000000000000000000000000000000000000000000000000.json",
        "missing anchor #no-such-heading in concepts/welcome.md",
        "unknown brain 'undeclared'",
        "missing cross-brain anchor bf://sib/concepts/shared.md#missing",
        "broken cross-brain link bf://sib/concepts/missing.md",
        "broken cross-brain link bf://sib/memories/events/0000000000000000000000000000000000000000000000000000000000000000.json",
    ):
        assert expected in joined, f"missing expected problem {expected!r} in:\n{joined}"

    from bf import markdown as _md

    assert _md.record_link_problem({"kind": "record", "to": "memories/github/foo\\bar.json", "anchor": ""})
    assert _md.record_link_problem({"kind": "url", "to": "https://example.com\n", "anchor": ""})
    assert _md.record_link_problem({"kind": "brain", "to": "bf://sib\\x/concepts/a.md", "anchor": ""})


def test_review_computes_newer_evidence_due_future_updated_actions_and_tasks(brain: Store) -> None:
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

    brain.write(
        "actions/2026-10-01_old/ACTION.md",
        b"---\ntype: action\nstatus: deprecated\nupdated: 2026-01-01\nstale_after: 1d\n---\n\n# Old\n\n- [ ] Ignored\n",
    )
    brain.write(
        "actions/2026-11-10_active/ACTION.md",
        b"---\ntype: action\nstatus: draft\nupdated: '2026-11-10T12:00:00+02:00'\nsummary: Ship v1\n---\n\n# Ship v1\n\n- [ ] Task A\n",
    )
    brain.write(
        "actions/2026-11-10_later/ACTION.md",
        b"---\ntype: action\nstatus: draft\nupdated: '2026-11-10T10:30:00.500Z'\nsummary: Later UTC\n---\n\n# Later UTC\n",
    )
    brain.write(
        "projects/future.md",
        b"---\ntype: project\nstatus: draft\nupdated: 2026-12-01\nstale_after: 2026-11-01T10:00:00+02:00\n---\n\n# Future\n",
    )
    brain.write(
        "projects/overdue.md",
        b"---\ntype: project\nstatus: stable\nupdated: 2026-10-01\nstale_after: 14d\nentity: bf:///projects/overdue.md\n---\n\n# Overdue\n",
    )
    brain.write(
        "projects/fresh-hours.md",
        b"---\ntype: project\nstatus: stable\nupdated: '2026-11-15T06:00:00Z'\nstale_after: 12h\n---\n\n# Fresh Hours\n",
    )
    brain.write(
        "projects/overflow-dur.md",
        b"---\ntype: project\nstatus: stable\nupdated: 2026-11-15\nstale_after: 9999y\n---\n\n# Overflow Dur\n",
    )
    records.ingest(
        brain,
        "jira",
        json.dumps(
            [
                {
                    "id": "WEB-9",
                    "title": "Launch update",
                    "updated": "2026-11-12T08:00:00Z",
                    "links": ["bf:///projects/offline.md"],
                    "aliases": ["alias:web-9"],
                }
            ]
        ).encode(),
        now=now,
    )

    brain.write(
        "concepts/newer-note.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-11-14\n---\n\n# Newer {#top}\n\n"
        b"See [overdue](../projects/overdue.md), [self](#top), and [repo](https://example.com/acme/web#readme).\n",
    )
    brain.write("memories/broken/b.json", b"{bad\n")
    brain.write("memories/notes/ignored.json", b"{}\n")
    brain.write("memories/scalar/s.json", b"123\n")
    brain.write(
        "memories/rawlinks/r1.json",
        b'{"id":"r1","title":"R","updated":"2026-11-13","links":[{"to":"bf:///projects/overdue.md#top","kind":"note","anchor":"top","meta":{},"section":""}]}\n',
    )

    brain.write(
        "projects/aliased.md",
        b"---\ntype: project\nstatus: stable\nupdated: 2026-11-09\n"
        b"aliases: [repo:acme/web, https://example.com/acme/web]\n---\n\n# Aliased\n\n"
        b"Self reference to <https://example.com/acme/web>.\n",
    )
    brain.write(
        "concepts/retrieved-only.md",
        b"---\ntype: guide\nstatus: stable\nstale_after: 30d\nsources:\n"
        b"  - resource: https://example.com/unretrieved\n"
        b"  - resource: https://example.com/old\n    retrieved: '2026-11-08'\n"
        b"  - resource: https://example.com/spec\n    retrieved: '2026-11-10T12:00:00+02:00'\n"
        b"  - resource: https://example.com/draft\n    retrieved: unknown\n---\n\n# Retrieved Only\n",
    )
    brain.write(
        "concepts/tz-upd.md",
        b"---\ntype: guide\nstatus: stable\nupdated: '2026-11-15T13:30:00+02:00'\nstale_after: 15m\n---\n# TZ Upd\n",
    )
    brain.write(
        "concepts/undated-dur.md",
        b"---\ntype: guide\nstatus: stable\nstale_after: 7d\n---\n# Undated Dur\n",
    )
    repo_mem = records.record_file("repo", "acme/web")
    records.ingest(
        brain,
        "github",
        json.dumps(
            [
                {
                    "id": "pr-7",
                    "title": "Alias PR",
                    "updated": "2026-11-10T11:00:00Z",
                    "links": [
                        {"to": "urn:repo:acme/web", "meta": {"rel": "repo"}},
                        {"to": f"bf:///{repo_mem}", "meta": {"rel": "record"}},
                        {"to": "bf:///concepts/retrieved-only.md", "meta": {"rel": "topic"}},
                    ],
                }
            ]
        ).encode(),
        now=now,
    )

    rep = engine.review(brain, now=now)
    assert rep["brain"] == "fixture"
    assert rep["ok"] is False
    assert len(rep["problems"]) == 1
    assert "memories/broken/b.json" in rep["problems"][0]
    assert rep["total_tasks_open"] == 2
    assert rep["total_actions_open"] == 2
    assert rep["total_stale"] == len(rep["stale"])
    assert rep["truncated"] is False
    assert rep["actions_open"] == [
        {
            "ref": "actions/2026-11-10_later/ACTION.md",
            "title": "Later UTC",
            "updated": "2026-11-10T10:30:00.500Z",
            "summary": "Later UTC",
        },
        {
            "ref": "actions/2026-11-10_active/ACTION.md",
            "title": "Ship v1",
            "updated": "2026-11-10T12:00:00+02:00",
            "summary": "Ship v1",
        },
    ]

    stale_by_ref = {item["ref"]: item for item in rep["stale"]}
    assert "projects/fresh-hours.md" not in stale_by_ref
    assert "projects/overflow-dur.md" not in stale_by_ref
    assert stale_by_ref["concepts/tz-upd.md"]["reasons"] == ["due"]
    assert stale_by_ref["concepts/undated-dur.md"]["reasons"] == ["due"]
    assert stale_by_ref["projects/future.md"]["reasons"] == ["future_updated", "due"]
    assert stale_by_ref["projects/overdue.md"] == {
        "ref": "projects/overdue.md",
        "title": "Overdue",
        "reasons": ["due", "newer_evidence"],
        "newer": ["concepts/newer-note.md", "rawlinks:r1"],
    }
    assert stale_by_ref["projects/offline.md"] == {
        "ref": "projects/offline.md",
        "title": "Offline retrieval",
        "reasons": ["newer_evidence"],
        "newer": ["jira:WEB-9"],
    }
    assert stale_by_ref["projects/aliased.md"] == {
        "ref": "projects/aliased.md",
        "title": "Aliased",
        "reasons": ["newer_evidence"],
        "newer": ["concepts/newer-note.md", "github:pr-7"],
    }
    assert stale_by_ref["concepts/retrieved-only.md"] == {
        "ref": "concepts/retrieved-only.md",
        "title": "Retrieved Only",
        "reasons": ["newer_evidence"],
        "newer": ["github:pr-7"],
    }


def test_validate_warnings_strict_limit_cache_and_v30_engine_apis(tmp_path: Path) -> None:
    created = engine.init_brain(tmp_path / "v30")
    store = Store(Path(created["path"]))
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)

    # Snapshot missing target + forgotten target emit warnings (ok=True unless strict=True); unknown source is an error!
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: v30\nloops:\n"
            "  snap:\n    run: [echo, ok]\n    ingest:\n      source: snapsrc\n      mode: snapshot\n"
        ).encode(),
    )
    records.ingest(store, "snapsrc", b'[{"id":"kept","title":"Kept"}]\n', now=now)
    records.ingest(store, "fgsrc", b'[{"id":"gone","title":"Gone"}]\n', now=now)
    store.write("memories/fgsrc/.forgotten", b"gone\n")
    store.delete(records.record_file("fgsrc", "gone"))
    store.write(
        "concepts/refs.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-11-10\n---\n\n# Refs\n\n"
        b"[Snap](snapsrc:missing) [Forgotten](fgsrc:gone) [Unknown](unksrc:missing)\n",
    )
    engine.build(store)
    w_only = engine.validate(store, limit=2)
    assert not w_only["ok"]
    assert w_only["summary"]["truncated"] is True
    assert len(w_only["problems"]) == 2
    assert w_only["warnings_count"] == 2
    assert w_only["errors_count"] == 1

    # Remove unknown link: warnings remain -> ok=True unless strict=True!
    store.write(
        "concepts/refs.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-11-10\n---\n\n# Refs\n\n"
        b"[Snap](snapsrc:missing) [Forgotten](fgsrc:gone)\n",
    )
    engine.build(store)
    ok_warn = engine.validate(store)
    assert ok_warn["ok"] is True
    assert ok_warn["warnings_count"] == 2
    assert not engine.validate(store, strict=True)["ok"]
    assert (store.root / ".bf" / "cache" / "state.json").is_file()
    assert engine.validate(store, force=True)["ok"] is True
    with pytest.raises(InputError):
        engine.validate(store, limit=0)

    # Review newer[:5] cap, limit/all_items/fields, check_brain, new_note, and index sync!
    recs = [{"id": f"n{i}", "title": f"N{i}", "updated": "2026-11-19T12:00:00Z"} for i in range(7)]
    records.ingest(store, "ev", json.dumps(recs).encode(), now=now)
    links_md = " ".join(f"[N{i}](ev:n{i})" for i in range(7))
    store.write(
        "concepts/many.md",
        f"---\ntype: guide\nstatus: stable\nupdated: 2026-11-10\n---\n# Many\n\n{links_md}\n".encode(),
    )
    rev_lim = engine.review(store, now=now, limit=1, fields="ref,reasons,newer,newer_count,truncated")
    assert rev_lim["total_stale"] >= 1
    assert rev_lim["stale"][0]["newer_count"] == 7
    assert rev_lim["stale"][0]["truncated"] is True
    assert len(rev_lim["stale"][0]["newer"]) == 5
    with pytest.raises(InputError):
        engine.review(store, limit=0)
    with pytest.raises(InputError):
        engine.review(store, fields=" , ")

    new_act = engine.new_note(store, None, "action", "ship-v30", title="Ship v30", now=now)
    assert new_act["path"] == "actions/2026-11-20_ship-v30/ACTION.md"
    assert store.exists(new_act["compiled"])
    new_con = engine.new_note(store, None, "concept", "arch-note", note_type="architecture", now=now)
    assert "type: architecture" in store.read(new_con["path"]).decode()
    with pytest.raises(Error, match="already exists"):
        engine.new_note(store, None, "action", "ship-v30", now=now)
    with pytest.raises(InputError):
        engine.new_note(store, None, "bad", "slug")
    with pytest.raises(InputError):
        engine.new_note(store, None, "concept", "BAD SLUG")

    chk = engine.check_brain(store)
    assert chk["ok"] is True
    assert chk["build"]["ok"]
    assert chk["validate"]["ok"]
    assert chk["eval"]["ok"]
    assert not engine.check_brain(store, strict=True)["ok"]


def test_engine_edge_paths(tmp_path: Path) -> None:
    sib = engine.init_brain(tmp_path / "sib")
    sib_store = Store(Path(sib["path"]))
    records.ingest(sib_store, "ev", b'[{"id":"e1","title":"E1","time":"2026-11-18T10:00:00Z"}]')
    assert len(engine.load_index(sib_store)) >= 2

    main = engine.init_brain(tmp_path / "main")
    store = Store(Path(main["path"]))
    store.write("bf.yaml", f"version: {FORMAT}\nname: main\nbrains:\n  sib: ../sib\n".encode())
    records.ingest(store, "ev", b'[{"id":"r1","title":"R1","time":"2026-11-18T10:00:00Z"}]')
    store.write(
        "concepts/edges.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-11-10\n---\n# Edges\n\n"
        b"[Ok](bf://sib/ev:e1) [BadSibRec](bf://sib/ev:e1#a) [BadRec](ev:r1#a) [BadJson](schemas/concepts.json#a)\n",
    )
    engine.build(store)
    store.write("memories/concepts/bad.txt", b"bad\n")
    store.write("memories/fg/.forgotten", b"\xff\xfe")
    store.write(".bf/index.jsonl", b"{not-json\n")
    assert len(engine.load_index(store)) >= 2

    val = engine.validate(store, all_problems=True)
    assert val["ok"] is False
    assert any("bad.txt" in p for p in val["problems"])
    assert any("not valid UTF-8" in p for p in val["problems"])
    assert any("JSON record links do not support #anchor fragments" in p for p in val["problems"])

    store.write("concepts/bad.md", b"\xff\xfe")
    val_unreadable = engine.validate(store, all_problems=True)
    assert any("note is not UTF-8" in p for p in val_unreadable["problems"])
    store.delete("concepts/bad.md")


def test_nested_note_index_validate_review_and_cache_omit_keys(tmp_path: Path) -> None:
    created = engine.init_brain(tmp_path / "nested-brain")
    store = Store(Path(created["path"]))
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)

    store.write(
        "projects/sub/roadmap.md",
        b"---\ntype: project\nstatus: stable\nupdated: 2026-10-01\nstale_after: 14d\n---\n\n# Roadmap\n\n- [ ] Nested task\n",
    )
    built = engine.build(store, now=now)
    assert built["ok"]
    assert store.exists("memories/projects/sub/roadmap.json")

    idx_paths = {e["path"]: e for e in engine.load_index(store)}
    assert "memories/projects/sub/roadmap.json" in idx_paths
    assert idx_paths["memories/projects/sub/roadmap.json"]["ref"] == "projects/sub/roadmap.md"

    val = engine.validate(store)
    assert val["ok"]

    rev = engine.review(store, now=now)
    assert rev["total_tasks_open"] == 1
    stale_by_ref = {item["ref"]: item for item in rev["stale"]}
    assert "projects/sub/roadmap.md" in stale_by_ref
    assert "due" in stale_by_ref["projects/sub/roadmap.md"]["reasons"]

    # Compiled note lacking `id` with empty `_OMIT_OPT` keys (`summary`, `aliases`) or non-canonical record link misses cache.
    mem_path = "memories/projects/sub/roadmap.json"
    bad_cached = json.loads(store.read(mem_path))
    bad_cached.update(summary="", aliases=[])
    store.write(mem_path, encode(bad_cached))
    _, hit = engine.load_or_compile_note(
        store, "projects/sub/roadmap.md", store.read("projects/sub/roadmap.md"), brain_name=created["brain"]
    )
    assert hit is None
    assert engine.build(store, incremental=True, now=now)["written"] == 1

    # Stale v14 compiled note with `source:id` in `links` also misses incremental cache and recompiles canonically.
    store.write(
        "concepts/rec-link.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-11-20\n---\n# Rec\n\n[Item](cal:e1)\n",
    )
    engine.build(store, now=now)
    rec_mem = "memories/concepts/rec-link.json"
    stale_v14 = json.loads(store.read(rec_mem))
    stale_v14["links"] = [{"to": "cal:e1", "kind": "record"}]
    store.write(rec_mem, encode(stale_v14))
    _, hit_v14 = engine.load_or_compile_note(
        store, "concepts/rec-link.md", store.read("concepts/rec-link.md"), brain_name=created["brain"]
    )
    assert hit_v14 is None
    assert engine.build(store, incremental=True, now=now)["written"] == 1

    # `sync_notes()` omits `notes.built` events while `build()` emits them.
    seq_before_sync = records.latest_event_seq(store)
    store.write(
        "concepts/rec-link.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-11-20\n---\n# Rec Updated\n\n[Item](cal:e1)\n",
    )
    assert engine.sync_notes(store, now=now)["written"] == 1
    assert records.latest_event_seq(store) == seq_before_sync
    store.write(
        "concepts/rec-link.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-11-20\n---\n# Rec Built\n\n[Item](cal:e1)\n",
    )
    assert engine.build(store, now=now)["written"] == 1
    assert records.latest_event_seq(store) == seq_before_sync + 1

    # Read-only `.bf/` never fails `load_index(store)` when saving cache/index.
    bf_dir = store.root / ".bf"
    (bf_dir / "index.jsonl").unlink()
    bf_dir.chmod(0o500)
    try:
        ro_idx = engine.load_index(store)
        assert any(e["path"] == mem_path for e in ro_idx)
    finally:
        bf_dir.chmod(0o700)

    # Incremental `.bf/cache/state.json` & `.bf/index.jsonl` invalidation, coherence, and malformed-note guards:
    inv_root = tmp_path / "cache-inv"
    engine.init_brain(inv_root, name="cache-inv")
    inv_store = Store(inv_root)
    records.ingest(
        inv_store, "src1", json.dumps([{"id": "r1", "title": "Rec1", "aliases": ["urn:demo:r1"]}]).encode(), now=now
    )
    assert engine.validate(inv_store)["ok"] is True
    # Adding a source schema immediately invalidates `clean_records`.
    inv_store.write("schemas/src1.json", json.dumps({"type": "object", "required": ["owner"]}).encode())
    assert not engine.validate(inv_store)["ok"]
    inv_store.delete("schemas/src1.json")
    assert engine.validate(inv_store)["ok"] is True
    # Poisoned `clean_records` cache entry is bypassed by `force=True`.
    r1_path = records.record_file("src1", "r1")
    r1_doc = json.loads(inv_store.read(r1_path))
    r1_doc["relations"] = []
    inv_store.write(r1_path, encode(r1_doc))
    st_json = json.loads((inv_root / ".bf" / "cache" / "state.json").read_text())
    r1_st = inv_store.stat(r1_path)
    st_json["validate"]["clean_records"][r1_path] = [r1_st.st_ino, r1_st.st_ctime_ns, r1_st.st_mtime_ns, r1_st.st_size]
    (inv_root / ".bf" / "cache" / "state.json").write_text(json.dumps(st_json))
    assert not engine.validate(inv_store, force=True)["ok"]
    r1_doc.pop("relations")
    inv_store.write(r1_path, encode(r1_doc))
    # Changing `bf.yaml` `name:` invalidates `clean_notes`, and wrong-version cache recovers cleanly.
    inv_store.write("bf.yaml", f"version: {FORMAT}\nname: cache-inv-2\n".encode())
    assert engine.validate(inv_store)["ok"] is True
    (inv_root / ".bf" / "cache" / "state.json").write_text(json.dumps({"version": 999}))
    assert engine.validate(inv_store)["ok"] is True
    # `validate()` preserves concurrent `index_stats` in `.bf/cache/state.json`.
    engine.load_index(inv_store)
    before_idx_stats = json.loads((inv_root / ".bf" / "cache" / "state.json").read_text())["index_stats"]
    assert engine.validate(inv_store)["ok"] is True
    assert json.loads((inv_root / ".bf" / "cache" / "state.json").read_text())["index_stats"] == before_idx_stats
    # Deleting a sensor record evicts it from `load_index()` and `find_by_alias()`; `read_only=True` preserves disk index.
    from bf import query as qmod

    assert qmod.find_by_alias(inv_store, "urn:demo:r1") is not None
    raw_idx_before = (inv_root / ".bf" / "index.jsonl").read_bytes()
    inv_store.delete(r1_path)
    ro_entries = engine.load_index(inv_store, read_only=True)
    assert all(e["path"] != r1_path for e in ro_entries)
    assert (inv_root / ".bf" / "index.jsonl").read_bytes() == raw_idx_before
    assert qmod.find_by_alias(inv_store, "urn:demo:r1") is None
    assert all(e["path"] != r1_path for e in engine.load_index(inv_store))
    # Malformed compiled note does not crash `review(read_only=True)` with KeyError.
    inv_store.write("memories/concepts/corrupt.json", b'{"kind":"concept"}\n')
    assert engine.review(inv_store, read_only=True)["ok"] is True
    inv_store.delete("memories/concepts/corrupt.json")
    # `new_note()` surfaces `build()` problems via `ok=False` when another note is broken.
    inv_store.write("concepts/prebroken.md", b"---\nid: bad\n---\n# Bad\n")
    new_res = engine.new_note(inv_store, None, "concept", "valid-peer", title="Valid Peer", now=now)
    assert new_res["ok"] is False
    assert new_res["problems"]
