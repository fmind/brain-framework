"""Markdown projection: heading slugs and anchors, claim footnotes, leads and located link errors."""

from __future__ import annotations

import time

import pytest

from bf.markdown import note, parse
from bf.models import Error


def slugs(data: bytes) -> list[str]:
    return [heading.slug for heading in parse("concepts/x.md", data).headings]


def test_repeated_headings_take_the_next_free_suffix_around_explicit_anchors() -> None:
    # A transcript repeats its headings: each repeat continues from the last suffix its slug took.
    transcript = "".join(f"## User\n\nQ{n}\n\n## Assistant\n\nA{n}\n\n" for n in range(3)).encode()
    assert slugs(transcript) == ["user", "assistant", "user-1", "assistant-1", "user-2", "assistant-2"]
    # Generated suffixes skip an explicit anchor wherever it is: an anchor after the heading it collides with no
    # longer fails the note, and both orders give each heading the same slug.
    assert slugs(b"## User\n## User\n## Bot {#user-1}\n## User\n") == ["user", "user-2", "user-1", "user-3"]
    assert slugs(b"## Bot {#user-1}\n## User\n## User\n## User\n") == ["user-1", "user", "user-2", "user-3"]
    assert slugs(b"## User 1\n## User\n## User\n") == ["user-1", "user", "user-2"]
    # A title whose own slug another heading anchors, in either order, is as ambiguous as two equal anchors.
    for data in (b"# Same\n# B {#same}\n", b"# B {#same}\n# Same\n", b"# A {#same}\n# B {#same}\n"):
        with pytest.raises(Error, match="duplicate explicit heading anchor"):
            parse("concepts/x.md", data)
    # `note.md#part.md` would read as a file name.
    with pytest.raises(Error, match=r"anchors cannot end in \.md"):
        parse("concepts/x.md", b"## Part {#part.md}\n")


def test_copied_documents_keep_conflicting_anchors_searchable() -> None:
    # A copied document, such as an MkDocs page among an action's inputs, is ordinary Markdown: a conflict that
    # fails an OKF note gives the later heading a generated slug instead of making the document unsearchable.
    path = "actions/2026-09-27_x/inputs/guide.md"
    data = b"# Guide\n## Configuration {#config}\n### Config\n## Again {#config}\n## Part {#part.md}\n"
    assert [heading.slug for heading in parse(path, data).headings] == [
        "guide",
        "config",
        "config-1",
        "again",
        "part-partmd",
    ]


@pytest.mark.parametrize(
    ("source", "slug", "title"),
    [
        ("## *Stable* name {#stable}", "stable", "Stable name"),
        # Braces in a code span, escaped or written as an entity are words, not an anchor.
        ("## Write anchors as `{#id}`", "write-anchors-as-id", "Write anchors as {#id}"),
        ("## Foo \\{#bar\\}", "foo-bar", "Foo {#bar}"),
        ("## Foo &#123;#bar}", "foo-bar", "Foo {#bar}"),
        # A setext title's lines join with a space, as they render.
        ("Title line one\nline two\n===", "title-line-one-line-two", "Title line one line two"),
        ("Setext title\n{#setext}\n---", "setext", "Setext title"),
    ],
)
def test_explicit_anchors_are_read_from_the_heading_source(source: str, slug: str, title: str) -> None:
    [heading] = parse("concepts/x.md", f"{source}\n".encode()).headings
    assert (heading.slug, heading.title) == (slug, title)


def test_claim_footnotes_support_the_section_that_cites_them() -> None:
    data = (
        b"---\ntype: project\n---\n# Web\n\nCite evidence as `[^1]`.\n\n"
        b"## Decision\n\nWe chose a static site.[^1] Hosting stays cheap.[^cost]\n\n"
        b"## Risks\n\nHosting costs could grow.\n\n"
        b"[^1]: [Meeting](bf://fixture/mail:m1?rel=depends-on)\n"
        b"[^cost]: [Quote](mail:m2) and\n  [estimate](mail:m3)\n"
        b"[^unused]: [Old](mail:m4)\n"
    )
    # Definitions usually close the note: before, every claim they hold took the last section as its origin. A
    # citation in a code span is an example, not the first citation. Each link keeps its own line.
    assert note("projects/web.md", data).contexts == [
        ("bf://fixture/mail:m1?rel=depends-on", "decision", 16),
        ("mail:m2", "decision", 17),
        ("mail:m3", "decision", 18),
        # A footnote nothing cites supports the section holding its definition.
        ("mail:m4", "risks", 19),
    ]


@pytest.mark.parametrize("label", ["```", "~~~", "<pre>", "<!--"])
def test_a_footnote_label_never_changes_the_note_structure(label: str) -> None:
    # The label could open a fence, an HTML block or an autolink that swallowed the sections after it.
    data = f"# Web\n\nClaim.[^{label}]\n\n[^{label}]: [Review](mail:m1)\n\n## Later\n\n[Plan](mail:m2)\n".encode()
    parsed = note("projects/web.md", data)
    assert parsed.slugs == {"web", "later"}
    assert parsed.contexts == [("mail:m1", "web", 5), ("mail:m2", "later", 9)]


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_a_footnote_example_inside_a_code_fence_defines_nothing(fence: str) -> None:
    # Its indented lines used to lose their indentation as a footnote's continuation: the inner fence then closed
    # the outer one early, and every later section disappeared.
    data = (
        f"# Guide\n\n{fence}markdown\n[^1]: A footnote\n    ```python\n    print(1)\n    ```\n{fence}\n\n"
        "## After\n\nSee [Plan](mail:m1).\n"
    ).encode()
    parsed = note("projects/guide.md", data)
    assert parsed.slugs == {"guide", "after"}
    assert parsed.contexts == [("mail:m1", "after", 12)]


def test_footnote_continuations_and_labels_follow_github() -> None:
    # An indented paragraph continues its footnote, and labels match regardless of case, as GitHub renders them.
    data = (
        b"# Web\n\n## Decision\n\nStatic site.[^Cost]\n\n## Risks\n\n"
        b"[^cost]: Estimate.\n\n    [Quote](mail:m1)\n\n\t[Invoice](mail:m2)\n\nAfter [Plan](mail:m3).\n\n    [Code](mail:m4)\n"
    )
    assert note("projects/web.md", data).contexts == [
        ("mail:m1", "decision", 11),
        ("mail:m2", "decision", 13),
        ("mail:m3", "risks", 15),
    ]


def test_a_run_of_footnote_openers_parses_in_linear_time() -> None:
    # A citation id holds no `[`, so each scan stops at the next one: before, every `[^` of this 100 KB run scanned to
    # its end, about 20 seconds per parse, and the citation took `[^a` as its id.
    data = b"# Web\n\n## Decision\n\nShip it.[^[^a] " + b"[^" * 50_000 + b"\n\n## Risks\n\n[^a]: [Review](mail:m1)\n"
    started = time.perf_counter()
    assert note("projects/web.md", data).contexts == [("mail:m1", "decision", 9)]
    # As in test_adversarial_markdown_parses_in_linear_time: another quadratic scan of the run fails too.
    assert time.perf_counter() - started < 10


@pytest.mark.parametrize(
    "data",
    [
        b"# Web\n\n- [ ] Ship the site after the review.[^r]\n\n  [^r]: [Review](mail:m1)\n",
        b"# Web\n\n> Quoted claim.[^r]\n>\n> [^r]: [Review](mail:m1)\n",
        b"# Web\n\nClaim.[^r]\n\n   [^r]: [Review](mail:m1)\n",
    ],
    ids=["list-item", "quote", "indented"],
)
def test_indented_footnote_definitions_keep_their_links(data: bytes) -> None:
    # CommonMark would read each definition as a link reference definition, its whole text as one target.
    assert note("projects/web.md", data).targets == ["mail:m1"]


def test_leads_and_tasks_drop_emphasis_but_keep_identifiers_and_code() -> None:
    data = (
        b"# Config\n\nSet `MAX_FILE_SIZE` in config_loader.py, edit `__init__.py` and compute 2*3*4: **all** _done_.\n\n"
        b"## Next\n\n- [ ] Rename *snake_case_name* to `__all__` in [the loader](config_loader.md).\n"
        b"- [ ] Review [*the* brief](https://docs.example.test/d/1Xy_/edit) and [x](https://example.test/_next/x.js).\n"
        b"- [ ] Read [drafts](_drafts/plan.md), [cfg](config_.md) and [notes](my%20notes_.md).\n"
        b"- [ ] Fix [init](https://example.test/src/pkg/__init__.py).\n"
    )
    projection = note("projects/config.md", data)
    # Before, every `_` and `*` went: "Set MAXFILESIZE in configloader.py, edit init.py and compute 234".
    assert projection.lead == "Set MAX_FILE_SIZE in config_loader.py, edit __init__.py and compute 2*3*4: all done."
    # A label loses its emphasis, a ref none of its characters: before, a document ID lost its last `_`, `_next` became
    # `next` and `__init__.py` became `init.py`.
    assert [task.text for task in projection.tasks] == [
        "Rename snake_case_name to __all__ in [the loader](projects/config_loader.md).",
        "Review [the brief](https://docs.example.test/d/1Xy_/edit) and [x](https://example.test/_next/x.js).",
        "Read [drafts](projects/_drafts/plan.md), [cfg](projects/config_.md) and [notes](projects/my notes_.md).",
        "Fix [init](https://example.test/src/pkg/__init__.py).",
    ]


def test_link_errors_name_their_line_or_frontmatter_key() -> None:
    # The line is the link's own, inside a paragraph spanning several.
    data = b"---\ntype: project\n---\n# Web\n\nA paragraph\nwhose second line [links](http://[your-host]:8080/admin).\n"
    with pytest.raises(Error, match=r"^projects/web\.md: line 7: invalid link$"):
        note("projects/web.md", data)
    with pytest.raises(Error, match=r"^projects/web\.md: line 4: invalid BF link; "):
        note("projects/web.md", b"# Web\n\nSee\n[the plan](bf://fixture/projects/100%.md).\n")
    with pytest.raises(Error, match=r"^projects/web\.md: links: invalid link$"):
        note("projects/web.md", b'---\nlinks: ["https://["]\n---\n# Web\n')
    with pytest.raises(Error, match=r"^projects/web\.md: sources: invalid link$"):
        note("projects/web.md", b'---\ntype: project\nsources:\n  - resource: "http://[x/y"\n---\n# Web\n')
    with pytest.raises(Error, match=r"^projects/web\.md: aliases: invalid BF link; "):
        note("projects/web.md", b'---\naliases: ["bf://fixture/people/100%"]\n---\n# Web\n')
    with pytest.raises(Error, match=r"^projects/web\.md: resource: invalid BF link; "):
        note("projects/web.md", b'---\nresource: "bf://fixture/docs/100%-done"\n---\n# Web\n')
    # A resource with spaces describes a population and names nothing, as OKF allows.
    assert not note("projects/web.md", b'---\nresource: "bf: every 100% record"\n---\n# Web\n').knowledge.names
    with pytest.raises(Error, match=r"^projects/web\.md: entity: identity must not contain a link relation$"):
        note("projects/web.md", b'---\nentity: "bf://fixture/people/alice?rel=owner"\n---\n# Web\n')


def test_ordinary_markdown_drops_urls_it_cannot_parse() -> None:
    # A document copied from another tool stays searchable: Python rejects a bracketed host that is no IP address.
    vendor = "actions/2026-09-27_import/inputs/vendor.md"
    data = (
        b"# Vendor\n\nOpen [the console](http://[your-host]:8080/admin), <http://[fe80::1%eth0/x>, [guide](guide.md).\n"
    )
    projection = note(vendor, data)
    assert (projection.targets, projection.contexts) == (["guide.md"], [("guide.md", "vendor", 3)])
    # A malformed BF address, which only BF writers use, is still an error.
    with pytest.raises(Error, match=r"^actions/2026-09-27_import/inputs/vendor\.md: line 3: invalid BF link; "):
        note(vendor, b"# Vendor\n\n[c](bf://fixture/projects/100%.md)\n")
