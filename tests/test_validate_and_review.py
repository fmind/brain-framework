"""Tests for init_brain, incremental build, unified memories/_notes.jsonl, optional schemas, validate, and review."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bf import engine, index, markdown, models, query, records, refs, render, status
from bf.models import FORMAT, NOTES_FILE, Error, InputError, encode
from bf.storage import Store

OBJ_SCHEMA = b'{"type":"object"}\n'


def test_init_brain_incremental_build_sync_and_note_pruning(tmp_path: Path) -> None:
    created = engine.init_brain(tmp_path / "123--My Brain!!")
    assert created["ok"] is True
    assert created["brain"] == "brain-123-my-brain"
    root = Path(created["path"])
    store = Store(root)
    assert not (root / ".agents").exists()
    assert not (root / "skills").exists()
    assert not (root / "usages").exists()
    assert not (root / "docs").exists()
    assert (root / NOTES_FILE).is_file()
    assert (root / ".bf" / "cache" / index.INDEX_NAME).is_file()
    for starter_schema in ("concepts.json", "projects.json", "actions.json"):
        assert (root / "schemas" / starter_schema).is_file()
    assert not (root / "schemas" / "usages.json").exists()
    assert (root / "evals" / "smoke.py").is_file()
    gi = (root / ".gitignore").read_text()
    assert "/memories/*\n!/memories/*.forgotten" in gi
    assert "/usages/" not in gi
    for folder in (
        "projects",
        "concepts",
        "actions",
        "memories",
        "schemas",
        "logs",
        "evals",
        "sensors",
        "routines",
        "agents",
    ):
        assert (root / folder).is_dir()
    for deferred in ("services", "channels", "prompts", "settings"):
        assert not (root / deferred).exists()
    assert engine.validate(store)["ok"]

    # Second build with unchanged notes and schemas reuses all notes incrementally!
    reuse_build = engine.build(store)
    assert reuse_build["ok"]
    assert reuse_build["notes"] == 1
    assert reuse_build["written"] == 0
    assert reuse_build["reused"] == 1
    assert reuse_build["output"] == NOTES_FILE
    assert reuse_build["dry_run"] is False

    # Universal `index.md` bundle: sibling files and subdirs (even symlinks) inside a bundle dir are never recursed!
    (root / "actions" / "ship-jsonl" / "inputs").mkdir(parents=True)
    (root / "actions" / "ship-jsonl" / "inputs" / "ext-link").symlink_to(tmp_path)
    store.write(
        "actions/ship-jsonl/index.md", b"---\ntype: action\nstatus: open\ncreated: 2026-11-15\n---\n\n# Ship JSONL\n"
    )
    store.write("actions/ship-jsonl/notes.md", b"# Ignored sibling\n")

    # `build(dry_run=True)` previews counts without mutating `memories/_notes.jsonl`.
    before_dry = store.read(NOTES_FILE)
    dry_action = engine.build(store, dry_run=True)
    assert dry_action["ok"]
    assert dry_action["dry_run"] is True
    assert (dry_action["notes"], dry_action["written"], dry_action["reused"]) == (2, 1, 1)
    assert store.read(NOTES_FILE) == before_dry

    built_action = engine.build(store)
    assert built_action["ok"]
    assert (built_action["notes"], built_action["written"], built_action["reused"]) == (2, 1, 1)
    assert len(list(records.iter_jsonl(store, NOTES_FILE))) == 2
    assert engine.validate(store)["ok"]

    # Schema edit invalidates build_sig and forces full recompile (reused == 0).
    store.write(
        "schemas/concepts.json",
        json.dumps({"type": "object", "required": ["type", "status"], "additionalProperties": True}).encode(),
    )
    rebuilt_sig = engine.build(store)
    assert rebuilt_sig["ok"]
    assert rebuilt_sig["reused"] == 0

    # `validate` is strictly read-only: detects unbuilt, orphaned, out-of-sync, and corrupt JSONL without writing!
    raw_welcome = store.read("concepts/welcome.md")
    store.write("concepts/unbuilt.md", b"---\ntype: guide\nstatus: draft\n---\n\n# Unbuilt\n")
    val_unbuilt = engine.validate(store)
    assert not val_unbuilt["ok"]
    assert any("missing compiled note for concepts/unbuilt.md" in p for p in val_unbuilt["problems"])
    store.delete("concepts/unbuilt.md")
    assert engine.validate(store)["ok"]

    orig_notes = store.read(NOTES_FILE)
    store.write(
        NOTES_FILE,
        orig_notes + b'{"ref":"concepts/orphan.md","source":"concepts","kind":"note","title":"Orphan"}\n',
    )
    val_orphan = engine.validate(store)
    assert not val_orphan["ok"]
    assert any("orphaned compiled note" in p for p in val_orphan["problems"])
    engine.build(store)
    assert "concepts/orphan.md" not in store.read(NOTES_FILE).decode()
    assert engine.validate(store)["ok"]

    compiled_bytes = store.read(NOTES_FILE)
    store.write("concepts/welcome.md", raw_welcome + b"\nEdited without rebuild.\n")
    assert any("out of sync with concepts/welcome.md" in p for p in engine.validate(store)["problems"])
    store.write(NOTES_FILE, b"{corrupt\n")
    assert any("invalid JSON" in p for p in engine.validate(store)["problems"])
    store.write("concepts/welcome.md", raw_welcome)
    store.write(NOTES_FILE, compiled_bytes)
    assert engine.validate(store)["ok"]

    # Tampered compiled record with matching `sha256` is caught by validate and fixed by full rebuild when schema/content changes!
    rows = [json.loads(ln) for ln in store.read(NOTES_FILE).decode().splitlines() if ln.strip()]
    for r in rows:
        if r["ref"] == "actions/ship-jsonl/index.md":
            r["title"] = "Tampered Title"
    store.write(NOTES_FILE, b"".join(encode(r) for r in rows))
    assert any("out of sync with actions/ship-jsonl/index.md" in p for p in engine.validate(store)["problems"])
    # Removing .bf/cache/index.sqlite3 forces recompile so tampered note is rewritten cleanly.
    store.delete(f".bf/cache/{index.INDEX_NAME}")
    assert engine.build(store)["written"] == 1
    assert engine.validate(store)["ok"]

    # Partial build failure preserves existing compiled notes instead of wiping them!
    store.write("concepts/broken-fm.md", b"---\nstatus: [\n---\n# Broken\n")
    bad_build = engine.build(store)
    assert not bad_build["ok"]
    assert "concepts/welcome.md" in store.read(NOTES_FILE).decode()
    store.delete("concepts/broken-fm.md")
    assert engine.build(store)["ok"]

    # Deleting all notes removes `memories/_notes.jsonl` completely (never 0-byte JSONL)!
    empty_b = tmp_path / "empty-notes"
    engine.init_brain(empty_b, name="empty-notes")
    empty_st = Store(empty_b)
    assert empty_st.exists(NOTES_FILE)
    empty_st.delete("concepts/welcome.md")
    assert engine.unbuilt_note_dirs(empty_st, ("concepts", "projects", "actions")) == [
        "concepts",
        "projects",
        "actions",
    ]
    built_after = engine.build(empty_st)
    assert built_after["removed"] == 1
    assert not empty_st.exists(NOTES_FILE)
    assert engine.unbuilt_note_dirs(empty_st, ("concepts", "projects", "actions")) == []
    empty_st.write("concepts/only.md", b"# Only\n")
    assert engine.unbuilt_note_dirs(empty_st, ("concepts", "projects", "actions")) == ["concepts"]
    empty_st.write(NOTES_FILE, b'{"ref":"retired/old.md","source":"retired","kind":"note","title":"Old"}\n')
    assert engine.unbuilt_note_dirs(empty_st, ("concepts", "projects", "actions")) == [
        "actions",
        "concepts",
        "projects",
    ]

    with pytest.raises(Error, match="not empty"):
        engine.init_brain(root)

    file_dst = tmp_path / "not-a-dir"
    file_dst.write_text("x")
    with pytest.raises(Error, match="not a real directory"):
        engine.init_brain(file_dst)

    with pytest.raises(InputError, match="lowercase letter"):
        engine.init_brain(tmp_path / "bad-name", name="Bad_Name")
    assert not (tmp_path / "bad-name").exists()

    dangling = tmp_path / "dangling-link"
    dangling.symlink_to(tmp_path / "missing-target")
    with pytest.raises(Error, match="not a real directory"):
        engine.init_brain(dangling)
    empty_real = tmp_path / "empty-real"
    empty_real.mkdir()
    sym_dir = tmp_path / "sym-empty-dir"
    sym_dir.symlink_to(empty_real)
    with pytest.raises(Error, match="not a real directory"):
        engine.init_brain(sym_dir)

    # Same-brain `bf:///` and uppercase `BF://<host>` frontmatter links compile cleanly.
    store.write(
        "concepts/norm-uri.md", b"---\ntype: guide\nstatus: stable\nlinks:\n  - bf:///concepts/welcome.md\n---\n# N\n"
    )
    store.write(
        "concepts/up-uri.md",
        b"---\ntype: guide\nstatus: stable\nlinks:\n  - BF://brain-123-my-brain/concepts/welcome.md\n---\n# U\n",
    )
    assert engine.build(store)["ok"]
    comp_up = markdown.compile_note("concepts/up-uri.md", store.read("concepts/up-uri.md"), brain_name="renamed")
    assert comp_up["links"][0]["kind"] == "brain"

    # Custom `notes:` directory works WITHOUT a domain schema (optional schemas!), supports bundles & detects collisions!
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: brain-123-my-brain\nnotes: [concepts, projects, actions, wiki]\n".encode(),
    )
    store.write("wiki/bundle-topic/index.md", b"---\ntype: wiki\nstatus: stable\n---\n# Bundle Topic\n")
    store.write("wiki/bundle-topic/outputs/draft.md", b"# Ignored Output\n")
    assert engine.build(store)["ok"]
    assert engine.validate(store)["ok"]
    # Adding optional `schemas/wiki.json` enforces domain constraints!
    store.write("schemas/wiki.json", json.dumps({"type": "object", "required": ["owner"]}).encode())
    assert any("missing required 'owner'" in p for p in engine.build(store)["problems"])
    assert any("missing required 'owner'" in p for p in engine.validate(store)["problems"])
    store.write("schemas/wiki.json", OBJ_SCHEMA)
    assert engine.build(store)["ok"]
    assert engine.validate(store)["ok"]
    # Flat vs bundle collision:
    store.write("wiki/bundle-topic.md", b"---\ntype: wiki\nstatus: stable\n---\n# Colliding Flat\n")
    assert any("collides with" in p for p in engine.build(store)["problems"])
    assert any("collides with" in p for p in engine.validate(store)["problems"])
    store.delete("wiki/bundle-topic.md")
    assert engine.build(store)["ok"]
    assert engine.validate(store)["ok"]
    # End-to-end custom `notes:` dir across `read_target`, `query`, `ingest`, and `status_summary`:
    assert query.read_target(store, "wiki/bundle-topic")["ref"] == "wiki/bundle-topic/index.md"
    assert query.read_target(store, "wiki/bundle-topic/outputs/draft.md")["kind"] == "file"
    assert query.query(store, "Bundle Topic", kind="note", scope="wiki")["count"] == 1
    with pytest.raises(InputError, match="conflicts with --kind memory"):
        query.query(store, "Bundle Topic", kind="memory", scope="wiki")
    with pytest.raises(InputError, match="reserved"):
        records.ingest(store, "wiki", b"[]")
    assert status.status_summary(store)["counts"]["notes"] >= 3


def test_validate_catches_schema_link_anchor_memory_and_cross_brain_errors(tmp_path: Path, brain: Store) -> None:
    assert engine.validate(brain)["ok"]

    bad_root = tmp_path / "bad-cfg"
    bad_root.mkdir()
    (bad_root / "bf.yaml").write_text("version: 99\nname: bad\n")
    invalid_cfg = engine.validate(Store(bad_root))
    assert not invalid_cfg["ok"]
    assert "version 99" in invalid_cfg["problems"][0]
    assert invalid_cfg["summary"]["errors"] == 1

    # Sibling brain for cross-brain link checks.
    sib_root = tmp_path / "sib"
    engine.init_brain(sib_root, name="sib")
    sib = Store(sib_root)
    records.ingest(sib, "events", b'[{"id":"ev-1","title":"Event 1","aliases":["repo:acme/demo"]}]')
    sib.write("memories/corrupt.jsonl", b"{bad\n")
    sib.write("concepts/shared.md", b"---\ntype: guide\nstatus: stable\n---\n\n# Shared\n\n## Known {#known}\n")
    sib.write("concepts/badutf.md", b"\xff\xfe")
    engine.build(sib)

    brain.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: fixture\nbrains:\n  sib: {sib_root}\n  missing-sib: {tmp_path / 'nope'}\n".encode(),
    )

    # Schema and .env errors.
    brain.write(".env", b'BAD_QUOTE="unclosed\\"\n', mode=0o600)
    brain.write("schemas/nested/extra.json", b"{}")
    brain.write("schemas/broken.json", b'{"type": "nope"}')
    brain.write("schemas/projects.json", json.dumps({"type": "object", "required": ["owner"]}).encode())
    brain.write("schemas/concepts.json", json.dumps({"type": "object", "required": ["status"]}).encode())
    brain.write("concepts/incomplete.md", b"---\ntype: guide\n---\n\n# Missing status\n")

    # Attachment Markdown file for non-OKF anchor checking.
    brain.write("actions/demo/index.md", b"---\ntype: action\nstatus: open\ncreated: 2026-11-15\n---\n\n# Demo\n")
    brain.write("actions/demo/outputs/report.md", b"# Report\n\n## Summary {#summary}\n")
    brain.write("actions/demo/outputs/bad.md", b"\xff\xfe")

    # JSONL memory errors: stray non-jsonl, subdir under memories/, reserved source, corrupt jsonl, duplicate id + bad links.
    # Note: `memories/noschema.jsonl` without `schemas/noschema.json` is VALID in v19 (optional domain schemas)!
    brain.write("memories/stray.txt", b"bad")
    brain.write("memories/nested/old.json", b"{}\n")
    brain.write("memories/repos.forgotten", b"ignored\n")
    brain.write("memories/scalar.jsonl", b"123\n")
    brain.write("memories/notes.jsonl", b"{}\n")
    brain.write(
        "memories/noschema.jsonl",
        b'{"id":"n1","ref":"noschema:n1","source":"noschema","kind":"memory","title":"No Schema"}\n',
    )
    brain.write("memories/corrupt.jsonl", b"{broken\n")
    dupe_line = json.dumps(
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
    brain.write("memories/dupes.jsonl", dupe_line + b"\n" + dupe_line + b"\n")
    brain.write(
        "memories/relbad.jsonl",
        json.dumps(
            {
                "id": "r1",
                "ref": "relbad:r1",
                "source": "relbad",
                "kind": "memory",
                "title": "Bad relations",
                "text": "",
                "tags": [],
                "links": [
                    {"to": "concepts:welcome", "kind": "record", "anchor": "", "meta": {}, "section": ""},
                    {"to": "projects:bad", "kind": "note", "anchor": "", "meta": {}, "section": ""},
                    {"to": "projects/../../etc/passwd", "kind": "note", "anchor": "", "meta": {}, "section": ""},
                    {"to": "javascript:alert(1)", "kind": "url", "anchor": "", "meta": {}, "section": ""},
                    {"to": "bf://sib/notes:bad", "kind": "brain", "anchor": "", "meta": {}, "section": ""},
                    {"to": "meetings:decision-1", "kind": "record", "anchor": "frag", "meta": {}, "section": ""},
                ],
            }
        ).encode()
        + b"\n",
    )
    brain.write(
        "memories/wrongkind.jsonl",
        json.dumps(
            {"id": "k1", "ref": "wrongkind:k1", "source": "wrongkind", "kind": "concept", "title": "Wrong kind"}
        ).encode()
        + b"\n",
    )

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
        b"- [Good memory](repos:r1)\n"
        b"- [Bad rec frag](repos:r1#.title)\n"
        b"- [Missing memory](repos:missing)\n"
        b"- [Good attachment](../actions/demo/outputs/report.md#summary)\n"
        b"- [Bad attachment anchor](../actions/demo/outputs/report.md#missing)\n"
        b"- [Unreadable attachment](../actions/demo/outputs/bad.md#sec)\n"
        b"- [Missing note](../concepts/ghost.md)\n"
        b"- [Missing anchor](../concepts/welcome.md#no-such-heading)\n"
        b"- [Unknown brain](bf://undeclared/concepts/x.md)\n"
        b"- [Good sib note](bf://sib/concepts/shared.md#known)\n"
        b"- [Good sib rec](bf://sib/events:ev-1)\n"
        b"- [Bad sib anchor](bf://sib/concepts/shared.md#missing)\n"
        b"- [Bad sib note](bf://sib/concepts/missing.md)\n"
        b"- [Bad sib mem](bf://sib/events:missing)\n"
        b"- [Bad sib rec frag](bf://sib/events:ev-1#.title)\n"
        b"- [Corrupt sib heading](bf://sib/concepts/badutf.md#sec)\n",
    )

    engine.build(brain)
    report = engine.validate(brain, all_problems=True)
    assert not report["ok"]
    joined = "\n".join(report["problems"])
    assert "noschema" not in joined
    for expected in (
        ".env:1: unclosed quote",
        "schemas/nested: schemas/ holds only flat",
        "schemas/broken.json: invalid JSON Schema",
        "projects/offline.md: missing required 'owner'",
        "projects/badstale.md:",
        "projects/badstaledur.md:",
        "projects/hugestale.md: stale_after",
        "concepts/incomplete.md: missing required 'status'",
        "memories/stray.txt: memories/ holds flat <source>.jsonl files",
        "memories/nested: memories/ holds flat <source>.jsonl files",
        "memories/scalar.jsonl:1: expected a JSON object",
        "memories/notes.jsonl: invalid or reserved source",
        "memories/corrupt.jsonl:1: invalid JSON",
        "duplicate record id 'd1' in dupes",
        "memories/dupes.jsonl:1: source must equal 'dupes'",
        "memories/dupes.jsonl:1: ref must equal 'dupes:d1'",
        "memories/relbad.jsonl:1: record link 'concepts:welcome' must use <source>:<id>",
        "memories/relbad.jsonl:1: note link 'projects:bad' must target a configured note directory",
        "memories/relbad.jsonl:1: note link 'projects/../../etc/passwd' must target a configured note directory",
        "memories/relbad.jsonl:1: url link must use https://",
        "memories/relbad.jsonl:1: cross-brain link 'bf://sib/notes:bad' must use bf://<brain>/<ref>",
        "memories/wrongkind.jsonl:1: kind must equal 'memory'",
        "bf.yaml brains.missing-sib:",
        "record links do not support #anchor fragments",
        "missing anchor #missing in actions/demo/outputs/report.md",
        "actions/demo/outputs/bad.md: note is not UTF-8",
        "broken link to concepts/ghost.md",
        "broken link to repos:missing",
        "missing anchor #no-such-heading in concepts/welcome.md",
        "not declared in bf.yaml brains:",
        "missing cross-brain anchor bf://sib/concepts/shared.md#missing",
        "broken cross-brain link bf://sib/concepts/missing.md",
        "broken cross-brain link bf://sib/events:missing",
    ):
        assert expected in joined, f"missing expected problem {expected!r} in:\n{joined}"

    assert refs.record_link_problem({"kind": "record", "to": "github:foo\nbar", "anchor": ""})
    assert refs.record_link_problem({"kind": "url", "to": "https://example.com\n", "anchor": ""})
    assert refs.record_link_problem({"kind": "brain", "to": "bf://sib\\x/concepts/a.md", "anchor": ""})


def test_review_computes_newer_evidence_due_future_updated_and_tasks(brain: Store) -> None:
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

    brain.write(
        "concepts/old-dep.md",
        b"---\ntype: guide\nstatus: deprecated\nupdated: 2026-01-01\nstale_after: 1d\n---\n\n# Old\n\n- [ ] Ignored\n",
    )
    brain.write(
        "projects/done-proj.md",
        b"---\ntype: project\nstatus: done\nupdated: 2026-01-01\nstale_after: 1d\n---\n\n# Done\n\n- [ ] Ignored\n",
    )
    brain.write(
        "projects/archived-proj.md",
        b"---\ntype: project\nstatus: archived\nupdated: 2026-01-01\nstale_after: 1d\n---\n\n# Archived\n\n- [ ] Ignored\n",
    )
    brain.write(
        "actions/cancelled-act/index.md",
        b"---\ntype: action\nstatus: cancelled\ncreated: 2026-01-01\nstale_after: 1d\n---\n\n# Cancelled\n\n- [ ] Ignored\n",
    )
    brain.write(
        "actions/active/index.md",
        b"---\ntype: action\nstatus: open\ncreated: 2026-11-10\nupdated: '2026-11-10T12:00:00+02:00'\ndescription: Ship v1\n---\n\n# Ship v1\n\n- [ ] Task A\n",
    )
    brain.write(
        "projects/future.md",
        b"---\ntype: project\nstatus: active\nupdated: 2026-12-01\nstale_after: 2026-11-01T10:00:00+02:00\n---\n\n# Future\n",
    )
    brain.write(
        "projects/overdue.md",
        b"---\ntype: project\nstatus: active\nupdated: 2026-10-01\nstale_after: 14d\nentity: bf:///projects/overdue.md\n---\n\n# Overdue\n",
    )
    brain.write(
        "projects/fresh-hours.md",
        b"---\ntype: project\nstatus: active\nupdated: '2026-11-15T06:00:00Z'\nstale_after: 12h\n---\n\n# Fresh Hours\n",
    )
    brain.write(
        "projects/overflow-dur.md",
        b"---\ntype: project\nstatus: active\nupdated: 2026-11-15\nstale_after: 9999y\n---\n\n# Overflow Dur\n",
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
    brain.write("memories/broken.jsonl", b"{bad\n")
    brain.write("memories/notes.jsonl", b"{}\n")
    brain.write("memories/scalar.jsonl", b"123\n")
    brain.write(
        "memories/rawlinks.jsonl",
        b'{"id":"r1","ref":"rawlinks:r1","source":"rawlinks","kind":"memory","title":"R","updated":"2026-11-13",'
        b'"links":[{"to":"bf:///projects/overdue.md#top","kind":"note","anchor":"top","meta":{},"section":""}]}\n',
    )

    brain.write(
        "projects/aliased.md",
        b"---\ntype: project\nstatus: active\nupdated: 2026-11-09\n"
        b"aliases: [repo:acme/web, https://example.com/acme/web]\n---\n\n# Aliased\n\n"
        b"Self reference to <https://example.com/acme/web>.\n",
    )
    brain.write(
        "concepts/created-only.md",
        b"---\ntype: guide\nstatus: stable\ncreated: '2026-11-10T12:00:00+02:00'\nstale_after: 30d\n---\n\n# Created Only\n",
    )
    brain.write(
        "concepts/tz-upd.md",
        b"---\ntype: guide\nstatus: stable\nupdated: '2026-11-15T13:30:00+02:00'\nstale_after: 15m\n---\n# TZ Upd\n",
    )
    brain.write(
        "concepts/undated-dur.md",
        b"---\ntype: guide\nstatus: stable\nstale_after: 7d\n---\n# Undated Dur\n",
    )
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
                        {"to": "bf:///concepts/created-only.md", "meta": {"rel": "topic"}},
                    ],
                }
            ]
        ).encode(),
        now=now,
    )

    engine.build(brain)
    # Remove SQLite index first to verify JSONL fallback path (including broken.jsonl diagnostic and index_stale).
    brain.delete(f".bf/cache/{index.INDEX_NAME}")
    rep = engine.review(brain, now=now)
    assert rep["brain"] == "fixture"
    assert rep["ok"] is False
    assert rep["index_stale"] is True
    assert len(rep["problems"]) == 1
    assert "memories/broken.jsonl" in rep["problems"][0]
    assert rep["total_tasks_open"] == 2
    assert "actions_open" not in rep
    assert rep["total_stale"] == len(rep["stale"])
    assert rep["truncated"] is False

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
    assert stale_by_ref["concepts/created-only.md"] == {
        "ref": "concepts/created-only.md",
        "title": "Created Only",
        "reasons": ["newer_evidence"],
        "newer": ["github:pr-7"],
    }

    # Indexed SQLite review produces identical staleness when memories/ and notes are valid!
    for bad_f in ("memories/broken.jsonl", "memories/notes.jsonl", "memories/scalar.jsonl", "projects/overflow-dur.md"):
        brain.delete(bad_f)
    assert engine.build(brain)["ok"]
    rep_idx = engine.review(brain, now=now)
    assert rep_idx["ok"] is True
    assert "index_stale" not in rep_idx
    idx_by_ref = {item["ref"]: item for item in rep_idx["stale"]}
    assert idx_by_ref["projects/offline.md"] == stale_by_ref["projects/offline.md"]
    assert idx_by_ref["projects/aliased.md"] == stale_by_ref["projects/aliased.md"]
    assert idx_by_ref["concepts/created-only.md"] == stale_by_ref["concepts/created-only.md"]


def test_validate_warnings_strict_limit_and_review_caps(tmp_path: Path) -> None:
    created = engine.init_brain(tmp_path / "v19")
    store = Store(Path(created["path"]))
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)

    # Snapshot missing target + forgotten target emit warnings (ok=True unless strict=True); unknown source is an error!
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: v19\nloops:\n"
            "  snap:\n    run: [echo, ok]\n    ingest:\n      source: snapsrc\n      mode: snapshot\n"
        ).encode(),
    )
    records.ingest(store, "snapsrc", b'[{"id":"kept","title":"Kept"}]\n', now=now)
    records.ingest(store, "fgsrc", b'[{"id":"gone","title":"Gone"}]\n', now=now)
    store.write("memories/fgsrc.forgotten", b"gone\n")
    store.delete("memories/fgsrc.jsonl")
    store.write(
        "concepts/refs.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-11-10\n---\n\n# Refs\n\n"
        b"[Snap](snapsrc:missing) [Forgotten](fgsrc:gone) [Unknown](unksrc:missing)\n",
    )
    engine.build(store)
    w_only = engine.validate(store, limit=2)
    assert not w_only["ok"]
    assert w_only["truncated"] is True
    assert len(w_only["problems"]) == 2
    assert w_only["summary"]["warnings"] == 2
    assert w_only["summary"]["errors"] == 1

    # Remove unknown link: warnings remain -> ok=True unless strict=True!
    store.write(
        "concepts/refs.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-11-10\n---\n\n# Refs\n\n"
        b"[Snap](snapsrc:missing) [Forgotten](fgsrc:gone)\n",
    )
    engine.build(store)
    ok_warn = engine.validate(store)
    assert ok_warn["ok"] is True
    assert ok_warn["summary"]["warnings"] == 2
    assert not engine.validate(store, strict=True)["ok"]
    with pytest.raises(InputError):
        engine.validate(store, limit=0)
    with pytest.raises(InputError):
        engine.validate(store, limit=5001)

    # Unreferenced missing `brains:` path emits a warning (ok=True unless --strict); referenced missing path is an error!
    store.write(
        "bf.yaml",
        f"version: {FORMAT}\nname: v19\nbrains:\n  offline-sib: {tmp_path / 'not-mounted'}\n".encode(),
    )
    store.delete("concepts/refs.md")
    engine.build(store)
    w_sib = engine.validate(store)
    assert w_sib["ok"] is True
    assert w_sib["summary"]["warnings"] == 1
    assert any("bf.yaml brains.offline-sib:" in p for p in w_sib["problems"])
    assert engine.validate(store, strict=True)["ok"] is False
    store.write(
        "concepts/use-sib.md", b"---\ntype: guide\nstatus: stable\n---\n# Use\n\n[X](bf://offline-sib/concepts/a.md)\n"
    )
    engine.build(store)
    err_sib = engine.validate(store)
    assert err_sib["ok"] is False
    assert err_sib["summary"]["errors"] >= 1
    store.delete("concepts/use-sib.md")
    store.write("bf.yaml", f"version: {FORMAT}\nname: v19\n".encode())
    engine.build(store)

    # Review newer[:5] cap (`newer_truncated`) and limit validation!
    recs = [{"id": f"n{i}", "title": f"N{i}", "updated": "2026-11-19T12:00:00Z"} for i in range(7)]
    records.ingest(store, "ev", json.dumps(recs).encode(), now=now)
    links_md = " ".join(f"[N{i}](ev:n{i})" for i in range(7))
    store.write(
        "concepts/many.md",
        f"---\ntype: guide\nstatus: stable\nupdated: 2026-11-10\n---\n# Many\n\n{links_md}\n".encode(),
    )
    engine.build(store)
    rev_lim = engine.review(store, now=now, limit=1)
    assert rev_lim["total_stale"] >= 1
    assert rev_lim["stale"][0]["newer_count"] == 7
    assert rev_lim["stale"][0]["newer_truncated"] is True
    assert len(rev_lim["stale"][0]["newer"]) == 5
    with pytest.raises(InputError):
        engine.review(store, limit=0)
    with pytest.raises(InputError):
        engine.review(store, limit=5001)


def test_engine_edge_paths_readonly_and_scan_caps(tmp_path: Path) -> None:
    sib = engine.init_brain(tmp_path / "sib")
    sib_store = Store(Path(sib["path"]))
    records.ingest(sib_store, "ev", b'[{"id":"e1","title":"E1","created":"2026-11-18T10:00:00Z"}]')

    main = engine.init_brain(tmp_path / "main")
    store = Store(Path(main["path"]))
    store.write("bf.yaml", f"version: {FORMAT}\nname: main\nbrains:\n  sib: ../sib\n".encode())
    records.ingest(store, "ev", b'[{"id":"r1","title":"R1","created":"2026-11-18T10:00:00Z"}]')
    store.write(
        "concepts/edges.md",
        b"---\ntype: guide\nstatus: stable\nupdated: 2026-11-10\n---\n# Edges\n\n"
        b"[Ok](bf://sib/ev:e1) [BadSibRec](bf://sib/ev:e1#a) [BadRec](ev:r1#a)\n",
    )
    engine.build(store)
    store.write("memories/bad.txt", b"bad\n")
    store.write("memories/fg.forgotten", b"\xff\xfe")
    val = engine.validate(store, all_problems=True)
    assert val["ok"] is False
    assert any("bad.txt" in p for p in val["problems"])
    assert any("not valid UTF-8" in p for p in val["problems"])
    assert any("record links do not support #anchor fragments" in p for p in val["problems"])

    store.write("concepts/bad.md", b"\xff\xfe")
    val_unreadable = engine.validate(store, all_problems=True)
    assert any("note is not UTF-8" in p for p in val_unreadable["problems"])
    store.delete("concepts/bad.md")

    # Nested notes, schema tightening, broken local `schemas/record.json`, symlink scan failure, and read-only `.bf/`.
    inv_root = tmp_path / "cache-inv"
    engine.init_brain(inv_root, name="cache-inv")
    inv_store = Store(inv_root)
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    inv_store.write(
        "projects/sub/roadmap.md",
        b"---\ntype: project\nstatus: active\nupdated: 2026-10-01\nstale_after: 14d\n---\n\n# Roadmap\n\n- [ ] Nested task\n",
    )
    assert engine.build(inv_store, now=now)["ok"]
    assert engine.validate(inv_store)["ok"]
    rev = engine.review(inv_store, now=now)
    assert rev["total_tasks_open"] == 1
    assert any(it["ref"] == "projects/sub/roadmap.md" and "due" in it["reasons"] for it in rev["stale"])

    records.ingest(
        inv_store,
        "src1",
        json.dumps(
            [{"id": "r1", "title": "Rec1", "aliases": ["urn:demo:r1"], "links": ["concepts/welcome.md"]}]
        ).encode(),
        now=now,
    )
    assert engine.validate(inv_store)["ok"] is True

    # Tightening `schemas/src1.json` + upserting a new conforming record still catches unvalidated pre-existing records!
    inv_store.write("schemas/src1.json", json.dumps({"type": "object", "required": ["owner"]}).encode())
    records.ingest(inv_store, "src1", b'[{"id":"r2","title":"Rec2","owner":"bob"}]', now=now)
    assert not engine.validate(inv_store)["ok"]
    inv_store.write("schemas/src1.json", OBJ_SCHEMA)
    assert engine.validate(inv_store)["ok"] is True

    # Reserved `schemas/record.json` is rejected by `validate()`, and broken `schemas/concepts.json` fails `build()` without wiping existing files!
    inv_store.write("schemas/record.json", b'{"type":"object"}')
    assert any("reserved schema name 'record'" in p for p in engine.validate(inv_store)["problems"])
    inv_store.delete("schemas/record.json")

    inv_store.write("schemas/concepts.json", b"{broken")
    before_c = inv_store.read(NOTES_FILE)
    bad_b = engine.build(inv_store)
    assert not bad_b["ok"]
    assert bad_b["written"] == 0
    assert inv_store.read(NOTES_FILE) == before_c
    assert any("schemas/concepts.json" in p for p in engine.validate(inv_store)["problems"])
    inv_store.write("schemas/concepts.json", OBJ_SCHEMA)

    # Symlink scan failure in `concepts/` preserves `memories/_notes.jsonl` with `written == 0`!
    (inv_root / "concepts" / "bad-link.md").symlink_to(inv_root / "bf.yaml")
    scan_fail = engine.build(inv_store)
    assert not scan_fail["ok"]
    assert scan_fail["written"] == 0
    assert inv_store.read(NOTES_FILE) == before_c
    (inv_root / "concepts" / "bad-link.md").unlink()

    # Duplicate compiled note in `memories/_notes.jsonl` is caught by validate!
    orig_c = inv_store.read(NOTES_FILE)
    inv_store.write(
        NOTES_FILE,
        orig_c + b'{"ref":"concepts/welcome.md","source":"concepts","kind":"note","title":"Dup"}\n',
    )
    assert any("duplicate compiled note" in p for p in engine.validate(inv_store)["problems"])
    inv_store.write(NOTES_FILE, orig_c)

    # Read-only `.bf/` works without failing or mutating disk!
    (inv_root / ".bf").chmod(0o500)
    try:
        assert engine.validate(inv_store)["ok"] is True
        assert engine.review(inv_store, now=now)["ok"] is True
    finally:
        (inv_root / ".bf").chmod(0o700)

    # Deleting `src1.jsonl` evicts alias immediately (index sees stale/missing source -> JSONL fallback)!
    src1_path = records.memory_file("src1")
    assert query.find_by_alias(inv_store, "urn:demo:r1") is not None
    inv_store.delete(src1_path)
    assert query.find_by_alias(inv_store, "urn:demo:r1") is None

    # `MAX_TOTAL_FILES` cap enforced across `validate`, `query` (scan fallback), and `status_summary` (scan fallback).
    records.ingest(inv_store, "cap2", b'[{"id":"c1","title":"Cap1"},{"id":"c2","title":"Cap2"}]')
    inv_store.delete(f".bf/cache/{index.INDEX_NAME}")
    with pytest.MonkeyPatch.context() as mp_cap:
        mp_cap.setattr(models, "MAX_TOTAL_FILES", 1)
        mp_cap.setattr(engine, "MAX_TOTAL_FILES", 1)
        with pytest.raises(models.Error, match="total scan limit"):
            engine.validate(inv_store)
        with pytest.raises(models.Error, match="total scan limit"):
            query.query(inv_store, kind="memory")
        with pytest.raises(models.Error, match="total scan limit"):
            status.status_summary(inv_store)

    # `authored_notes` symlink/error paths for bundle `index.md`.
    bad_act = inv_root / "actions" / "2026-11-20_symlink"
    bad_act.mkdir(parents=True, exist_ok=True)
    (bad_act / "index.md").symlink_to(inv_root / "concepts" / "welcome.md")
    act_skips: list[str] = []
    markdown.authored_notes(inv_store, skipped=act_skips)
    assert any("index.md" in s and "symlinks" in s for s in act_skips)
    with pytest.raises(models.Error, match="symlinks"):
        markdown.authored_notes(inv_store)
    (bad_act / "index.md").unlink()
    bad_act.rmdir()

    # `authored_notes` enforces 64-level depth limit and cumulative `MAX_FILES` cap!
    with pytest.MonkeyPatch.context() as mp_af:
        mp_af.setattr(models, "MAX_FILES", 1)
        with pytest.raises(models.Error, match="entry scan limit"):
            markdown.authored_notes(inv_store)
    deep_note_dir = inv_root / "concepts" / "/".join(f"d{i}" for i in range(65))
    deep_note_dir.mkdir(parents=True)
    with pytest.raises(models.Error, match="64-level depth limit"):
        markdown.authored_notes(inv_store)

    # Unicode U+2028/U+2029 line/paragraph separators inside JSONL strings do NOT split records!
    ls_items, ls_inc = records.parse_batch_envelope(
        b'{"id":"u1","title":"A\xe2\x80\xa8B"}\n{"id":"u2","title":"C\xe2\x80\xa9D"}\n'
    )
    assert ([it["id"] for it in ls_items], ls_inc) == (["u1", "u2"], False)


def test_validate_checks_loops_explicit_run_scripts_and_explicit_schema(tmp_path: Path) -> None:
    root = tmp_path / "loop-val"
    engine.init_brain(root, name="loop-val")
    store = Store(root)
    store.write(
        "bf.yaml",
        (
            f"version: {FORMAT}\nname: loop-val\n"
            "loops:\n"
            "  no-script:\n    when: 1h\n"
            "  bad-explicit-run:\n    run: [python3, routines/missing.py]\n"
            "  missing-schema:\n    run: [echo, '[]']\n    ingest:\n      source: gmail-2026\n      schema: gmail\n"
            "  optional-schema:\n    run: [echo, '[]']\n    ingest: upsert\n"
        ).encode(),
    )
    rep = engine.validate(store, all_problems=True)
    assert not rep["ok"]
    joined = "\n".join(rep["problems"])
    for expected in (
        "loops.no-script:",
        "loops.bad-explicit-run: script routines/missing.py must be a regular file inside brain",
        "loops.missing-schema: explicit schema schemas/gmail.json not found",
    ):
        assert expected in joined, f"missing {expected!r} in:\n{joined}"
    assert "optional-schema" not in joined


def test_validate_notes_only_and_text_formatters(tmp_path: Path) -> None:
    root = tmp_path / "fastcheck"
    engine.init_brain(root, name="fastcheck")
    store = Store(root)
    store.write("memories/feed.jsonl", b"{corrupt\n")
    assert not engine.validate(store)["ok"]
    notes_rep = engine.validate(store, notes_only=True)
    assert notes_rep["ok"] is True
    assert notes_rep["notes_only"] is True
    assert notes_rep["counts"]["memories"] is None

    # Text formatters cover OK, FAILED, truncated, FRESH, STALE (with newer & title detail), query text, and ANSI color.
    chk_ok_txt = render.format_check_text(notes_rep, color=True)
    assert "OK" in chk_ok_txt
    assert "memories=skipped" in chk_ok_txt
    assert "\033[32m" in chk_ok_txt
    chk_fail_txt = render.format_check_text(
        {
            "ok": False,
            "brain": "b",
            "notes_only": False,
            "counts": {"notes": 1, "memories": 0},
            "summary": {"errors": 2, "warnings": 0, "by_kind": {"note": 2}},
            "truncated": True,
            "problems": ["p1"],
        },
        color=False,
    )
    assert "FAILED (2 errors, 0 warnings)" in chk_fail_txt
    assert "truncated" in chk_fail_txt

    rev_fresh = render.format_review_text(
        {"ok": True, "brain": "b", "total_stale": 0, "total_tasks_open": 0, "stale": []}
    )
    assert "[FRESH]" in rev_fresh
    rev_fail = render.format_review_text(
        {"ok": False, "brain": "b", "total_stale": 0, "total_tasks_open": 0, "stale": [], "problems": ["broken.jsonl"]},
        color=True,
    )
    assert "FAILED (1 problems)" in rev_fail
    assert "FRESH" not in rev_fail
    assert "broken.jsonl" in rev_fail
    rev_stale = render.format_review_text(
        {
            "ok": True,
            "brain": "b",
            "total_stale": 2,
            "total_tasks_open": 3,
            "stale": [
                {"ref": "concepts/a.md", "title": "A", "reasons": ["due"]},
                {"ref": "projects/p.md", "title": "P", "reasons": ["newer_evidence"], "newer": ["jira:1"]},
            ],
            "truncated": True,
            "unbuilt_notes": ["concepts"],
        },
        color=True,
    )
    assert "STALE (2)" in rev_stale
    assert "newer: jira:1" in rev_stale
    assert "truncated" in rev_stale
    assert "unbuilt notes: concepts" in rev_stale

    q_txt = render.format_query_text(
        {
            "engine": "fts5",
            "query": "alpha",
            "items": [{"ref": "concepts/a.md", "title": "Alpha", "anchor": "top", "snippet": "Match alpha here"}],
            "truncated": True,
            "index_stale": True,
            "unbuilt_notes": ["concepts"],
            "excluded_sources": ["agent-telemetry"],
        },
        color=True,
    )
    assert "Query (fts5): 1 result(s) for 'alpha'" in q_txt
    assert "concepts/a.md#top" in q_txt
    assert "Match alpha here" in q_txt
    assert "truncated" in q_txt
    assert "index stale" in q_txt
    assert "unbuilt notes: concepts" in q_txt
    assert "excluded by bf.yaml: agent-telemetry" in q_txt

    # All-disabled loops omit the table header; crashed loops, index_stale, problems-without-attention, and orphans render cleanly!
    all_off = render.format_status_text(
        {
            "brain": "b",
            "ok": False,
            "counts": {"notes": 1, "memories": 0, "sources": 0, "loops": 1},
            "events_seq": 0,
            "index_stale": True,
            "problems": ["bf.yaml: broken"],
            "orphans": {"states": ["bad\nname"], "dirs": [], "logs": []},
            "loops": {"off": {"enabled": False, "status": "idle", "description": "line1\nline2"}},
        }
    )
    assert "LOOP" not in all_off
    assert "(1 disabled loop(s))" in all_off
    assert "bad\\u000aname" in all_off
    assert "bf state prune" in all_off
    assert "index: stale" in all_off
    assert "bf.yaml: broken" in all_off

    crashed_txt = render.format_status_text(
        {
            "brain": "b",
            "ok": False,
            "counts": {"notes": 1, "memories": 0, "sources": 0, "loops": 1},
            "events_seq": 0,
            "attention": [{"name": "d1", "reason": "crashed", "status": "stopped"}],
            "loops": {"d1": {"enabled": True, "status": "stopped", "crashed": True, "mode": "daemon"}},
        }
    )
    assert "crashed" in crashed_txt

    bad_build_ln = render.format_watch_line(
        "watch.build", {"ok": False, "notes": 1, "written": 0, "problems": ["bad note", "second"]}, color=False
    )
    assert "bad note" in bad_build_ln
    assert "+1 more" in bad_build_ln
    rem_build_ln = render.format_watch_line(
        "watch.build", {"ok": True, "notes": 1, "written": 0, "removed": 2}, color=False
    )
    assert "2 removed" in rem_build_ln


def test_validation_scale_warning_bool_drift_review_tz_and_anchor_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hard"
    engine.init_brain(root, name="hard")
    store = Store(root)

    # Empty directory inside schemas/ is caught by store.subdirs("schemas").
    (root / "schemas" / "nested").mkdir()
    rep_dir = engine.validate(store)
    assert not rep_dir["ok"]
    assert any("schemas/nested" in p for p in rep_dir["problems"])
    (root / "schemas" / "nested").rmdir()

    # Soft scale warning fires when a JSONL file exceeds SCALE_WARN_RECORDS (non-fatal unless --strict).
    records.ingest(store, "src", b'[{"id":"a","title":"A"},{"id":"b","title":"B"}]')
    monkeypatch.setattr(models, "SCALE_WARN_RECORDS", 1)
    rep_scale = engine.validate(store)
    assert rep_scale["ok"] is True
    assert rep_scale["summary"]["warnings"] == 1
    assert any("consider partitioning" in p for p in rep_scale["problems"])
    assert engine.validate(store, strict=True)["ok"] is False
    st_scale = status.status_summary(store, include_loops=True)
    assert st_scale["scale_warnings"]
    assert "scale warnings:" in render.format_status_text(st_scale)
    monkeypatch.setattr(models, "SCALE_WARN_RECORDS", 20_000)

    # bool <-> int drift is caught by canonical JSON comparison in validate().
    store.write(
        "concepts/bools.md",
        b"---\ntype: guide\nstatus: stable\nflag: 1\n---\n# Bools\n",
    )
    engine.build(store)
    raw_mem = store.read(NOTES_FILE).replace(b'"flag":1', b'"flag":true')
    store.write(NOTES_FILE, raw_mem)
    rep_bool = engine.validate(store)
    assert not rep_bool["ok"]
    assert any("out of sync" in p for p in rep_bool["problems"])
    store.delete(f".bf/cache/{index.INDEX_NAME}")
    engine.build(store)

    # review() handles non-UTC timestamp offsets cleanly with explicit `stale_after: 1d`.
    store.write(
        "concepts/tz.md",
        b'---\ntype: guide\nstatus: stable\nupdated: "2026-11-10T23:30:00-02:00"\nstale_after: 1d\n---\n# TZ\n',
    )
    engine.build(store)
    # 2026-11-10T23:30:00-02:00 == 2026-11-11T01:30:00Z -> +1d == 2026-11-12T01:30:00Z!
    rev_before = engine.review(store, now=datetime(2026, 11, 12, 1, 0, tzinfo=UTC))
    assert all(it["ref"] != "concepts/tz.md" for it in rev_before["stale"])
    rev_after = engine.review(store, now=datetime(2026, 11, 12, 2, 0, tzinfo=UTC))
    assert any(it["ref"] == "concepts/tz.md" and "due" in it["reasons"] for it in rev_after["stale"])

    # check_anchor catches OSError cleanly without aborting validate().
    store.write("actions/demo/index.md", b"---\ntype: action\nstatus: open\ncreated: 2026-11-12\n---\n# Demo\n")
    store.write("actions/demo/outputs/extra.md", b"# Extra\n")
    store.write(
        "concepts/link-extra.md",
        b"---\ntype: guide\nstatus: stable\n---\n# Link\n\nSee [Extra](../actions/demo/outputs/extra.md#extra).\n",
    )
    engine.build(store)
    real_read = Store.read

    def _boom_read(self: Store, name: str, limit: int = models.MAX_FILE, *, private: bool = False) -> bytes:
        if name == "actions/demo/outputs/extra.md":
            raise PermissionError("denied")
        return real_read(self, name, limit, private=private)

    monkeypatch.setattr(Store, "read", _boom_read)
    rep_perm = engine.validate(store)
    assert not rep_perm["ok"]
    assert any("unreadable file" in p for p in rep_perm["problems"])


def test_authored_note_links_to_tracked_repo_files_validate_while_bf_read_stays_confined(tmp_path: Path) -> None:
    """Notes may link to non-private repo files (README.md, bf.yaml) while bf read and ingest stay note-dir confined."""
    from bf import query as query_mod
    from bf.models import InputError

    engine.init_brain(tmp_path / "repo-brain", name="repo-brain")
    store = Store(tmp_path / "repo-brain")
    store.write("README.md", b"# Repo\n\n## Operations {#operations}\n\nDocs.\n")
    store.write("skills/demo/SKILL.md", b"---\nname: demo\n---\n# Demo\n")
    store.write(
        "projects/main.md",
        (
            b"---\ntype: project\nstatus: active\nlinks:\n  - bf:///bf.yaml\n---\n\n# Main\n\n"
            b"See [ops](../README.md#operations) and [skill](../skills/demo/SKILL.md).\n"
        ),
    )
    built = engine.build(store)
    assert built["ok"]
    rep = engine.validate(store)
    assert rep["ok"], rep["problems"]

    # bf read remains strictly confined to configured note directories (including bare dir names).
    store.write("concepts.md", b"# Root Decoy\n")
    for denied in ("README.md", "bf:///bf.yaml", "skills/demo/SKILL.md", "bf:///concepts", "concepts?rel=x"):
        with pytest.raises(InputError, match="only configured note directories are readable"):
            query_mod.read_target(store, denied)

    # Broken repo file targets, missing Markdown anchors, and symlinks are caught by bf check.
    (store.root / "LINK.md").symlink_to(store.root / "README.md")
    store.write(
        "concepts/bad-repo-links.md",
        (
            b"---\ntype: concept\nstatus: draft\n---\n\n# Bad\n\n"
            b"[miss](../MISSING.md) [anc](../README.md#nope) [sym](../LINK.md)\n"
        ),
    )
    engine.build(store)
    bad_rep = engine.validate(store)
    joined = "\n".join(bad_rep["problems"])
    assert not bad_rep["ok"]
    assert "broken link to MISSING.md" in joined
    assert "missing anchor #nope in README.md" in joined
    assert "broken link to LINK.md" in joined


def test_live_screen_sorting_clipping_and_painting() -> None:
    """Default status sort pins failures/attention first; live screen clips ANSI/rows and paints TTY frames."""
    now_dt = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    summary = {
        "brain": "ops-brain",
        "ok": False,
        "counts": {"notes": 12, "memories": 340, "sources": 4, "loops": 16},
        "events_seq": 99,
        "attention": [{"name": "fail-high", "reason": "failed"}],
        "loops": {
            "idle-b": {"enabled": True, "status": "idle", "trigger": "manual"},
            "idle-a": {"enabled": True, "status": "idle", "trigger": "manual"},
            "stop-1": {"enabled": True, "status": "stopped", "trigger": "daemon"},
            "pause-1": {
                "enabled": True,
                "status": "paused",
                "paused": True,
                "paused_until": "2026-11-27T13:00:00Z",
                "trigger": "every 1h",
            },
            "ok-old": {
                "enabled": True,
                "status": "ok",
                "last_end": "2026-11-27T09:00:00Z",
                "trigger": "every 1h",
            },
            "ok-new": {
                "enabled": True,
                "status": "ok",
                "last_end": "2026-11-27T11:55:00Z",
                "trigger": "every 15m",
            },
            "run-old": {
                "enabled": True,
                "status": "running",
                "last_start": "2026-11-27T11:30:00Z",
                "trigger": "daemon",
            },
            "run-new": {
                "enabled": True,
                "status": "running",
                "last_start": "2026-11-27T11:59:00Z",
                "trigger": "every 5m",
            },
            "over-1": {"enabled": True, "status": "ok", "overdue": True, "trigger": "every 1h"},
            "deg-1": {"enabled": True, "status": "ok", "degraded": True, "trigger": "every 1h"},
            "lag-low": {"enabled": True, "status": "ok", "events_missed": 2, "trigger": "on a"},
            "lag-high": {"enabled": True, "status": "ok", "events_missed": 10, "trigger": "on b"},
            "crash-1": {"enabled": True, "status": "stopped", "crashed": True, "trigger": "daemon"},
            "fail-old": {
                "enabled": True,
                "status": "failed",
                "consecutive_failures": 2,
                "last_end": "2026-11-27T10:00:00Z",
                "error": "old timeout",
                "trigger": "every 5m",
            },
            "fail-new": {
                "enabled": True,
                "status": "failed",
                "consecutive_failures": 2,
                "last_end": "2026-11-27T11:05:00Z",
                "error": "new auth error",
                "trigger": "every 5m",
            },
            "fail-high": {
                "enabled": True,
                "status": "failed",
                "consecutive_failures": 5,
                "last_end": "2026-11-27T11:00:00Z",
                "error": "repeated failure",
                "trigger": "every 5m",
            },
        },
    }
    rendered = render.format_status_text(summary, color=False, now=now_dt)
    expected_order = [
        "fail-high",
        "fail-new",
        "fail-old",
        "crash-1",
        "lag-high",
        "lag-low",
        "deg-1",
        "over-1",
        "run-new",
        "run-old",
        "ok-new",
        "ok-old",
        "pause-1",
        "stop-1",
        "idle-a",
        "idle-b",
    ]
    positions = [rendered.index(f"  {name} ") for name in expected_order]
    assert positions == sorted(positions), rendered
    assert render._neg_epoch("") == 0.0  # noqa: SLF001
    assert render._neg_epoch("not-a-date") == 0.0  # noqa: SLF001

    # `_clip_ansi`: short/narrow pass-through, plain clip, mid-SGR clip with reset, post-SGR clip.
    assert render._clip_ansi("abcdef", 3) == "abcdef"  # noqa: SLF001
    assert render._clip_ansi("short", 10) == "short"  # noqa: SLF001
    assert render._clip_ansi("abcdefghij", 6) == "abcde…"  # noqa: SLF001
    clipped_mid = render._clip_ansi("  \033[31;1mfailed\033[0m  detail", 7)  # noqa: SLF001
    assert clipped_mid.endswith("…\033[0m")
    assert len(render._ANSI_SGR.sub("", clipped_mid)) == 7  # noqa: SLF001
    clipped_post = render._clip_ansi("  \033[31;1mfailed\033[0m  long detail here", 15)  # noqa: SLF001
    assert clipped_post.endswith("…\033[0m")
    assert len(render._ANSI_SGR.sub("", clipped_post)) == 15  # noqa: SLF001

    # `format_live_screen`: normal with recent (max 5) + footer, partial recent trim, zero spare recent, tight row truncation, narrow cols.
    recent_6 = [f"12:00:0{i}  ok  loop-{i}" for i in range(6)]
    full_screen = render.format_live_screen(
        summary,
        recent=recent_6,
        footer="Watching ops-brain (4w) · Ctrl+C to stop",
        color=True,
        now=now_dt,
    )
    assert "RECENT" in full_screen
    assert "loop-0" not in full_screen
    assert "loop-5" in full_screen
    assert "\033[90m" in full_screen
    assert "Watching ops-brain" in full_screen

    small_summary = {
        "brain": "mini",
        "ok": True,
        "counts": {"notes": 1, "memories": 0, "sources": 0, "loops": 1},
        "events_seq": 0,
        "loops": {"a": {"enabled": True, "status": "ok", "trigger": "manual"}},
    }
    # Body = 5 lines, footer = 2 lines, RECENT header = 2 lines -> rows=10 leaves spare=1 recent line!
    trimmed_recent = render.format_live_screen(
        small_summary, recent=recent_6, footer="foot", color=False, now=now_dt, rows=10
    )
    assert "RECENT" in trimmed_recent
    assert "loop-5" in trimmed_recent
    assert "loop-4" not in trimmed_recent
    # rows=8 leaves spare=-1 -> drops RECENT cleanly without truncating body!
    no_recent = render.format_live_screen(
        small_summary, recent=recent_6, footer="foot", color=False, now=now_dt, rows=8
    )
    assert "RECENT" not in no_recent
    assert "… (+" not in no_recent

    # Tight rows on 16-loop summary truncates body with explicit overflow hint and clamps cols!
    tight_screen = render.format_live_screen(
        summary,
        recent=recent_6,
        footer="Watching ops-brain · Ctrl+C to stop",
        color=True,
        now=now_dt,
        cols=36,
        rows=10,
    )
    tight_lines = tight_screen.rstrip("\n").splitlines()
    assert len(tight_lines) == 10
    assert "RECENT" not in tight_screen
    assert "more; run bf status" in tight_screen
    assert all(len(render._ANSI_SGR.sub("", ln)) <= 36 for ln in tight_lines)  # noqa: SLF001

    # `paint_frame`: plain UTF-8 on non-TTY vs `\033[H` + `\033[K` + `\033[J` on TTY.
    plain_b = render.paint_frame("line1\nline2\n", tty=False)
    assert plain_b == b"line1\nline2\n"
    tty_b = render.paint_frame("line1\nline2\n", tty=True)
    assert tty_b == b"\033[Hline1\033[K\nline2\033[K\033[J"
