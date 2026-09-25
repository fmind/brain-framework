"""Search refreshes its cache incrementally and ranks exact, all-term and any-term matches."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import cast

import pytest

from bf import index, usage
from bf.config import register
from bf.models import Error, Query, Record, timestamp
from bf.retrieve import read, search
from bf.storage import Store, state_store, writer
from conftest import records_file


def refs(store: Store | list[Store], text: str = "", **options: object) -> list[str]:
    stores = store if isinstance(store, list) else [store]
    items = cast("list[dict[str, object]]", search(stores, Query.model_validate({"text": text, **options}))["items"])
    return [str(item["ref"]) for item in items]


def test_all_terms_rank_before_any_term_and_notes_before_records(brain: Store) -> None:
    assert refs(brain, "offline retrieval")[:2] == ["projects/offline.md", "meetings:decision-1"]
    # One result per note: its best-matching section.
    assert refs(brain, "retention guide") == ["projects/offline.md#next-actions"]
    assert refs(brain, "Tuesday lunch") == ["meetings:lunch"]
    assert refs(brain, "zzabsent") == []
    # Function words are dropped, but a query made only of them still searches literally.
    assert refs(brain, "what is the retention decision")[0] == "projects/offline.md#decision"
    assert refs(brain, "the")
    # English stemming: plural and verb forms match their stem.
    assert refs(brain, "decisions keeping")[0] == "projects/offline.md#decision"


def test_results_are_compact_and_cite_readable_refs(brain: Store) -> None:
    reply = search([brain], Query(text="durable evidence"))
    items = reply["items"]
    assert isinstance(items, list)
    record = next(i for i in items if i["kind"] == "record")
    assert record == {
        "brain": "fixture",
        "excerpt": "The team chose offline retrieval.",
        "kind": "record",
        "ref": "meetings:decision-1",
        "uri": "bf://fixture/meetings:decision-1",
        "source": "meetings",
        "time": "2026-08-31T12:00:00.000000Z",
        "title": "Preserve durable evidence",
        "type": "record",
    }
    assert "untrusted" in str(reply["notice"])


def test_exact_identities_and_their_linked_items(brain: Store) -> None:
    assert refs(brain, "repo:example/project") == ["projects/offline.md", "meetings:decision-1"]
    assert refs(brain, "meeting:decision-1")[0] == "meetings:decision-1"
    assert refs(brain, "meetings:decision-1")[0] == "meetings:decision-1"
    assert refs(brain, "projects/offline.md")[0] == "projects/offline.md"


def test_time_windows_list_recent_items_and_filter_searches(brain: Store) -> None:
    since, until = timestamp("2026-08-31T00:00:00Z"), timestamp("2026-09-02T00:00:00Z")
    assert refs(brain, since=since, until=until) == ["projects/offline.md", "meetings:decision-1"]
    assert refs(brain, since=since, until=timestamp("2026-09-01T00:00:00Z")) == ["meetings:decision-1"]
    assert refs(brain, "lunch", until=since) == ["meetings:lunch"]
    assert refs(brain, "offline", source="meetings") == ["meetings:decision-1"]
    assert refs(brain, "offline", type="project")[0] == "projects/offline.md"
    assert refs(brain, "originals", type="concept") == ["concepts/evidence.md"]
    assert refs(brain, "offline", status="active")[0] == "projects/offline.md"
    assert refs(brain, "offline retrieval", recent=True)[:2] == ["projects/offline.md", "meetings:decision-1"]
    assert refs(brain, "evidence", limit=1) == ["concepts/evidence.md"]
    assert refs(brain, type="project") == ["projects/offline.md"]
    assert refs(brain, source="meetings") == ["meetings:decision-1", "meetings:lunch"]


def test_demoted_notes_rank_last_but_stay_visible(brain: Store) -> None:
    brain.write("projects/old.md", b"---\nstatus: archived\n---\n# Old offline retrieval\n\nOffline retrieval.\n")
    found = refs(brain, "offline retrieval")
    assert found.index("projects/old.md") > found.index("meetings:decision-1")


def test_cache_follows_edits_additions_removals_and_touches(brain: Store) -> None:
    assert refs(brain, "zirconium") == []
    brain.write("concepts/new.md", b"---\ntype: concept\n---\n# New\n\nZirconium matters.\n")
    assert refs(brain, "zirconium") == ["concepts/new.md"]
    stat = (brain.root / "concepts/new.md").stat()
    # A same-size edit that preserves mtime is still noticed through ctime.
    with (brain.root / "concepts/new.md").open("r+b") as stream:
        stream.write(b"---\ntype: concept\n---\n# New\n\nHafniums matters.\n")
    os.utime(brain.root / "concepts/new.md", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert refs(brain, "hafniums") == ["concepts/new.md"]
    (brain.root / "concepts/new.md").unlink()
    assert refs(brain, "hafniums") == []
    result = index.refresh(brain)
    assert result["changed"] == 0
    assert index.refresh(brain, full=True)["changed"] == 3


def test_invalid_files_are_skipped_and_reported(brain: Store) -> None:
    brain.write("projects/broken.md", b"---\nstatus: current\n---\n# Broken\n")
    brain.write("memories/bad/2026-09.jsonl", b'{"id":"x"}\n')
    records_file(brain, "dupes", "2026-09", [Record(id="a", title="A", time="2026-09-01T00:00:00Z")])
    records_file(brain, "dupes", "2026-08", [Record(id="a", title="A", time="2026-08-01T00:00:00Z")])
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"
    problems = index.status(brain)["problems"]
    assert isinstance(problems, list)
    assert len(problems) == 3
    assert any("projects/broken.md" in p and "status" in p for p in problems)
    assert any("duplicate reference dupes:a" in p for p in problems)


def test_outdated_or_corrupt_cache_is_rebuilt(brain: Store) -> None:
    refs(brain, "offline")
    cache = brain.root / index.CACHE
    with sqlite3.connect(cache) as connection:
        connection.execute("PRAGMA user_version=1")
    connection.close()
    assert refs(brain, "offline")[0] == "projects/offline.md"
    for suffix in ("-wal", "-shm"):
        cache.with_name(cache.name + suffix).unlink(missing_ok=True)
    cache.write_bytes(b"not a database")
    assert refs(brain, "offline")[0] == "projects/offline.md"
    cache.unlink()
    (brain.root / ".bf/index.sqlite").symlink_to(brain.root / "bf.yaml")
    with pytest.raises(Error, match="regular file"):
        refs(brain, "offline")


def test_a_busy_writer_serves_the_current_cache_as_stale(brain: Store) -> None:
    refs(brain, "offline")
    brain.write("concepts/late.md", b"# Late\n\nLatecomer.\n")
    with writer(brain):
        reply = search([brain], Query(text="latecomer"))
    assert reply == {"items": [], "notice": reply["notice"], "stale": ["fixture"]}
    assert refs(brain, "latecomer") == ["concepts/late.md"]


@pytest.mark.parametrize("outdated", [False, True])
def test_concurrent_first_search_waits_for_a_complete_cache(
    brain: Store, monkeypatch: pytest.MonkeyPatch, outdated: bool
) -> None:
    if outdated:
        index.refresh(brain)
        with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
            connection.execute(f"PRAGMA user_version={index.SCHEMA - 1}")
    ingest_started, release_ingest, reader_refreshing = Event(), Event(), Event()
    original_index, original_refresh = index._index, index.refresh  # noqa: SLF001 - coordinated ingestion failure boundary
    waits: list[float] = []

    def paused(connection: sqlite3.Connection, store: Store, path: str) -> list[str]:
        ingest_started.set()
        assert release_ingest.wait(10)
        return original_index(connection, store, path)

    def observe_refresh(store: Store, *, full: bool = False, wait: float = 30) -> dict[str, object]:
        waits.append(wait)
        reader_refreshing.set()
        return original_refresh(store, full=full, wait=wait)

    monkeypatch.setattr(index, "_index", paused)
    monkeypatch.setattr(index, "refresh", observe_refresh)
    with ThreadPoolExecutor(max_workers=2) as executor:
        build = executor.submit(original_refresh, brain)
        try:
            assert ingest_started.wait(10)
            result = executor.submit(search, [brain], Query(text="offline retrieval"), counted=False)
            assert reader_refreshing.wait(10)
            assert waits == [120]  # Incomplete generations cannot be served as stale evidence.
        finally:
            release_ingest.set()
        assert build.result(timeout=10)["files"] == 3
        reply = result.result(timeout=10)
    assert "stale" not in reply
    assert [item["ref"] for item in cast("list[dict[str, object]]", reply["items"])][:2] == [
        "projects/offline.md",
        "meetings:decision-1",
    ]


def test_failed_full_rebuild_does_not_publish_an_incomplete_cache(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    original_index = index._index  # noqa: SLF001 - inject failure after a file was indexed
    indexed: list[str] = []

    def fail_second(connection: sqlite3.Connection, store: Store, path: str) -> list[str]:
        if indexed:
            raise sqlite3.OperationalError("simulated disk full")
        indexed.append(path)
        return original_index(connection, store, path)

    with monkeypatch.context() as patch:
        patch.setattr(index, "_index", fail_second)
        with pytest.raises(Error, match="check free space"):
            index.refresh(brain, full=True)
    assert len(indexed) == 1
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"


def test_live_pending_commit_serves_stale_cache_but_abandoned_commit_errors(brain: Store) -> None:
    expected = refs(brain, "offline retrieval")
    with writer(brain):
        brain.write("memories/.pending/0.before", b"staged original")
        reply = search([brain], Query(text="offline retrieval"), counted=False)
    assert reply["stale"] == ["fixture"]
    assert [item["ref"] for item in cast("list[dict[str, object]]", reply["items"])] == expected
    with pytest.raises(Error, match="interrupted transaction"):
        search([brain], Query(text="offline retrieval"), counted=False)
    assert brain.read("memories/.pending/0.before") == b"staged original"


def test_several_brains_interleave_and_reads_name_their_brain(brain: Store, tmp_path: Path) -> None:
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 4\nname: team\n")
    team.write("projects/offline.md", b"# Team offline\n\nThe team keeps offline retrieval too.\n")
    register(team, collect=False)
    stores = [brain, team]
    items = search(stores, Query(text="offline retrieval"))["items"]
    assert isinstance(items, list)
    assert [(i["brain"], i["ref"]) for i in items[:2]] == [
        ("fixture", "projects/offline.md"),
        ("team", "projects/offline.md"),
    ]
    with pytest.raises(Error, match="several brains"):
        read(stores, "projects/offline.md")
    assert read(stores, "projects/offline.md", "team")["brain"] == "team"
    assert read(stores, "meetings:lunch")["brain"] == "fixture"
    with pytest.raises(Error, match="unknown brain"):
        read(stores, "meetings:lunch", "absent")
    timeline = search(stores, Query(since=timestamp("2026-01-01T00:00:00Z")))["items"]
    assert isinstance(timeline, list)
    assert [i["ref"] for i in timeline] == ["projects/offline.md", "meetings:decision-1", "meetings:lunch"]


def test_exact_reads(brain: Store) -> None:
    whole = read([brain], "projects/offline.md")
    assert str(whole["text"]).startswith("---\ntype: project")
    section = read([brain], "projects/offline.md#next-actions")
    assert section["text"] == "## Next actions\n\n- Publish the retention guide.\n"
    record = read([brain], "meetings:decision-1")
    assert record["path"] == "memories/meetings/2026-08.jsonl"
    assert cast("dict[str, object]", record["record"])["text"] == "The team chose offline retrieval."
    assert read([brain], "meeting:decision-1")["ref"] == "meetings:decision-1"
    assert read([brain], "repo:example/project")["ref"] == "projects/offline.md"
    for missing in ["projects/absent.md", "meetings:absent", "unknown:thing", "bf.yaml"]:
        with pytest.raises(Error, match="not found"):
            read([brain], missing)
    with pytest.raises(Error, match="does not exist"):
        read([brain], "projects/offline.md#absent")
    with pytest.raises(Error):
        read([brain], "projects/../bf.yaml.md")
    with pytest.raises(Error, match="8192"):
        read([brain], "x" * 9000)


def test_oversized_replies_are_rejected_not_truncated(brain: Store) -> None:
    brain.write("concepts/quoted.md", b"# Quoted\n\n" + b'"' * 3_000_000)
    with pytest.raises(Error, match="byte limit"):
        read([brain], "concepts/quoted.md")
    brain.write("concepts/large.md", b"# Large\n\n" + b"x" * (5 << 20))
    with pytest.raises(Error, match="exceeds"):
        read([brain], "concepts/large.md")


def test_status_lists_active_projects_due_for_review(brain: Store) -> None:
    brain.write("projects/fresh.md", b"---\nstatus: active\nupdated: 2026-09-20\n---\n# Fresh\n")
    brain.write("projects/undated.md", b"---\nstatus: blocked\n---\n# Undated\n")
    brain.write("projects/closed.md", b"---\nstatus: done\nupdated: 2026-01-01\n---\n# Closed\n")
    review = index.status(brain, now=datetime(2026, 9, 22, tzinfo=UTC))["review"]
    assert review == [
        {"ref": "projects/undated.md", "title": "Undated", "updated": ""},
        {"ref": "projects/offline.md", "title": "Offline retrieval", "updated": "2026-09-01"},
    ]


def test_usage_counts_searches_empty_results_and_reads_without_queries(brain: Store) -> None:
    refs(brain, "offline")
    refs(brain, "zzabsent")
    read([brain], "meetings:lunch")
    search([brain], Query(text="offline"), counted=False)
    log = state_store(brain.root).root / usage.USAGE
    assert "offline" not in log.read_text()
    assert usage.summary(brain) == {
        "7d": {"search": 2, "empty": 1, "read": 1},
        "30d": {"search": 2, "empty": 1, "read": 1},
    }
    later = datetime.now(UTC) + timedelta(days=10)
    assert usage.summary(brain, now=later)["7d"] == {"search": 0, "empty": 0, "read": 0}
    log.write_text(log.read_text() + "not json\n" + '{"at": "x"}\n')
    assert usage.summary(brain)["30d"]["search"] == 2
    log.write_bytes(b'{"at":"2026-01-01T00:00:00+00:00","op":"search","results":1}\n' * 40_000)
    usage.note(brain, "search", 3)
    assert log.stat().st_size <= usage.LIMIT // 2 + 200
    log.unlink()
    log.mkdir()
    usage.note(brain, "search", 1)  # an unwritable log never breaks retrieval
    assert usage.summary(brain)["7d"]["search"] == 0


def test_recent_limits_after_ordering_all_matching_records(brain: Store) -> None:
    records_file(
        brain,
        "updates",
        "2026-09",
        [
            Record(id="old", title="Needle needle needle", time="2026-09-01T00:00:00Z"),
            Record(id="new", title="Latest update", text="needle " + "detail " * 100, time="2026-09-23T00:00:00Z"),
        ],
    )
    assert refs(brain, "needle", recent=True, limit=1) == ["updates:new"]


def test_identity_relations_keep_newest_first(brain: Store) -> None:
    records_file(
        brain,
        "updates",
        "2026-09",
        [
            Record(id="a", title="Old", links=["repo:unique-evidence"], time="2026-09-01T00:00:00Z"),
            Record(id="b", title="New", links=["repo:unique-evidence"], time="2026-09-23T00:00:00Z"),
        ],
    )
    assert refs(brain, "repo:unique-evidence") == ["updates:b", "updates:a"]


def test_malformed_link_does_not_block_other_notes(brain: Store) -> None:
    brain.write("projects/bad-url.md", b'---\nlinks: ["https://["]\n---\n# Invalid link\n')
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"
    assert "invalid link" in str(index.status(brain)["problems"])


def test_removing_a_duplicate_retries_its_other_partition(brain: Store) -> None:
    for month in ("2026-08", "2026-09"):
        records_file(brain, "duplicates", month, [Record(id="same", title="Needle", time=f"{month}-01T00:00:00Z")])
    assert refs(brain, "needle") == ["duplicates:same"]
    brain.delete("memories/duplicates/2026-08.jsonl")
    assert refs(brain, "needle") == ["duplicates:same"]
    assert index.status(brain)["problems"] == []


def test_current_schema_cache_with_missing_table_rebuilds(brain: Store) -> None:
    refs(brain, "offline")
    with sqlite3.connect(brain.root / index.CACHE) as connection:
        connection.execute("DROP TABLE files")
    connection.close()
    assert refs(brain, "offline")[0] == "projects/offline.md"
    assert read([brain], "meetings:decision-1")["ref"] == "meetings:decision-1"


def test_direct_record_read_survives_unavailable_cache(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_store: Store) -> str:
        raise Error("cache unavailable")

    monkeypatch.setattr(index, "fresh", unavailable)
    brain.write("memories/meetings/2026-09.jsonl", b"malformed unrelated newer partition\n")
    assert read([brain], "meetings:decision-1")["ref"] == "meetings:decision-1"


@pytest.mark.parametrize("hint", ["memories/other/2026-09.jsonl", "memories/meetings/2026-09.jsonl"])
def test_record_cache_hint_cannot_change_the_requested_source(brain: Store, hint: str) -> None:
    records_file(
        brain,
        "other",
        "2026-09",
        [Record(id="decision-1", title="Evidence from another source", time="2026-09-01T00:00:00Z")],
    )
    brain.write("memories/meetings/2026-09.jsonl", b"malformed unrelated newer partition\n")
    index.refresh(brain)
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection, connection:
        connection.execute("UPDATE items SET path=? WHERE ref=?", (hint, "meetings:decision-1"))
    reply = read([brain], "meetings:decision-1")
    assert reply["path"] == "memories/meetings/2026-08.jsonl"
    assert cast("dict[str, object]", reply["record"])["title"] == "Preserve durable evidence"


def test_large_note_does_not_consume_other_items_candidates(brain: Store) -> None:
    brain.write(
        "projects/large.md", ("# Large\n\n" + "".join(f"## Needle {i}\n\nneedle\n\n" for i in range(2100))).encode()
    )
    brain.write("projects/other.md", ("# Other\n\nneedle " + "detail " * 1000).encode())
    assert refs(brain, "needle", limit=50) == ["projects/large.md#needle-0", "projects/other.md"]


def test_ambiguous_identity_requires_an_exact_ref(brain: Store) -> None:
    brain.write("projects/duplicate.md", b'---\naliases: ["repo:example/project"]\n---\n# Duplicate owner\n')
    with pytest.raises(Error, match=r"ambiguous.*exact ref"):
        read([brain], "repo:example/project")


def test_changed_since_uses_modification_time_without_changing_event_time(brain: Store) -> None:
    records_file(
        brain,
        "updates",
        "2026-01",
        [
            Record(
                id="old",
                title="Changed event",
                time="2026-01-01T00:00:00Z",
                attributes={"partial": True, "updated": "2026-09-23T00:00:00Z", "observed": "2026-09-23T01:00:00Z"},
            ),
        ],
    )
    records_file(
        brain,
        "updates",
        "2026-09",
        [
            Record(id="recent", title="New event", time="2026-09-22T00:00:00Z"),
        ],
    )
    assert refs(brain, source="updates", changed_since=timestamp("2026-09-21T00:00:00Z")) == [
        "updates:recent",
        "updates:old",
    ]
    assert refs(brain, source="updates", since=timestamp("2026-09-21T00:00:00Z")) == ["updates:recent"]
    items = search([brain], Query(source="updates", changed_since=timestamp("2026-09-23T00:00:00Z")))["items"]
    assert isinstance(items, list)
    item = items[0]
    assert item["time"] == timestamp("2026-01-01T00:00:00Z")
    assert item["updated"] == timestamp("2026-09-23T00:00:00Z")
    assert item["observed"] == timestamp("2026-09-23T01:00:00Z")
    assert item["partial"] is True


def test_current_selects_enabled_sources_and_reports_collection_coverage(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 4\nname: fixture\nsensors:\n  meetings:\n    command: [true-command]\n    refresh: 3600\n  disabled:\n    command: [true-command]\n    enabled: false\n",
    )
    for source in ("disabled", "historical"):
        records_file(brain, source, "undated", [Record(id="x", title="Offline retrieval")])
    items = search([brain], Query(text="offline retrieval", current=True))["items"]
    assert isinstance(items, list)
    assert any(item["kind"] == "note" for item in items)
    assert {item["source"] for item in items if item["kind"] == "record"} == {"meetings"}
    for source, expected in (("meetings", "active"), ("disabled", "disabled"), ("historical", "historical")):
        result = search([brain], Query(source=source))
        sources = result["sources"]
        assert isinstance(sources, list)
        assert sources[0]["state"] == expected
    result = search([brain], Query(source="absent", current=True))
    assert result["items"] == []
    assert result["sources"] == [
        {"brain": "fixture", "source": "absent", "state": "historical", "freshness": "unknown"}
    ]
    collection = read([brain], "meetings:decision-1")["collection"]
    assert isinstance(collection, dict)
    # Retrieval does not consult machine collection trust; no local run means unknown coverage.
    assert collection["freshness"] == "unknown"


def test_okf_provenance_resources_are_searchable_identity_links(brain: Store) -> None:
    brain.write(
        "concepts/concept.md", b'---\ntype: concept\nsources:\n  - resource: "repo:unique-source"\n---\n# Concept\n'
    )
    assert refs(brain, "repo:unique-source") == ["concepts/concept.md"]


def test_identity_search_does_not_infer_relations_from_words(brain: Store) -> None:
    brain.write("projects/prose.md", b"# Repo example\n\nThese words do not declare an identity.\n")
    assert refs(brain, "repo:example") == []
    assert refs(brain, "repo:example/project") == ["projects/offline.md", "meetings:decision-1"]


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_cache_sidecars_cannot_redirect_reads(brain: Store, suffix: str) -> None:
    cache = brain.root / index.CACHE
    cache.parent.mkdir()
    cache.with_name(cache.name + suffix).symlink_to(brain.root / "bf.yaml")
    before = brain.read("bf.yaml")
    with pytest.raises(Error, match="regular file"):
        refs(brain, "offline")
    assert brain.read("bf.yaml") == before


def test_cache_write_failure_is_a_safe_actionable_error(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_store: Store) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(index, "_create", fail)
    with pytest.raises(Error, match="check free space") as raised:
        index.refresh(brain)
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)


def test_french_question_words_do_not_outweigh_the_subject(brain: Store) -> None:
    brain.write("projects/storage.md", b"# Stockage\n\nLes fichiers gardent les preuves hors ligne.\n")
    brain.write("projects/noise.md", b"# Pourquoi les\n\nUn titre sans rapport avec le sujet.\n")
    assert refs(brain, "Pourquoi les fichiers ?")[0] == "projects/storage.md"
    assert "projects/noise.md" in refs(brain, "pourquoi")  # all-stopword queries remain literal


def test_recent_identity_keeps_owners_ahead_of_newer_relations_across_brains(brain: Store, tmp_path: Path) -> None:
    records_file(
        brain,
        "updates",
        "2026-09",
        [
            Record(id="latest", title="Latest activity", time="2026-09-23T00:00:00Z", links=["repo:example/project"]),
        ],
    )
    assert refs(brain, "repo:example/project", recent=True) == [
        "projects/offline.md",
        "updates:latest",
        "meetings:decision-1",
    ]
    folder = tmp_path / "shared"
    folder.mkdir()
    team = Store(folder)
    team.write("bf.yaml", b"version: 4\nname: team\n")
    team.write("projects/owner.md", b'---\nupdated: 2026-08-01\naliases: ["repo:example/project"]\n---\n# Owner\n')
    result = search([team, brain], Query(text="repo:example/project", recent=True))["items"]
    assert isinstance(result, list)
    assert [(item["brain"], item["ref"]) for item in result] == [
        ("fixture", "projects/offline.md"),
        ("team", "projects/owner.md"),
        ("fixture", "updates:latest"),
        ("fixture", "meetings:decision-1"),
    ]
    assert all(not any(key.startswith("_") or key in {"score", "position"} for key in item) for item in result)


def test_an_exact_record_ref_takes_precedence_over_a_colliding_alias(brain: Store) -> None:
    brain.write("projects/alias.md", b'---\nupdated: 2026-09-23\naliases: ["meetings:lunch"]\n---\n# Alias\n')
    assert refs(brain, "meetings:lunch", recent=True)[0] == "meetings:lunch"
    assert read([brain], "meetings:lunch")["ref"] == "meetings:lunch"


def test_search_reports_omitted_files_and_isolates_unavailable_brains(brain: Store, tmp_path: Path) -> None:
    brain.write("projects/broken.md", b"---\nstatus: typo\n---\n# Hidden answer\n")
    broken = tmp_path / "broken"
    broken.mkdir()
    other = Store(broken)
    other.write("bf.yaml", b"version: 4\nname: interrupted\n")
    other.write("memories/.pending/0.before", b"preserve this original")
    reply = search([brain, other], Query(text="offline retrieval"), counted=False)
    assert isinstance(reply["items"], list)
    assert isinstance(reply["problems"], list)
    assert reply["items"][0]["brain"] == "fixture"
    assert reply["problems"][0]["brain"] == "fixture"
    assert "projects/broken.md" in reply["problems"][0]["files"][0]
    assert reply["problems"][1]["brain"] == "interrupted"
    assert "interrupted transaction" in reply["problems"][1]["error"]
    assert other.read("memories/.pending/0.before") == b"preserve this original"
    empty = search([brain], Query(text="hidden answer"), counted=False)
    assert not empty["items"]
    assert empty["problems"]
    # Exact reads cannot silently assume a broken brain contains no competing identity.
    with pytest.raises(Error):
        read([brain, other], "meetings:lunch")
    other.write("bf.yaml", b"version: 4\nname: [invalid]\n")
    assert search([brain, other], Query(text="offline"), counted=False)["items"]
    with pytest.raises(Error, match=r"invalid bf\.yaml"):
        search([other, other], Query(text="offline"), counted=False)


def test_search_waits_if_a_rebuild_replaces_the_checked_generation(
    brain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.refresh(brain)
    checked, reopen, ingesting, finish, retrying = (Event() for _ in range(5))
    original_fresh, original_index = index.fresh, index._index  # noqa: SLF001 - coordinate a real generation replacement
    checks = 0

    def paused_fresh(store: Store) -> str:
        nonlocal checks
        checks += 1
        if checks > 1:
            retrying.set()
        state = original_fresh(store)
        if checks == 1:
            checked.set()
            assert reopen.wait(10)
        return state

    def paused_index(connection: sqlite3.Connection, store: Store, path: str) -> list[str]:
        ingesting.set()
        assert finish.wait(10)
        return original_index(connection, store, path)

    monkeypatch.setattr(index, "fresh", paused_fresh)
    monkeypatch.setattr(index, "_index", paused_index)
    with ThreadPoolExecutor(max_workers=2) as executor:
        query = executor.submit(search, [brain], Query(text="offline"), counted=False)
        try:
            assert checked.wait(10)
            rebuild = executor.submit(index.refresh, brain, full=True)
            assert ingesting.wait(10)
            reopen.set()
            assert retrying.wait(10)
        finally:
            reopen.set()
            finish.set()
        assert rebuild.result(timeout=10)["files"] == 3
        reply = query.result(timeout=10)
    assert isinstance(reply["items"], list)
    assert reply["items"][0]["ref"] == "projects/offline.md"
    assert "problems" not in reply


def test_evaluation_names_missing_retrieval_cases(brain: Store) -> None:
    from bf.evaluate import evaluate

    with pytest.raises(Error, match="evals has no suites"):
        evaluate(brain)
    brain.write("evals/retrieval.yaml", b"version: 4\ncases:\n  - name: later\n    since: soon\n    empty: true\n")
    with pytest.raises(Error, match="case later: since"):
        evaluate(brain)


def test_evaluation_rejects_incomplete_empty_answers(brain: Store) -> None:
    from bf.evaluate import evaluate

    brain.write("projects/broken.md", b"---\nstatus: typo\n---\n# Lost answer\n")
    brain.write(
        "evals/retrieval.yaml", b"version: 4\ncases:\n  - name: absent\n    query: lost answer\n    empty: true\n"
    )
    reply = evaluate(brain)
    assert not reply["passed"]
    assert isinstance(reply["cases"], list)
    assert reply["cases"][0]["problems"]


def test_search_refs_keep_hash_characters_in_note_filenames(brain: Store) -> None:
    path = "projects/C# guide.md"
    brain.write(path, b"# Sharpneedle\n\n## Decision\n\nUniquesection keeps the full path.\n")
    whole = refs(brain, "sharpneedle")[0]
    assert whole == path
    assert str(read([brain], whole)["text"]).startswith("# Sharpneedle")
    part = refs(brain, "uniquesection")[0]
    assert part == path + "#decision"
    assert str(read([brain], part)["text"]).startswith("## Decision")


def test_retrieval_cases_distinguish_record_ids_from_note_sections(brain: Store) -> None:
    from bf.evaluate import evaluate

    records_file(brain, "issues", "undated", [Record(id="item#comment", title="Hashneedle")])
    brain.write(
        "evals/retrieval.yaml",
        b"version: 4\ncases:\n  - name: exact-record\n    query: hashneedle\n    expect: [issues:item]\n",
    )
    assert not evaluate(brain)["passed"]
