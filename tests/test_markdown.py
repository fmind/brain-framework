"""Tests for Python-Markdown compilation, keyed attr_list metadata, universal refs, and bundle discovery."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bf import markdown, schemas
from bf.models import MAX_NOTE, Error
from bf.storage import Store


def test_note_roles_bundles_and_editor_locks(tmp_path: Path) -> None:
    assert markdown.note_role("projects/.#roadmap.md") is None
    assert markdown.note_role("projects/roadmap.txt") is None
    assert markdown.note_role("projects/sub/roadmap.md") == ("projects", False)
    assert markdown.note_role("concepts/aaif.md") == ("concepts", False)
    assert markdown.note_role("concepts/aaif/index.md") == ("concepts", True)
    assert markdown.note_role("notes/other.md") is None
    assert markdown.note_role("wiki/topic.md", ("wiki",)) == ("wiki", False)
    assert markdown.note_role("wiki/topic/index.md", ("wiki",)) == ("wiki", True)

    # Universal bundle rule: any subdir with `index.md` compiles ONLY `index.md`, ignoring sibling files/dirs.
    store = Store(tmp_path)
    store.write("actions/2026-11-15_ship/index.md", b"# Ship\n")
    store.write("actions/2026-11-15_ship/notes.md", b"# Sibling ignored\n")
    store.write("actions/2026-11-15_ship/outputs/decision.md", b"# Nested ignored\n")
    store.write("concepts/nested/topic.md", b"# Nested flat note\n")
    assert markdown.authored_notes(store) == [
        "actions/2026-11-15_ship/index.md",
        "concepts/nested/topic.md",
    ]


def test_compile_note_extracts_frontmatter_headings_links_tasks_and_unescapes_html() -> None:
    raw = (
        b"---\n"
        b"type: architecture\n"
        b"status: stable\n"
        b"updated: 2026-11-15\n"
        b"stale_after: 30d\n"
        b"owner: platform\n"
        b"tags: [mcp, ai, 2026, mcp]\n"
        b"links:\n"
        b"  - bf:///projects/launch.md\n"
        b"  - bf:///projects/launch.md\n"
        b"  - to: github:acme/agent-protocol#1\n"
        b"    meta:\n"
        b"      rel: implements\n"
        b"  - to: https://github.com/example/agent-protocol\n"
        b"    meta:\n"
        b"      rel: depends-on\n"
        b"  - to: urn:gcp:folder:123456789\n"
        b"    meta:\n"
        b"      rel: part-of\n"
        b"sources:\n"
        b"  - resource: jira:WEB-1\n"
        b"---\n\n"
        b"# Agent Protocol\n\n"
        b"Lead claim for R&D (`a < b && c > d`) with a footnote[^1], "
        b"[inline link](aaif.md#mcp){rel=depends-on}, "
        b"and [duplicate](../concepts/aaif.md#mcp){rel=depends-on}.\n\n"
        b"<div>Raw HTML block with A &amp; B</div>\n\n"
        b"## [MCP](https://modelcontextprotocol.io) {#mcp .protocol update-date=2026-11-15 creator=Anthropic}\n\n"
        b"| Name | Value |\n"
        b"| :--- | ----: |\n"
        b"| Cap  | 100   |\n\n"
        b"```python\n"
        b"# {#not-an-error?inside=fence}\n"
        b"<<<<<<< also ignored inside fence\n"
        b"x = 1 & 2\n"
        b"```\n\n"
        b"- [ ] Open task\n"
        b"  {: #t1 due=2026-12-01}\n"
        b"- [x] Finished task\n\n"
        b"> - [ ] Quoted checkbox is not a note task\n\n"
        b"[^1]: Footnote citing [evidence](demo:retention){rel=cites}.\n"
    )
    compiled = markdown.compile_note("concepts/protocol.md", raw, brain_name="demo-brain")
    assert "id" not in compiled
    assert compiled["ref"] == "concepts/protocol.md"
    assert compiled["source"] == "concepts"
    assert compiled["kind"] == "note"
    assert compiled["title"] == "Agent Protocol"
    assert compiled["owner"] == "platform"
    assert compiled["tags"] == ["mcp", "ai", "2026"]
    assert "a < b && c > d" in compiled["text"]
    assert "Raw HTML block with A & B" in compiled["text"]
    assert "&amp;" not in compiled["text"]
    assert "x = 1 & 2" in compiled["text"]
    assert "↩" not in compiled["text"]

    headings = {h["id"]: h for h in compiled["headings"]}
    assert headings["agent-protocol"] == {"id": "agent-protocol", "title": "Agent Protocol", "level": 1}
    assert headings["mcp"] == {
        "id": "mcp",
        "title": "MCP",
        "level": 2,
        "meta": {"class": "protocol", "update-date": "2026-11-15", "creator": "Anthropic"},
    }

    links = {(lnk["to"], lnk.get("anchor", "")): lnk for lnk in compiled["links"]}
    assert links[("projects/launch.md", "")]["kind"] == "note"
    assert ("jira:WEB-1", "") not in links
    assert links[("github:acme/agent-protocol", "1")]["meta"] == {"rel": "implements"}
    assert links[("https://github.com/example/agent-protocol", "")] == {
        "to": "https://github.com/example/agent-protocol",
        "kind": "url",
        "meta": {"rel": "depends-on"},
    }
    assert links[("urn:gcp:folder:123456789", "")] == {
        "to": "urn:gcp:folder:123456789",
        "kind": "url",
        "meta": {"rel": "part-of"},
    }
    assert links[("concepts/aaif.md", "mcp")] == {
        "to": "concepts/aaif.md",
        "kind": "note",
        "anchor": "mcp",
        "meta": {"rel": "depends-on"},
        "section": "agent-protocol",
    }
    assert links[("demo:retention", "")] == {
        "to": "demo:retention",
        "kind": "record",
        "meta": {"rel": "cites"},
        "section": "agent-protocol",
    }
    assert compiled["tasks"] == [
        {"text": "Open task", "done": False, "section": "mcp", "meta": {"due": "2026-12-01"}},
        {"text": "Finished task", "done": True, "section": "mcp"},
    ]


def test_compile_note_supports_task_and_link_attr_meta() -> None:
    raw = (
        b"---\n"
        b"type: guide\n"
        b"status: stable\n"
        b"links:\n"
        b"  - to: bf:///concepts/prior.md\n"
        b"    meta:\n"
        b"      rel: supersedes\n"
        b"---\n\n"
        b"# Attrs Note\n\n"
        b"See [evidence](demo:retention){rel=cites}.\n\n"
        b"- [ ] Ship release.\n"
        b"  {: priority=p0}\n"
    )
    compiled = markdown.compile_note("concepts/dprint.md", raw, brain_name="b")
    assert "{: priority=p0}" not in compiled["text"]
    assert compiled["tasks"] == [
        {"text": "Ship release.", "done": False, "section": "attrs-note", "meta": {"priority": "p0"}}
    ]
    assert compiled["links"] == [
        {"to": "concepts/prior.md", "kind": "note", "meta": {"rel": "supersedes"}},
        {"to": "demo:retention", "kind": "record", "meta": {"rel": "cites"}, "section": "attrs-note"},
    ]
    assert markdown.anchor_ids("concepts/dprint.md", raw) == {"attrs-note"}
    assert markdown.compiled_anchors(compiled) == {"attrs-note"}

    loose = markdown.compile_note(
        "concepts/loose-tasks.md",
        b"---\ntype: guide\nstatus: stable\n---\n\n# Loose Tasks\n\n"
        b"- [ ] Audit permissions\n  {: owner=sec}\n\n"
        b"- [x] Rotate keys\n  {: priority=p1}\n",
        brain_name="b",
    )
    assert loose["tasks"] == [
        {"text": "Audit permissions", "done": False, "section": "loose-tasks", "meta": {"owner": "sec"}},
        {"text": "Rotate keys", "done": True, "section": "loose-tasks", "meta": {"priority": "p1"}},
    ]
    assert "tags" not in loose
    assert "links" not in loose


def test_compile_note_fallbacks_and_custom_user_metadata() -> None:
    note_date = markdown.compile_note(
        "actions/2026-11-15_audit-trail/index.md",
        b"---\nstale_after: 2026-12-01\ncustom_field: 42\n---\n\nPlain body without headings.\n",
        brain_name="b",
    )
    assert note_date["title"] == "2026 11 15_audit trail"
    assert note_date["stale_after"] == "2026-12-01"
    assert note_date["custom_field"] == 42

    note_ts = markdown.compile_note(
        "projects/alpha.md",
        b"---\ntitle: Explicit Title\nstale_after: 2026-12-01T09:00:00+01:00\n---\n\n# Ignored H1\n",
        brain_name="b",
    )
    assert note_ts["title"] == "Explicit Title"


@pytest.mark.parametrize(
    ("path", "payload", "match"),
    [
        ("notes/bad.md", b"# Hi\n", "not an authored note path"),
        ("concepts/big.md", b"x" * (MAX_NOTE + 1), "exceeds"),
        ("concepts/utf.md", b"\xff\xfe", "not UTF-8"),
        ("concepts/unclosed.md", b"---\ntype: guide\n# Missing end\n", "unclosed YAML frontmatter"),
        ("concepts/reserved.md", b"---\nid: custom\n---\n# Hi\n", "cannot override computed field id"),
        ("concepts/conflict.md", b"# Hi\n<<<<<<< HEAD\n", "merge conflict marker"),
        ("concepts/badattr.md", b"## MCP {#mcp?license=mit}\n", "invalid attr_list syntax"),
        ("concepts/badtitle.md", b"---\ntitle: 123\n---\n# Hi\n", "title must be a string"),
        ("concepts/badtags.md", b"---\ntags: not-a-list\n---\n# Hi\n", "tags must be a list"),
        ("concepts/badtagval.md", b"---\ntags: ['has/slash']\n---\n# Hi\n", "without spaces or slashes"),
        ("concepts/badlinks.md", b"---\nlinks: [123]\n---\n# Hi\n", "frontmatter links must be a list"),
        ("concepts/badlinkmap.md", b"---\nlinks: nope\n---\n# Hi\n", "frontmatter links must be a list"),
        (
            "concepts/badlinkobj.md",
            b"---\nlinks:\n  - to: https://example.com\n    extra: 1\n---\n# Hi\n",
            "frontmatter links must be a list",
        ),
        ("concepts/badrecfm.md", b"---\nlinks: [concepts:welcome]\n---\n# Hi\n", "invalid record reference"),
        ("concepts/badprosebf.md", b"# Hi\n\nSee [bad](bf:///concepts:welcome).\n", "invalid record reference"),
        ("concepts/badrecid.md", b"# Hi\n\nSee [bad](bf:///jira:).\n", "invalid record reference"),
        (
            "concepts/badhttpsfrag.md",
            b"# Hi\n\nSee [bad](doc:HTTPS://example.com/spec#).\n",
            "invalid record reference",
        ),
        ("concepts/duphid.md", b"## A {#dup}\n\n## B {#dup}\n", "duplicate heading anchor #dup"),
    ],
)
def test_compile_note_rejects_invalid_inputs(path: str, payload: bytes, match: str) -> None:
    with pytest.raises(Error, match=match):
        markdown.compile_note(path, payload, brain_name="demo")


def test_parse_link_variants_and_rejections() -> None:
    same = markdown.parse_link("projects/web.md", "#plan", "my-brain")
    assert (same["to"], same["kind"], same["anchor"]) == ("projects/web.md", "note", "plan")

    act = markdown.parse_link("projects/web.md", "../actions/2026-11-15_go", "my-brain")
    assert act["to"] == "actions/2026-11-15_go"

    bf_concept = markdown.parse_link(
        "projects/web.md", "bf:///concepts/aaif.md#mcp", "my-brain", attr_meta={"rel": "uses"}
    )
    assert bf_concept == {
        "to": "concepts/aaif.md",
        "kind": "note",
        "anchor": "mcp",
        "meta": {"rel": "uses"},
        "section": "",
    }
    with pytest.raises(Error, match="bf:///concepts/"):
        markdown.parse_link("projects/web.md", "bf://concepts/aaif.md", "my-brain")
    with pytest.raises(Error, match="bf:///memories/"):
        markdown.parse_link("projects/web.md", "bf://memories/jira.jsonl", "my-brain")

    extless = markdown.parse_link("projects/web.md", "bf:///concepts/aaif#mcp", "my-brain")
    assert (extless["to"], extless["kind"], extless["anchor"]) == ("concepts/aaif", "note", "mcp")

    bf_self_rec = markdown.parse_link("projects/web.md", "bf://my-brain/jira:WEB-1", "my-brain")
    assert (bf_self_rec["to"], bf_self_rec["kind"]) == ("jira:WEB-1", "record")

    bf_other = markdown.parse_link(
        "projects/web.md", "bf://team-brain/actions/2026-11-15_sync/index.md#done", "my-brain"
    )
    assert (bf_other["to"], bf_other["kind"], bf_other["anchor"]) == (
        "bf://team-brain/actions/2026-11-15_sync/index.md",
        "brain",
        "done",
    )

    urn_url = markdown.parse_link("projects/web.md", "urn:ietf:rfc:3986", "my-brain", require_uri=True)
    assert urn_url["kind"] == "url"

    ext_url = markdown.parse_link(
        "projects/web.md", "https://example.com/docs?x=1", "my-brain", attr_meta={"rel": "ref"}, require_uri=True
    )
    assert (ext_url["kind"], ext_url["meta"]) == ("url", {"rel": "ref"})

    http_rec = markdown.parse_link("projects/web.md", "doc:https://example.com/spec", "my-brain")
    assert (http_rec["to"], http_rec["kind"]) == ("doc:https://example.com/spec", "record")

    rec_meta = markdown.parse_link("projects/web.md", "jira:WEB-1#top", "my-brain", attr_meta={"role": "blocker"})
    assert (rec_meta["to"], rec_meta["anchor"], rec_meta["meta"]) == ("jira:WEB-1", "top", {"role": "blocker"})

    env_ex = markdown.parse_link("projects/web.md", "../.env.example", "my-brain")
    assert (env_ex["to"], env_ex["kind"]) == (".env.example", "note")

    for bad_target, match in (
        ("https://example.com/" + "a" * 8200, "exceeds 8192"),
        ("concepts/bi\u202edi.md", "invisible"),
        ("bf://my-brain/concepts/%E2%80%AE.md", "invisible"),
        ("concepts/%00bad.md", "control characters"),
        ("concepts/%0abad.md", "control characters"),
        ("concepts/%20bad.md", "invalid Markdown link target"),
        ("bf:///concepts/%20bad.md", "expected bf:///path"),
        ("bf://%00bad/concepts/x.md", "control characters"),
        ("../.env", "private or runtime paths"),
        ("../.env.local", "private or runtime paths"),
        ("../.bf/events.jsonl", "private or runtime paths"),
        ("../logs/events.jsonl", "private or runtime paths"),
        ("bf:///logs/events.jsonl", "private or runtime paths"),
        ("bf://team-brain/.env", "private or runtime paths"),
        ("javascript:alert(1)", "unsupported link scheme"),
        ("bf:concepts/x.md", "invalid bf:// URI"),
        ("bf://my-brain/concepts/x.md#", "empty # fragment"),
        ("bf://my-brain/concepts/../secret.md", "invalid path in bf://"),
        ("BAD+SCHEME:123", "invalid record reference"),
        ("jira: has space", "invalid record reference"),
        ("jira:WEB-1#", "invalid record reference"),
        ("concepts/has space.md", "invalid Markdown link target"),
        ("concepts/x.md#", "trailing #"),
        ("concepts/%FF.md", "invalid UTF-8 percent-encoding"),
        ("/etc/passwd", "invalid relative or brain-root link path"),
        ("concepts//x.md", "invalid relative or brain-root link path"),
        ("concepts/../x.md", "must not contain . or .."),
        ("../../escape.md", "leaves the brain"),
        ("concepts/x.md?", "invalid link query"),
        ("bf:///concepts/x.md?", "invalid link query"),
        ("concepts/x.md?bad", "invalid link query"),
        ("concepts/x.md?rel=", "must be nonempty"),
        ("concepts/x.md?1bad=", "unique identifiers"),
        ("concepts/x.md?1bad=a", "unique identifiers"),
        ("concepts/x.md?rel=a&rel=b", "unique identifiers"),
        ("concepts/x.md?rel=a&rel=", "unique identifiers"),
        ("concepts/%20bad.md?rel=a", "invalid Markdown link target"),
        ("jira:?rel=a", "record id must be nonempty"),
    ):
        with pytest.raises(Error, match=match):
            markdown.parse_link("projects/web.md", bad_target, "my-brain")

    with pytest.raises(Error, match="control characters"):
        markdown.parse_link("projects/web.md", "concepts/x.md", "my-brain", section="bad\nsec")
    with pytest.raises(Error, match="invisible"):
        markdown.parse_link("projects/web.md", "concepts/x.md", "my-brain", section="bad\u202esec")
    with pytest.raises(Error, match="invalid link attribute"):
        markdown.parse_link("projects/web.md", "concepts/x.md", "my-brain", attr_meta={"1bad": "v"})
    with pytest.raises(Error, match="conflicting link attribute"):
        markdown.parse_link("projects/web.md", "concepts/x.md?rel=a", "my-brain", attr_meta={"rel": "b"})
    with pytest.raises(Error, match="fragment-only link"):
        markdown.parse_link("projects/web.md", "#only-frag", "my-brain", relative_base=False)

    # Query-param shorthand `?key=value` on note, `bf://`, cross-brain, and non-URL `source:id` links.
    q_note = markdown.parse_link("projects/web.md", "bf:///projects/fmind-dev.md?rel=related-to#sec", "my-brain")
    assert q_note == {
        "to": "projects/fmind-dev.md",
        "kind": "note",
        "anchor": "sec",
        "meta": {"rel": "related-to"},
        "section": "",
    }
    q_rel = markdown.parse_link("projects/web.md", "../concepts/a.md?rel=cites&weight=high", "my-brain")
    assert q_rel["to"] == "concepts/a.md"
    assert q_rel["meta"] == {"rel": "cites", "weight": "high"}
    q_rec = markdown.parse_link("projects/web.md", "jira:WEB-1?rel=blocks", "my-brain")
    assert q_rec["to"] == "jira:WEB-1"
    assert q_rec["meta"] == {"rel": "blocks"}
    q_bf_rec = markdown.parse_link("projects/web.md", "bf://team/jira:WEB-2?rel=depends-on", "my-brain")
    assert q_bf_rec["to"] == "bf://team/jira:WEB-2"
    assert q_bf_rec["kind"] == "brain"
    assert q_bf_rec["meta"] == {"rel": "depends-on"}
    q_url_rec = markdown.parse_link("projects/web.md", "web:https://example.com/p?a=1&b=2", "my-brain")
    assert q_url_rec["to"] == "web:https://example.com/p?a=1&b=2"
    assert q_url_rec["meta"] == {}
    q_bf_url_rec = markdown.parse_link("projects/web.md", "bf://team/web:https://example.com/p?a=1", "my-brain")
    assert q_bf_url_rec["to"] == "bf://team/web:https://example.com/p?a=1"
    assert q_bf_url_rec["meta"] == {}
    q_merge = markdown.parse_link(
        "projects/web.md", "concepts/a.md?rel=cites", "my-brain", attr_meta={"owner": "alice"}
    )
    assert q_merge["meta"] == {"rel": "cites", "owner": "alice"}
    assert markdown.record_link_problem({"kind": "note", "to": "concepts/a.md?rel=cites", "anchor": ""})
    assert markdown.record_link_problem({"kind": "brain", "to": "bf://team/concepts/a.md?rel=cites", "anchor": ""})
    assert markdown.record_link_problem({"kind": "record", "to": "jira:WEB-1?rel=cites", "anchor": ""})
    assert markdown.record_link_problem({"kind": "brain", "to": "bf://team/jira:WEB-1?rel=cites", "anchor": ""})


def test_anchor_ids_handles_frontmatter_and_rejects_bad_files() -> None:
    raw = b"\xef\xbb\xbf---\ntitle: Attachment\n---\n\n# Overview\n\n## Details {#custom-id}\n\nBody.\n"
    assert markdown.anchor_ids("actions/a/outputs/out.md", raw) == {"overview", "custom-id"}
    with pytest.raises(Error, match="exceeds"):
        markdown.anchor_ids("out.md", b"x" * (MAX_NOTE + 1))
    with pytest.raises(Error, match="not UTF-8"):
        markdown.anchor_ids("out.md", b"\xff")

    tricky = (
        "Setext Section {#setext-id}\n--------------\n\n"
        "- list item\n  wrapped before hr\n---\n\nAfter hr.\n\n## Next\n\nTail.\n"
    )
    comp = markdown.compile_note("concepts/setext.md", tricky.encode(), brain_name="demo")
    sliced = markdown.extract_section(tricky, "setext-id", comp)
    assert "After hr." in sliced["text"]
    assert "Tail." not in sliced["text"]


def test_universal_ref_links_schema_and_lazy_imports() -> None:
    raw = (
        b"---\n"
        b"links:\n"
        b"  - projects/launch.md\n"
        b"  - to: jira:WEB-1\n"
        b"    meta:\n"
        b"      rel: tracks\n"
        b"  - bf://work/jira:WEB-2\n"
        b"---\n\n"
        b"# Unified\n\n"
        b"See [same](bf:///concepts/other.md#sec), [rec](jira:WEB-3){rel=cites}, "
        b"[urlrec](doc:HTTPS://example.com/spec#v2).\n"
    )
    compiled = markdown.compile_note("concepts/unified.md", raw, brain_name="demo")
    val = schemas.compile_schema(schemas.RECORD_SCHEMA, "record.json")
    assert schemas.validate_instance(val, compiled, "concepts/unified.md") == []
    assert {(lnk["kind"], lnk["to"], lnk.get("anchor", "")) for lnk in compiled["links"]} == {
        ("note", "projects/launch.md", ""),
        ("record", "jira:WEB-1", ""),
        ("brain", "bf://work/jira:WEB-2", ""),
        ("note", "concepts/other.md", "sec"),
        ("record", "jira:WEB-3", ""),
        ("record", "doc:HTTPS://example.com/spec", "v2"),
    }

    assert markdown.is_record_ref("jira:WEB-1")
    assert not markdown.is_record_ref("concepts:welcome")
    assert not markdown.is_record_ref("jira:bad id")
    assert markdown.record_link_problem({"kind": "record", "to": "jira:WEB-1"}) == ""
    assert markdown.record_link_problem({"kind": "brain", "to": "bf://work/jira:WEB-1"}) == ""
    assert "must use <source>:<id>" in markdown.record_link_problem({"kind": "record", "to": "concepts:welcome"})
    assert "#anchor" in markdown.record_link_problem({"kind": "record", "to": "jira:WEB-1", "anchor": "frag"})
    assert "#anchor" in markdown.record_link_problem({"kind": "brain", "to": "bf://work/jira:WEB-1", "anchor": "frag"})

    pct_ref = "local-docs:docs/My%20Report.pdf"
    assert markdown.parse_link("concepts/a.md", pct_ref, "b")["to"] == pct_ref
    pct_bf = markdown.parse_link("concepts/a.md", f"bf:///{pct_ref}", "b", attr_meta={"rel": "cites"})
    assert (pct_bf["to"], pct_bf["meta"]) == (pct_ref, {"rel": "cites"})
    http_ref = "web:https://example.com/p?a=1&b=2"
    assert markdown.parse_link("concepts/a.md", http_ref, "b")["to"] == http_ref
    assert markdown.parse_link("concepts/a.md", f"bf:///{http_ref}", "b")["to"] == http_ref

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, bf.markdown; "
                "bf.markdown.parse_link('concepts/a.md', 'jira:WEB-1', 'b'); "
                "assert 'markdown' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
