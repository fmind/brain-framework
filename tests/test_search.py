"""Search refreshes its cache incrementally and ranks exact, all-term and any-term matches."""

from __future__ import annotations

import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import cast

import pytest

from bf import index, usage
from bf.config import register, user_path
from bf.markdown import LEAD
from bf.models import Error, Query, Record, timestamp
from bf.pages import scope
from bf.retrieve import read, search
from bf.storage import Store, state_store, writer
from bf.validate import validate
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
        # Searches retain collected excerpts; the reply's notice marks all retrieved content as untrusted.
    }
    assert "untrusted" in str(reply["notice"])
    # Note excerpts preview the section's words on one line; its heading is already the title.
    note = cast("list[dict[str, str]]", search([brain], Query(text="historical evidence"))["items"])[0]
    assert (note["ref"], note["title"]) == ("projects/offline.md#decision", "Offline retrieval — Decision")
    assert note["excerpt"].startswith("Provider retention cannot guarantee")
    concepts = search([brain], Query(text="durable evidence", **scope("concepts")))["items"]
    assert cast("list[dict[str, str]]", concepts)[0]["excerpt"] == "Originals outlive providers."


def test_exact_identities_and_their_linked_items(brain: Store) -> None:
    assert refs(brain, "repo:example/project") == ["projects/offline.md", "meetings:decision-1"]
    assert refs(brain, "meeting:decision-1")[0] == "meetings:decision-1"
    assert refs(brain, "meetings:decision-1")[0] == "meetings:decision-1"
    assert refs(brain, "projects/offline.md")[0] == "projects/offline.md"


def test_scopes_bound_searches_to_a_folder_a_period_or_a_source(brain: Store) -> None:
    since = timestamp("2026-08-31T00:00:00Z")
    assert refs(brain, "lunch", until=since) == ["meetings:lunch"]
    assert refs(brain, "offline", **scope("memories/meetings")) == ["meetings:decision-1"]
    assert refs(brain, "offline", **scope("memories/meetings/")) == ["meetings:decision-1"]
    assert refs(brain, "offline", **scope("projects"))[0] == "projects/offline.md"
    assert refs(brain, "originals", **scope("concepts")) == ["concepts/evidence.md"]
    assert refs(brain, "offline", **scope("concepts")) == []
    assert refs(brain, "offline", **scope("2026-08")) == ["meetings:decision-1"]
    assert refs(brain, "offline", **scope("2026-09-01")) == ["projects/offline.md"]
    assert refs(brain, "lunch", **scope("memories/meetings/2026-08-30")) == ["meetings:lunch"]
    assert refs(
        brain,
        "lunch",
        **scope("memories/meetings/f3bb79e48a7a8af1d028f6acb7fa34b0c7f75da596fc205907d2a708fe28ebef.json"),
    ) == ["meetings:lunch"]
    assert refs(brain, "lunch", **scope("memories/meetings/undated")) == []
    assert refs(brain, "offline", **scope("repo:example/project")) == ["meetings:decision-1"]
    assert refs(brain, "evidence", limit=1) == ["concepts/evidence.md"]
    for invalid in ["bf.yaml", "sensors", "../x", "memories/../bf.yaml", "soon"]:
        with pytest.raises(Error, match="scope accepts"):
            scope(invalid)
    assert scope("") == {}


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
    assert index.refresh(brain, full=True)["changed"] == 4


def test_invalid_files_are_skipped_and_reported(brain: Store) -> None:
    brain.write("projects/broken.md", b"---\nstatus: current\n---\n# Broken\n")
    brain.write("memories/bad/2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881.json", b'{"id":"x"}\n')
    records_file(brain, "dupes", "2026-09", [Record(id="a", title="A", time="2026-09-01T00:00:00Z")])
    brain.write("memories/dupes/" + "0" * 64 + ".json", b'{"id":"a","title":"A"}\n')
    assert refs(brain, "offline retrieval")[0] == "projects/offline.md"
    problems = index.status(brain)["problems"]
    assert isinstance(problems, list)
    assert len(problems) == 3
    assert any(p.startswith("projects/broken.md: invalid frontmatter: status") for p in problems)
    assert any("record id does not match" in p for p in problems)


def test_links_and_special_files_are_reported_without_hiding_the_brain(brain: Store, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nExfiltrated secret.\n")
    brain.write("actions/2026-09-26_demo/ACTION.md", b"---\ntype: action\n---\n# Demo\n\nResume the offline import.\n")
    # Every skipped link is reported: even a .bin path could point to a directory of notes.
    (brain.root / "actions/2026-09-26_demo/inputs").mkdir()
    (brain.root / "actions/2026-09-26_demo/inputs/dataset.bin").symlink_to(outside)
    (brain.root / "concepts/linked.md").symlink_to(outside)
    os.mkfifo(brain.root / "memories/meetings/2026-07.jsonl")
    reply = search([brain], Query(text="offline"))
    assert cast(list, reply["items"])[0]["ref"] == "projects/offline.md"
    files = cast(list, reply["problems"])[0]["files"]
    assert [name.split(":")[0] for name in files] == [
        "actions/2026-09-26_demo/inputs/dataset.bin",
        "concepts/linked.md",
        "memories/meetings/2026-07.jsonl",
    ]
    assert "symlinks are not followed" in files[0]
    assert refs(brain, "exfiltrated") == []
    assert index.status(brain)["index"] == "ready"
    assert "projects" in read([brain])
    action = read([brain], "actions/2026-09-26_demo")
    assert "actions/2026-09-26_demo/inputs/dataset.bin" not in cast(list, action["files"])
    assert any(
        "dataset.bin: symlinks and special files are not listed" in f
        for p in cast(list, action["problems"])
        for f in p.get("files", [])
    )
    # The cache names the partition, and a skipped partition keeps an absent record from looking absent.
    assert cast(dict, read([brain], "meetings:decision-1")["record"])["id"] == "decision-1"
    with pytest.raises(Error, match="unreadable records"):
        read([brain], "meetings:missing")
    report = validate(brain)
    assert not report["valid"]
    assert sorted(p.split(":")[0] for p in cast(list, report["problems"])) == [
        "actions/2026-09-26_demo/inputs/dataset.bin",
        "concepts/linked.md",
        "memories/meetings/2026-07.jsonl",
    ]


def test_search_says_when_results_exist_beyond_the_limit(brain: Store) -> None:
    assert search([brain], Query(text="offline", limit=1))["more"] is True
    assert "more" not in search([brain], Query(text="offline", limit=50))


def test_full_build_creates_every_index_after_loading_and_serves_in_wal_mode(brain: Store) -> None:
    index.refresh(brain, full=True)
    brain.write("concepts/late.md", b"# Late\n\nOsmium evidence.\n")
    assert refs(brain, "osmium") == ["concepts/late.md"]
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        created = {row[0] for row in connection.execute("SELECT sql FROM sqlite_schema WHERE type='index'") if row[0]}
    assert created == set(index._INDEXES)  # noqa: SLF001 - deferred secondary indexes


def test_one_reply_compares_files_with_the_cache_once_per_brain(brain: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[Path] = []
    original = index.fresh

    def counted(store: Store) -> str:
        checked.append(store.root)
        return original(store)

    monkeypatch.setattr(index, "fresh", counted)
    # A note with backlinks opens the cache for identities, backlinks and claims.
    read([brain], "projects/offline.md")
    assert checked == [brain.root]
    search([brain], Query(text="offline"))
    assert checked == [brain.root, brain.root]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "  short\n\ttext  ",
        " " * 1276 + "abcdefghijklmnop" * 40,
        "\u00a0\u2003word\x1c" * 400,
        "x" * 2000,
        "a" * 1279 + " tail" * 200,
    ],
)
def test_record_leads_collapse_whitespace_of_a_prefix_like_the_whole_text(text: str) -> None:
    assert index.lead(Record(id="x", title="T", text=text)) == re.sub(r"\s+", " ", text).strip()[:LEAD]


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
    assert reply == {"items": [], "notice": reply["notice"], "stale": ["fixture"], "sources": reply["sources"]}
    assert cast(list[dict], reply["sources"])[0]["source"] == "meetings"
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
        assert build.result(timeout=10)["files"] == 4
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
    team.write("bf.yaml", b"version: 6\nname: team\n")
    team.write("projects/offline.md", b"# Team offline\n\nThe team keeps offline retrieval too.\n")
    register(team)
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
    team.write("projects/august.md", b"---\nupdated: 2026-08-31\n---\n# August\n")
    timeline = read(stores, "2026-08")["items"]
    assert isinstance(timeline, list)
    assert [(i["brain"], i["ref"]) for i in timeline] == [
        ("fixture", "meetings:decision-1"),
        ("team", "projects/august.md"),
        ("fixture", "meetings:lunch"),
    ]


def test_exact_reads(brain: Store) -> None:
    whole = read([brain], "projects/offline.md")
    assert str(whole["text"]).startswith("---\ntype: project")
    section = read([brain], "projects/offline.md#next-actions")
    assert section["text"] == "## Next actions\n\n- Publish the retention guide.\n"
    record = read([brain], "meetings:decision-1")
    assert record["path"] == "memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json"
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


def test_oversized_replies_are_explicit_json_chunks(brain: Store) -> None:
    brain.write("concepts/quoted.md", b"# Quoted\n\n" + b'"' * 3_000_000)
    reply = read([brain], "concepts/quoted.md")
    assert reply["format"] == "json"
    assert reply["offset"] == 0
    assert reply["next_offset"] == 65536
    assert reply["sha256"]
    assert len(str(reply["chunk"])) == 65536


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


def test_removing_a_misnamed_record_clears_its_problem(brain: Store) -> None:
    records_file(brain, "duplicates", "2026-08", [Record(id="same", title="Needle")])
    wrong = "memories/duplicates/" + "0" * 64 + ".json"
    brain.write(wrong, b'{"id":"same","title":"Needle"}\n')
    assert refs(brain, "needle") == ["duplicates:same"]
    assert index.status(brain)["problems"]
    brain.delete(wrong)
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
    brain.write(
        "memories/meetings/11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437.json",
        b"malformed unrelated newer partition\n",
    )
    assert read([brain], "meetings:decision-1")["ref"] == "meetings:decision-1"


@pytest.mark.parametrize(
    "hint",
    [
        "memories/other/2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881.json",
        "memories/meetings/11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437.json",
    ],
)
def test_record_cache_hint_cannot_change_the_requested_source(brain: Store, hint: str) -> None:
    records_file(
        brain,
        "other",
        "2026-09",
        [Record(id="decision-1", title="Evidence from another source", time="2026-09-01T00:00:00Z")],
    )
    brain.write(
        "memories/meetings/11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437.json",
        b"malformed unrelated newer partition\n",
    )
    index.refresh(brain)
    with closing(sqlite3.connect(brain.root / index.CACHE)) as connection, connection:
        connection.execute("UPDATE items SET path=? WHERE ref=?", (hint, "meetings:decision-1"))
    reply = read([brain], "meetings:decision-1")
    assert reply["path"] == "memories/meetings/e031d461072b6d47eb45e7cd0f15f0a65e717da62363d564c234d7f7e8713208.json"
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
    page = read([brain], "2026-09")
    assert [i["ref"] for i in cast("list[dict[str, object]]", page["items"])] == [
        "updates:recent",
        "projects/offline.md",
    ]
    # Modified in the period, but dated outside it: listed separately with its event time unchanged.
    changed = cast("list[dict[str, object]]", page["changed"])
    assert [i["ref"] for i in changed] == ["updates:old"]
    assert read([brain], "2026-09-23")["items"] == []
    item = cast("list[dict[str, object]]", read([brain], "2026-09-23")["changed"])[0]
    assert item["time"] == timestamp("2026-01-01T00:00:00Z")
    assert item["updated"] == timestamp("2026-09-23T00:00:00Z")
    assert item["observed"] == timestamp("2026-09-23T01:00:00Z")
    assert item["partial"] is True


def test_searches_report_collection_coverage_of_their_sources(brain: Store) -> None:
    brain.write(
        "bf.yaml",
        b"version: 6\nname: fixture\nsensors:\n  meetings:\n    command: [true-command]\n    refresh: 3600\n  disabled:\n    command: [true-command]\n    enabled: false\n",
    )
    for source in ("disabled", "historical"):
        records_file(brain, source, "undated", [Record(id="x", title="Offline retrieval")])
    for source, expected in (("meetings", "active"), ("disabled", "disabled"), ("historical", "historical")):
        result = search([brain], Query(text="offline", **scope(f"memories/{source}")))
        sources = result["sources"]
        assert isinstance(sources, list)
        assert sources[0]["state"] == expected
    result = search([brain], Query(text="offline", **scope("memories/absent")))
    assert result["items"] == []
    assert result["sources"] == [
        {"brain": "fixture", "source": "absent", "state": "historical", "freshness": "unknown"}
    ]
    collection = read([brain], "meetings:decision-1")["collection"]
    assert isinstance(collection, dict)
    # A scheduled sensor without a successful local run has never collected here.
    assert collection["freshness"] == "never"
    user_path().write_text("brains: [not a mapping]\n")
    # Explicitly selected brains do not need a readable registry.
    assert cast("dict[str, object]", read([brain], "meetings:decision-1")["collection"])["freshness"] == "never"


@pytest.mark.parametrize("path", ["projects/new.md", "concepts/new.md", "actions/2026-09-27_new/ACTION.md"])
def test_okf_provenance_resources_are_searchable_identity_links(brain: Store, path: str) -> None:
    brain.write(path, b'---\ntype: concept\nsources:\n  - resource: "repo:unique-source"\n---\n# Concept\n')
    assert refs(brain, "repo:unique-source") == [path]


def test_identity_search_does_not_infer_relations_from_words(brain: Store) -> None:
    brain.write("projects/prose.md", b"# Repo example\n\nThese words do not declare an identity.\n")
    # Nothing is, names or links to repo:example: its words rank, labeled, and claim no relation.
    reply = search([brain], Query(text="repo:example"))
    items = cast("list[dict[str, object]]", reply["items"])
    assert reply["identity"] == "unknown"
    assert items[0]["ref"] == "projects/prose.md"
    assert not any("relations" in item for item in items)
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
    assert refs(brain, "repo:example/project") == [
        "projects/offline.md",
        "updates:latest",
        "meetings:decision-1",
    ]
    folder = tmp_path / "shared"
    folder.mkdir()
    team = Store(folder)
    team.write("bf.yaml", b"version: 6\nname: team\n")
    team.write("projects/owner.md", b'---\nupdated: 2026-08-01\naliases: ["repo:example/project"]\n---\n# Owner\n')
    result = search([team, brain], Query(text="repo:example/project"))["items"]
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
    assert refs(brain, "meetings:lunch")[0] == "meetings:lunch"
    assert read([brain], "meetings:lunch")["ref"] == "meetings:lunch"


def test_search_reports_omitted_files_and_isolates_unavailable_brains(brain: Store, tmp_path: Path) -> None:
    brain.write("projects/broken.md", b"---\nstatus: typo\n---\n# Hidden answer\n")
    broken = tmp_path / "broken"
    broken.mkdir()
    other = Store(broken)
    other.write("bf.yaml", b"version: 6\nname: interrupted\n")
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
    other.write("bf.yaml", b"version: 6\nname: [invalid]\n")
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
        assert rebuild.result(timeout=10)["files"] == 4
        reply = query.result(timeout=10)
    assert isinstance(reply["items"], list)
    assert reply["items"][0]["ref"] == "projects/offline.md"
    assert "problems" not in reply


def test_evaluation_names_missing_retrieval_cases(brain: Store) -> None:
    from bf.evaluate import evaluate

    with pytest.raises(Error, match="evals has no suites"):
        evaluate(brain)
    brain.write(
        "evals/retrieval.yaml", b"version: 5\ncases:\n  - name: later\n    query: x\n    scope: soon\n    empty: true\n"
    )
    with pytest.raises(Error, match="case later: scope accepts"):
        evaluate(brain)
    for case in (b"    query: x\n    read: today\n", b"    read: today\n    scope: 7d\n", b"    since: 7d\n"):
        brain.write("evals/retrieval.yaml", b"version: 5\ncases:\n  - name: bad\n" + case + b"    empty: true\n")
        with pytest.raises(Error, match=r"case bad|since"):
            evaluate(brain)


def test_evaluation_rejects_incomplete_empty_answers(brain: Store) -> None:
    from bf.evaluate import evaluate

    brain.write("projects/broken.md", b"---\nstatus: typo\n---\n# Lost answer\n")
    brain.write(
        "evals/retrieval.yaml", b"version: 5\ncases:\n  - name: absent\n    query: lost answer\n    empty: true\n"
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
        b"version: 5\ncases:\n  - name: exact-record\n    query: hashneedle\n    expect: [issues:item]\n",
    )
    assert not evaluate(brain)["passed"]


def test_one_ranked_query_keeps_notes_above_long_records_that_happen_to_hold_every_word(brain: Store) -> None:
    # The note holds three of the four words; every trace holds all four in a long body.
    brain.write("projects/ranking.md", b"# Search ranking\n\nThe framework orders results lexically.\n")
    filler = " ".join(f"unrelated{i}" for i in range(400))
    records_file(
        brain,
        "traces",
        "undated",
        [
            Record(id=f"t{i}", title=f"Trace {i}", text=f"{filler} brain {filler} framework search ranking")
            for i in range(12)
        ],
    )
    # Before 12.0.0 every item holding all four words ranked first, so these traces hid the note entirely.
    assert refs(brain, "brain framework search ranking", limit=3)[0] == "projects/ranking.md"


def test_only_distilled_notes_rank_above_evidence() -> None:
    assert index.distilled("projects/archive.md")
    assert index.distilled("concepts/retention.md")
    assert index.distilled("actions/2026-09-25_review/ACTION.md")
    for working in (
        "concepts/index.md",
        "concepts/log.md",
        "concepts/team/index.md",
        "actions/2026-09-25_review/inputs/request.md",
        "actions/2026-09-25_review/outputs/ACTION.md",
    ):
        assert not index.distilled(working)


def test_a_known_identity_ignores_other_brains_words(brain: Store, tmp_path: Path) -> None:
    root = tmp_path / "team"
    root.mkdir()
    team = Store(root)
    team.write("bf.yaml", b"version: 6\nname: team\n")
    team.write("projects/words.md", b"# Repo example project\n\nOnly words, no link.\n")
    reply = search([brain, team], Query(text="repo:example/project"))
    assert "identity" not in reply
    assert {(i["brain"], i["ref"]) for i in cast("list[dict[str, object]]", reply["items"])} == {
        ("fixture", "projects/offline.md"),
        ("fixture", "meetings:decision-1"),
    }
