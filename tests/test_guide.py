"""`bf guide` lists and displays the packaged Brain Framework documentation."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from bf.cli import app
from bf.config import user_path
from bf.guide import _summary, read_guide, render_index, topics
from bf.models import InputError

EXPECTED_TOPICS = [
    "index",
    "getting-started",
    "authoring",
    "retrieval",
    "jobs",
    "assist",
    "operations",
    "reference",
]


def test_topics_and_read_guide_cover_all_packaged_guides() -> None:
    listed = topics()
    assert [slug for slug, _ in listed] == EXPECTED_TOPICS
    for slug, summary in listed:
        assert summary
        assert "\n" not in summary
        assert not summary.startswith("#")
        body = read_guide(slug)
        assert body.startswith("# ")
        assert summary in body


def test_render_index_lists_every_topic() -> None:
    index = render_index()
    assert index.startswith("# Brain Framework guides\n")
    for slug, summary in topics():
        assert f"- `{slug}`: {summary}" in index


@pytest.mark.parametrize("blank", ["", "   "])
def test_read_guide_rejects_empty_topic(blank: str) -> None:
    with pytest.raises(InputError) as exc_info:
        read_guide(blank)
    assert exc_info.value.argument == "topic"
    assert "give a guide topic; available: " in str(exc_info.value)
    for slug in EXPECTED_TOPICS:
        assert slug in str(exc_info.value)


def test_read_guide_rejects_unknown_and_truncates_long_topic() -> None:
    with pytest.raises(InputError) as exc_info:
        read_guide("missing-topic")
    assert exc_info.value.argument == "topic"
    assert "unknown guide 'missing-topic'; available: " in str(exc_info.value)

    long_topic = "z" * 100
    with pytest.raises(InputError) as long_exc:
        read_guide(long_topic)
    assert "z" * 64 + "..." in str(long_exc.value)
    assert long_topic not in str(long_exc.value)


def test_summary_handles_frontmatter_headings_and_fallback() -> None:
    assert _summary("a", '---\ndescription: "Quoted summary"\n---\n# Ignored\n') == "Quoted summary"
    assert _summary("a", "---\ndescription: Plain summary\n---\n# Ignored\n") == "Plain summary"
    assert _summary("a", "---\ndescription:\n---\n## Second Heading {#custom-id}\n") == "Second Heading"
    assert _summary("fallback-slug", "Plain text without a heading.\n") == "fallback-slug"


def test_cli_guide_lists_reads_and_validates_topics() -> None:
    path = user_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("default: [broken\n")

    listed = CliRunner().invoke(app, ["guide"])
    assert (listed.exit_code, listed.stderr) == (0, "")
    assert listed.stdout == render_index()

    for slug in EXPECTED_TOPICS:
        shown = CliRunner().invoke(app, ["guide", slug])
        assert (shown.exit_code, shown.stderr) == (0, "")
        assert shown.stdout == read_guide(slug)

    missing = CliRunner().invoke(app, ["guide", "unknown"])
    assert missing.exit_code == 2
    assert missing.stdout == ""
    assert "bf: invalid input: TOPIC: unknown guide 'unknown'" in missing.stderr

    empty = CliRunner().invoke(app, ["guide", ""])
    assert empty.exit_code == 2
    assert empty.stdout == ""
    assert "bf: invalid input: TOPIC: give a guide topic" in empty.stderr

    helped = CliRunner().invoke(app, ["guide", "--help"])
    assert (helped.exit_code, helped.stderr) == (0, "")
    assert "TOPIC" in helped.stdout
