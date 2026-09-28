"""The task page enumerates actual open checkboxes without treating source attachments as work."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import cast

import pytest

from bf import index, pages
from bf.models import Error, Record
from bf.retrieve import read
from bf.storage import Store
from bf.validate import validate
from conftest import records_file


def test_tasks_include_source_sections_lines_and_counts_for_canonical_notes(brain: Store) -> None:
    brain.write(
        "projects/tasks.md",
        b"---\ntype: project\n---\n# Plan\n\n## Next {#next}\n\n- [ ] Read [the source](../concepts/evidence.md).\n- [X] Finished.\n\n```markdown\n- [ ] Example only.\n```\n\n[ ] Not a list.\n\n1. [ ] Numbered.\n",
    )
    brain.write("concepts/done.md", b"# Done\n\n- [x] Documented.\n")
    brain.write("actions/2026-09-27_work/ACTION.md", b"# Work\n\n- [ ] Resume.\n")
    brain.write("actions/2026-09-27_work/inputs/source.md", b"# Source\n\n- [ ] Quoted requirement.\n")
    brain.write("actions/2026-09-27_work/outputs/result.md", b"# Output\n\n- [ ] Copied task.\n")
    brain.write("actions/guide.md", b"# Guide\n\n- [ ] Example.\n")
    brain.write("projects/retired.md", b"---\nstatus: deprecated\n---\n# Old\n\n- [ ] Retired.\n")
    result = read([brain], "tasks")
    assert result["summary"] == {"open": 3, "done": 2, "notes": 3}
    assert result["total"] == 3
    items = cast("list[dict[str, object]]", result["items"])
    found = items[1]
    assert found == {
        "brain": "fixture",
        "ref": "projects/tasks.md#next",
        "uri": "bf://fixture/projects/tasks.md#next",
        "note": "projects/tasks.md",
        "title": "Plan",
        "line": 8,
        "text": "Read the source.",
    }
    assert "- [ ] Read" in str(read([brain], str(found["uri"]))["text"])
    assert [item["text"] for item in items] == ["Resume.", "Read the source.", "Numbered."]
    assert read([brain], "bf://fixture/tasks")["summary"] == result["summary"]


def test_tasks_continue_in_one_stable_order_across_brains(brain: Store, tmp_path: Path) -> None:
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 6\nname: team\n")
    for store in (brain, team):
        store.write("projects/work.md", ("# Work\n\n" + "\n".join(f"- [ ] Task {n}." for n in range(63))).encode())
    stores = [team, brain]
    offset = 0
    results = []
    while True:
        result = read(stores, "tasks", offset=offset)
        assert result["summary"] == {"open": 126, "done": 0, "notes": 2}
        assert result["total"] == 126
        items = cast("list[dict[str, object]]", result["items"])
        assert len(items) <= pages.PAGE
        results.extend((item["line"], item["brain"]) for item in items)
        if "next_offset" not in result:
            break
        offset = cast("int", result["next_offset"])
    assert results == [(line, name) for line in range(3, 66) for name in ("fixture", "team")]
    assert read(list(reversed(stores)), "tasks")["items"] == read(stores, "tasks")["items"]
    assert read(stores, "tasks", offset=2**63 - 1)["items"] == []


def test_tasks_refresh_changed_deleted_and_invalid_notes_and_report_incompleteness(brain: Store) -> None:
    path = "projects/changing.md"
    brain.write(path, b"# Change\n\n- [ ] First.\n- [ ] Second.\n")
    assert read([brain], "tasks")["total"] == 2
    brain.write(path, b"# Change\n\n- [x] First.\n")
    assert read([brain], "tasks")["summary"] == {"open": 0, "done": 1, "notes": 1}
    brain.write(path, b"---\nreview_after: invalid\n---\n# Change\n\n- [ ] First.\n")
    result = read([brain], "tasks")
    assert result["total"] == 0
    assert path in str(result["problems"])
    (brain.root / path).unlink()
    assert "problems" not in read([brain], "tasks")


def test_task_page_identity_cannot_be_claimed_by_notes_or_records(brain: Store) -> None:
    for metadata in ("entity: bf://fixture/tasks", "aliases: [bf://fixture/tasks]"):
        brain.write("projects/owner.md", f"---\ntype: project\n{metadata}\n---\n# Owner\n".encode())
        assert not validate(brain)["valid"]
        assert read([brain], "tasks")["problems"]
    brain.delete("projects/owner.md")
    records_file(brain, "mail", [Record(id="owner", title="Owner", aliases=["bf://fixture/tasks"])])
    assert not validate(brain)["valid"]
    assert read([brain], "tasks")["problems"]
    brain.delete("memories/mail/4c1029697ee358715d3a14a2add817c4b01651440de808371f78165ac90dc581.json")
    brain.write("projects/link.md", b"---\ntype: project\n---\n# Link\n\n[Queue](bf://fixture/tasks).\n")
    assert validate(brain)["valid"]
    assert "problems" not in read([brain], "tasks")


def test_task_page_has_no_sections(brain: Store) -> None:
    with pytest.raises(Error, match="invalid BF link"):
        read([brain], "bf://fixture/tasks#section")


def test_task_parser_excludes_literal_code_and_escaped_checkbox_examples(brain: Store) -> None:
    brain.write(
        "projects/examples.md",
        b"# Examples\n\n- [ ] Real.\n- `[ ]` Inline code.\n- \\[ ] Escaped.\n- [ ](https://example.invalid) Link.\n\n"
        b"~~~markdown\n- [ ] Fenced.\n~~~\n\n    - [ ] Indented code.\n\n"
        b"> - [ ] Quoted source.\n> > - [ ] Nested quote.\n\n- plain\n  - [ ] Nested.\n",
    )
    result = read([brain], "tasks")
    assert [item["text"] for item in cast("list[dict[str, object]]", result["items"])] == ["Real.", "Nested."]


def test_task_counts_and_items_share_a_cache_snapshot(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    brain.write("projects/concurrent.md", b"# Work\n\n- [ ] Keep this snapshot.\n")
    assert read([brain], "tasks")["total"] == 1
    original = index.database
    changed = False

    @contextmanager
    def concurrent(store: Store) -> Iterator[tuple[sqlite3.Connection, str]]:
        with original(store) as (connection, state):

            def rewrite(statement: str) -> None:
                nonlocal changed
                if not changed and statement.startswith("SELECT i.ref,i.title,t.line"):
                    changed = True
                    with closing(sqlite3.connect(brain.root / index.CACHE)) as writer, writer:
                        writer.execute("DELETE FROM tasks")

            connection.set_trace_callback(rewrite)
            yield connection, state

    monkeypatch.setattr(index, "database", concurrent)
    result = read([brain], "tasks")
    assert changed
    assert result["summary"] == {"open": 1, "done": 0, "notes": 1}
    assert len(cast("list", result["items"])) == 1
