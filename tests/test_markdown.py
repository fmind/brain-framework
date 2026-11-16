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
        b"sources:\n"
        b"  - resource: bf:///memories/jira/6e727f27acc6af6f2cd2f6f2bc2eb801d4b22cfe3d43e5eb35404d0f6ac70fe4.json\n"
        b"relations:\n"
        b"  - target: bf:///memories/github/0359a07b64f9a310ea420eca77e5b713b6b10ee3cdc9be5e6ad0ea1323889879.json\n"
        b"    type: implements\n"
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
    assert compiled["id"] == "concepts/protocol.md"
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
    assert headings["agent-protocol"]["level"] == 1
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

    links = {(lnk["to"], lnk["anchor"]): lnk for lnk in compiled["links"]}
    assert links[("projects/launch.md", "")]["kind"] == "note"
    assert (
        links[("memories/jira/6e727f27acc6af6f2cd2f6f2bc2eb801d4b22cfe3d43e5eb35404d0f6ac70fe4.json", "")]["kind"]
        == "record"
    )
    assert links[("memories/github/0359a07b64f9a310ea420eca77e5b713b6b10ee3cdc9be5e6ad0ea1323889879.json", "")][
        "meta"
    ] == {"rel": "implements"}
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
        "anchor": "",
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
        {"text": "Finished task", "done": True, "section": "mcp", "meta": {}},
    ]


def test_compile_note_supports_dprint_single_line_block_attrs_and_link_ids() -> None:
    # dprint (`textWrap: "never"`) collapses `{: #id ...}` onto the end of paragraphs, definitions, and tasks!
    raw = (
        b"---\n"
        b"type: guide\n"
        b"status: stable\n"
        b"relations:\n"
        b"  supersedes: [bf:///concepts/prior.md]\n"
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
        {"to": "concepts/prior.md", "kind": "note", "anchor": "", "meta": {"rel": "supersedes"}, "section": ""},
        {
            "to": "memories/demo/e8bd59a71e7e467bb66d5c98314e26f78547a7dd6aba8980cf2e4edd046a4851.json",
            "kind": "record",
            "anchor": "",
            "meta": {"rel": "cites"},
            "section": "single-line-attrs",
        },
    ]
    assert markdown.anchor_ids("concepts/dprint.md", raw) >= {"single-line-attrs", "para-1", "lnk-1", "task-ship"}


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
        ("concepts/relfm.md", b"---\nlinks: [projects/launch.md]\n---\n# Hi\n", "frontmatter links must use bf://"),
        ("concepts/badrel.md", b"---\nrelations: 123\n---\n# Hi\n", "relations must be a list or mapping"),
        ("concepts/badrelv.md", b"---\nrelations:\n  uses: 123\n---\n# Hi\n", "must be a list of URI strings"),
        ("concepts/legacylnk.md", b"# Hi\n\nSee [ev](demo:ev-1).\n", "unsupported link scheme demo:"),
        (
            "concepts/prosebf.md",
            b"# Hi\n\nSee [same](bf:///concepts/aaif.md).\n",
            "use a relative path in Markdown prose",
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

    with pytest.raises(Error, match="legacy record reference in bf:// URI"):
        markdown.parse_link("projects/web.md", "bf://my-brain/jira:WEB-1", "my-brain")
    bf_self_rec = markdown.parse_link("projects/web.md", "bf://my-brain/jira:WEB-1", "my-brain", allow_record_ref=True)
    assert (bf_self_rec["to"], bf_self_rec["kind"]) == ("jira:WEB-1", "record")

    bf_other = markdown.parse_link("projects/web.md", "bf://team-brain/actions/2026-11-15_sync#done", "my-brain")
    assert bf_other["to"] == "bf://team-brain/actions/2026-11-15_sync/ACTION.md"
    assert (bf_other["kind"], bf_other["anchor"]) == ("brain", "done")

    urn_url = markdown.parse_link("projects/web.md", "urn:ietf:rfc:3986", "my-brain", require_uri=True)
    assert urn_url["kind"] == "url"

    ext_url = markdown.parse_link(
        "projects/web.md", "https://example.com/docs?x=1", "my-brain", attr_meta={"rel": "ref"}, require_uri=True
    )
    assert (ext_url["kind"], ext_url["meta"]) == ("url", {"rel": "ref"})

    http_rec = markdown.parse_link("projects/web.md", "doc:https://example.com/spec", "my-brain", allow_record_ref=True)
    assert (http_rec["to"], http_rec["kind"]) == ("doc:https://example.com/spec", "record")

    rec_meta = markdown.parse_link("projects/web.md", "jira:WEB-1?role=blocker#top", "my-brain", allow_record_ref=True)
    assert (rec_meta["to"], rec_meta["anchor"], rec_meta["meta"]) == ("jira:WEB-1", "top", {"role": "blocker"})

    for bad_target, match in (
        ("https://example.com/" + "a" * 8200, "exceeds 8192"),
        ("concepts/bi\u202edi.md", "invisible"),
        ("bf://my-brain/concepts/%E2%80%AE.md", "invisible"),
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
            markdown.parse_link("projects/web.md", bad_target, "my-brain", allow_record_ref=True)

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
