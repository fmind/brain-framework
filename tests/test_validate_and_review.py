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
    assert (root / "docs" / "index.md").is_file()
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

    # Compiled-note drift, corruption, missing compiled JSON, and orphans fail `validate`.
    raw_welcome = store.read("concepts/welcome.md")
    store.write("concepts/unbuilt.md", b"---\ntype: guide\nstatus: draft\n---\n\n# Unbuilt\n")
    assert any("missing compiled note for concepts/unbuilt.md" in p for p in engine.validate(store)["problems"])
    store.delete("concepts/unbuilt.md")

    store.write("memories/concepts/orphan.json", b'{"ref":"concepts/orphan.md"}\n')
    assert any("memories/concepts/orphan.json: orphaned compiled note" in p for p in engine.validate(store)["problems"])
    store.delete("memories/concepts/orphan.json")

    compiled_bytes = store.read("memories/concepts/welcome.json")
    store.write("concepts/welcome.md", raw_welcome + b"\nEdited without rebuild.\n")
    assert any("out of sync with concepts/welcome.md" in p for p in engine.validate(store)["problems"])
    store.write("memories/concepts/welcome.json", b"{corrupt\n")
    assert any("memories/concepts/welcome.json: invalid JSON" in p for p in engine.validate(store)["problems"])
    store.write("concepts/welcome.md", raw_welcome)
    store.write("memories/concepts/welcome.json", compiled_bytes)

    tampered = json.loads(store.read("memories/actions/2026-11-15_ship-pure-json.json"))
    tampered["title"] = "Tampered Title"
    store.write("memories/actions/2026-11-15_ship-pure-json.json", encode(tampered))
    assert any(
        "out of sync with actions/2026-11-15_ship-pure-json/ACTION.md" in p for p in engine.validate(store)["problems"]
    )
    assert engine.build(store, incremental=True)["written"] == 0
    assert not engine.validate(store)["ok"]
    assert engine.build(store)["written"] == 1
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

    # Tampered compiled note JSON with matching `sha256` is still caught by `validate`!
    tampered = json.loads(store.read("memories/concepts/welcome.json"))
    tampered["status"] = "invalid-status"
    store.write("memories/concepts/welcome.json", (json.dumps(tampered) + "\n").encode())
    assert any(
        "memories/concepts/welcome.json" in p or "concepts/welcome.md" in p for p in engine.validate(store)["problems"]
    )
    engine.build(store)
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

    # Schema, .env, and eval errors (`logs/` holds `structlog` JSON files and is not schema-validated).
    brain.write(".env", b'BAD_QUOTE="unclosed\\"\n')
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
                    {"to": "meetings:decision-1", "kind": "record", "anchor": "", "meta": {}, "section": ""},
                    {"to": "projects:bad", "kind": "note", "anchor": "", "meta": {}, "section": ""},
                    {"to": "bf://sib/events:ev-1", "kind": "brain", "anchor": "", "meta": {}, "section": ""},
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

    report = engine.validate(brain)
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
        "memories/dupes/b.json: duplicate record id 'd1'",
        "memories/dupes/a.json: source must equal 'dupes'",
        "memories/dupes/a.json: ref must equal 'dupes:d1'",
        "memories/relbad/r.json: 'relations' was removed; use 'links'",
        "memories/relbad/r.json: record link 'meetings:decision-1' must use memories/<source>/<sha256>.json",
        "memories/relbad/r.json: note link 'projects:bad' must target projects/, concepts/, or actions/",
        "memories/relbad/r.json: cross-brain link 'bf://sib/events:ev-1' must use bf://<brain>/<path>",
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
    assert rep["tasks_open"] == 2
    assert rep["actions"] == [
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
