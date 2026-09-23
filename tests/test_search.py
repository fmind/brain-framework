"""Search refreshes its cache incrementally and ranks exact, all-term and any-term matches."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from conftest import records_file
from fkf import index
from fkf.config import register
from fkf.models import Error, Query, Record, timestamp
from fkf.retrieve import read, search
from fkf.storage import Store, writer


def refs(store: Store | list[Store], text: str = "", **options: object) -> list[str]:
    stores = store if isinstance(store, list) else [store]
    items = cast("list[dict[str, object]]", search(stores, Query.model_validate({"text": text, **options}))["items"])
    return [str(item["ref"]) for item in items]


def test_all_terms_rank_before_any_term_and_notes_before_records(base: Store) -> None:
    assert refs(base, "offline retrieval")[:2] == ["projects/offline.md", "meetings:decision-1"]
    # One result per note: its best-matching section.
    assert refs(base, "retention guide") == ["projects/offline.md#next-actions"]
    assert refs(base, "Tuesday lunch") == ["meetings:lunch"]
    assert refs(base, "zzabsent") == []
    # Function words are dropped, but a query made only of them still searches literally.
    assert refs(base, "what is the retention decision")[0] == "projects/offline.md#decision"
    assert refs(base, "the")
    # English stemming: plural and verb forms match their stem.
    assert refs(base, "decisions keeping")[0] == "projects/offline.md#decision"


def test_results_are_compact_and_cite_readable_refs(base: Store) -> None:
    reply = search([base], Query(text="durable evidence"))
    items = reply["items"]
    assert isinstance(items, list)
    record = next(i for i in items if i["kind"] == "record")
    assert record == {
        "base": "fixture",
        "excerpt": "The team chose offline retrieval.",
        "kind": "record",
        "ref": "meetings:decision-1",
        "source": "meetings",
        "time": "2026-08-31T12:00:00.000000Z",
        "title": "Preserve durable evidence",
        "type": "record",
    }
    assert "untrusted" in str(reply["notice"])


def test_exact_identities_and_their_linked_items(base: Store) -> None:
    assert refs(base, "repo:example/project") == ["projects/offline.md", "meetings:decision-1"]
    assert refs(base, "meeting:decision-1")[0] == "meetings:decision-1"
    assert refs(base, "meetings:decision-1")[0] == "meetings:decision-1"
    assert refs(base, "projects/offline.md")[0] == "projects/offline.md"


def test_time_windows_list_recent_items_and_filter_searches(base: Store) -> None:
    since, until = timestamp("2026-08-31T00:00:00Z"), timestamp("2026-09-02T00:00:00Z")
    assert refs(base, since=since, until=until) == ["projects/offline.md", "meetings:decision-1"]
    assert refs(base, since=since, until=timestamp("2026-09-01T00:00:00Z")) == ["meetings:decision-1"]
    assert refs(base, "lunch", until=since) == ["meetings:lunch"]
    assert refs(base, "offline", source="meetings") == ["meetings:decision-1"]
    assert refs(base, "offline", type="project")[0] == "projects/offline.md"
    assert refs(base, "originals", type="concept") == ["wiki/evidence.md"]
    assert refs(base, "offline", status="active")[0] == "projects/offline.md"
    assert refs(base, "offline retrieval", recent=True)[:2] == ["projects/offline.md", "meetings:decision-1"]
    assert refs(base, "evidence", limit=1) == ["wiki/evidence.md"]
    assert refs(base, type="project") == ["projects/offline.md"]
    assert refs(base, source="meetings") == ["meetings:decision-1", "meetings:lunch"]


def test_demoted_notes_rank_last_but_stay_visible(base: Store) -> None:
    base.write("projects/old.md", b"---\nstatus: archived\n---\n# Old offline retrieval\n\nOffline retrieval.\n")
    found = refs(base, "offline retrieval")
    assert found.index("projects/old.md") > found.index("meetings:decision-1")


def test_cache_follows_edits_additions_removals_and_touches(base: Store) -> None:
    assert refs(base, "zirconium") == []
    base.write("wiki/new.md", b"---\ntype: concept\n---\n# New\n\nZirconium matters.\n")
    assert refs(base, "zirconium") == ["wiki/new.md"]
    stat = (base.root / "wiki/new.md").stat()
    # A same-size edit that preserves mtime is still noticed through ctime.
    with (base.root / "wiki/new.md").open("r+b") as stream:
        stream.write(b"---\ntype: concept\n---\n# New\n\nHafniums matters.\n")
    os.utime(base.root / "wiki/new.md", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert refs(base, "hafniums") == ["wiki/new.md"]
    (base.root / "wiki/new.md").unlink()
    assert refs(base, "hafniums") == []
    result = index.refresh(base)
    assert result["changed"] == 0
    assert index.refresh(base, full=True)["changed"] == 3


def test_invalid_files_are_skipped_and_reported(base: Store) -> None:
    base.write("projects/broken.md", b"---\nstatus: current\n---\n# Broken\n")
    base.write("records/bad/2026-09.jsonl", b'{"id":"x"}\n')
    records_file(base, "dupes", "2026-09", [Record(id="a", title="A", time="2026-09-01T00:00:00Z")])
    records_file(base, "dupes", "2026-08", [Record(id="a", title="A", time="2026-08-01T00:00:00Z")])
    assert refs(base, "offline retrieval")[0] == "projects/offline.md"
    problems = index.status(base)["problems"]
    assert isinstance(problems, list)
    assert len(problems) == 3
    assert any("projects/broken.md" in p and "status" in p for p in problems)
    assert any("duplicate reference dupes:a" in p for p in problems)


def test_outdated_or_corrupt_cache_is_rebuilt(base: Store) -> None:
    refs(base, "offline")
    cache = base.root / index.CACHE
    with sqlite3.connect(cache) as connection:
        connection.execute("PRAGMA user_version=1")
    connection.close()
    assert refs(base, "offline")[0] == "projects/offline.md"
    for suffix in ("-wal", "-shm"):
        cache.with_name(cache.name + suffix).unlink(missing_ok=True)
    cache.write_bytes(b"not a database")
    assert refs(base, "offline")[0] == "projects/offline.md"
    cache.unlink()
    (base.root / ".fkf/index.sqlite").symlink_to(base.root / "fkf.yaml")
    with pytest.raises(Error, match="regular file"):
        refs(base, "offline")


def test_a_busy_writer_serves_the_current_cache_as_stale(base: Store) -> None:
    refs(base, "offline")
    base.write("wiki/late.md", b"# Late\n\nLatecomer.\n")
    with writer(base):
        reply = search([base], Query(text="latecomer"))
    assert reply == {"items": [], "notice": reply["notice"], "stale": ["fixture"]}
    assert refs(base, "latecomer") == ["wiki/late.md"]


def test_several_bases_interleave_and_reads_name_their_base(base: Store, tmp_path: Path) -> None:
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("fkf.yaml", b"version: 2\nname: team\n")
    team.write("projects/offline.md", b"# Team offline\n\nThe team keeps offline retrieval too.\n")
    register(team, collect=False)
    stores = [base, team]
    items = search(stores, Query(text="offline retrieval"))["items"]
    assert isinstance(items, list)
    assert [(i["base"], i["ref"]) for i in items[:2]] == [
        ("fixture", "projects/offline.md"),
        ("team", "projects/offline.md"),
    ]
    with pytest.raises(Error, match="several bases"):
        read(stores, "projects/offline.md")
    assert read(stores, "projects/offline.md", "team")["base"] == "team"
    assert read(stores, "meetings:lunch")["base"] == "fixture"
    with pytest.raises(Error, match="unknown base"):
        read(stores, "meetings:lunch", "absent")
    timeline = search(stores, Query(since=timestamp("2026-01-01T00:00:00Z")))["items"]
    assert isinstance(timeline, list)
    assert [i["ref"] for i in timeline] == ["projects/offline.md", "meetings:decision-1", "meetings:lunch"]


def test_exact_reads(base: Store) -> None:
    whole = read([base], "projects/offline.md")
    assert str(whole["text"]).startswith("---\ntype: project")
    section = read([base], "projects/offline.md#next-actions")
    assert section["text"] == "## Next actions\n\n- Publish the retention guide.\n"
    record = read([base], "meetings:decision-1")
    assert record["path"] == "records/meetings/2026-08.jsonl"
    assert cast("dict[str, object]", record["record"])["text"] == "The team chose offline retrieval."
    assert read([base], "meeting:decision-1")["ref"] == "meetings:decision-1"
    assert read([base], "repo:example/project")["ref"] == "projects/offline.md"
    for missing in ["projects/absent.md", "meetings:absent", "unknown:thing", "fkf.yaml"]:
        with pytest.raises(Error, match="not found"):
            read([base], missing)
    with pytest.raises(Error, match="does not exist"):
        read([base], "projects/offline.md#absent")
    with pytest.raises(Error):
        read([base], "projects/../fkf.yaml.md")
    with pytest.raises(Error, match="8192"):
        read([base], "x" * 9000)


def test_oversized_replies_are_rejected_not_truncated(base: Store) -> None:
    base.write("wiki/quoted.md", b"# Quoted\n\n" + b'"' * 3_000_000)
    with pytest.raises(Error, match="byte limit"):
        read([base], "wiki/quoted.md")
    base.write("wiki/large.md", b"# Large\n\n" + b"x" * (5 << 20))
    with pytest.raises(Error, match="exceeds"):
        read([base], "wiki/large.md")
