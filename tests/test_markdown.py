"""Tests for Python-Markdown compilation, keyed attr_list metadata, bf:// URLs, and OKF rules."""

from __future__ import annotations

import pytest

from bf import markdown
from bf.models import MAX_NOTE, Error


def test_okf_roles_and_editor_locks() -> None:
    assert markdown.editor_lock("projects/.#roadmap.md")
    assert markdown.okf_role("projects/.#roadmap.md") is None
    assert markdown.okf_role("projects/roadmap.txt") is None
    assert markdown.okf_role("projects/sub/roadmap.md") == ("projects", "project")
    assert markdown.okf_role("concepts/aaif.md") == ("concepts", "concept")
    assert markdown.okf_role("actions/2026-11-15_ship/ACTION.md") == ("actions", "action")
    assert markdown.okf_role("actions/2026-11-15_ship/outputs/decision.md") is None
    assert markdown.okf_role("notes/other.md") is None


def test_compile_note_extracts_frontmatter_headings_keyed_attrs_and_unescapes_html() -> None:
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
        b"  - bf:///memories/github/0359a07b64f9a310ea420eca77e5b713b6b10ee3cdc9be5e6ad0ea1323889879.json?rel=implements\n"
        b"  - to: https://github.com/example/agent-protocol\n"
        b"    meta:\n"
        b"      rel: depends-on\n"
        b"  - to: urn:gcp:folder:123456789\n"
        b"    meta:\n"
        b"      rel: part-of\n"
        b"sources:\n"
        b"  - resource: bf:///memories/jira/6e727f27acc6af6f2cd2f6f2bc2eb801d4b22cfe3d43e5eb35404d0f6ac70fe4.json\n"
        b"---\n\n"
        b"# Agent Protocol\n\n"
        b"Lead claim for R&D (`a < b && c > d`) with a footnote[^1], "
        b"[inline link](aaif.md?date=2026#mcp){rel=depends-on}, "
        b"and [duplicate](../concepts/aaif.md?date=2026#mcp){rel=depends-on}.\n"
        b"{: #lead confidence=high .featured}\n\n"
        b"<div>Raw HTML block with A &amp; B</div>\n\n"
        b"## [MCP](https://modelcontextprotocol.io) {#mcp .protocol update-date=2026-11-15 creator=Anthropic}\n\n"
        b"Term\n"
        b":   Definition body\n"
        b"    {: #def-mcp scope=local}\n\n"
        b'!!! warning custom "Quota"\n'
        b"    {: #quota-box}\n"
        b"    Watch rate limits.\n\n"
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
        b"[^1]: Footnote citing [evidence](../memories/demo/e8bd59a71e7e467bb66d5c98314e26f78547a7dd6aba8980cf2e4edd046a4851.json){rel=cites}.\n"
    )
    compiled = markdown.compile_note("concepts/protocol.md", raw, brain_name="demo-brain")
    assert "id" not in compiled
    assert compiled["ref"] == "concepts/protocol.md"
    assert compiled["source"] == "concepts"
    assert compiled["kind"] == "concept"
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

    # Keyed attr_list map (`attrs`) contains headings, blocks, definitions, and tasks by #id!
    assert compiled["attrs"]["mcp"] == {"class": "protocol", "update-date": "2026-11-15", "creator": "Anthropic"}
    assert compiled["attrs"]["lead"] == {"confidence": "high", "class": "featured"}
    assert compiled["attrs"]["def-mcp"] == {"scope": "local"}
    assert compiled["attrs"]["t1"] == {"due": "2026-12-01"}

    links = {(lnk["to"], lnk.get("anchor", "")): lnk for lnk in compiled["links"]}
    assert links[("projects/launch.md", "")]["kind"] == "note"
    assert (
        links[("memories/jira/6e727f27acc6af6f2cd2f6f2bc2eb801d4b22cfe3d43e5eb35404d0f6ac70fe4.json", "")]["kind"]
        == "record"
    )
    assert links[("memories/github/0359a07b64f9a310ea420eca77e5b713b6b10ee3cdc9be5e6ad0ea1323889879.json", "")][
        "meta"
    ] == {"rel": "implements"}
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
        "meta": {"date": "2026", "rel": "depends-on"},
        "section": "agent-protocol",
    }
    assert links[("memories/demo/e8bd59a71e7e467bb66d5c98314e26f78547a7dd6aba8980cf2e4edd046a4851.json", "")] == {
        "to": "memories/demo/e8bd59a71e7e467bb66d5c98314e26f78547a7dd6aba8980cf2e4edd046a4851.json",
        "kind": "record",
        "meta": {"rel": "cites"},
        "section": "agent-protocol",
    }

    elem_by_tag = {el["tag"]: el for el in compiled["elements"]}
    assert elem_by_tag["p"]["id"] == "lead"
    assert elem_by_tag["p"]["meta"] == {"confidence": "high", "class": "featured"}
    assert elem_by_tag["dd"]["id"] == "def-mcp"
    assert elem_by_tag["admonition"]["meta"]["type"] == "warning"

    assert compiled["tasks"] == [
        {"id": "t1", "text": "Open task", "done": False, "section": "mcp", "meta": {"due": "2026-12-01"}},
        {"text": "Finished task", "done": True, "section": "mcp"},
    ]


def test_compile_note_supports_dprint_single_line_block_attrs_and_link_ids() -> None:
    # dprint (`textWrap: "never"`) collapses `{: #id ...}` onto the end of paragraphs, definitions, and tasks!
    raw = (
        b"---\n"
        b"type: guide\n"
        b"status: stable\n"
        b"links:\n"
        b"  - bf:///concepts/prior.md?rel=supersedes\n"
        b"---\n\n"
        b"# Single Line Attrs\n\n"
        b"Paragraph formatted on one line. {: #para-1 role=principle}\n\n"
        b"See [evidence](../memories/demo/e8bd59a71e7e467bb66d5c98314e26f78547a7dd6aba8980cf2e4edd046a4851.json){#lnk-1 rel=cites}.\n\n"
        b"- [ ] Ship release. {: #task-ship priority=p0}\n"
    )
    compiled = markdown.compile_note("concepts/dprint.md", raw, brain_name="b")
    assert "{: #para-1" not in compiled["text"]
    assert "{: #task-ship" not in compiled["text"]
    assert compiled["attrs"]["para-1"] == {"role": "principle"}
    assert compiled["attrs"]["lnk-1"] == {"rel": "cites"}
    assert compiled["attrs"]["task-ship"] == {"priority": "p0"}
    assert compiled["tasks"] == [
        {
            "id": "task-ship",
            "text": "Ship release.",
            "done": False,
            "section": "single-line-attrs",
            "meta": {"priority": "p0"},
        }
    ]
    assert compiled["links"] == [
        {"to": "concepts/prior.md", "kind": "note", "meta": {"rel": "supersedes"}},
        {
            "to": "memories/demo/e8bd59a71e7e467bb66d5c98314e26f78547a7dd6aba8980cf2e4edd046a4851.json",
            "kind": "record",
            "meta": {"rel": "cites"},
            "section": "single-line-attrs",
        },
    ]
    assert markdown.anchor_ids("concepts/dprint.md", raw) >= {"single-line-attrs", "para-1", "lnk-1", "task-ship"}

    # Loose task list items (blank lines between items -> `<p>` inside `<li>`) preserve `{: #id key=val}` without duplicate `<p>` elements!
    loose = markdown.compile_note(
        "concepts/loose-tasks.md",
        b"---\ntype: guide\nstatus: stable\n---\n\n# Loose Tasks\n\n"
        b"- [ ] Audit permissions\n  {: #loose-1 owner=sec}\n\n"
        b"- [x] Rotate keys {: #loose-2 priority=p1}\n",
        brain_name="b",
    )
    assert loose["attrs"]["loose-1"] == {"owner": "sec"}
    assert loose["attrs"]["loose-2"] == {"priority": "p1"}
    assert loose["tasks"] == [
        {
            "id": "loose-1",
            "text": "Audit permissions",
            "done": False,
            "section": "loose-tasks",
            "meta": {"owner": "sec"},
        },
        {"id": "loose-2", "text": "Rotate keys", "done": True, "section": "loose-tasks", "meta": {"priority": "p1"}},
    ]
    assert "elements" not in loose
    assert "tags" not in loose
    assert "links" not in loose


def test_compile_note_fallbacks_and_custom_user_metadata() -> None:
    note_date = markdown.compile_note(
        "actions/2026-11-15_audit-trail/ACTION.md",
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
        ("notes/bad.md", b"# Hi\n", "not an OKF note path"),
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
        ("concepts/badrel.md", b"---\nrelations: []\n---\n# Hi\n", "'relations' was removed; use 'links'"),
        ("concepts/badrecfm.md", b"---\nlinks: [concepts:welcome]\n---\n# Hi\n", "must use memories/"),
        ("concepts/badprosebf.md", b"# Hi\n\nSee [bad](bf:///concepts:welcome).\n", "must use memories/"),
        ("concepts/badrecid.md", b"# Hi\n\nSee [bad](bf:///jira:).\n", "invalid record reference"),
        (
            "concepts/badhttpsfrag.md",
            b"# Hi\n\nSee [bad](doc:HTTPS://example.com/spec#).\n",
            "invalid record reference",
        ),
        ("concepts/duphid.md", b"## A {#dup}\n\n## B {#dup}\n", "duplicate anchor #dup"),
    ],
)
def test_compile_note_rejects_invalid_inputs(path: str, payload: bytes, match: str) -> None:
    with pytest.raises(Error, match=match):
        markdown.compile_note(path, payload, brain_name="demo")


def test_parse_link_variants_and_rejections() -> None:
    same = markdown.parse_link("projects/web.md", "#plan", "my-brain")
    assert (same["to"], same["kind"], same["anchor"]) == ("projects/web.md", "note", "plan")

    act = markdown.parse_link("projects/web.md", "../actions/2026-11-15_go", "my-brain")
    assert act["to"] == "actions/2026-11-15_go/ACTION.md"

    # Standard RFC 3986 triple-slash bf:///concepts/aaif.md succeeds; 2-slash bf://concepts/... is rejected!
    bf_concept = markdown.parse_link("projects/web.md", "bf:///concepts/aaif.md?rel=uses#mcp", "my-brain")
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
        markdown.parse_link("projects/web.md", "bf://memories/jira/abc.json", "my-brain")

    # Extensionless note target (`concepts/aaif` -> `concepts/aaif.md`) and `#<jq_filter>` fragment:
    extless = markdown.parse_link("projects/web.md", "bf:///concepts/aaif#mcp", "my-brain")
    assert (extless["to"], extless["kind"], extless["anchor"]) == ("concepts/aaif.md", "note", "mcp")

    jq_frag = markdown.parse_link(
        "projects/web.md", 'bf:///memories/concepts/aaif.json#select(.status=="stable")', "my-brain"
    )
    assert jq_frag["anchor"] == 'select(.status=="stable")'

    bf_self_rec = markdown.parse_link("projects/web.md", "bf://my-brain/jira:WEB-1", "my-brain")
    assert (bf_self_rec["to"], bf_self_rec["kind"]) == (markdown.record_path("jira:WEB-1"), "record")

    bf_other = markdown.parse_link("projects/web.md", "bf://team-brain/actions/2026-11-15_sync#done", "my-brain")
    assert bf_other["to"] == "bf://team-brain/actions/2026-11-15_sync/ACTION.md"
    assert (bf_other["kind"], bf_other["anchor"]) == ("brain", "done")

    urn_url = markdown.parse_link("projects/web.md", "urn:ietf:rfc:3986", "my-brain", require_uri=True)
    assert urn_url["kind"] == "url"

    ext_url = markdown.parse_link(
        "projects/web.md", "https://example.com/docs?x=1", "my-brain", attr_meta={"rel": "ref"}, require_uri=True
    )
    assert (ext_url["kind"], ext_url["meta"]) == ("url", {"rel": "ref"})

    http_rec = markdown.parse_link("projects/web.md", "doc:https://example.com/spec", "my-brain")
    assert (http_rec["to"], http_rec["kind"]) == (markdown.record_path("doc:https://example.com/spec"), "record")

    rec_meta = markdown.parse_link("projects/web.md", "jira:WEB-1?role=blocker#top", "my-brain")
    assert (rec_meta["to"], rec_meta["anchor"], rec_meta["meta"]) == (
        markdown.record_path("jira:WEB-1"),
        "top",
        {"role": "blocker"},
    )

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
        ("../logs/events.log", "private or runtime paths"),
        ("bf:///logs/events.log", "private or runtime paths"),
        ("bf://team-brain/.env", "private or runtime paths"),
        ("javascript:alert(1)", "unsupported link scheme"),
        ("bf:concepts/x.md", "invalid bf:// URI"),
        ("bf://my-brain/concepts/x.md#", "empty # fragment"),
        ("bf://my-brain/concepts/../secret.md", "invalid path in bf://"),
        ("BAD+SCHEME:123", "invalid record reference"),
        ("jira: has space", "invalid record reference"),
        ("jira:WEB-1#", "invalid record reference"),
        ("jira:?a=1", "record id must be nonempty"),
        ("concepts/has space.md", "invalid Markdown link target"),
        ("concepts/x.md#", "trailing #"),
        ("concepts/%FF.md", "invalid UTF-8 percent-encoding"),
        ("?rel=x", "empty link target"),
        ("/etc/passwd", "invalid relative or brain-root link path"),
        ("concepts//x.md", "invalid relative or brain-root link path"),
        ("concepts/../x.md", "must not contain . or .."),
        ("../../escape.md", "leaves the brain"),
        ("concepts/x.md?badquery", "invalid link query"),
        ("concepts/x.md?a=1&a=2", "unique identifiers"),
    ):
        with pytest.raises(Error, match=match):
            markdown.parse_link("projects/web.md", bad_target, "my-brain")

    with pytest.raises(Error, match="control characters"):
        markdown.parse_link("projects/web.md", "concepts/x.md", "my-brain", section="bad\nsec")
    with pytest.raises(Error, match="invisible"):
        markdown.parse_link("projects/web.md", "concepts/x.md", "my-brain", section="bad\u202esec")

    with pytest.raises(Error, match="conflicting link metadata"):
        markdown.parse_link("projects/web.md", "concepts/x.md?rel=a", "my-brain", attr_meta={"rel": "b"})
    with pytest.raises(Error, match="invalid link attribute"):
        markdown.parse_link("projects/web.md", "concepts/x.md", "my-brain", attr_meta={"1bad": "v"})
    with pytest.raises(Error, match="fragment-only link"):
        markdown.parse_link("projects/web.md", "#only-frag", "my-brain", relative_base=False)


def test_anchor_ids_handles_frontmatter_and_rejects_bad_files() -> None:
    raw = b"\xef\xbb\xbf---\ntitle: Attachment\n---\n\n# Overview\n\n## Details {#custom-id}\n\nBody.\n{: #blk-1}\n"
    assert markdown.anchor_ids("actions/a/outputs/out.md", raw) == {"overview", "custom-id", "blk-1"}
    with pytest.raises(Error, match="exceeds"):
        markdown.anchor_ids("out.md", b"x" * (MAX_NOTE + 1))
    with pytest.raises(Error, match="not UTF-8"):
        markdown.anchor_ids("out.md", b"\xff")

    # Setext `{#id}` and wrapped list items immediately before `---` thematic break slice accurately without truncation!
    tricky = (
        "Setext Section {#setext-id}\n--------------\n\n"
        "- list item\n  wrapped before hr\n---\n\nAfter hr.\n\n## Next\n\nTail.\n"
    )
    comp = markdown.compile_note("concepts/setext.md", tricky.encode(), brain_name="demo")
    sliced = markdown.extract_section(tricky, "setext-id", comp)
    assert "After hr." in sliced["text"]
    assert "Tail." not in sliced["text"]


def test_unified_links_compact_schema_and_reading_helpers() -> None:
    import subprocess
    import sys

    from bf import schemas

    raw = (
        b"---\n"
        b"links:\n"
        b"  - projects/launch.md\n"
        b"  - jira:WEB-1?rel=tracks\n"
        b"  - bf://work/jira:WEB-2\n"
        b"---\n\n"
        b"# Unified\n\n"
        b"See [same](bf:///concepts/other.md#sec), [rec](jira:WEB-3?rel=cites), "
        b"[urlrec](doc:HTTPS://example.com/spec#v2).\n"
    )
    compiled = markdown.compile_note("concepts/unified.md", raw, brain_name="demo")
    val = schemas.compile_schema(schemas.RECORD_SCHEMA, "record.json")
    assert schemas.validate_instance(val, compiled, "concepts/unified.md") == []
    assert {(lnk["kind"], lnk["to"], lnk.get("anchor", "")) for lnk in compiled["links"]} == {
        ("note", "projects/launch.md", ""),
        ("record", markdown.record_path("jira:WEB-1"), ""),
        ("brain", f"bf://work/{markdown.record_path('jira:WEB-2')}", ""),
        ("note", "concepts/other.md", "sec"),
        ("record", markdown.record_path("jira:WEB-3"), ""),
        ("record", markdown.record_path("doc:HTTPS://example.com/spec"), "v2"),
    }

    assert markdown.is_record_ref("jira:WEB-1")
    assert not markdown.is_record_ref("concepts:welcome")
    assert not markdown.is_record_ref("jira:bad id")
    w1_path = markdown.record_path("jira:WEB-1")
    assert w1_path is not None
    assert markdown.record_link_problem({"kind": "record", "to": w1_path}) == ""
    assert markdown.record_link_problem({"kind": "brain", "to": f"bf://work/{w1_path}"}) == ""
    assert "must use memories/" in markdown.record_link_problem({"kind": "record", "to": "jira:WEB-1"})
    assert "must use memories/" in markdown.record_link_problem({"kind": "record", "to": "concepts:welcome"})
    assert "#anchor" in markdown.record_link_problem({"kind": "record", "to": w1_path, "anchor": "frag"})

    # Percent-encoded sensor record IDs and HTTP record IDs with query strings hash verbatim in bare and bf:// forms.
    pct_ref = "local-docs:docs/My%20Report.pdf"
    pct_expected = markdown.record_path(pct_ref)
    assert pct_expected is not None
    assert markdown.parse_link("concepts/a.md", pct_ref, "b")["to"] == pct_expected
    pct_q = markdown.parse_link("concepts/a.md", f"{pct_ref}?rel=cites", "b")
    assert pct_q["to"] == pct_expected
    assert pct_q["meta"] == {"rel": "cites"}
    pct_bf = markdown.parse_link("concepts/a.md", f"bf:///{pct_ref}?rel=cites", "b")
    assert pct_bf["to"] == pct_expected
    assert pct_bf["meta"] == {"rel": "cites"}
    http_ref = "web:https://example.com/p?a=1&b=2"
    http_expected = markdown.record_path(http_ref)
    assert http_expected is not None
    assert markdown.parse_link("concepts/a.md", http_ref, "b")["to"] == http_expected
    assert markdown.parse_link("concepts/a.md", f"bf:///{http_ref}", "b")["to"] == http_expected

    assert markdown.parse_line_spec("3") == (3, 1)
    assert markdown.parse_line_spec("2:") == (2, None)
    assert markdown.parse_line_spec("2:5") == (2, 5)
    for bad_spec in ("0", "1:2:3", "1:-2", "abc"):
        with pytest.raises(Error):
            markdown.parse_line_spec(bad_spec)
    with pytest.raises(Error):
        markdown.slice_lines("a\nb", start=0)

    sl = markdown.slice_lines("a\nb\nc\nd", start=2, count=2)
    assert sl == {"total_lines": 4, "start": 2, "count": 2, "content": "b\nc"}
    assert markdown.slice_lines("a\nb\nc", start=2, count=None) == {
        "total_lines": 3,
        "start": 2,
        "count": 2,
        "content": "b\nc",
    }
    assert markdown.slice_lines("a\nb", start=1, count=0) == {"total_lines": 2, "start": 1, "count": 0, "content": ""}
    assert markdown.slice_lines("a\nb", start=9, count=2) == {"total_lines": 2, "start": 9, "count": 0, "content": ""}
    outline = markdown.note_outline(compiled)
    assert outline["links_count"] == 6
    assert outline["tasks_open"] == 0
    assert "text" not in outline

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, bf.markdown; "
                "bf.markdown.parse_link('concepts/a.md', 'jira:WEB-1', 'b'); "
                "bf.markdown.slice_lines('a\\nb', 1, 1); "
                "assert 'markdown' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
